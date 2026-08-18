const baseUrl = process.env.N8N_BASE_URL ?? 'http://127.0.0.1:5678';
const ownerPassword = `V${crypto.randomUUID().replaceAll('-', '')}9!`;

const browserId = 'dayz-runtime-validation';

function unwrap(value) {
  return value && typeof value === 'object' && 'data' in value ? value.data : value;
}

async function readJson(response, label) {
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      throw new Error(`${label} returned non-JSON (${response.status}): ${text.slice(0, 500)}`);
    }
  }
  if (!response.ok) {
    throw new Error(`${label} failed (${response.status}): ${JSON.stringify(body)}`);
  }
  return body;
}

let setupResponse;
const setupDeadline = Date.now() + 30_000;
do {
  setupResponse = await fetch(`${baseUrl}/rest/owner/setup`, {
    method: 'POST',
    headers: {
      'browser-id': browserId,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      email: 'local-validation@example.invalid',
      firstName: 'Local',
      lastName: 'Validation',
      password: ownerPassword,
    }),
  });
  if (setupResponse.status !== 404 || Date.now() >= setupDeadline) break;
  await setupResponse.text();
  await new Promise((resolve) => setTimeout(resolve, 250));
} while (true);
await readJson(setupResponse, 'owner setup');

const setCookie = setupResponse.headers.get('set-cookie');
if (!setCookie) {
  throw new Error('owner setup did not return an authentication cookie');
}
const cookie = setCookie.split(';', 1)[0];
const authHeaders = {
  'browser-id': browserId,
  cookie,
};

async function api(path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      ...authHeaders,
      ...(options.body ? { 'content-type': 'application/json' } : {}),
      ...(options.headers ?? {}),
    },
  });
  return await readJson(response, path);
}

const workflowList = unwrap(await api('/rest/workflows?take=100'));
const workflows = Array.isArray(workflowList) ? workflowList : workflowList?.data;
if (!Array.isArray(workflows)) {
  throw new Error(`unexpected workflow-list response: ${JSON.stringify(workflowList)}`);
}
const bootstrapSummary = workflows.find((workflow) => workflow.name === 'DayZ | 00 | Bootstrap Data Tables');
if (!bootstrapSummary?.id) {
  throw new Error('bootstrap workflow was not imported');
}

const workflow = unwrap(await api(`/rest/workflows/${encodeURIComponent(bootstrapSummary.id)}`));
for (const field of ['checksum', 'scopes', 'shared']) delete workflow[field];

async function runBootstrap(runNumber) {
  const started = unwrap(await api(`/rest/workflows/${encodeURIComponent(workflow.id)}/run`, {
    method: 'POST',
    body: JSON.stringify({
      workflowData: workflow,
      triggerToStartFrom: { name: 'Manual Trigger' },
    }),
  }));
  const executionId = started?.executionId;
  if (!executionId) {
    throw new Error(`bootstrap run ${runNumber} returned no execution id: ${JSON.stringify(started)}`);
  }

  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const execution = unwrap(await api(`/rest/executions/${encodeURIComponent(executionId)}?includeData=true`));
    if (execution?.status === 'success') return execution;
    if (['error', 'crashed', 'canceled'].includes(execution?.status)) {
      throw new Error(`bootstrap run ${runNumber} ended as ${execution.status}: ${JSON.stringify(execution)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`bootstrap run ${runNumber} did not finish within 30 seconds`);
}

await runBootstrap(1);
await runBootstrap(2);

const refreshed = unwrap(await api(`/rest/workflows/${encodeURIComponent(workflow.id)}`));
const projectId = refreshed?.shared?.[0]?.projectId ?? refreshed?.shared?.[0]?.project?.id;
if (!projectId) {
  throw new Error('could not determine bootstrap workflow project id');
}

const tableList = unwrap(await api(`/rest/projects/${encodeURIComponent(projectId)}/data-tables?take=100`));
const tables = Array.isArray(tableList) ? tableList : tableList?.data;
if (!Array.isArray(tables)) {
  throw new Error(`unexpected data-table response: ${JSON.stringify(tableList)}`);
}
const names = tables.map((table) => table.name).sort();
const expected = ['dayz_cooldowns', 'dayz_deliveries', 'dayz_events', 'dayz_routes'];
if (JSON.stringify(names) !== JSON.stringify(expected)) {
  throw new Error(`expected exactly ${expected.join(', ')}, got ${names.join(', ')}`);
}

const expectedColumns = new Map(
  workflow.nodes
    .filter((node) => node.type === 'n8n-nodes-base.dataTable' && node.parameters?.operation === 'create')
    .map((node) => [
      node.parameters.tableName,
      (node.parameters.columns?.column ?? []).map((column) => `${column.name}:${column.type}`).sort(),
    ]),
);
for (const table of tables) {
  const columnList = unwrap(await api(
    `/rest/projects/${encodeURIComponent(projectId)}/data-tables/${encodeURIComponent(table.id)}/columns`,
  ));
  const columns = Array.isArray(columnList) ? columnList : columnList?.data;
  if (!Array.isArray(columns)) {
    throw new Error(`unexpected columns response for ${table.name}: ${JSON.stringify(columnList)}`);
  }
  const actual = columns.map((column) => `${column.name}:${column.type}`).sort();
  const wanted = expectedColumns.get(table.name);
  if (!wanted || JSON.stringify(actual) !== JSON.stringify(wanted)) {
    throw new Error(`column mismatch for ${table.name}: expected ${wanted}, got ${actual}`);
  }
}

console.log(`runtime_bootstrap: ok (workflow=${workflow.id}, runs=2, tables=${names.join(',')}, columns=exact)`);
