'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WORKFLOWS_DIR = path.join(ROOT, 'workflows');
const POLICY = JSON.parse(fs.readFileSync(path.join(ROOT, 'config', 'policy.v1.json'), 'utf8'));
const DIGEST_SYSTEM_PROMPT = fs.readFileSync(
  path.join(ROOT, 'prompts', 'digest_system.txt'),
  'utf8',
).trim();
const DIGEST_USER_PROMPT = fs.readFileSync(
  path.join(ROOT, 'prompts', 'digest_user.txt'),
  'utf8',
).trim();

const SIGNAL_SUBMISSION_FLOOR_SECONDS =
  POLICY.limits.delivery_lease_seconds
  + POLICY.limits.delivery_scan_interval_seconds
  + POLICY.limits.signal_reconcile_buffer_seconds;
if (
  !Number.isInteger(POLICY.limits.delivery_scan_interval_seconds)
  || POLICY.limits.delivery_scan_interval_seconds < 1
  || POLICY.limits.delivery_scan_interval_seconds % 60 !== 0
) {
  throw new Error('delivery_scan_interval_seconds must be a positive whole number of minutes');
}
if (
  !Number.isInteger(SIGNAL_SUBMISSION_FLOOR_SECONDS)
  || SIGNAL_SUBMISSION_FLOOR_SECONDS < 1
  || SIGNAL_SUBMISSION_FLOOR_SECONDS >= POLICY.limits.signal_ttl_max_seconds
) {
  throw new Error('Signal recovery window must be positive and smaller than signal_ttl_max_seconds');
}
if (
  !Array.isArray(POLICY.limits.delivery_retry_seconds)
  || POLICY.limits.delivery_retry_seconds.length !== POLICY.limits.delivery_max_attempts - 1
) {
  throw new Error('delivery_retry_seconds must define exactly one delay between each total attempt');
}

const CREDENTIALS = {
  ingest: {
    type: 'httpHeaderAuth',
    id: '00000000-0000-4000-8000-000000000001',
    name: 'REPLACE: DayZ Ingest Header Auth',
  },
  llm: {
    type: 'httpHeaderAuth',
    id: '00000000-0000-4000-8000-000000000002',
    name: 'REPLACE: DayZ LLM Header Auth',
  },
  signal: {
    type: 'httpHeaderAuth',
    id: '00000000-0000-4000-8000-000000000003',
    name: 'REPLACE: DayZ Signal Header Auth',
  },
  telegram: {
    type: 'telegramApi',
    id: '00000000-0000-4000-8000-000000000004',
    name: 'REPLACE: DayZ Telegram Bot',
  },
};

const TABLES = {
  dayz_routes: [
    ['server_id', 'string'],
    ['mode', 'string'],
    ['enabled', 'boolean'],
    ['telegram_enabled', 'boolean'],
    ['telegram_chat_id', 'string'],
    ['game_enabled', 'boolean'],
    ['signal_base_url', 'string'],
    ['llm_enabled', 'boolean'],
    ['llm_base_url', 'string'],
    ['llm_model', 'string'],
    ['timezone', 'string'],
    ['public_delay_seconds', 'number'],
    ['game_cooldown_seconds', 'number'],
    ['rules_message_ru', 'string'],
    ['ops_alerts', 'boolean'],
  ],
  dayz_events: [
    ['record_id', 'string'],
    ['record_kind', 'string'],
    ['batch_id', 'string'],
    ['event_id', 'string'],
    ['server_id', 'string'],
    ['sent_at', 'date'],
    ['received_at', 'date'],
    ['event_type', 'string'],
    ['occurred_at', 'date'],
    ['observed_at', 'date'],
    ['processing_expires_at', 'date'],
    ['severity', 'string'],
    ['audience_ceiling', 'string'],
    ['public_json', 'string'],
    ['admin_json', 'string'],
    ['digest_state', 'string'],
    ['chronicle_state', 'string'],
    ['safety_state', 'string'],
    ['announcement_state', 'string'],
    ['immediate_state', 'string'],
    ['status', 'string'],
    ['payload_json', 'string'],
    ['attempt_count', 'number'],
    ['next_attempt_at', 'date'],
    ['lease_token', 'string'],
    ['last_error', 'string'],
    ['private_expires_at', 'date'],
    ['expires_at', 'date'],
  ],
  dayz_deliveries: [
    ['delivery_id', 'string'],
    ['event_id', 'string'],
    ['server_id', 'string'],
    ['channel', 'string'],
    ['message', 'string'],
    ['status', 'string'],
    ['attempt_count', 'number'],
    ['next_attempt_at', 'date'],
    ['expires_at', 'date'],
    ['delete_after', 'date'],
    ['last_error', 'string'],
    ['sent_at', 'date'],
    ['created_at', 'date'],
    ['payload_hash', 'string'],
    ['policy_version', 'string'],
    ['message_kind', 'string'],
    ['cooldown_scope', 'string'],
    ['cooldown_seconds', 'number'],
    ['secondary_cooldown_scope', 'string'],
    ['secondary_cooldown_seconds', 'number'],
    ['rate_scope_prefix', 'string'],
    ['rate_slot_scope', 'string'],
    ['lease_token', 'string'],
    ['claimed_at', 'date'],
    ['source_event_ids_json', 'string'],
    ['signal_status_url', 'string'],
  ],
  dayz_cooldowns: [
    ['scope_key', 'string'],
    ['until', 'date'],
    ['last_delivery_id', 'string'],
    ['updated_at', 'date'],
  ],
};

const BATCH_RECORD_COLUMNS = [
  'record_id', 'record_kind', 'batch_id', 'server_id', 'sent_at', 'received_at',
  'status', 'payload_json', 'attempt_count', 'next_attempt_at', 'lease_token',
  'last_error', 'private_expires_at', 'expires_at',
];

const EVENT_RECORD_COLUMNS = [
  'record_id', 'record_kind', 'batch_id', 'event_id', 'server_id', 'event_type',
  'occurred_at', 'observed_at', 'processing_expires_at', 'received_at', 'severity', 'audience_ceiling',
  'public_json', 'admin_json', 'digest_state', 'chronicle_state', 'safety_state', 'announcement_state', 'immediate_state',
  'private_expires_at', 'expires_at',
];

function stableUuid(seed) {
  const bytes = Buffer.from(crypto.createHash('sha256').update(seed).digest('hex').slice(0, 32), 'hex');
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function credentialRef(credential) {
  return {
    [credential.type]: {
      id: credential.id,
      name: credential.name,
    },
  };
}

function node(workflowName, name, type, typeVersion, position, parameters, extra = {}) {
  return {
    parameters,
    id: stableUuid(`${workflowName}:${name}`),
    name,
    type,
    typeVersion,
    position,
    ...extra,
  };
}

function workflow(fileName, name, nodes, connections, settings = {}) {
  return {
    name,
    nodes,
    pinData: {},
    connections,
    active: false,
    settings: {
      executionOrder: 'v1',
      saveManualExecutions: false,
      saveDataErrorExecution: 'none',
      saveDataSuccessExecution: 'none',
      saveExecutionProgress: false,
      timezone: 'Europe/Moscow',
      callerPolicy: 'workflowsFromSameOwner',
      ...settings,
    },
    versionId: stableUuid(`${fileName}:version`),
    meta: {
      templateCredsSetupCompleted: false,
    },
    tags: [],
  };
}

function mainConnection(target, index = 0) {
  return { node: target, type: 'main', index };
}

function tableLocator(tableName) {
  return { __rl: true, value: tableName, mode: 'name' };
}

function mapperSchema(tableName) {
  return TABLES[tableName].map(([columnName, columnType]) => ({
    id: columnName,
    displayName: columnName,
    required: false,
    defaultMatch: false,
    display: true,
    type: columnType,
    canBeUsedToMatch: true,
  }));
}

function mapper(tableName, values) {
  return {
    mappingMode: 'defineBelow',
    value: values,
    matchingColumns: [],
    schema: mapperSchema(tableName),
    attemptToConvertTypes: false,
    convertFieldsToString: false,
  };
}

function tableCreateNode(workflowName, tableName, position) {
  return node(
    workflowName,
    `Create ${tableName}`,
    'n8n-nodes-base.dataTable',
    1.1,
    position,
    {
      resource: 'table',
      operation: 'create',
      tableName,
      columns: {
        column: TABLES[tableName].map(([name, type]) => ({ name, type })),
      },
      options: { createIfNotExists: true },
    },
  );
}

function tableRowNode(
  workflowName,
  nodeName,
  operation,
  tableName,
  position,
  { values, conditions, matchType = 'allConditions', returnAll, limit, orderBy, orderByColumn, orderByDirection, options = {} } = {},
  extra = {},
) {
  const parameters = {
    resource: 'row',
    operation,
    dataTableId: tableLocator(tableName),
  };
  if (conditions) {
    parameters.matchType = matchType;
    parameters.filters = { conditions };
  }
  if (values) parameters.columns = mapper(tableName, values);
  if (returnAll !== undefined) parameters.returnAll = returnAll;
  if (limit !== undefined) parameters.limit = limit;
  if (orderBy !== undefined) parameters.orderBy = orderBy;
  if (orderByColumn !== undefined) parameters.orderByColumn = orderByColumn;
  if (orderByDirection !== undefined) parameters.orderByDirection = orderByDirection;
  if (['insert', 'update', 'upsert', 'deleteRows'].includes(operation)) parameters.options = options;
  return node(
    workflowName,
    nodeName,
    'n8n-nodes-base.dataTable',
    1.1,
    position,
    parameters,
    extra,
  );
}

function booleanIfNode(workflowName, name, expression, position) {
  return node(workflowName, name, 'n8n-nodes-base.if', 2.2, position, {
    conditions: {
      options: {
        caseSensitive: true,
        leftValue: '',
        typeValidation: 'strict',
        version: 2,
      },
      conditions: [
        {
          id: stableUuid(`${workflowName}:${name}:condition`),
          leftValue: expression,
          rightValue: '',
          operator: {
            type: 'boolean',
            operation: 'true',
            singleValue: true,
          },
        },
      ],
      combinator: 'and',
    },
    options: {},
  });
}

function codeNode(workflowName, name, position, jsCode, extra = {}) {
  const { mode, ...nodeExtra } = extra;
  const parameters = { jsCode };
  if (mode) parameters.mode = mode;
  return node(
    workflowName,
    name,
    'n8n-nodes-base.code',
    2,
    position,
    parameters,
    nodeExtra,
  );
}

function loopOverItemsNode(workflowName, name, position) {
  return node(workflowName, name, 'n8n-nodes-base.splitInBatches', 3, position, {
    batchSize: 1,
    options: {},
  });
}

function combineExistingCandidateCode(loopNodeName, idField) {
  return [
    'const candidate = $("' + loopNodeName + '").item.json;',
    'const existing = $json || {};',
    'const alreadyExists = typeof existing.' + idField + ' === "string" && existing.' + idField + ' === candidate.' + idField + ';',
    'return { json: { ...candidate, already_exists: alreadyExists } };',
  ].join('\n');
}

function classifyBatchLedgerCode() {
  return [
    "const normalized = $('Validate and Normalize Batch').item.json;",
    'const existing = $json || {};',
    'const duplicate = existing.record_id === normalized.batch_row.record_id;',
    'return { json: { ...normalized, duplicate } };',
  ].join('\n');
}

function respondNode(workflowName, name, position, responseBody, responseCode) {
  return node(workflowName, name, 'n8n-nodes-base.respondToWebhook', 1.4, position, {
    respondWith: 'json',
    responseBody,
    options: { responseCode },
  });
}

function scheduleNode(workflowName, name, position, field, value) {
  const interval = { field };
  if (field === 'minutes') interval.minutesInterval = value;
  if (field === 'hours') interval.hoursInterval = value;
  if (field === 'days') interval.daysInterval = value;
  return node(workflowName, name, 'n8n-nodes-base.scheduleTrigger', 1.2, position, {
    rule: { interval: [interval] },
  });
}

function gatewayValidationCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const body = ($json && $json.body && typeof $json.body === 'object') ? $json.body : $json;
const headers = ($json && $json.headers && typeof $json.headers === 'object') ? $json.headers : {};
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();

function resultError(code, message, status = 400) {
  return [{ json: {
    valid: false,
    http_status: status,
    response: { schema: 'dayz.ingest-response.v1', accepted: false, error: code, message },
  } }];
}

function utf8Length(value) {
  let bytes = 0;
  for (const char of value) {
    const cp = char.codePointAt(0);
    bytes += cp <= 0x7f ? 1 : cp <= 0x7ff ? 2 : cp <= 0xffff ? 3 : 4;
  }
  return bytes;
}

function cleanText(value, maxLength) {
  return String(value ?? '').replace(/[\\u0000-\\u001f\\u007f]+/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, maxLength);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function validateBoundedTree(value, path = '', depth = 0) {
  if (depth > 7) return path || '$';
  if (Array.isArray(value)) {
    if (value.length > 128) return path || '$';
    for (let i = 0; i < value.length; i += 1) {
      const hit = validateBoundedTree(value[i], path + '[' + i + ']', depth + 1);
      if (hit) return hit;
    }
    return null;
  }
  if (isPlainObject(value)) {
    if (Object.keys(value).length > 128) return path || '$';
    for (const [key, child] of Object.entries(value)) {
      if (!/^[A-Za-z][A-Za-z0-9_.-]{0,79}$/.test(key)) return path + '.' + key;
      const hit = validateBoundedTree(child, path + '.' + key, depth + 1);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof value === 'string') {
    if (value.length > 1000 || /[\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f]/.test(value)) return path || '$';
    return null;
  }
  if (value === null || typeof value === 'boolean') return null;
  if (typeof value === 'number' && Number.isFinite(value)) return null;
  return path || '$';
}

function findForbiddenIdentity(value, path = '') {
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const hit = findForbiddenIdentity(value[i], path + '[' + i + ']');
      if (hit) return hit;
    }
    return null;
  }
  if (isPlainObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      if (/^(uid|guid|steam_?id|player_?id|identity_?id|raw|raw_?line|log_?line)$/i.test(key)) return path + '.' + key;
      if ((key === 'ref' || /_ref$/i.test(key)) && typeof child === 'string' && !/^p_[0-9a-f]{20}$/.test(child)) return path + '.' + key;
      const hit = findForbiddenIdentity(child, path + '.' + key);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof value === 'string' && /\\bid\\s*=|\\bsteam(?:id)?\\b/i.test(value)) return path || '$';
  return null;
}

function findUnsafePublic(value, path = '') {
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const hit = findUnsafePublic(value[i], path + '[' + i + ']');
      if (hit) return hit;
    }
    return null;
  }
  if (isPlainObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      if (/^(actor|attacker|target|ref|id|display_name|grid_100m|source|file|x|y|z|pos|position|coordinates|lat|lon|latitude|longitude)$/i.test(key) || /_ref$/i.test(key)) return path + '.' + key;
      if (key === 'location' && isPlainObject(child)) {
        if (child.precision !== 'coarse') return path + '.location.precision';
      }
      const hit = findUnsafePublic(child, path + '.' + key);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof value === 'string' && (/https?:\\/\\//i.test(value) || /\\bid\\s*=/i.test(value) || /\\b[0-9a-f]{32,64}\\b/i.test(value) || /(?:^|\\D)-?\\d{2,5}(?:\\.\\d+)?\\s*[,;\\/]\\s*-?\\d{2,5}(?:\\.\\d+)?(?:\\D|$)/.test(value))) return path || '$';
  return null;
}

function normalizePublicView(eventType, value) {
  const failure = (reason) => ({ ok: false, reason, value: null });
  const success = (safeValue) => ({ ok: true, reason: '', value: safeValue });
  const exactKeys = (object, allowed, required = allowed) => {
    if (!isPlainObject(object)) return false;
    const keys = Object.keys(object);
    return keys.every((key) => allowed.includes(key)) && required.every((key) => key in object);
  };
  const finite = (candidate, minimum, maximum) => typeof candidate === 'number' && Number.isFinite(candidate) && candidate >= minimum && candidate <= maximum;
  const integer = (candidate, minimum, maximum) => Number.isInteger(candidate) && candidate >= minimum && candidate <= maximum;
  const publicTypes = new Set([
    'player.command','player.sos','player.admin_message',
    'combat.infected_pressure','combat.vehicle_incident','combat.wildlife_pressure',
    'world.snapshot','world.event.present','world.event.started','world.event.updated','world.event.ended',
    'server.started','server.restart_due',
  ]);
  if (!publicTypes.has(eventType)) return value === null ? success(null) : failure('public_view_not_allowed');
  if (!isPlainObject(value)) return failure('public_view_required');
  if (findUnsafePublic(value)) return failure('unsafe_public_value');

  if (eventType === 'player.command') {
    if (!exactKeys(value, ['command']) || !['status','weather','time','rules'].includes(value.command)) return failure('invalid_command_projection');
    return success({ command: value.command });
  }
  if (eventType === 'player.sos' || eventType === 'player.admin_message') {
    const expected = eventType === 'player.sos' ? 'sos_received' : 'admin_message_received';
    if (!exactKeys(value, ['acknowledgement']) || value.acknowledgement !== expected) return failure('invalid_acknowledgement_projection');
    return success({ acknowledgement: expected });
  }
  if (['combat.infected_pressure','combat.vehicle_incident','combat.wildlife_pressure'].includes(eventType)) {
    const expected = eventType.slice('combat.'.length);
    if (!exactKeys(value, ['episode_type','signal_count']) || value.episode_type !== expected || !integer(value.signal_count,1,1000)) return failure('invalid_chronicle_projection');
    return success({ episode_type: expected, signal_count: value.signal_count });
  }
  if (eventType === 'server.started') {
    if (!exactKeys(value, ['status']) || value.status !== 'started') return failure('invalid_server_projection');
    return success({ status: 'started' });
  }
  if (eventType === 'server.restart_due') {
    if (!exactKeys(value, ['restart_in_seconds']) || !integer(value.restart_in_seconds,0,86400)) return failure('invalid_restart_projection');
    return success({ restart_in_seconds: value.restart_in_seconds });
  }
  if (eventType.startsWith('world.event.')) {
    if (!exactKeys(value, ['lifecycle','kind','location'], ['lifecycle','kind'])) return failure('invalid_world_event_projection');
    const lifecycle = eventType.slice('world.event.'.length);
    if (value.lifecycle !== lifecycle || typeof value.kind !== 'string' || !/^[a-z][a-z0-9_]{0,63}$/.test(value.kind)) return failure('invalid_world_event_identity');
    const safe = { lifecycle, kind: value.kind };
    if ('location' in value) {
      const location = value.location;
      if (!exactKeys(location, ['sector','size_m','precision']) || typeof location.sector !== 'string' || !/^[A-Z][A-Z0-9-]{0,11}$/.test(location.sector) || location.size_m !== 2000 || location.precision !== 'coarse') return failure('invalid_coarse_location');
      safe.location = { sector: location.sector, size_m: 2000, precision: 'coarse' };
    }
    return success(safe);
  }
  if (eventType === 'world.snapshot') {
    if (!exactKeys(value, ['game_time','weather','players_online'], [])) return failure('invalid_snapshot_projection');
    const safe = {};
    if ('players_online' in value) {
      if (!integer(value.players_online,0,500)) return failure('invalid_snapshot_players');
      safe.players_online = value.players_online;
    }
    if ('game_time' in value) {
      const game = value.game_time;
      const allowed = ['year','month','day','hour','minute','decimal_hour','is_night'];
      if (!exactKeys(game, allowed, [])) return failure('invalid_snapshot_time');
      const ranges = { year:[2000,2200], month:[1,12], day:[1,31], hour:[0,23], minute:[0,59], decimal_hour:[0,24] };
      const safeGame = {};
      for (const [key,range] of Object.entries(ranges)) if (key in game) {
        const valid = key === 'decimal_hour' ? finite(game[key],range[0],range[1]) : integer(game[key],range[0],range[1]);
        if (!valid) return failure('invalid_snapshot_time_value');
        safeGame[key] = game[key];
      }
      if ('is_night' in game) {
        if (typeof game.is_night !== 'boolean') return failure('invalid_snapshot_night');
        safeGame.is_night = game.is_night;
      }
      safe.game_time = safeGame;
    }
    if ('weather' in value) {
      const weather = value.weather;
      const phenomena = ['overcast','rain','fog','snowfall','wind_magnitude','wind_direction'];
      if (!exactKeys(weather, phenomena.concat(['base_environment_temperature_c']), [])) return failure('invalid_snapshot_weather');
      const safeWeather = {};
      for (const name of phenomena) if (name in weather) {
        const part = weather[name];
        if (!exactKeys(part, ['actual','forecast','next_change_seconds'], [])) return failure('invalid_snapshot_weather_part');
        const safePart = {};
        for (const field of ['actual','forecast']) if (field in part) {
          if (!finite(part[field],-1000,1000)) return failure('invalid_snapshot_weather_value');
          safePart[field] = part[field];
        }
        if ('next_change_seconds' in part) {
          if (!finite(part.next_change_seconds,0,86400)) return failure('invalid_snapshot_weather_change');
          safePart.next_change_seconds = part.next_change_seconds;
        }
        safeWeather[name] = safePart;
      }
      if ('base_environment_temperature_c' in weather) {
        if (!finite(weather.base_environment_temperature_c,-100,100)) return failure('invalid_snapshot_temperature');
        safeWeather.base_environment_temperature_c = weather.base_environment_temperature_c;
      }
      safe.weather = safeWeather;
    }
    return success(safe);
  }
  return failure('unsupported_public_projection');
}

if (!isPlainObject(body)) return resultError('invalid_body', 'Body must be one JSON object.');
const serialized = JSON.stringify(body);
if (utf8Length(serialized) > POLICY.limits.batch_max_bytes) return resultError('payload_too_large', 'Batch exceeds size limit.', 413);
const batchAllowed = new Set(['schema','batch_id','sent_at','producer','events']);
for (const key of Object.keys(body)) if (!batchAllowed.has(key)) return resultError('unknown_batch_field', 'Unknown batch field: ' + key);
for (const key of batchAllowed) if (!(key in body)) return resultError('missing_batch_field', 'Missing batch field: ' + key);
if (body.schema !== 'dayz.event-batch.v1') return resultError('unsupported_schema', 'Only dayz.event-batch.v1 is accepted.');
if (!/^batch_[0-9a-f]{32}$/.test(String(body.batch_id))) return resultError('invalid_batch_id', 'Invalid batch_id.');
const idempotencyKey = String(headers['idempotency-key'] ?? headers['Idempotency-Key'] ?? '');
if (idempotencyKey !== body.batch_id) return resultError('idempotency_mismatch', 'Idempotency-Key must equal batch_id.', 409);
const sentAt = new Date(body.sent_at);
if (!Number.isFinite(sentAt.getTime())) return resultError('invalid_sent_at', 'sent_at must be RFC3339.');
if (sentAt.getTime() > now.getTime() + POLICY.limits.event_max_future_seconds * 1000) return resultError('batch_from_future', 'sent_at is too far in the future.');
if (now.getTime() - sentAt.getTime() > POLICY.limits.event_max_age_hours * 3600000) return resultError('batch_too_old', 'Batch is outside the ingestion window.');
if (!isPlainObject(body.producer)) return resultError('invalid_producer', 'producer must be an object.');
for (const key of Object.keys(body.producer)) if (!['name','source'].includes(key)) return resultError('invalid_producer', 'Unknown producer field.');
if (body.producer.name !== 'dayz-log-monitor' || typeof body.producer.source !== 'string' || !body.producer.source.trim() || body.producer.source.length > 80) return resultError('invalid_producer', 'Invalid producer identity.');
if (!Array.isArray(body.events) || body.events.length < 1 || body.events.length > POLICY.limits.batch_max_events) return resultError('invalid_events', 'events must contain 1..' + POLICY.limits.batch_max_events + ' items.');

const eventAllowed = new Set(['schema','event_id','server_id','type','occurred_at','observed_at','expires_at','severity','audience_ceiling','admin_view','public_view','facts','source']);
const sourceKinds = new Set(['adm','rpt','script','error','monitor','storage']);
const audienceRank = { admin: 0, public_delayed: 1, public: 2 };
const seenEventIds = new Set();
const normalizedEvents = [];
let serverId = '';
for (let index = 0; index < body.events.length; index += 1) {
  const event = body.events[index];
  const prefix = 'events[' + index + ']';
  if (!isPlainObject(event)) return resultError('invalid_event', prefix + ' must be an object.', 422);
  if (utf8Length(JSON.stringify(event)) > POLICY.limits.event_max_bytes) return resultError('event_too_large', prefix + ' exceeds size limit.', 413);
  for (const key of Object.keys(event)) if (!eventAllowed.has(key)) return resultError('unknown_event_field', prefix + ' has unknown field: ' + key, 422);
  for (const key of eventAllowed) if (!(key in event)) return resultError('missing_event_field', prefix + ' is missing: ' + key, 422);
  if (event.schema !== 'dayz.event.v1') return resultError('unsupported_event_schema', prefix + ' has an unsupported schema.', 422);
  if (!/^evt_[0-9a-f]{32}$/.test(String(event.event_id)) || seenEventIds.has(event.event_id)) return resultError('invalid_event_id', prefix + ' has an invalid or duplicate event_id.', 422);
  seenEventIds.add(event.event_id);
  if (!/^[a-z0-9][a-z0-9_-]{1,47}$/.test(String(event.server_id))) return resultError('invalid_server_id', prefix + ' has an invalid server_id.', 422);
  if (!serverId) serverId = event.server_id;
  if (serverId !== event.server_id) return resultError('mixed_server_batch', 'All events in a batch must have one server_id.', 422);
  const rule = POLICY.event_types[event.type];
  if (!rule) return resultError('unknown_event_type', prefix + ' type is not allowlisted.', 422);
  if (!['info','warning','critical'].includes(event.severity)) return resultError('invalid_severity', prefix + ' has invalid severity.', 422);
  if (!(event.audience_ceiling in audienceRank) || audienceRank[event.audience_ceiling] > audienceRank[rule.max_audience]) return resultError('invalid_audience', prefix + ' exceeds the policy audience ceiling.', 422);
  const occurredAt = new Date(event.occurred_at);
  const observedAt = new Date(event.observed_at);
  const processingExpiresAt = new Date(event.expires_at);
  if (![occurredAt, observedAt, processingExpiresAt].every((value) => Number.isFinite(value.getTime()))) return resultError('invalid_timestamp', prefix + ' has an invalid timestamp.', 422);
  if (occurredAt.getTime() > now.getTime() + POLICY.limits.event_max_future_seconds * 1000) return resultError('event_from_future', prefix + ' occurred_at is too far in the future.', 422);
  if (now.getTime() - occurredAt.getTime() > POLICY.limits.event_max_age_hours * 3600000) return resultError('event_too_old', prefix + ' is outside the ingestion window.', 422);
  if (processingExpiresAt.getTime() <= now.getTime()) return resultError('event_expired', prefix + ' processing deadline has passed.', 422);
  if (processingExpiresAt.getTime() - occurredAt.getTime() > POLICY.limits.processing_ttl_max_seconds * 1000) return resultError('invalid_expiry', prefix + ' processing TTL is too large.', 422);
  if (!isPlainObject(event.admin_view) || !isPlainObject(event.facts)) return resultError('invalid_private_projection', prefix + ' admin_view and facts must be objects.', 422);
  if (event.public_view !== null && !isPlainObject(event.public_view)) return resultError('invalid_public_projection', prefix + ' public_view must be an object or null.', 422);
  const boundedHit = validateBoundedTree({ admin_view: event.admin_view, public_view: event.public_view, facts: event.facts });
  if (boundedHit) return resultError('projection_too_complex', prefix + ' contains an invalid or oversized value.', 422);
  const identityHit = findForbiddenIdentity({ admin_view: event.admin_view, facts: event.facts });
  if (identityHit) return resultError('forbidden_private_field', prefix + ' contains raw identity or raw log data.', 422);
  const publicProjection = normalizePublicView(event.type,event.public_view);
  if (!publicProjection.ok) return resultError('unsafe_public_projection', prefix + ' public_view crosses the privacy boundary: ' + publicProjection.reason, 422);
  const safePublicView = publicProjection.value;
  if (!isPlainObject(event.source)) return resultError('invalid_source', prefix + ' source must be an object.', 422);
  for (const key of Object.keys(event.source)) if (!['kind','file','offset_start','offset_end'].includes(key)) return resultError('invalid_source', prefix + ' source has an unknown field.', 422);
  if (!sourceKinds.has(event.source.kind) || typeof event.source.file !== 'string' || !event.source.file || event.source.file.length > 180 || /[\\/\\\\]/.test(event.source.file)) return resultError('invalid_source', prefix + ' source identity is invalid.', 422);
  if (!Number.isInteger(event.source.offset_start) || !Number.isInteger(event.source.offset_end) || event.source.offset_start < 0 || event.source.offset_end < event.source.offset_start) return resultError('invalid_source', prefix + ' source offsets are invalid.', 422);
  const safeAcknowledgement = ['player.sos','player.admin_message'].includes(event.type)
    && isPlainObject(safePublicView)
    && ['sos_received','admin_message_received'].includes(safePublicView.acknowledgement);
  const hasPublicProjection = safePublicView !== null && (event.audience_ceiling !== 'admin' || safeAcknowledgement);
  const interactiveGameReply = ['player.command','player.sos','player.admin_message'].includes(event.type);
  const immediateChannels = rule.immediate_channels.filter((channel) => channel !== 'game' || interactiveGameReply || hasPublicProjection && event.audience_ceiling === 'public');
  const worldLifecycle = event.type.startsWith('world.event.');
  const safetyWarning = hasPublicProjection && worldLifecycle && safePublicView.kind === 'contaminated_area';
  const delayedAnnouncement = hasPublicProjection && worldLifecycle && ['heli_crash','military_convoy','train','police_situation'].includes(String(safePublicView.kind || ''));
  const digestState = rule.digest && hasPublicProjection && !worldLifecycle ? 'pending' : 'none';
  const chronicleEligible = rule.chronicle && hasPublicProjection && event.audience_ceiling === 'public';
  const chronicleState = chronicleEligible ? 'pending' : 'none';
  const chroniclePrivate = chronicleEligible ? {
    actor_ref: /^p_[0-9a-f]{20}$/.test(String(event.facts.actor_ref || '')) ? event.facts.actor_ref : '',
    sector_2km: /^[A-Z][A-Z0-9-]{0,11}$/.test(String(event.facts.sector_2km || '')) ? event.facts.sector_2km : '',
  } : null;
  normalizedEvents.push({
    batch_id: body.batch_id,
    event_id: event.event_id,
    server_id: event.server_id,
    event_type: event.type,
    occurred_at: occurredAt.toISOString(),
    observed_at: observedAt.toISOString(),
    severity: event.severity,
    audience_ceiling: event.audience_ceiling,
    processing_expires_at: processingExpiresAt.toISOString(),
    admin_view: event.admin_view,
    public_view: hasPublicProjection ? safePublicView : null,
    chronicle_private: chroniclePrivate,
    routing: { immediate_channels: immediateChannels, digest_state: digestState, chronicle_state: chronicleState, safety_state: safetyWarning ? 'pending' : 'none', announcement_state: delayedAnnouncement ? 'pending' : 'none' },
  });
}

const privateExpiresAt = new Date(now.getTime() + POLICY.limits.private_retention_hours * 3600000);
const retentionExpiresAt = new Date(now.getTime() + POLICY.limits.event_retention_hours * 3600000);

return [{ json: {
  valid: true,
  http_status: 202,
  batch_row: {
    record_id: 'batch:' + body.batch_id,
    record_kind: 'batch',
    batch_id: body.batch_id,
    server_id: serverId,
    sent_at: sentAt.toISOString(),
    received_at: now.toISOString(),
    status: 'queued',
    payload_json: JSON.stringify({ schema: 'dayz.normalized-batch.v1', batch_id: body.batch_id, events: normalizedEvents }),
    attempt_count: 0,
    next_attempt_at: now.toISOString(),
    lease_token: '',
    last_error: '',
    private_expires_at: privateExpiresAt.toISOString(),
    expires_at: retentionExpiresAt.toISOString(),
  },
  response: { schema: 'dayz.ingest-response.v1', accepted: true, duplicate: false, batch_id: body.batch_id, event_count: normalizedEvents.length },
} }];`;
}

function filterDueBatchesCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const now = Date.now();
const executionId = (typeof $execution !== 'undefined' && $execution && $execution.id) ? String($execution.id) : String(now);
return $input.all().filter((item) => {
  const row = item.json;
  const next = new Date(row.next_attempt_at).getTime();
  const expires = new Date(row.expires_at).getTime();
  return row.status === 'queued' && Number(row.attempt_count || 0) < POLICY.limits.batch_max_attempts && Number.isFinite(next) && next <= now && Number.isFinite(expires) && expires > now;
}).map((item) => ({ json: {
  ...item.json,
  claim_token: 'batch_lease_' + executionId + '_' + item.json.batch_id,
  claim_until: new Date(now + POLICY.limits.batch_lease_seconds * 1000).toISOString(),
  claimed_at: new Date(now).toISOString(),
} }));`;
}

function expandBatchEventsCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const output = [];
for (const [batchIndex, item] of $input.all().entries()) {
  const batch = item.json;
  let payload;
  try { payload = JSON.parse(batch.payload_json || '{}'); } catch { throw new Error('invalid_normalized_batch_json'); }
  if (!payload || payload.schema !== 'dayz.normalized-batch.v1' || payload.batch_id !== batch.batch_id || !Array.isArray(payload.events) || !payload.events.length) throw new Error('invalid_normalized_batch_contract');
  for (const event of payload.events) {
    const adminDocument = {
    schema: 'dayz.admin-event.v1',
    event_id: event.event_id,
    server_id: event.server_id,
    occurred_at: event.occurred_at,
    processing_expires_at: event.processing_expires_at,
    type: event.event_type,
    severity: event.severity,
    view: event.admin_view,
    chronicle_private: event.chronicle_private,
    routing: event.routing,
    policy_version: POLICY.version,
  };
    const publicDocument = event.public_view === null ? null : {
    schema: 'dayz.public-event.v1',
    event_id: event.event_id,
    server_id: event.server_id,
    occurred_at: event.occurred_at,
    type: event.event_type,
    severity: event.severity,
    view: event.public_view,
  };
    output.push({ json: {
      record_id: 'event:' + event.event_id,
      record_kind: 'event',
      batch_id: batch.batch_id,
      event_id: event.event_id,
      server_id: event.server_id,
      event_type: event.event_type,
      occurred_at: event.occurred_at,
      observed_at: event.observed_at,
      processing_expires_at: event.processing_expires_at,
      received_at: batch.received_at,
      severity: event.severity,
      audience_ceiling: event.audience_ceiling,
      public_json: publicDocument ? JSON.stringify(publicDocument) : '',
      admin_json: JSON.stringify(adminDocument),
      digest_state: event.routing.digest_state,
      chronicle_state: event.routing.chronicle_state || 'none',
      safety_state: event.routing.safety_state || 'none',
      announcement_state: event.routing.announcement_state || 'none',
      immediate_state: event.event_type === 'player.command' ? 'command_pending' : (event.routing.immediate_channels.length ? 'pending' : 'none'),
      private_expires_at: batch.private_expires_at,
      expires_at: batch.expires_at,
    }, pairedItem: { item: batchIndex } });
  }
}
return output;`;
}

function immediateDeliveryCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const output = [];
for (const [rowIndex, item] of $input.all().entries()) {
const row = item.json;
let event;
try { event = JSON.parse(row.admin_json || '{}'); } catch { continue; }
if (event.type === 'player.command') continue;
const channels = event.routing && Array.isArray(event.routing.immediate_channels) ? event.routing.immediate_channels : [];
if (!channels.length) continue;

function clean(value, max = 180) {
  return String(value ?? '').replace(/[\\u0000-\\u001f\\u007f]+/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, max);
}
function fnv(value) {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return (hash >>> 0).toString(16).padStart(8, '0');
}
const view = event.view && typeof event.view === 'object' ? event.view : {};
const actor = view.actor && typeof view.actor === 'object' ? view.actor : {};
const attacker = view.attacker && typeof view.attacker === 'object' ? view.attacker : {};
const location = view.location && typeof view.location === 'object' ? view.location : {};
const name = clean(actor.display_name || 'неизвестный игрок', 64);
const target = clean(attacker.display_name || 'неизвестный игрок', 64);
const where = location.grid_100m ? ', сетка ' + clean(location.grid_100m, 24) : '';
let telegram = '[' + event.server_id + '] ' + event.type + where + '.';
let game = '';
switch (event.type) {
  case 'player.sos':
    telegram = '[' + event.server_id + '] SOS от ' + name + where + '.';
    game = 'SOS принят. Администраторы уведомлены.';
    break;
  case 'player.admin_message':
    telegram = '[' + event.server_id + '] Сообщение администратору от ' + name + where + ': ' + clean(view.message || 'без текста', 500);
    game = 'Сообщение администраторам принято.';
    break;
  case 'combat.pvp': case 'combat.pvp_kill': telegram = '[' + event.server_id + '] PvP: ' + name + ', атаковал ' + target + where + '.'; break;
  case 'combat.explosion': telegram = '[' + event.server_id + '] Взрыв рядом с ' + name + where + '.'; break;
  case 'player.death': case 'player.killed': case 'player.suicide': telegram = '[' + event.server_id + '] Критическое событие ' + event.type + ': ' + name + where + '.'; break;
  case 'base.dismantled': telegram = '[' + event.server_id + '] Демонтаж ' + clean(view.object || 'объекта', 100) + ', игрок ' + name + where + '.'; break;
  case 'world.event.present': case 'world.event.started': case 'world.event.updated': case 'world.event.ended': {
    const telemetry = view.telemetry && typeof view.telemetry === 'object' ? view.telemetry : {};
    const kind = clean(telemetry.kind || 'unknown', 64);
    const exact = Number.isFinite(Number(location.x)) && Number.isFinite(Number(location.z)) ? ', x=' + Number(location.x).toFixed(1) + ', z=' + Number(location.z).toFixed(1) : '';
    telegram = '[' + event.server_id + '] Мир: ' + kind + ', ' + event.type + exact + where + '.';
    break;
  }
  case 'server.started': telegram = '[' + event.server_id + '] Сервер запущен.'; break;
  case 'server.restart_due': {
    let publicEvent = {};
    try { publicEvent = JSON.parse(row.public_json || '{}'); } catch { publicEvent = {}; }
    const seconds = Math.max(0, Math.min(86400, Number(publicEvent.view && publicEvent.view.restart_in_seconds || 0)));
    telegram = '[' + event.server_id + '] Перезапуск сервера через ' + seconds + ' с. Найдите безопасное место.';
    game = telegram;
    break;
  }
  case 'server.stopping': telegram = '[' + event.server_id + '] Сервер завершает работу.'; break;
  case 'server.script_error': telegram = '[' + event.server_id + '] Ошибка скрипта: ' + clean(view.summary || 'без деталей', 500); break;
  case 'server.economy_anomaly': telegram = '[' + event.server_id + '] Аномалия экономики: ' + clean(view.summary || 'без деталей', 500); break;
  case 'server.telemetry_stale': telegram = '[' + event.server_id + '] Телеметрия не обновляется, секунд: ' + clean(view.stale_seconds || 'unknown', 40) + '.'; break;
  case 'telemetry.error': case 'telemetry.stopped': telegram = '[' + event.server_id + '] Проблема телеметрии: ' + clean(view.reason || event.type, 300) + '.'; break;
  case 'storage.health_anomaly': telegram = '[' + event.server_id + '] Аномалия хранилища: проверьте файлы и резервные копии.'; break;
  case 'storage.health_recovered': telegram = '[' + event.server_id + '] Состояние хранилища восстановлено.'; break;
}
telegram = clean(telegram, POLICY.limits.telegram_message_max_chars);
game = clean(game, POLICY.limits.game_message_max_chars);
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const minimumSignalRemainingMs = (
  POLICY.limits.delivery_lease_seconds
  + POLICY.limits.delivery_scan_interval_seconds
  + POLICY.limits.signal_reconcile_buffer_seconds
) * 1000;
const processingDeadline = new Date(event.processing_expires_at || now.getTime() + 900000);
const hardDeadline = new Date(now.getTime() + 3600000);
const expiresAt = new Date(Math.min(processingDeadline.getTime(), hardDeadline.getTime()));
const deleteAfter = new Date(now.getTime() + POLICY.limits.delivery_retention_days * 86400000);
for (const channel of channels) {
  const message = channel === 'telegram' ? telegram : game;
  if (!message) continue;
  const channelExpiresAt = channel === 'game'
    ? new Date(Math.min(expiresAt.getTime(), now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000))
    : expiresAt;
  const remainingMs = channelExpiresAt.getTime() - now.getTime();
  if (!Number.isFinite(channelExpiresAt.getTime()) || remainingMs < 1000) continue;
  const insufficientSignalWindow = channel === 'game' && remainingMs <= minimumSignalRemainingMs;
  const deliveryMessage = insufficientSignalWindow ? '' : message;
  const deliveryId = 'dlv_' + event.event_id.slice(4) + '_' + channel + '_v1';
  const interactive = ['player.sos','player.admin_message'].includes(event.type);
  const actorRef = /^p_[0-9a-f]{20}$/.test(String(actor.ref || '')) ? actor.ref : 'anonymous';
  output.push({ json: {
    delivery_id: deliveryId,
    event_id: event.event_id,
    server_id: event.server_id,
    channel,
    message: deliveryMessage,
    status: insufficientSignalWindow ? 'expired' : 'pending',
    attempt_count: 0,
    next_attempt_at: now.toISOString(),
    expires_at: channelExpiresAt.toISOString(),
    delete_after: deleteAfter.toISOString(),
    last_error: insufficientSignalWindow ? 'insufficient_signal_recovery_window' : '',
    sent_at: null,
    created_at: now.toISOString(),
    payload_hash: fnv(deliveryMessage),
    policy_version: POLICY.version,
    message_kind: event.type,
    cooldown_scope: channel === 'game' ? (interactive ? 'command|' + event.server_id + '|player|' + actorRef : 'auto|' + event.server_id + '|interval') : '',
    cooldown_seconds: channel === 'game' ? (interactive ? POLICY.limits.command_player_cooldown_seconds : POLICY.limits.auto_game_min_interval_seconds) : 0,
    secondary_cooldown_scope: channel === 'game' && interactive ? 'command|' + event.server_id + '|global' : '',
    secondary_cooldown_seconds: channel === 'game' && interactive ? POLICY.limits.command_global_cooldown_seconds : 0,
    rate_scope_prefix: channel === 'game' && !interactive ? 'auto|' + event.server_id + '|slot|' : '',
    rate_slot_scope: '',
    lease_token: '',
    claimed_at: null,
    source_event_ids_json: JSON.stringify([event.event_id]),
    signal_status_url: '',
  }, pairedItem: { item: rowIndex } });
}
}
return output;`;
}

function commandDeliveryCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const row = $('Get Pending Player Commands').item.json;
let adminEvent = {};
let publicEvent = {};
let snapshotEvent = {};
try { adminEvent = JSON.parse(row.admin_json || '{}'); } catch { adminEvent = {}; }
try { publicEvent = JSON.parse(row.public_json || '{}'); } catch { publicEvent = {}; }
const snapshotRow = $('Get Latest Snapshot for Command').item.json;
try { snapshotEvent = JSON.parse(snapshotRow.public_json || '{}'); } catch { snapshotEvent = {}; }
const command = String(publicEvent.view && publicEvent.view.command || '').toLowerCase();
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const snapshotAt = new Date(snapshotEvent.occurred_at).getTime();
const snapshotFresh = Number.isFinite(snapshotAt) && snapshotAt <= now.getTime() + POLICY.limits.event_max_future_seconds * 1000 && now.getTime() - snapshotAt <= POLICY.limits.snapshot_fresh_seconds * 1000;
const snapshot = snapshotFresh && snapshotEvent.server_id === row.server_id && snapshotEvent.type === 'world.snapshot'
  ? (snapshotEvent.view || {}) : {};
function clean(value, max) { return String(value ?? '').replace(/[\\u0000-\\u001f\\u007f]+/g,' ').replace(/\\s+/g,' ').trim().slice(0,max); }
function phenomenon(name) {
  const value = snapshot.weather && snapshot.weather[name] && Number(snapshot.weather[name].actual);
  if (!Number.isFinite(value)) return null;
  return Math.round(Math.max(0, Math.min(1, value)) * 100);
}
let message = 'Команда временно недоступна.';
if (command === 'status') {
  const online = Number(snapshot.players_online);
  message = Number.isFinite(online) ? 'Сервер работает. Игроков онлайн: ' + Math.max(0, Math.round(online)) + '.' : 'Данные о состоянии сервера временно недоступны.';
} else if (command === 'weather') {
  const parts = [['rain','дождь'],['overcast','облачность'],['fog','туман']]
    .map(([key,label]) => { const value = phenomenon(key); return value === null ? '' : label + ' ' + value + '%'; })
    .filter(Boolean);
  message = parts.length ? 'Погода: ' + parts.join(', ') + '.' : 'Данные о погоде пока недоступны.';
} else if (command === 'time') {
  const game = snapshot.game_time || {};
  const hour = Number(game.hour); const minute = Number(game.minute);
  message = Number.isFinite(hour) && Number.isFinite(minute)
    ? 'Игровое время: ' + String(Math.max(0,Math.min(23,Math.trunc(hour)))).padStart(2,'0') + ':' + String(Math.max(0,Math.min(59,Math.trunc(minute)))).padStart(2,'0') + (game.is_night === true ? ', ночь.' : game.is_night === false ? ', день.' : '.')
    : 'Игровое время пока недоступно.';
} else if (command === 'rules') {
  const configuredRules = typeof $json.rules_message_ru === 'string' ? clean($json.rules_message_ru, POLICY.limits.game_message_max_chars) : '';
  message = configuredRules || 'Правила сервера пока не настроены. Обратитесь к администратору.';
}
message = clean(message, POLICY.limits.game_message_max_chars);
const actor = adminEvent.view && adminEvent.view.actor || {};
const actorRef = /^p_[0-9a-f]{20}$/.test(String(actor.ref || '')) ? actor.ref : 'anonymous';
const processingDeadline = new Date(adminEvent.processing_expires_at || now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000);
const expiresAt = new Date(Math.min(processingDeadline.getTime(), now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000));
const minimumSignalRemainingMs = (
  POLICY.limits.delivery_lease_seconds
  + POLICY.limits.delivery_scan_interval_seconds
  + POLICY.limits.signal_reconcile_buffer_seconds
) * 1000;
if (!row.event_id || !message || !Number.isFinite(expiresAt.getTime()) || expiresAt.getTime() - now.getTime() <= minimumSignalRemainingMs) return { json: { skip: true, event_id: row.event_id || '' } };
let hash = 2166136261; for (let index=0; index<message.length; index+=1) { hash ^= message.charCodeAt(index); hash = Math.imul(hash,16777619); }
return { json: {
  delivery_id: 'dlv_' + row.event_id.slice(4) + '_game_v1', event_id: row.event_id,
  server_id: row.server_id, channel: 'game', message, status: 'pending', attempt_count: 0,
  next_attempt_at: now.toISOString(), expires_at: expiresAt.toISOString(),
  delete_after: new Date(now.getTime() + POLICY.limits.delivery_retention_days * 86400000).toISOString(),
  last_error: '', sent_at: null, created_at: now.toISOString(),
  payload_hash: (hash>>>0).toString(16).padStart(8,'0'), policy_version: POLICY.version,
  message_kind: 'player.command.' + command,
  cooldown_scope: 'command|' + row.server_id + '|player|' + actorRef,
  cooldown_seconds: POLICY.limits.command_player_cooldown_seconds,
  secondary_cooldown_scope: 'command|' + row.server_id + '|global',
  secondary_cooldown_seconds: POLICY.limits.command_global_cooldown_seconds,
  rate_scope_prefix: '', rate_slot_scope: '',
  lease_token: '', claimed_at: null,
  source_event_ids_json: JSON.stringify([row.event_id]),
  signal_status_url: '',
} };`;
}

function safetyWarningDeliveryCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const output = [];
for (const [rowIndex,item] of $input.all().entries()) {
  const row = item.json;
  let event = {};
  try { event = JSON.parse(row.public_json || '{}'); } catch { event = {}; }
  const view = event.view && typeof event.view === 'object' ? event.view : {};
  if (!event.type || !event.type.startsWith('world.event.') || view.kind !== 'contaminated_area') continue;
  const sector = view.location && view.location.precision === 'coarse' && /^[A-Z][A-Z0-9-]{0,11}$/.test(String(view.location.sector || '')) ? String(view.location.sector) : '';
  const ended = event.type === 'world.event.ended';
  let message = ended ? 'Опасная заражённая зона больше не активна' : 'Опасная заражённая зона активна';
  if (sector) message += ' в грубом секторе ' + sector;
  message += ended ? '.' : '. Обходите этот район.';
  message = message.replace(/[\\u0000-\\u001f\\u007f]+/g,' ').replace(/\\s+/g,' ').trim().slice(0,POLICY.limits.public_message_max_chars);
  const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
  const minimumSignalRemainingMs = (
    POLICY.limits.delivery_lease_seconds
    + POLICY.limits.delivery_scan_interval_seconds
    + POLICY.limits.signal_reconcile_buffer_seconds
  ) * 1000;
  const baseExpires = new Date(row.processing_expires_at || now.getTime() + 3600000);
  const deleteAfter = new Date(now.getTime() + POLICY.limits.delivery_retention_days * 86400000).toISOString();
  let hash = 2166136261; for (let index=0; index<message.length; index+=1) { hash ^= message.charCodeAt(index); hash = Math.imul(hash,16777619); }
  for (const channel of ['game']) {
    const expiresAt = channel === 'game' ? new Date(Math.min(baseExpires.getTime(), now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000)) : baseExpires;
    if (!Number.isFinite(expiresAt.getTime())) continue;
    const insufficientSignalWindow = expiresAt.getTime() - now.getTime() <= minimumSignalRemainingMs;
    const deliveryMessage = insufficientSignalWindow ? '' : message;
    output.push({ json: {
      delivery_id: 'dlv_' + row.event_id.slice(4) + '_safety_' + channel + '_v1', event_id: row.event_id,
      server_id: row.server_id, channel, message: deliveryMessage, status: insufficientSignalWindow ? 'expired' : 'pending', attempt_count: 0,
      next_attempt_at: now.toISOString(), expires_at: expiresAt.toISOString(), delete_after: deleteAfter,
      last_error: insufficientSignalWindow ? 'insufficient_signal_recovery_window' : '', sent_at: null, created_at: now.toISOString(),
      payload_hash: insufficientSignalWindow ? '811c9dc5' : (hash>>>0).toString(16).padStart(8,'0'), policy_version: POLICY.version,
      message_kind: 'world.safety.contaminated_area',
      cooldown_scope: channel === 'game' ? 'auto|' + row.server_id + '|interval' : '',
      cooldown_seconds: channel === 'game' ? POLICY.limits.auto_game_min_interval_seconds : 0,
      secondary_cooldown_scope: '', secondary_cooldown_seconds: 0,
      rate_scope_prefix: channel === 'game' ? 'auto|' + row.server_id + '|slot|' : '', rate_slot_scope: '',
      lease_token: '', claimed_at: null, source_event_ids_json: JSON.stringify([row.event_id]), signal_status_url: '',
    }, pairedItem: { item: rowIndex } });
  }
}
return output;`;
}

function delayedWorldAnnouncementCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const output = [];
for (const [rowIndex,item] of $input.all().entries()) {
  const row = item.json; let event = {};
  try { event = JSON.parse(row.public_json || '{}'); } catch { event = {}; }
  const view = event.view && typeof event.view === 'object' ? event.view : {};
  const kind = String(view.kind || '');
  if (!event.type || !event.type.startsWith('world.event.') || !['heli_crash','military_convoy','train','police_situation'].includes(kind)) continue;
  const occurred = new Date(event.occurred_at).getTime();
  if (!Number.isFinite(occurred) || occurred + POLICY.limits.public_delay_seconds * 1000 > now.getTime()) continue;
  const sector = view.location && view.location.precision === 'coarse' && /^[A-Z][A-Z0-9-]{0,11}$/.test(String(view.location.sector || '')) ? String(view.location.sector) : '';
  const labels = { heli_crash: 'вертолётное событие', military_convoy: 'военный конвой', train: 'поезд', police_situation: 'полицейское событие' };
  const ended = event.type === 'world.event.ended';
  let message = ended ? (labels[kind] + ' завершилось') : ('Обнаружено событие: ' + labels[kind]);
  if (sector) message += ' в грубом секторе ' + sector;
  message += '.';
  message = message.replace(/[\\u0000-\\u001f\\u007f]+/g,' ').replace(/\\s+/g,' ').trim().slice(0,POLICY.limits.public_message_max_chars);
  const minimumSignalRemainingMs = (
    POLICY.limits.delivery_lease_seconds
    + POLICY.limits.delivery_scan_interval_seconds
    + POLICY.limits.signal_reconcile_buffer_seconds
  ) * 1000;
  const baseExpires = new Date(row.processing_expires_at || now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000);
  const expiresAt = new Date(Math.min(baseExpires.getTime(), now.getTime() + POLICY.limits.signal_ttl_max_seconds * 1000));
  if (!Number.isFinite(expiresAt.getTime())) continue;
  const insufficientSignalWindow = expiresAt.getTime() - now.getTime() <= minimumSignalRemainingMs;
  const deliveryMessage = insufficientSignalWindow ? '' : message;
  let hash = 2166136261; for (let index=0; index<message.length; index+=1) { hash ^= message.charCodeAt(index); hash = Math.imul(hash,16777619); }
  output.push({ json: {
    delivery_id: 'dlv_' + row.event_id.slice(4) + '_announce_game_v1', event_id: row.event_id,
    server_id: row.server_id, channel: 'game', message: deliveryMessage, status: insufficientSignalWindow ? 'expired' : 'pending', attempt_count: 0,
    next_attempt_at: now.toISOString(), expires_at: expiresAt.toISOString(),
    delete_after: new Date(now.getTime()+POLICY.limits.delivery_retention_days*86400000).toISOString(),
    last_error: insufficientSignalWindow ? 'insufficient_signal_recovery_window' : '', sent_at: null, created_at: now.toISOString(),
    payload_hash: insufficientSignalWindow ? '811c9dc5' : (hash>>>0).toString(16).padStart(8,'0'), policy_version: POLICY.version,
    message_kind: 'world.announcement.' + kind,
    cooldown_scope: 'auto|' + row.server_id + '|interval', cooldown_seconds: POLICY.limits.auto_game_min_interval_seconds,
    secondary_cooldown_scope: '', secondary_cooldown_seconds: 0,
    rate_scope_prefix: 'auto|' + row.server_id + '|slot|', rate_slot_scope: '',
    lease_token: '', claimed_at: null, source_event_ids_json: JSON.stringify([row.event_id]), signal_status_url: '',
  }, pairedItem: { item: rowIndex } });
}
return output;`;
}

function aggregateDigestCode() {
  return `const rows = $input.all().map((item) => item.json);
if (!rows.length) return [];
const grouped = new Map();
for (const row of rows) {
  let event;
  try { event = JSON.parse(row.public_json || '{}'); } catch { continue; }
  if (!event || event.schema !== 'dayz.public-event.v1' || event.server_id !== row.server_id) continue;
  if (!grouped.has(row.server_id)) grouped.set(row.server_id, []);
  grouped.get(row.server_id).push({
    event_id: row.event_id,
    occurred_at: event.occurred_at,
    processing_expires_at: row.processing_expires_at,
    type: event.type,
    severity: event.severity,
    public_view: event.view && typeof event.view === 'object' ? event.view : {},
  });
}
return Array.from(grouped.entries()).map(([server_id, candidates]) => ({ json: {
  server_id,
  candidate_events_json: JSON.stringify(candidates),
} }));`;
}

function combineDigestRouteCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const batches = new Map($('Aggregate Public Digest').all().map((item) => [item.json.server_id, item.json]));
const output = [];
for (const [routeIndex, item] of $input.all().entries()) {
const route = item.json;
const batch = batches.get(route && route.server_id);
if (!batch) continue;
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const publicDelay = Math.max(0, Math.min(86400, Number(route.public_delay_seconds ?? POLICY.limits.public_delay_seconds)));
let candidates = [];
try { candidates = JSON.parse(batch.candidate_events_json || '[]'); } catch { candidates = []; }
const cutoff = now.getTime() - publicDelay * 1000;
const windowStart = now.getTime() - POLICY.limits.digest_window_seconds * 1000;
const eligible = candidates.filter((event) => {
  const occurred = new Date(event.occurred_at).getTime();
  const deadline = new Date(event.processing_expires_at).getTime();
  return Number.isFinite(occurred) && Number.isFinite(deadline) && deadline > now.getTime() && occurred <= (event.type === 'world.snapshot' ? now.getTime() : cutoff);
}).sort((left,right) => String(left.occurred_at).localeCompare(String(right.occurred_at)));
if (!eligible.length) continue;
const sourceDeadline = Math.min(...eligible.map((event) => new Date(event.processing_expires_at).getTime()));
const deliveryDeadline = Math.min(now.getTime() + 3600000, sourceDeadline);
if (!Number.isFinite(deliveryDeadline) || deliveryDeadline - now.getTime() < 1000) continue;
const stale = eligible.filter((event) => new Date(event.occurred_at).getTime() < windowStart);
const snapshots = eligible.filter((event) => event.type === 'world.snapshot');
const worldEvents = eligible.filter((event) => event.type !== 'world.snapshot' && new Date(event.occurred_at).getTime() >= windowStart);
const transitions = [];
function actual(view, name) {
  const value = view && view.weather && view.weather[name] && Number(view.weather[name].actual);
  return Number.isFinite(value) ? value : null;
}
for (let index=1; index<snapshots.length; index+=1) {
  const before = snapshots[index-1].public_view || {};
  const after = snapshots[index].public_view || {};
  const beforeNight = before.game_time && before.game_time.is_night;
  const afterNight = after.game_time && after.game_time.is_night;
  if (typeof beforeNight === 'boolean' && typeof afterNight === 'boolean' && beforeNight !== afterNight) {
    transitions.push({ type: 'world.transition', severity: 'info', public_view: { transition: afterNight ? 'day_to_night' : 'night_to_day' } });
  }
  for (const [name,threshold] of [['rain',0.5],['fog',0.6]]) {
    const left = actual(before,name); const right = actual(after,name);
    if (left !== null && right !== null && left < threshold && right >= threshold) transitions.push({ type: 'world.weather_anomaly', severity: 'warning', public_view: { phenomenon: name, state: 'started' } });
    if (left !== null && right !== null && left >= threshold && right < threshold) transitions.push({ type: 'world.weather_anomaly', severity: 'info', public_view: { phenomenon: name, state: 'ended' } });
  }
}
const significant = worldEvents.length > 0;
const publishEntries = significant ? worldEvents.concat(transitions) : transitions;
const consumeIds = new Set(stale.map((event) => event.event_id));
for (const snapshot of snapshots.slice(0,-1)) consumeIds.add(snapshot.event_id);
if (significant) for (const event of worldEvents) consumeIds.add(event.event_id);
if (!publishEntries.length && !consumeIds.size) continue;
const bucketMs = POLICY.limits.digest_window_seconds * 1000;
const bucket = new Date(Math.floor(now.getTime() / bucketMs) * bucketMs).toISOString();
const counts = {};
const regions = {};
for (const event of publishEntries) {
  counts[event.type] = (counts[event.type] || 0) + 1;
  const sector = event.public_view && event.public_view.location && event.public_view.location.sector;
  if (sector) regions[sector] = (regions[sector] || 0) + 1;
}
const highlights = publishEntries
  .slice()
  .sort((a,b) => String(b.occurred_at).localeCompare(String(a.occurred_at)))
  .slice(0,20)
  .map((event) => ({ type: event.type, severity: event.severity, public_view: event.public_view }));
const safeDigest = { schema: 'dayz.public-digest.v1', server_id: batch.server_id, bucket, total_events: publishEntries.length, counts, regions, highlights };
const ruLabels = {
  'world.event.present': 'активных мировых событий',
  'world.event.started': 'начавшихся мировых событий',
  'world.event.updated': 'изменённых мировых событий',
  'world.event.ended': 'завершившихся мировых событий',
  'world.transition': 'переходов дня и ночи',
  'world.weather_anomaly': 'изменений опасной погоды'
};
const countPartsRu = Object.entries(counts).sort((a,b) => b[1]-a[1]).slice(0,6).map(([key,value]) => value + ' ' + (ruLabels[key] || key));
const digestId = 'digest_' + batch.server_id + '_' + bucket.replace(/[-:.TZ]/g, '').slice(0,12);
output.push({ json: {
  digest_id: digestId,
  server_id: batch.server_id,
  bucket,
  publish: publishEntries.length > 0,
  public_delay_seconds: publicDelay,
  public_digest_json: JSON.stringify(safeDigest),
  source_event_ids_json: JSON.stringify(Array.from(consumeIds)),
  fallback_message: publishEntries.length ? ('Сводка Livonia за час: ' + countPartsRu.join(', ') + '.').slice(0, POLICY.limits.public_message_max_chars) : '',
  created_at: now.toISOString(),
  expires_at: new Date(deliveryDeadline).toISOString(),
  delete_after: new Date(now.getTime() + POLICY.limits.delivery_retention_days * 86400000).toISOString(),
  route,
}, pairedItem: { item: routeIndex } });
}
return output;`;
}

function digestWordingCode(useLlmResponse) {
  const responseSection = useLlmResponse
    ? `let parsed = null;
try {
  const body = $json && $json.body !== undefined ? $json.body : $json;
  let content = body && body.choices && body.choices[0] && body.choices[0].message ? body.choices[0].message.content : '';
  if (typeof content === 'string') {
    content = content.trim().replace(/^\u0060\u0060\u0060(?:json)?\\s*/i, '').replace(/\\s*\u0060\u0060\u0060$/, '');
    parsed = JSON.parse(content);
  } else if (content && typeof content === 'object') parsed = content;
} catch { parsed = null; }
const forbidden = (text) => /\\bid\\s*=|\\b[0-9a-f]{32,64}\\b|https?:\\/\\/|\\b[xyz]\\s*[=:]\\s*-?\\d|\\b\\d{3,5}\\s*[,;/]\\s*\\d{3,5}\\b/i.test(text);
const exactKeys = parsed && Object.keys(parsed).sort().join(',') === 'message_ru,safety_flags';
const safeFlags = exactKeys && Array.isArray(parsed.safety_flags) && parsed.safety_flags.length === 0;
const rawCandidate = safeFlags && typeof parsed.message_ru === 'string' ? parsed.message_ru : '';
const hasControls = /[\\u0000-\\u001f\\u007f-\\u009f]/.test(rawCandidate);
const candidate = hasControls ? '' : rawCandidate.replace(/\\s+/g, ' ').trim();
if (candidate && candidate.length <= POLICY.limits.public_message_max_chars && !forbidden(candidate)) message = candidate;`
    : '';
  return `const POLICY = ${JSON.stringify(POLICY)};
const context = ${useLlmResponse ? "$('Combine Digest and Route').item.json" : '$json'};
let message = String(context.fallback_message || '').replace(/[\\u0000-\\u001f\\u007f-\\u009f]+/g, ' ').replace(/\\s+/g, ' ').trim();
${responseSection}
message = message.slice(0, POLICY.limits.public_message_max_chars);
return { json: { ...context, message_ru: message } };`;
}

function expandDigestDeliveriesCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
function fnv(value) { let hash = 2166136261; for (let i=0;i<value.length;i+=1) { hash ^= value.charCodeAt(i); hash = Math.imul(hash,16777619); } return (hash>>>0).toString(16).padStart(8,'0'); }
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const minimumSignalRemainingMs = (
  POLICY.limits.delivery_lease_seconds
  + POLICY.limits.delivery_scan_interval_seconds
  + POLICY.limits.signal_reconcile_buffer_seconds
) * 1000;
const output = [];
for (const [contextIndex, item] of $input.all().entries()) {
  const context = item.json;
  const route = context.route || {};
  const createdAtMs = new Date(context.created_at).getTime();
  const expiresAtMs = new Date(context.expires_at).getTime();
  const channels = [];
  if (context.publish && route.telegram_enabled && context.message_ru) channels.push(['telegram', context.message_ru]);
  if (context.publish && route.game_enabled && context.message_ru) channels.push(['game', context.message_ru]);
  if (!channels.length) channels.push(['none', '']);
  for (const [channel,message] of channels) {
  const deliveryExpiresAt = channel === 'game'
    ? new Date(Math.min(expiresAtMs, createdAtMs + POLICY.limits.signal_ttl_max_seconds * 1000)).toISOString()
    : context.expires_at;
  const insufficientSignalWindow = channel === 'game' && new Date(deliveryExpiresAt).getTime() - now.getTime() <= minimumSignalRemainingMs;
  const deliveryMessage = insufficientSignalWindow ? '' : message;
  output.push({ json: {
    delivery_id: 'dlv_' + context.digest_id + '_' + channel + '_v1',
    event_id: context.digest_id,
    server_id: context.server_id,
    channel,
    message: deliveryMessage,
    status: channel === 'none' ? 'suppressed_configuration' : insufficientSignalWindow ? 'expired' : 'pending',
    attempt_count: 0,
    next_attempt_at: context.created_at,
    expires_at: deliveryExpiresAt,
    delete_after: context.delete_after,
    last_error: channel === 'none' ? 'no_output_channel_enabled' : insufficientSignalWindow ? 'insufficient_signal_recovery_window' : '',
    sent_at: null,
    created_at: context.created_at,
    payload_hash: fnv(deliveryMessage),
    policy_version: POLICY.version,
    message_kind: context.message_kind || 'public.digest',
    cooldown_scope: channel === 'game' ? 'auto|' + context.server_id + '|interval' : '',
    cooldown_seconds: channel === 'game' ? Math.max(POLICY.limits.auto_game_min_interval_seconds, Number(route.game_cooldown_seconds || POLICY.limits.game_global_cooldown_seconds)) : 0,
    secondary_cooldown_scope: '',
    secondary_cooldown_seconds: 0,
    rate_scope_prefix: channel === 'game' ? 'auto|' + context.server_id + '|slot|' : '',
    rate_slot_scope: '',
    lease_token: '',
    claimed_at: null,
    source_event_ids_json: context.source_event_ids_json,
    signal_status_url: '',
  }, pairedItem: { item: contextIndex } });
  }
}
return output;`;
}

function expandDigestEventIdsCode() {
  return `const seen = new Set();
const output = [];
for (const item of $input.all()) {
  let ids = [];
  try { ids = JSON.parse(item.json.source_event_ids_json || '[]'); } catch { ids = []; }
  for (const eventId of ids) {
    if (typeof eventId !== 'string' || seen.has(eventId)) continue;
    seen.add(eventId);
    output.push({ json: { event_id: eventId, digest_state: 'queued' } });
  }
}
return output;`;
}

function aggregateChronicleCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const windowStart = now.getTime() - POLICY.limits.chronicle_window_seconds * 1000;
const grouped = new Map();
for (const item of $input.all()) {
  const row = item.json;
  let pub = {}; let admin = {};
  try { pub = JSON.parse(row.public_json || '{}'); } catch { pub = {}; }
  try { admin = JSON.parse(row.admin_json || '{}'); } catch { admin = {}; }
  const view = pub.view && typeof pub.view === 'object' ? pub.view : {};
  if (!['combat.infected_pressure','combat.vehicle_incident','combat.wildlife_pressure'].includes(row.event_type)) continue;
  const occurred = new Date(row.occurred_at).getTime();
  const processingDeadline = new Date(row.processing_expires_at).getTime();
  if (!Number.isFinite(occurred) || !Number.isFinite(processingDeadline) || processingDeadline <= now.getTime()) continue;
  if (!grouped.has(row.server_id)) grouped.set(row.server_id, []);
  const cohort = admin.chronicle_private && typeof admin.chronicle_private === 'object' ? admin.chronicle_private : {};
  grouped.get(row.server_id).push({
    event_id: row.event_id, event_type: row.event_type, occurred, processing_deadline: processingDeadline,
    signal_count: Math.max(1, Math.min(1000, Number(view.signal_count || 1))),
    actor_ref: /^p_[0-9a-f]{20}$/.test(String(cohort.actor_ref || '')) ? cohort.actor_ref : '',
    sector_2km: /^[A-Z][A-Z0-9-]{0,11}$/.test(String(cohort.sector_2km || '')) ? cohort.sector_2km : '',
  });
}
const output = [];
for (const [server_id,events] of grouped.entries()) {
  const stale = events.filter((event) => event.occurred < windowStart);
  const current = events.filter((event) => event.occurred >= windowStart);
  const publish = current.length >= POLICY.limits.chronicle_min_significant_events;
  const consume = new Set(stale.map((event) => event.event_id));
  if (publish) for (const event of current) consume.add(event.event_id);
  if (!publish && !consume.size) continue;
  const deliveryDeadline = Math.min(now.getTime()+3600000,...events.map((event) => event.processing_deadline));
  if (!Number.isFinite(deliveryDeadline) || deliveryDeadline-now.getTime()<1000) continue;
  const counts = {};
  for (const event of current) counts[event.event_type] = (counts[event.event_type] || 0) + event.signal_count;
  const labels = {
    'combat.infected_pressure': 'атак заражённых',
    'combat.vehicle_incident': 'аварий с транспортом',
    'combat.wildlife_pressure': 'опасных встреч с животными',
  };
  const parts = Object.entries(counts).filter(([,count]) => count > 0).map(([type,count]) => count + ' ' + labels[type]);
  const distinctActors = new Set(current.map((event) => event.actor_ref).filter(Boolean));
  const sectors = new Set();
  if (distinctActors.size >= 3) for (const event of current) {
    if (event.sector_2km) sectors.add(event.sector_2km);
  }
  let message = publish ? 'Хроника Livonia за час: ' + parts.join(', ') + '.' : '';
  if (message && sectors.size) message += ' Грубые секторы: ' + Array.from(sectors).slice(0,3).join(', ') + '.';
  message = message.replace(/[\\u0000-\\u001f\\u007f]+/g,' ').replace(/\\s+/g,' ').trim().slice(0,POLICY.limits.public_message_max_chars);
  const bucket = new Date(Math.floor(now.getTime()/(POLICY.limits.chronicle_window_seconds*1000))*(POLICY.limits.chronicle_window_seconds*1000)).toISOString();
  output.push({ json: {
    digest_id: 'chronicle_' + server_id + '_' + bucket.replace(/[-:.TZ]/g,'').slice(0,12),
    server_id, bucket, publish, message_ru: message,
    source_event_ids_json: JSON.stringify(Array.from(consume)),
    created_at: now.toISOString(), expires_at: new Date(deliveryDeadline).toISOString(),
    delete_after: new Date(now.getTime()+POLICY.limits.delivery_retention_days*86400000).toISOString(),
    message_kind: 'public.chronicle',
  } });
}
return output;`;
}

function combineChronicleRouteCode() {
  return `const contexts = new Map($('Aggregate Hourly Chronicle').all().map((item) => [item.json.server_id,item.json]));
const output = [];
for (const [index,item] of $input.all().entries()) {
  const route = item.json; const context = contexts.get(route && route.server_id);
  if (context) output.push({ json: { ...context, route }, pairedItem: { item: index } });
}
return output;`;
}

function expandChronicleEventIdsCode() {
  return `const seen = new Set(); const output = [];
for (const item of $input.all()) {
  let ids = []; try { ids = JSON.parse(item.json.source_event_ids_json || '[]'); } catch { ids = []; }
  for (const event_id of ids) if (typeof event_id === 'string' && !seen.has(event_id)) { seen.add(event_id); output.push({ json: { event_id, chronicle_state: 'queued' } }); }
}
return output;`;
}

function filterDueDeliveriesCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const now = Date.now();
const executionId = (typeof $execution !== 'undefined' && $execution && $execution.id) ? String($execution.id) : String(now);
return $input.all().filter((item) => {
  const row = item.json;
  const next = new Date(row.next_attempt_at).getTime();
  const expires = new Date(row.expires_at).getTime();
  return row.status === 'pending' && Number(row.attempt_count || 0) < ${POLICY.limits.delivery_max_attempts} && Number.isFinite(next) && next <= now && Number.isFinite(expires) && expires > now;
}).map((item) => ({ json: {
  ...item.json,
  claim_token: 'lease_' + executionId + '_' + item.json.delivery_id,
  claim_until: new Date(now + POLICY.limits.delivery_lease_seconds * 1000).toISOString(),
  claimed_at: new Date(now).toISOString(),
} }));`;
}

function deferForCooldownCode() {
  return `const delivery = $json;
const seconds = Math.max(1, Math.min(3600, Number(delivery.cooldown_seconds || ${POLICY.limits.game_global_cooldown_seconds})));
return { json: {
  ...delivery,
  status: 'pending',
  next_attempt_at: new Date(Date.now() + seconds * 1000).toISOString(),
  last_error: 'game_cooldown_active',
  sent_at: null,
} };`;
}

function combineDeliveryRouteCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const delivery = $('Claim Due Delivery').item.json;
const route = $json;
let deliverable = true;
let terminal_status = '';
let reason = '';
const routeFound = route && route.server_id === delivery.server_id && typeof route.mode === 'string' && typeof route.enabled === 'boolean';
if (!routeFound) { deliverable = false; terminal_status = 'failed_configuration'; reason = 'route_not_found'; }
else if (route.mode === 'shadow') { deliverable = false; terminal_status = 'shadowed'; reason = 'route_shadow_mode'; }
else if (!route.enabled) { deliverable = false; terminal_status = 'suppressed_configuration'; reason = 'route_disabled'; }
else if (delivery.channel === 'telegram' && (!route.telegram_enabled || !route.telegram_chat_id || route.telegram_chat_id === 'REPLACE_ME')) { deliverable = false; terminal_status = 'suppressed_configuration'; reason = 'telegram_disabled'; }
else if (delivery.channel === 'game' && (!route.game_enabled || !/^https?:\\/\\//i.test(String(route.signal_base_url || '')) || /\\.invalid(?:\\/|$)/i.test(String(route.signal_base_url || '')))) { deliverable = false; terminal_status = 'suppressed_configuration'; reason = 'signal_disabled_or_invalid'; }
else if (!['telegram','game'].includes(delivery.channel)) { deliverable = false; terminal_status = 'failed_configuration'; reason = 'unknown_channel'; }
const routeCooldown = Math.max(POLICY.limits.auto_game_min_interval_seconds, Math.min(3600, Number(route && route.game_cooldown_seconds || POLICY.limits.game_global_cooldown_seconds)));
const cooldownSeconds = delivery.rate_scope_prefix ? routeCooldown : Number(delivery.cooldown_seconds || 0);
const telegramTextHtml = String(delivery.message || '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');
return { json: { ...delivery, cooldown_seconds: cooldownSeconds, route, deliverable, terminal_status, route_error: reason, telegram_text_html: telegramTextHtml } };`;
}

function finalizeSuppressedCode() {
  return `const now = new Date().toISOString();
return { json: {
  ...$json,
  status: $json.terminal_status || 'failed_configuration',
  attempt_count: Number($json.attempt_count || 0),
  next_attempt_at: now,
  last_error: String($json.route_error || 'route_configuration_error').slice(0,500),
  sent_at: null,
  message: '',
} };`;
}

function telegramSuccessCode() {
  return `const delivery = $('Combine Delivery and Route').item.json;
const now = new Date().toISOString();
return { json: { ...delivery, status: 'sent', attempt_count: Number(delivery.attempt_count || 0) + 1, next_attempt_at: now, last_error: '', sent_at: now, message: '' } };`;
}

function telegramFailureCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const delivery = $('Combine Delivery and Route').item.json;
const attempt = Number(delivery.attempt_count || 0) + 1;
const rawError = JSON.stringify($json && ($json.error || $json)).slice(0,8192);
const explicitRetryable = /429|too many|rate limit|5\\d\\d/i.test(rawError);
const ambiguous = /timeout|timed out|network|socket|econn/i.test(rawError);
const category = ambiguous ? 'ambiguous' : explicitRetryable ? 'retryable' : 'terminal';
let hash = 2166136261;
for (let index=0; index<rawError.length; index+=1) { hash ^= rawError.charCodeAt(index); hash = Math.imul(hash,16777619); }
const errorText = 'telegram_' + category + '_err_' + (hash>>>0).toString(16).padStart(8,'0');
let status = 'failed';
let next = new Date().toISOString();
if (explicitRetryable && attempt < POLICY.limits.delivery_max_attempts) {
  status = 'pending';
  const delay = POLICY.limits.delivery_retry_seconds[Math.min(attempt - 1, POLICY.limits.delivery_retry_seconds.length - 1)];
  next = new Date(Date.now() + delay * 1000).toISOString();
} else if (ambiguous) status = 'delivery_unknown';
return { json: { ...delivery, status, attempt_count: attempt, next_attempt_at: next, last_error: errorText, sent_at: null, message: status === 'pending' ? delivery.message : '' } };`;
}

function signalCommandCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const delivery = $json;
const base = String(delivery.route.signal_base_url || '').replace(/\\/+$/, '');
const commandId = 'cmd_' + delivery.delivery_id;
const statusUrl = base + '/v1/broadcasts/' + encodeURIComponent(commandId);
const poll = Boolean(delivery.signal_status_url) || delivery.last_error === 'signal_lease_reconcile';
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
const createdAt = new Date(delivery.created_at).getTime();
const expiresAt = new Date(delivery.expires_at).getTime();
const minimumSignalRemainingMs = (
  POLICY.limits.delivery_lease_seconds
  + POLICY.limits.delivery_scan_interval_seconds
  + POLICY.limits.signal_reconcile_buffer_seconds
) * 1000;
const remainingMs = expiresAt - now.getTime();
const wireTtlMs = expiresAt - createdAt;
const validWireTtl = Number.isFinite(createdAt) && Number.isFinite(expiresAt) && wireTtlMs >= 1000 && wireTtlMs <= POLICY.limits.signal_ttl_max_seconds * 1000;
const signalAttemptAllowed = validWireTtl && remainingMs > (poll ? 0 : minimumSignalRemainingMs);
return { json: { ...delivery, signal_url: poll ? statusUrl : base + '/v1/broadcasts', signal_status_url: poll ? statusUrl : '', status_url: statusUrl, signal_method: poll ? 'GET' : 'POST', signal_attempt_allowed: signalAttemptAllowed, signal_remaining_seconds: Math.max(0, Math.floor(remainingMs / 1000)), signal_wire_ttl_seconds: Math.floor(wireTtlMs / 1000), command_id: commandId, command: {
  schema: 'dayz.command.v1',
  command_id: commandId,
  server_id: delivery.server_id,
  created_at: delivery.created_at,
  expires_at: delivery.expires_at,
  channel: 'global',
  message: delivery.message,
  metadata: { event_id: delivery.event_id, policy_version: delivery.policy_version },
} } };`;
}

function expireUnsafeSignalSubmissionCode() {
  return `const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
return { json: {
  ...$json,
  status: 'expired',
  attempt_count: Number($json.attempt_count || 0),
  next_attempt_at: now.toISOString(),
  last_error: $json.signal_method === 'GET' ? 'signal_reconcile_deadline_expired' : 'insufficient_signal_recovery_window',
  sent_at: null,
  message: '',
  signal_status_url: '',
  set_cooldown: false,
} };`;
}

function signalResultCode() {
  return `const POLICY = ${JSON.stringify(POLICY)};
const delivery = $('Build Signal Command').item.json;
const statusCode = Number($json && $json.statusCode || 0);
const body = $json && $json.body && typeof $json.body === 'object' ? $json.body : {};
const remoteStatus = String(body.status || '');
const remoteCommandId = String(body.command_id || '');
const remoteRequestId = String(body.request_id || '');
const matchingRequest = remoteCommandId === delivery.command_id && remoteRequestId === delivery.command_id;
const attempt = Number(delivery.attempt_count || 0) + 1;
let attemptCount = attempt;
const now = (typeof __TEST_NOW__ !== 'undefined') ? new Date(__TEST_NOW__) : new Date();
let status = 'failed';
let next = now.toISOString();
let lastError = '';
let sentAt = null;
let message = delivery.message;
let setCooldown = false;
let signalStatusUrl = String(delivery.signal_status_url || '');
if (delivery.signal_method === 'GET' && statusCode === 404 && body.ok === false && body.error_code === 'not_found') {
  status = 'pending';
  attemptCount = Number(delivery.attempt_count || 0);
  signalStatusUrl = '';
  lastError = 'signal_not_found_retry_post';
} else if ((statusCode === 200 || statusCode === 202) && body.ok === true && matchingRequest && remoteStatus === 'acknowledged') {
  status = 'sent'; sentAt = now.toISOString(); message = ''; setCooldown = true;
} else if ((statusCode === 200 || statusCode === 202) && body.ok === true && matchingRequest && ['queued','sending'].includes(remoteStatus) && attempt < POLICY.limits.delivery_max_attempts) {
  status = 'pending';
  signalStatusUrl = delivery.status_url;
  const delay = POLICY.limits.delivery_retry_seconds[Math.min(attempt - 1, POLICY.limits.delivery_retry_seconds.length - 1)];
  next = new Date(now.getTime() + delay * 1000).toISOString();
  lastError = ('signal_' + remoteStatus).slice(0,500);
} else if ((statusCode === 0 || statusCode === 429 || statusCode === 503 || statusCode >= 500 && ![502,504].includes(statusCode)) && attempt < POLICY.limits.delivery_max_attempts) {
  status = 'pending';
  const delay = POLICY.limits.delivery_retry_seconds[Math.min(attempt - 1, POLICY.limits.delivery_retry_seconds.length - 1)];
  next = new Date(now.getTime() + delay * 1000).toISOString();
  lastError = ('signal_http_' + (statusCode || 'network')).slice(0,500);
} else {
  if (statusCode === 410 || remoteStatus === 'expired') status = 'expired';
  else if (statusCode === 504 || statusCode === 0 || remoteStatus === 'delivery_unknown' || ['queued','sending'].includes(remoteStatus)) status = 'delivery_unknown';
  else status = 'failed';
  lastError = ('signal_' + (remoteStatus || 'http_' + (statusCode || 'network'))).slice(0,500);
  message = '';
}
return { json: { ...delivery, status, attempt_count: attemptCount, next_attempt_at: next, last_error: lastError, sent_at: sentAt, message, signal_status_url: signalStatusUrl, set_cooldown: setCooldown } };`;
}

function cooldownRowCode() {
  return `const delivery = $('Classify Signal Result').item.json;
const now = new Date();
const scopes = [
  [delivery.cooldown_scope, Number(delivery.cooldown_seconds || 0)],
  [delivery.secondary_cooldown_scope, Number(delivery.secondary_cooldown_seconds || 0)],
  [delivery.rate_slot_scope, ${POLICY.limits.auto_game_window_seconds}],
];
return scopes.filter(([scope,seconds]) => scope && seconds > 0).map(([scope,seconds]) => ({ json: {
  scope_key: scope,
  until: new Date(now.getTime() + Math.min(3600,seconds) * 1000).toISOString(),
  last_delivery_id: delivery.delivery_id,
  updated_at: now.toISOString(),
} }));`;
}

function restoreSignalUpdateCode() {
  return `return [{ json: $('Classify Signal Result').item.json }];`;
}

function assignRateSlotCode(slot) {
  return `return { json: { ...$json, rate_slot_scope: String($json.rate_scope_prefix || '') + '${slot}' } };`;
}

function buildBootstrapWorkflow() {
  const wf = 'DayZ | 00 | Bootstrap Data Tables';
  const nodes = [
    node(wf, 'Manual Trigger', 'n8n-nodes-base.manualTrigger', 1, [-900, 0], {}),
    tableCreateNode(wf, 'dayz_routes', [-680, 0]),
    tableCreateNode(wf, 'dayz_events', [-440, 0]),
    tableCreateNode(wf, 'dayz_deliveries', [-200, 0]),
    tableCreateNode(wf, 'dayz_cooldowns', [40, 0]),
    codeNode(wf, 'Bootstrap Summary', [280, 0], `return [{ json: { ok: true, schema: 'dayz.bootstrap-result.v1', tables: ${JSON.stringify(Object.keys(TABLES))}, next: 'Create one dayz_routes row and replace all credential placeholders before activation.' } }];`),
  ];
  const connections = {
    'Manual Trigger': { main: [[mainConnection('Create dayz_routes')]] },
    'Create dayz_routes': { main: [[mainConnection('Create dayz_events')]] },
    'Create dayz_events': { main: [[mainConnection('Create dayz_deliveries')]] },
    'Create dayz_deliveries': { main: [[mainConnection('Create dayz_cooldowns')]] },
    'Create dayz_cooldowns': { main: [[mainConnection('Bootstrap Summary')]] },
  };
  return workflow('00-bootstrap-data-tables.json', wf, nodes, connections, { saveManualExecutions: true });
}

function eventValues(prefix = '$json.event_row') {
  return Object.fromEntries(EVENT_RECORD_COLUMNS.map((column) => [column, `={{ ${prefix}.${column} }}`]));
}

function batchValues(prefix = '$json.batch_row') {
  return Object.fromEntries(BATCH_RECORD_COLUMNS.map((column) => [column, `={{ ${prefix}.${column} }}`]));
}

function deliveryValues(prefix = '$json') {
  return Object.fromEntries(TABLES.dayz_deliveries.map(([column]) => [column, `={{ ${prefix}.${column} }}`]));
}

function buildGatewayWorkflow() {
  const wf = 'DayZ | 10 | Event Gateway';
  const nodes = [
    node(wf, 'Authenticated Event Webhook', 'n8n-nodes-base.webhook', 2.1, [-1100, 0], {
      httpMethod: 'POST',
      path: 'dayz/events/v1',
      authentication: 'headerAuth',
      responseMode: 'responseNode',
      options: {},
    }, {
      webhookId: stableUuid(`${wf}:webhook`),
      credentials: credentialRef(CREDENTIALS.ingest),
    }),
    codeNode(wf, 'Validate and Normalize Batch', [-860, 0], gatewayValidationCode()),
    booleanIfNode(wf, 'Batch Is Valid', '={{ $json.valid }}', [-620, 0]),
    respondNode(wf, 'Reject Event Batch', [-380, 260], '={{ $json.response }}', '={{ $json.http_status }}'),
    tableRowNode(wf, 'Get Existing Batch', 'get', 'dayz_events', [-380, -120], {
      conditions: [{ keyName: 'record_id', condition: 'eq', keyValue: '={{ $json.batch_row.record_id }}' }],
      returnAll: false,
      limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Classify Batch Ledger', [-140, -120], classifyBatchLedgerCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Batch Already Exists', '={{ $json.duplicate === true }}', [100, -120]),
    tableRowNode(wf, 'Persist New Batch', 'insert', 'dayz_events', [120, -160], {
      values: batchValues(),
      options: {},
    }),
    respondNode(
      wf,
      'Acknowledge Accepted Batch',
      [360, -160],
      "={{ $('Validate and Normalize Batch').item.json.response }}",
      202,
    ),
    respondNode(
      wf,
      'Acknowledge Duplicate Batch',
      [120, 80],
      "={{ {...$('Validate and Normalize Batch').item.json.response,duplicate:true} }}",
      200,
    ),
  ];
  const connections = {
    'Authenticated Event Webhook': { main: [[mainConnection('Validate and Normalize Batch')]] },
    'Validate and Normalize Batch': { main: [[mainConnection('Batch Is Valid')]] },
    'Batch Is Valid': {
      main: [[mainConnection('Get Existing Batch')], [mainConnection('Reject Event Batch')]],
    },
    'Get Existing Batch': { main: [[mainConnection('Classify Batch Ledger')]] },
    'Classify Batch Ledger': { main: [[mainConnection('Batch Already Exists')]] },
    'Batch Already Exists': {
      main: [[mainConnection('Acknowledge Duplicate Batch')], [mainConnection('Persist New Batch')]],
    },
    'Persist New Batch': { main: [[mainConnection('Acknowledge Accepted Batch')]] },
  };
  return workflow('10-event-gateway.json', wf, nodes, connections);
}

function buildBatchProcessorWorkflow() {
  const wf = 'DayZ | 15 | Durable Batch Processor';
  const nodes = [
    scheduleNode(wf, 'Every Minute', [-1160, 0], 'minutes', 1),
    tableRowNode(wf, 'Recover Stale Batch Claims', 'update', 'dayz_events', [-920, 0], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'status', condition: 'eq', keyValue: 'processing' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
      ],
      values: {
        status: 'queued',
        lease_token: '',
        last_error: 'processor_lease_recovered',
        next_attempt_at: '={{ $now.toISO() }}',
      },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Recovered Batch Claims', [-800, -80], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Expire Queued Batches', 'update', 'dayz_events', [-680, -80], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'status', condition: 'eq', keyValue: 'queued' },
        { keyName: 'expires_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
      ],
      values: {
        status: 'expired',
        payload_json: '',
        lease_token: '',
        last_error: 'batch_expired_before_processing',
        next_attempt_at: '={{ $now.toISO() }}',
      },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Expired Batches', [-560, -80], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Fail Exhausted Batches', 'update', 'dayz_events', [-440, -80], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'status', condition: 'eq', keyValue: 'queued' },
        { keyName: 'attempt_count', condition: 'gte', keyValue: POLICY.limits.batch_max_attempts },
      ],
      values: {
        status: 'failed',
        lease_token: '',
        last_error: 'batch_attempts_exhausted',
        next_attempt_at: '={{ $now.toISO() }}',
      },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Exhausted Batches', [-320, -80], 'return [{ json: { tick: true } }];'),
    codeNode(wf, 'Start Batch Scan', [-200, -80], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Get Queued Batches', 'get', 'dayz_events', [40, -80], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'status', condition: 'eq', keyValue: 'queued' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'attempt_count', condition: 'lt', keyValue: POLICY.limits.batch_max_attempts },
      ],
      returnAll: false,
      limit: 10,
      orderBy: true,
      orderByColumn: 'next_attempt_at',
      orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Filter Due Batches', [280, -80], filterDueBatchesCode()),
    tableRowNode(wf, 'Claim Queued Batch', 'update', 'dayz_events', [520, -80], {
      conditions: [
        { keyName: 'record_id', condition: 'eq', keyValue: '={{ $json.record_id }}' },
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'status', condition: 'eq', keyValue: 'queued' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'attempt_count', condition: 'lt', keyValue: POLICY.limits.batch_max_attempts },
      ],
      values: {
        status: 'processing',
        attempt_count: '={{ Number($json.attempt_count || 0) + 1 }}',
        next_attempt_at: '={{ $json.claim_until }}',
        lease_token: '={{ $json.claim_token }}',
        last_error: '',
      },
      options: {},
    }),
    codeNode(wf, 'Expand Normalized Batch', [760, -80], expandBatchEventsCode()),
    loopOverItemsNode(wf, 'Loop Event Candidates', [1000, -80]),
    tableRowNode(wf, 'Get Existing Event Row', 'get', 'dayz_events', [1240, -160], {
      conditions: [{ keyName: 'record_id', condition: 'eq', keyValue: '={{ $json.record_id }}' }],
      returnAll: false,
      limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Event Candidate and Existing', [1480, -160], combineExistingCandidateCode('Loop Event Candidates', 'record_id')),
    booleanIfNode(wf, 'Event Already Exists', '={{ $json.already_exists === true }}', [1720, -160]),
    tableRowNode(wf, 'Insert New Event Row', 'insert', 'dayz_events', [1960, -240], {
      values: eventValues('$json'),
      options: {},
    }),
    codeNode(wf, 'Build Batch Completion', [760, 0], `const now = new Date().toISOString();
const seen = new Set();
const output = [];
for (const item of $('Claim Queued Batch').all()) {
  const claim = item.json;
  if (!claim.batch_id || seen.has(claim.batch_id)) continue;
  seen.add(claim.batch_id);
  output.push({ json: {
    record_id: claim.record_id,
    batch_id: claim.batch_id,
    lease_token: claim.lease_token,
    status: 'processed',
    payload_json: '',
    next_attempt_at: now,
    last_error: '',
  } });
}
return output;`),
    tableRowNode(wf, 'Mark Batch Processed', 'update', 'dayz_events', [1000, 0], {
      conditions: [
        { keyName: 'record_id', condition: 'eq', keyValue: '={{ $json.record_id }}' },
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'lease_token', condition: 'eq', keyValue: '={{ $json.lease_token }}' },
      ],
      values: {
        status: '={{ $json.status }}',
        payload_json: '={{ $json.payload_json }}',
        next_attempt_at: '={{ $json.next_attempt_at }}',
        lease_token: '',
        last_error: '={{ $json.last_error }}',
      },
      options: {},
    }),
  ];
  const connections = {
    'Every Minute': { main: [[mainConnection('Recover Stale Batch Claims')]] },
    'Recover Stale Batch Claims': { main: [[mainConnection('Collapse Recovered Batch Claims')]] },
    'Collapse Recovered Batch Claims': { main: [[mainConnection('Expire Queued Batches')]] },
    'Expire Queued Batches': { main: [[mainConnection('Collapse Expired Batches')]] },
    'Collapse Expired Batches': { main: [[mainConnection('Fail Exhausted Batches')]] },
    'Fail Exhausted Batches': { main: [[mainConnection('Collapse Exhausted Batches')]] },
    'Collapse Exhausted Batches': { main: [[mainConnection('Start Batch Scan')]] },
    'Start Batch Scan': { main: [[mainConnection('Get Queued Batches')]] },
    'Get Queued Batches': { main: [[mainConnection('Filter Due Batches')]] },
    'Filter Due Batches': { main: [[mainConnection('Claim Queued Batch')]] },
    'Claim Queued Batch': { main: [[mainConnection('Expand Normalized Batch')]] },
    'Expand Normalized Batch': { main: [[mainConnection('Loop Event Candidates')]] },
    'Loop Event Candidates': {
      main: [[mainConnection('Build Batch Completion')], [mainConnection('Get Existing Event Row')]],
    },
    'Get Existing Event Row': { main: [[mainConnection('Combine Event Candidate and Existing')]] },
    'Combine Event Candidate and Existing': { main: [[mainConnection('Event Already Exists')]] },
    'Event Already Exists': {
      main: [[mainConnection('Loop Event Candidates')], [mainConnection('Insert New Event Row')]],
    },
    'Insert New Event Row': { main: [[mainConnection('Loop Event Candidates')]] },
    'Build Batch Completion': { main: [[mainConnection('Mark Batch Processed')]] },
  };
  return workflow('15-batch-processor.json', wf, nodes, connections);
}

function buildDigestWorkflow() {
  const wf = 'DayZ | 20 | Digest and LLM Publisher';
  const pendingConditions = [
    { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
    { keyName: 'digest_state', condition: 'eq', keyValue: 'pending' },
    { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
  ];
  const routeConditions = [{ keyName: 'server_id', condition: 'eq', keyValue: '={{ $json.server_id }}' }];
  const upsertConditions = [{ keyName: 'delivery_id', condition: 'eq', keyValue: '={{ $json.delivery_id }}' }];
  const nodes = [
    scheduleNode(wf, 'Every Minute for Delayed World Announcements', [-1080, -1340], 'minutes', 1),
    tableRowNode(wf, 'Get Pending World Announcements', 'get', 'dayz_events', [-840, -1340], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'announcement_state', condition: 'eq', keyValue: 'pending' },
        { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
      returnAll: false, limit: 100, orderBy: true, orderByColumn: 'occurred_at', orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Build Delayed World Announcements', [-600, -1340], delayedWorldAnnouncementCode()),
    loopOverItemsNode(wf, 'Loop World Announcement Deliveries', [-360, -1340]),
    tableRowNode(wf, 'Get Existing World Announcement Delivery', 'get', 'dayz_deliveries', [-120, -1420], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine World Announcement Candidate', [120, -1420], combineExistingCandidateCode('Loop World Announcement Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'World Announcement Delivery Exists', '={{ $json.already_exists === true }}', [360, -1420]),
    tableRowNode(wf, 'Insert New World Announcement Delivery', 'insert', 'dayz_deliveries', [600, -1500], {
      values: deliveryValues(), options: {},
    }),
    tableRowNode(wf, 'Mark World Announcement Queued', 'update', 'dayz_events', [840, -1340], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'announcement_state', condition: 'eq', keyValue: 'pending' },
      ],
      values: { announcement_state: 'queued' }, options: {},
    }),
    scheduleNode(wf, 'Every Minute for Safety Warnings', [-1080, -1060], 'minutes', 1),
    tableRowNode(wf, 'Get Pending Safety Warnings', 'get', 'dayz_events', [-840, -1060], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'safety_state', condition: 'eq', keyValue: 'pending' },
        { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
      returnAll: false, limit: 100, orderBy: true,
      orderByColumn: 'occurred_at', orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Build Contamination Safety Deliveries', [-600, -1060], safetyWarningDeliveryCode()),
    loopOverItemsNode(wf, 'Loop Safety Deliveries', [-360, -1060]),
    tableRowNode(wf, 'Get Existing Safety Delivery', 'get', 'dayz_deliveries', [-120, -1140], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Safety Candidate', [120, -1140], combineExistingCandidateCode('Loop Safety Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'Safety Delivery Exists', '={{ $json.already_exists === true }}', [360, -1140]),
    tableRowNode(wf, 'Insert New Safety Delivery', 'insert', 'dayz_deliveries', [600, -1220], {
      values: deliveryValues(), options: {},
    }),
    tableRowNode(wf, 'Mark Safety Event Queued', 'update', 'dayz_events', [840, -1060], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'safety_state', condition: 'eq', keyValue: 'pending' },
      ],
      values: { safety_state: 'queued' }, options: {},
    }),
    scheduleNode(wf, 'Every Minute for Player Commands', [-1080, -760], 'minutes', 1),
    tableRowNode(wf, 'Get Pending Player Commands', 'get', 'dayz_events', [-840, -760], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_type', condition: 'eq', keyValue: 'player.command' },
        { keyName: 'immediate_state', condition: 'eq', keyValue: 'command_pending' },
        { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
      returnAll: false,
      limit: 100,
      orderBy: true,
      orderByColumn: 'occurred_at',
      orderByDirection: 'ASC',
    }),
    tableRowNode(wf, 'Get Latest Snapshot for Command', 'get', 'dayz_events', [-600, -760], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'server_id', condition: 'eq', keyValue: '={{ $json.server_id }}' },
        { keyName: 'event_type', condition: 'eq', keyValue: 'world.snapshot' },
      ],
      returnAll: false,
      limit: 1,
      orderBy: true,
      orderByColumn: 'occurred_at',
      orderByDirection: 'DESC',
    }, { alwaysOutputData: true }),
    tableRowNode(wf, 'Get Command Route', 'get', 'dayz_routes', [-360, -760], {
      conditions: [{ keyName: 'server_id', condition: 'eq', keyValue: "={{ $('Get Pending Player Commands').item.json.server_id }}" }],
      returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Build Command Reply', [-120, -760], commandDeliveryCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Command Delivery Is Valid', '={{ $json.skip !== true && Boolean($json.delivery_id) }}', [120, -760]),
    loopOverItemsNode(wf, 'Loop Command Deliveries', [360, -840]),
    tableRowNode(wf, 'Get Existing Command Delivery', 'get', 'dayz_deliveries', [600, -920], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Command Candidate', [840, -920], combineExistingCandidateCode('Loop Command Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'Command Delivery Exists', '={{ $json.already_exists === true }}', [1080, -920]),
    tableRowNode(wf, 'Insert New Command Delivery', 'insert', 'dayz_deliveries', [1320, -1000], {
      values: deliveryValues(),
      options: {},
    }),
    tableRowNode(wf, 'Mark Command Event Queued', 'update', 'dayz_events', [1560, -840], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'immediate_state', condition: 'eq', keyValue: 'command_pending' },
      ],
      values: { immediate_state: 'queued' },
      options: {},
    }),
    tableRowNode(wf, 'Mark Command Event Expired', 'update', 'dayz_events', [360, -680], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'immediate_state', condition: 'eq', keyValue: 'command_pending' },
      ],
      values: { immediate_state: 'expired' },
      options: {},
    }),
    scheduleNode(wf, 'Every Minute for Immediate Events', [-1080, -420], 'minutes', 1),
    tableRowNode(wf, 'Get Pending Immediate Events', 'get', 'dayz_events', [-840, -420], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'immediate_state', condition: 'eq', keyValue: 'pending' },
        { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
      returnAll: false,
      limit: 100,
      orderBy: true,
      orderByColumn: 'occurred_at',
      orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Build Immediate Deliveries', [-600, -420], immediateDeliveryCode()),
    loopOverItemsNode(wf, 'Loop Immediate Deliveries', [-360, -420]),
    tableRowNode(wf, 'Get Existing Immediate Delivery', 'get', 'dayz_deliveries', [-120, -500], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Immediate Candidate', [120, -500], combineExistingCandidateCode('Loop Immediate Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'Immediate Delivery Exists', '={{ $json.already_exists === true }}', [360, -500]),
    tableRowNode(wf, 'Insert New Immediate Delivery', 'insert', 'dayz_deliveries', [600, -580], {
      values: deliveryValues(),
      options: {},
    }),
    tableRowNode(wf, 'Mark Immediate Event Queued', 'update', 'dayz_events', [840, -420], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'immediate_state', condition: 'eq', keyValue: 'pending' },
      ],
      values: { immediate_state: 'queued' },
      options: {},
    }),
    scheduleNode(wf, 'Every Minute for World Transitions', [-1080, 0], 'minutes', 1),
    tableRowNode(wf, 'Get Pending Public Events', 'get', 'dayz_events', [-840, 0], {
      conditions: pendingConditions,
      returnAll: false,
      limit: 500,
      orderBy: true,
      orderByColumn: 'occurred_at',
      orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Aggregate Public Digest', [-600, 0], aggregateDigestCode()),
    tableRowNode(wf, 'Get Digest Route', 'get', 'dayz_routes', [-360, 0], {
      conditions: routeConditions,
      returnAll: false,
      limit: 1,
    }),
    codeNode(wf, 'Combine Digest and Route', [-120, 0], combineDigestRouteCode()),
    booleanIfNode(wf, 'LLM Enabled', '={{ $json.publish && $json.route.mode === "active" && $json.route.enabled && $json.route.llm_enabled && $json.route.llm_base_url && !$json.route.llm_base_url.includes(".invalid") }}', [120, 0]),
    node(wf, 'OpenAI Compatible Digest Wording', 'n8n-nodes-base.httpRequest', 4.2, [360, -100], {
      method: 'POST',
      url: '={{ $json.route.llm_base_url }}',
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendBody: true,
      contentType: 'json',
      specifyBody: 'json',
      jsonBody: `={{ JSON.stringify({model:$json.route.llm_model,temperature:0.2,max_tokens:500,messages:[{role:'system',content:${JSON.stringify(DIGEST_SYSTEM_PROMPT)}},{role:'user',content:${JSON.stringify(DIGEST_USER_PROMPT)}.replace('{{PUBLIC_DIGEST_JSON}}',$json.public_digest_json)}]}) }}`,
      options: {
        timeout: 12000,
        response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } },
      },
    }, {
      credentials: credentialRef(CREDENTIALS.llm),
      onError: 'continueRegularOutput',
    }),
    codeNode(wf, 'Validate LLM or Use Fallback', [600, -100], digestWordingCode(true), { mode: 'runOnceForEachItem' }),
    codeNode(wf, 'Build Deterministic Fallback', [360, 120], digestWordingCode(false), { mode: 'runOnceForEachItem' }),
    codeNode(wf, 'Build Digest Deliveries', [840, 0], expandDigestDeliveriesCode()),
    loopOverItemsNode(wf, 'Loop Digest Deliveries', [1080, 0]),
    tableRowNode(wf, 'Get Existing Digest Delivery', 'get', 'dayz_deliveries', [1320, -80], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Digest Candidate', [1560, -80], combineExistingCandidateCode('Loop Digest Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'Digest Delivery Exists', '={{ $json.already_exists === true }}', [1800, -80]),
    tableRowNode(wf, 'Insert New Digest Delivery', 'insert', 'dayz_deliveries', [2040, -160], {
      values: deliveryValues(),
      options: {},
    }),
    codeNode(wf, 'Expand Digested Event IDs', [2280, 0], expandDigestEventIdsCode()),
    tableRowNode(wf, 'Mark Events Queued', 'update', 'dayz_events', [2520, 0], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'digest_state', condition: 'eq', keyValue: 'pending' },
      ],
      values: { digest_state: '={{ $json.digest_state }}' },
      options: {},
    }),
    scheduleNode(wf, 'Every Hour for Chronicle', [-1080, 400], 'hours', 1),
    tableRowNode(wf, 'Get Pending Chronicle Events', 'get', 'dayz_events', [-840, 400], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'chronicle_state', condition: 'eq', keyValue: 'pending' },
        { keyName: 'processing_expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
      returnAll: false, limit: 500, orderBy: true,
      orderByColumn: 'occurred_at', orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Aggregate Hourly Chronicle', [-600, 400], aggregateChronicleCode()),
    tableRowNode(wf, 'Get Chronicle Route', 'get', 'dayz_routes', [-360, 400], {
      conditions: routeConditions, returnAll: false, limit: 1,
    }),
    codeNode(wf, 'Combine Chronicle and Route', [-120, 400], combineChronicleRouteCode()),
    codeNode(wf, 'Build Chronicle Deliveries', [120, 400], expandDigestDeliveriesCode()),
    loopOverItemsNode(wf, 'Loop Chronicle Deliveries', [360, 400]),
    tableRowNode(wf, 'Get Existing Chronicle Delivery', 'get', 'dayz_deliveries', [600, 320], {
      conditions: upsertConditions, returnAll: false, limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Chronicle Candidate', [840, 320], combineExistingCandidateCode('Loop Chronicle Deliveries', 'delivery_id')),
    booleanIfNode(wf, 'Chronicle Delivery Exists', '={{ $json.already_exists === true }}', [1080, 320]),
    tableRowNode(wf, 'Insert New Chronicle Delivery', 'insert', 'dayz_deliveries', [1320, 240], {
      values: deliveryValues(), options: {},
    }),
    codeNode(wf, 'Expand Chronicled Event IDs', [1560, 400], expandChronicleEventIdsCode()),
    tableRowNode(wf, 'Mark Chronicle Events Queued', 'update', 'dayz_events', [1800, 400], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'event_id', condition: 'eq', keyValue: '={{ $json.event_id }}' },
        { keyName: 'chronicle_state', condition: 'eq', keyValue: 'pending' },
      ],
      values: { chronicle_state: '={{ $json.chronicle_state }}' }, options: {},
    }),
  ];
  const connections = {
    'Every Minute for Delayed World Announcements': { main: [[mainConnection('Get Pending World Announcements')]] },
    'Get Pending World Announcements': { main: [[mainConnection('Build Delayed World Announcements')]] },
    'Build Delayed World Announcements': { main: [[mainConnection('Loop World Announcement Deliveries')]] },
    'Loop World Announcement Deliveries': {
      main: [[mainConnection('Mark World Announcement Queued')], [mainConnection('Get Existing World Announcement Delivery')]],
    },
    'Get Existing World Announcement Delivery': { main: [[mainConnection('Combine World Announcement Candidate')]] },
    'Combine World Announcement Candidate': { main: [[mainConnection('World Announcement Delivery Exists')]] },
    'World Announcement Delivery Exists': {
      main: [[mainConnection('Loop World Announcement Deliveries')], [mainConnection('Insert New World Announcement Delivery')]],
    },
    'Insert New World Announcement Delivery': { main: [[mainConnection('Loop World Announcement Deliveries')]] },
    'Every Minute for Safety Warnings': { main: [[mainConnection('Get Pending Safety Warnings')]] },
    'Get Pending Safety Warnings': { main: [[mainConnection('Build Contamination Safety Deliveries')]] },
    'Build Contamination Safety Deliveries': { main: [[mainConnection('Loop Safety Deliveries')]] },
    'Loop Safety Deliveries': {
      main: [[mainConnection('Mark Safety Event Queued')], [mainConnection('Get Existing Safety Delivery')]],
    },
    'Get Existing Safety Delivery': { main: [[mainConnection('Combine Safety Candidate')]] },
    'Combine Safety Candidate': { main: [[mainConnection('Safety Delivery Exists')]] },
    'Safety Delivery Exists': {
      main: [[mainConnection('Loop Safety Deliveries')], [mainConnection('Insert New Safety Delivery')]],
    },
    'Insert New Safety Delivery': { main: [[mainConnection('Loop Safety Deliveries')]] },
    'Every Minute for Player Commands': { main: [[mainConnection('Get Pending Player Commands')]] },
    'Get Pending Player Commands': { main: [[mainConnection('Get Latest Snapshot for Command')]] },
    'Get Latest Snapshot for Command': { main: [[mainConnection('Get Command Route')]] },
    'Get Command Route': { main: [[mainConnection('Build Command Reply')]] },
    'Build Command Reply': { main: [[mainConnection('Command Delivery Is Valid')]] },
    'Command Delivery Is Valid': {
      main: [[mainConnection('Loop Command Deliveries')], [mainConnection('Mark Command Event Expired')]],
    },
    'Loop Command Deliveries': {
      main: [[mainConnection('Mark Command Event Queued')], [mainConnection('Get Existing Command Delivery')]],
    },
    'Get Existing Command Delivery': { main: [[mainConnection('Combine Command Candidate')]] },
    'Combine Command Candidate': { main: [[mainConnection('Command Delivery Exists')]] },
    'Command Delivery Exists': {
      main: [[mainConnection('Loop Command Deliveries')], [mainConnection('Insert New Command Delivery')]],
    },
    'Insert New Command Delivery': { main: [[mainConnection('Loop Command Deliveries')]] },
    'Every Minute for Immediate Events': { main: [[mainConnection('Get Pending Immediate Events')]] },
    'Get Pending Immediate Events': { main: [[mainConnection('Build Immediate Deliveries')]] },
    'Build Immediate Deliveries': { main: [[mainConnection('Loop Immediate Deliveries')]] },
    'Loop Immediate Deliveries': {
      main: [[mainConnection('Mark Immediate Event Queued')], [mainConnection('Get Existing Immediate Delivery')]],
    },
    'Get Existing Immediate Delivery': { main: [[mainConnection('Combine Immediate Candidate')]] },
    'Combine Immediate Candidate': { main: [[mainConnection('Immediate Delivery Exists')]] },
    'Immediate Delivery Exists': {
      main: [[mainConnection('Loop Immediate Deliveries')], [mainConnection('Insert New Immediate Delivery')]],
    },
    'Insert New Immediate Delivery': { main: [[mainConnection('Loop Immediate Deliveries')]] },
    'Every Minute for World Transitions': { main: [[mainConnection('Get Pending Public Events')]] },
    'Get Pending Public Events': { main: [[mainConnection('Aggregate Public Digest')]] },
    'Aggregate Public Digest': { main: [[mainConnection('Get Digest Route')]] },
    'Get Digest Route': { main: [[mainConnection('Combine Digest and Route')]] },
    'Combine Digest and Route': { main: [[mainConnection('LLM Enabled')]] },
    'LLM Enabled': {
      main: [[mainConnection('OpenAI Compatible Digest Wording')], [mainConnection('Build Deterministic Fallback')]],
    },
    'OpenAI Compatible Digest Wording': { main: [[mainConnection('Validate LLM or Use Fallback')]] },
    'Validate LLM or Use Fallback': { main: [[mainConnection('Build Digest Deliveries')]] },
    'Build Deterministic Fallback': { main: [[mainConnection('Build Digest Deliveries')]] },
    'Build Digest Deliveries': { main: [[mainConnection('Loop Digest Deliveries')]] },
    'Loop Digest Deliveries': {
      main: [[mainConnection('Expand Digested Event IDs')], [mainConnection('Get Existing Digest Delivery')]],
    },
    'Get Existing Digest Delivery': { main: [[mainConnection('Combine Digest Candidate')]] },
    'Combine Digest Candidate': { main: [[mainConnection('Digest Delivery Exists')]] },
    'Digest Delivery Exists': {
      main: [[mainConnection('Loop Digest Deliveries')], [mainConnection('Insert New Digest Delivery')]],
    },
    'Insert New Digest Delivery': { main: [[mainConnection('Loop Digest Deliveries')]] },
    'Expand Digested Event IDs': { main: [[mainConnection('Mark Events Queued')]] },
    'Every Hour for Chronicle': { main: [[mainConnection('Get Pending Chronicle Events')]] },
    'Get Pending Chronicle Events': { main: [[mainConnection('Aggregate Hourly Chronicle')]] },
    'Aggregate Hourly Chronicle': { main: [[mainConnection('Get Chronicle Route')]] },
    'Get Chronicle Route': { main: [[mainConnection('Combine Chronicle and Route')]] },
    'Combine Chronicle and Route': { main: [[mainConnection('Build Chronicle Deliveries')]] },
    'Build Chronicle Deliveries': { main: [[mainConnection('Loop Chronicle Deliveries')]] },
    'Loop Chronicle Deliveries': {
      main: [[mainConnection('Expand Chronicled Event IDs')], [mainConnection('Get Existing Chronicle Delivery')]],
    },
    'Get Existing Chronicle Delivery': { main: [[mainConnection('Combine Chronicle Candidate')]] },
    'Combine Chronicle Candidate': { main: [[mainConnection('Chronicle Delivery Exists')]] },
    'Chronicle Delivery Exists': {
      main: [[mainConnection('Loop Chronicle Deliveries')], [mainConnection('Insert New Chronicle Delivery')]],
    },
    'Insert New Chronicle Delivery': { main: [[mainConnection('Loop Chronicle Deliveries')]] },
    'Expand Chronicled Event IDs': { main: [[mainConnection('Mark Chronicle Events Queued')]] },
  };
  return workflow('20-digest-llm-publisher.json', wf, nodes, connections);
}

function deliveryUpdateValues() {
  return {
    status: '={{ $json.status }}',
    attempt_count: '={{ $json.attempt_count }}',
    next_attempt_at: '={{ $json.next_attempt_at }}',
    last_error: '={{ $json.last_error }}',
    sent_at: '={{ $json.sent_at }}',
    message: '={{ $json.message }}',
    signal_status_url: '={{ $json.signal_status_url || "" }}',
    lease_token: '',
  };
}

function buildRetryWorkflow() {
  const wf = 'DayZ | 30 | Delivery Retry and Cleanup';
  const routeCondition = [{ keyName: 'server_id', condition: 'eq', keyValue: '={{ $json.server_id }}' }];
  const nodes = [
    scheduleNode(wf, 'Every Minute', [-1320, -180], 'minutes', POLICY.limits.delivery_scan_interval_seconds / 60),
    tableRowNode(wf, 'Recover Stale Telegram Claims', 'update', 'dayz_deliveries', [-1080, -300], {
      conditions: [
        { keyName: 'status', condition: 'eq', keyValue: 'sending' },
        { keyName: 'channel', condition: 'eq', keyValue: 'telegram' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
      ],
      values: {
        status: 'delivery_unknown',
        message: '',
        lease_token: '',
        last_error: 'telegram_ambiguous_after_lease',
        next_attempt_at: '={{ $now.toISO() }}',
      },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Recovered Telegram Claims', [-960, -300], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Recover Stale Game Claims', 'update', 'dayz_deliveries', [-840, -300], {
      conditions: [
        { keyName: 'status', condition: 'eq', keyValue: 'sending' },
        { keyName: 'channel', condition: 'eq', keyValue: 'game' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
      ],
      values: {
        status: 'pending',
        lease_token: '',
        last_error: 'signal_lease_reconcile',
        next_attempt_at: '={{ $now.toISO() }}',
      },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Recovered Game Claims', [-720, -300], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Expire Pending Deliveries', 'update', 'dayz_deliveries', [-600, -300], {
      conditions: [
        { keyName: 'status', condition: 'eq', keyValue: 'pending' },
        { keyName: 'expires_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
      ],
      values: { status: 'expired', message: '', lease_token: '', last_error: 'delivery_expired_before_send', next_attempt_at: '={{ $now.toISO() }}' },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Expired Deliveries', [-480, -300], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Fail Exhausted Deliveries', 'update', 'dayz_deliveries', [-360, -300], {
      conditions: [
        { keyName: 'status', condition: 'eq', keyValue: 'pending' },
        { keyName: 'attempt_count', condition: 'gte', keyValue: POLICY.limits.delivery_max_attempts },
      ],
      values: { status: 'failed', message: '', lease_token: '', last_error: 'delivery_attempts_exhausted', next_attempt_at: '={{ $now.toISO() }}' },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Exhausted Deliveries', [-240, -300], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Purge Expired Delivery Messages', 'update', 'dayz_deliveries', [-120, -300], {
      conditions: [{ keyName: 'expires_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' }],
      values: { message: '' },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Purged Delivery Messages', [0, -300], 'return [{ json: { tick: true } }];'),
    codeNode(wf, 'Start Delivery Scan', [120, -300], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Get Pending Deliveries', 'get', 'dayz_deliveries', [360, -300], {
      conditions: [
        { keyName: 'status', condition: 'eq', keyValue: 'pending' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'attempt_count', condition: 'lt', keyValue: POLICY.limits.delivery_max_attempts },
      ],
      returnAll: false,
      limit: 1,
      orderBy: true,
      orderByColumn: 'next_attempt_at',
      orderByDirection: 'ASC',
    }),
    codeNode(wf, 'Filter Due Deliveries', [120, -180], filterDueDeliveriesCode()),
    tableRowNode(wf, 'Claim Due Delivery', 'update', 'dayz_deliveries', [360, -180], {
      conditions: [
        { keyName: 'delivery_id', condition: 'eq', keyValue: '={{ $json.delivery_id }}' },
        { keyName: 'status', condition: 'eq', keyValue: 'pending' },
        { keyName: 'next_attempt_at', condition: 'lte', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'expires_at', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
        { keyName: 'attempt_count', condition: 'lt', keyValue: POLICY.limits.delivery_max_attempts },
      ],
      values: {
        status: 'sending',
        next_attempt_at: '={{ $json.claim_until }}',
        lease_token: '={{ $json.claim_token }}',
        claimed_at: '={{ $json.claimed_at }}',
      },
      options: {},
    }),
    tableRowNode(wf, 'Get Delivery Route', 'get', 'dayz_routes', [600, -180], {
      conditions: routeCondition,
      returnAll: false,
      limit: 1,
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Combine Delivery and Route', [840, -180], combineDeliveryRouteCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Route Is Deliverable', '={{ $json.deliverable }}', [1080, -180]),
    codeNode(wf, 'Finalize Suppressed Delivery', [1320, 40], finalizeSuppressedCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Telegram Channel', '={{ $json.channel === "telegram" }}', [1320, -300]),
    node(wf, 'Send Telegram', 'n8n-nodes-base.telegram', 1.2, [1560, -420], {
      resource: 'message',
      operation: 'sendMessage',
      chatId: '={{ $json.route.telegram_chat_id }}',
      text: '={{ $json.telegram_text_html }}',
      additionalFields: { appendAttribution: false, parse_mode: 'HTML' },
    }, {
      credentials: credentialRef(CREDENTIALS.telegram),
      onError: 'continueErrorOutput',
    }),
    codeNode(wf, 'Telegram Sent', [1800, -480], telegramSuccessCode(), { mode: 'runOnceForEachItem' }),
    codeNode(wf, 'Telegram Failed', [1800, -360], telegramFailureCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Signal Reconciliation Required', '={{ Boolean($json.signal_status_url) || $json.last_error === "signal_lease_reconcile" }}', [1560, -260]),
    tableRowNode(wf, 'Game Cooldown Is Free', 'rowNotExists', 'dayz_cooldowns', [1560, -180], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.cooldown_scope }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Game Cooldown Is Active', 'rowExists', 'dayz_cooldowns', [1560, -60], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.cooldown_scope }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    booleanIfNode(wf, 'Interactive Game Delivery', '={{ Boolean($json.secondary_cooldown_scope) }}', [1800, -180]),
    tableRowNode(wf, 'Command Global Cooldown Is Free', 'rowNotExists', 'dayz_cooldowns', [2040, -300], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.secondary_cooldown_scope }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Command Global Cooldown Is Active', 'rowExists', 'dayz_cooldowns', [2040, -220], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.secondary_cooldown_scope }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Auto Rate Slot 0 Is Free', 'rowNotExists', 'dayz_cooldowns', [2040, -100], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "0" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Auto Rate Slot 0 Is Active', 'rowExists', 'dayz_cooldowns', [2040, -20], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "0" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    codeNode(wf, 'Assign Auto Rate Slot 0', [2280, -100], assignRateSlotCode(0), { mode: 'runOnceForEachItem' }),
    tableRowNode(wf, 'Auto Rate Slot 1 Is Free', 'rowNotExists', 'dayz_cooldowns', [2280, -20], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "1" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Auto Rate Slot 1 Is Active', 'rowExists', 'dayz_cooldowns', [2280, 60], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "1" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    codeNode(wf, 'Assign Auto Rate Slot 1', [2520, -20], assignRateSlotCode(1), { mode: 'runOnceForEachItem' }),
    tableRowNode(wf, 'Auto Rate Slot 2 Is Free', 'rowNotExists', 'dayz_cooldowns', [2520, 60], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "2" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    tableRowNode(wf, 'Auto Rate Slot 2 Is Active', 'rowExists', 'dayz_cooldowns', [2520, 140], {
      conditions: [
        { keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.rate_scope_prefix + "2" }}' },
        { keyName: 'until', condition: 'gt', keyValue: '={{ $now.toISO() }}' },
      ],
    }),
    codeNode(wf, 'Assign Auto Rate Slot 2', [2760, 60], assignRateSlotCode(2), { mode: 'runOnceForEachItem' }),
    codeNode(wf, 'Defer Game for Cooldown', [1800, -60], deferForCooldownCode(), { mode: 'runOnceForEachItem' }),
    codeNode(wf, 'Build Signal Command', [1800, -180], signalCommandCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Signal Attempt Has Recovery Window', '={{ $json.signal_attempt_allowed === true }}', [2040, -180]),
    codeNode(wf, 'Expire Unsafe Signal Submission', [2280, 0], expireUnsafeSignalSubmissionCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Signal Status Poll', '={{ $json.signal_method === "GET" }}', [2040, -180]),
    node(wf, 'Send Signal v1', 'n8n-nodes-base.httpRequest', 4.2, [2280, -240], {
      method: 'POST',
      url: '={{ $json.signal_url }}',
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      sendHeaders: true,
      headerParameters: {
        parameters: [{ name: 'Idempotency-Key', value: '={{ $json.command_id }}' }],
      },
      sendBody: true,
      contentType: 'json',
      specifyBody: 'json',
      jsonBody: '={{ JSON.stringify($json.command) }}',
      options: {
        timeout: 10000,
        response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } },
      },
    }, {
      credentials: credentialRef(CREDENTIALS.signal),
      onError: 'continueRegularOutput',
    }),
    node(wf, 'Get Signal v1 Status', 'n8n-nodes-base.httpRequest', 4.2, [2280, -120], {
      method: 'GET',
      url: '={{ $json.signal_url }}',
      authentication: 'genericCredentialType',
      genericAuthType: 'httpHeaderAuth',
      options: {
        timeout: 10000,
        response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } },
      },
    }, {
      credentials: credentialRef(CREDENTIALS.signal),
      onError: 'continueRegularOutput',
    }),
    codeNode(wf, 'Classify Signal Result', [2520, -180], signalResultCode(), { mode: 'runOnceForEachItem' }),
    booleanIfNode(wf, 'Set Game Cooldown', '={{ $json.set_cooldown }}', [2520, -180]),
    codeNode(wf, 'Build Cooldown Rows', [2760, -280], cooldownRowCode()),
    tableRowNode(wf, 'Upsert Game Cooldown', 'upsert', 'dayz_cooldowns', [3000, -280], {
      conditions: [{ keyName: 'scope_key', condition: 'eq', keyValue: '={{ $json.scope_key }}' }],
      values: {
        scope_key: '={{ $json.scope_key }}',
        until: '={{ $json.until }}',
        last_delivery_id: '={{ $json.last_delivery_id }}',
        updated_at: '={{ $json.updated_at }}',
      },
      options: {},
    }),
    codeNode(wf, 'Restore Signal Delivery Update', [3240, -280], restoreSignalUpdateCode()),
    tableRowNode(wf, 'Update Delivery State', 'update', 'dayz_deliveries', [3480, -120], {
      conditions: [
        { keyName: 'delivery_id', condition: 'eq', keyValue: '={{ $json.delivery_id }}' },
        { keyName: 'lease_token', condition: 'eq', keyValue: '={{ $json.lease_token }}' },
      ],
      values: deliveryUpdateValues(),
      options: {},
    }),
    scheduleNode(wf, 'Hourly Cleanup', [-1320, 360], 'hours', 1),
    tableRowNode(wf, 'Clear Expired Batch Payloads', 'update', 'dayz_events', [-1080, 360], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'private_expires_at', condition: 'lt', keyValue: '={{ $now.toISO() }}' },
      ],
      values: { payload_json: '' },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Cleared Batch Payloads', [-960, 440], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Delete Expired Batches', 'deleteRows', 'dayz_events', [-840, 360], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'batch' },
        { keyName: 'expires_at', condition: 'lt', keyValue: '={{ $now.toISO() }}' },
      ],
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Deleted Batches', [-720, 440], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Clear Expired Private Payloads', 'update', 'dayz_events', [-600, 360], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'private_expires_at', condition: 'lt', keyValue: '={{ $now.toISO() }}' },
      ],
      values: { admin_json: '' },
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Cleared Private Payloads', [-480, 440], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Delete Expired Events', 'deleteRows', 'dayz_events', [-360, 360], {
      conditions: [
        { keyName: 'record_kind', condition: 'eq', keyValue: 'event' },
        { keyName: 'expires_at', condition: 'lt', keyValue: '={{ $now.toISO() }}' },
      ],
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Deleted Events', [-240, 440], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Delete Expired Deliveries', 'deleteRows', 'dayz_deliveries', [-120, 360], {
      conditions: [{ keyName: 'delete_after', condition: 'lt', keyValue: '={{ $now.toISO() }}' }],
      options: {},
    }, { alwaysOutputData: true }),
    codeNode(wf, 'Collapse Deleted Deliveries', [0, 440], 'return [{ json: { tick: true } }];'),
    tableRowNode(wf, 'Delete Expired Cooldowns', 'deleteRows', 'dayz_cooldowns', [120, 360], {
      conditions: [{ keyName: 'until', condition: 'lt', keyValue: '={{ $now.toISO() }}' }],
      options: {},
    }, { alwaysOutputData: true }),
  ];
  const connections = {
    'Every Minute': { main: [[mainConnection('Recover Stale Telegram Claims')]] },
    'Recover Stale Telegram Claims': { main: [[mainConnection('Collapse Recovered Telegram Claims')]] },
    'Collapse Recovered Telegram Claims': { main: [[mainConnection('Recover Stale Game Claims')]] },
    'Recover Stale Game Claims': { main: [[mainConnection('Collapse Recovered Game Claims')]] },
    'Collapse Recovered Game Claims': { main: [[mainConnection('Expire Pending Deliveries')]] },
    'Expire Pending Deliveries': { main: [[mainConnection('Collapse Expired Deliveries')]] },
    'Collapse Expired Deliveries': { main: [[mainConnection('Fail Exhausted Deliveries')]] },
    'Fail Exhausted Deliveries': { main: [[mainConnection('Collapse Exhausted Deliveries')]] },
    'Collapse Exhausted Deliveries': { main: [[mainConnection('Purge Expired Delivery Messages')]] },
    'Purge Expired Delivery Messages': { main: [[mainConnection('Collapse Purged Delivery Messages')]] },
    'Collapse Purged Delivery Messages': { main: [[mainConnection('Start Delivery Scan')]] },
    'Start Delivery Scan': { main: [[mainConnection('Get Pending Deliveries')]] },
    'Get Pending Deliveries': { main: [[mainConnection('Filter Due Deliveries')]] },
    'Filter Due Deliveries': { main: [[mainConnection('Claim Due Delivery')]] },
    'Claim Due Delivery': { main: [[mainConnection('Get Delivery Route')]] },
    'Get Delivery Route': { main: [[mainConnection('Combine Delivery and Route')]] },
    'Combine Delivery and Route': { main: [[mainConnection('Route Is Deliverable')]] },
    'Route Is Deliverable': {
      main: [[mainConnection('Telegram Channel')], [mainConnection('Finalize Suppressed Delivery')]],
    },
    'Finalize Suppressed Delivery': { main: [[mainConnection('Update Delivery State')]] },
    'Telegram Channel': {
      main: [[mainConnection('Send Telegram')], [mainConnection('Signal Reconciliation Required')]],
    },
    'Send Telegram': {
      main: [[mainConnection('Telegram Sent')], [mainConnection('Telegram Failed')]],
    },
    'Telegram Sent': { main: [[mainConnection('Update Delivery State')]] },
    'Telegram Failed': { main: [[mainConnection('Update Delivery State')]] },
    'Signal Reconciliation Required': {
      main: [[mainConnection('Build Signal Command')], [mainConnection('Game Cooldown Is Free'), mainConnection('Game Cooldown Is Active')]],
    },
    'Game Cooldown Is Free': { main: [[mainConnection('Interactive Game Delivery')]] },
    'Game Cooldown Is Active': { main: [[mainConnection('Defer Game for Cooldown')]] },
    'Interactive Game Delivery': {
      main: [[mainConnection('Command Global Cooldown Is Free'), mainConnection('Command Global Cooldown Is Active')], [mainConnection('Auto Rate Slot 0 Is Free'), mainConnection('Auto Rate Slot 0 Is Active')]],
    },
    'Command Global Cooldown Is Free': { main: [[mainConnection('Build Signal Command')]] },
    'Command Global Cooldown Is Active': { main: [[mainConnection('Defer Game for Cooldown')]] },
    'Auto Rate Slot 0 Is Free': { main: [[mainConnection('Assign Auto Rate Slot 0')]] },
    'Auto Rate Slot 0 Is Active': { main: [[mainConnection('Auto Rate Slot 1 Is Free'), mainConnection('Auto Rate Slot 1 Is Active')]] },
    'Assign Auto Rate Slot 0': { main: [[mainConnection('Build Signal Command')]] },
    'Auto Rate Slot 1 Is Free': { main: [[mainConnection('Assign Auto Rate Slot 1')]] },
    'Auto Rate Slot 1 Is Active': { main: [[mainConnection('Auto Rate Slot 2 Is Free'), mainConnection('Auto Rate Slot 2 Is Active')]] },
    'Assign Auto Rate Slot 1': { main: [[mainConnection('Build Signal Command')]] },
    'Auto Rate Slot 2 Is Free': { main: [[mainConnection('Assign Auto Rate Slot 2')]] },
    'Auto Rate Slot 2 Is Active': { main: [[mainConnection('Defer Game for Cooldown')]] },
    'Assign Auto Rate Slot 2': { main: [[mainConnection('Build Signal Command')]] },
    'Defer Game for Cooldown': { main: [[mainConnection('Update Delivery State')]] },
    'Build Signal Command': { main: [[mainConnection('Signal Attempt Has Recovery Window')]] },
    'Signal Attempt Has Recovery Window': {
      main: [[mainConnection('Signal Status Poll')], [mainConnection('Expire Unsafe Signal Submission')]],
    },
    'Expire Unsafe Signal Submission': { main: [[mainConnection('Update Delivery State')]] },
    'Signal Status Poll': { main: [[mainConnection('Get Signal v1 Status')], [mainConnection('Send Signal v1')]] },
    'Send Signal v1': { main: [[mainConnection('Classify Signal Result')]] },
    'Get Signal v1 Status': { main: [[mainConnection('Classify Signal Result')]] },
    'Classify Signal Result': { main: [[mainConnection('Set Game Cooldown')]] },
    'Set Game Cooldown': {
      main: [[mainConnection('Build Cooldown Rows')], [mainConnection('Update Delivery State')]],
    },
    'Build Cooldown Rows': { main: [[mainConnection('Upsert Game Cooldown')]] },
    'Upsert Game Cooldown': { main: [[mainConnection('Restore Signal Delivery Update')]] },
    'Restore Signal Delivery Update': { main: [[mainConnection('Update Delivery State')]] },
    'Hourly Cleanup': { main: [[mainConnection('Clear Expired Batch Payloads')]] },
    'Clear Expired Batch Payloads': { main: [[mainConnection('Collapse Cleared Batch Payloads')]] },
    'Collapse Cleared Batch Payloads': { main: [[mainConnection('Delete Expired Batches')]] },
    'Delete Expired Batches': { main: [[mainConnection('Collapse Deleted Batches')]] },
    'Collapse Deleted Batches': { main: [[mainConnection('Clear Expired Private Payloads')]] },
    'Clear Expired Private Payloads': { main: [[mainConnection('Collapse Cleared Private Payloads')]] },
    'Collapse Cleared Private Payloads': { main: [[mainConnection('Delete Expired Events')]] },
    'Delete Expired Events': { main: [[mainConnection('Collapse Deleted Events')]] },
    'Collapse Deleted Events': { main: [[mainConnection('Delete Expired Deliveries')]] },
    'Delete Expired Deliveries': { main: [[mainConnection('Collapse Deleted Deliveries')]] },
    'Collapse Deleted Deliveries': { main: [[mainConnection('Delete Expired Cooldowns')]] },
  };
  return workflow('30-delivery-retry-cleanup.json', wf, nodes, connections);
}

function buildErrorWorkflow() {
  const wf = 'DayZ | 90 | Sanitized Error Alert';
  const nodes = [
    node(wf, 'Workflow Error Trigger', 'n8n-nodes-base.errorTrigger', 1, [-700, 0], {}),
    codeNode(wf, 'Build Sanitized Alert', [-460, 0], `const source = $json || {};
const workflow = source.workflow || {};
const execution = source.execution || {};
const error = execution.error || source.error || {};
const workflowNames = new Set([
  'DayZ | 00 | Bootstrap Data Tables',
  'DayZ | 10 | Event Gateway',
  'DayZ | 15 | Durable Batch Processor',
  'DayZ | 20 | Digest and LLM Publisher',
  'DayZ | 30 | Delivery Retry and Cleanup',
  'DayZ | 90 | Sanitized Error Alert',
]);
const errorClasses = new Set(['Error','ApplicationError','ExecutionBaseError','NodeApiError','NodeOperationError','WorkflowOperationError']);
const workflowName = workflowNames.has(String(workflow.name || '')) ? String(workflow.name) : 'unknown_workflow';
const executionIdRaw = String(execution.id || '');
const executionId = /^[A-Za-z0-9_-]{1,80}$/.test(executionIdRaw) ? executionIdRaw : 'unknown';
const classRaw = String(error.name || 'Error');
const errorClass = errorClasses.has(classRaw) ? classRaw : 'Error';
const rawMessage = String(error.message || error.description || '').slice(0,8192);
const folded = rawMessage.toLowerCase();
let category = 'internal';
if (/timeout|timed out/.test(folded)) category = 'timeout';
else if (/\\b429\\b|rate limit|too many requests/.test(folded)) category = 'rate_limited';
else if (/\\b401\\b|\\b403\\b|unauthori[sz]ed|forbidden|credential|authentication|bearer/.test(folded)) category = 'authentication';
else if (/econn|network|socket|dns|connection/.test(folded)) category = 'network';
else if (/schema|validation|invalid|malformed/.test(folded)) category = 'validation';
else if (/data table|database|sqlite|storage/.test(folded)) category = 'storage';
const fingerprintMaterial = [classRaw, rawMessage, String(error.code || ''), String(execution.lastNodeExecuted || '')].join('\\u001f');
let hash = 2166136261;
for (let index=0; index<fingerprintMaterial.length; index+=1) { hash ^= fingerprintMaterial.charCodeAt(index); hash = Math.imul(hash,16777619); }
const fingerprint = 'err_' + (hash>>>0).toString(16).padStart(8,'0');
const message = [
  '[DayZ automation] Ошибка workflow.',
  'Workflow: ' + workflowName,
  'Execution: ' + executionId,
  'Категория: ' + category,
  'Класс: ' + errorClass,
  'Fingerprint: ' + fingerprint,
].join('\\n').slice(0,3500);
const messageHtml = message
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');
return [{ json: { message, message_html: messageHtml } }];`),
    tableRowNode(wf, 'Get Operations Alert Route', 'get', 'dayz_routes', [-220, 0], {
      conditions: [
        { keyName: 'enabled', condition: 'isTrue' },
        { keyName: 'telegram_enabled', condition: 'isTrue' },
        { keyName: 'ops_alerts', condition: 'isTrue' },
      ],
      returnAll: false,
      limit: 1,
    }),
    node(wf, 'Send Sanitized Error to Telegram', 'n8n-nodes-base.telegram', 1.2, [40, 0], {
      resource: 'message',
      operation: 'sendMessage',
      chatId: '={{ $json.telegram_chat_id }}',
      text: "={{ $('Build Sanitized Alert').item.json.message_html }}",
      additionalFields: { appendAttribution: false, parse_mode: 'HTML' },
    }, {
      credentials: credentialRef(CREDENTIALS.telegram),
      onError: 'continueRegularOutput',
    }),
  ];
  const connections = {
    'Workflow Error Trigger': { main: [[mainConnection('Build Sanitized Alert')]] },
    'Build Sanitized Alert': { main: [[mainConnection('Get Operations Alert Route')]] },
    'Get Operations Alert Route': { main: [[mainConnection('Send Sanitized Error to Telegram')]] },
  };
  return workflow('90-error-alert.json', wf, nodes, connections);
}

function writeWorkflow(fileName, data) {
  const output = `${JSON.stringify(data, null, 2)}\n`;
  fs.writeFileSync(path.join(WORKFLOWS_DIR, fileName), output, 'utf8');
}

function buildAll() {
  fs.mkdirSync(WORKFLOWS_DIR, { recursive: true });
  const outputs = {
    '00-bootstrap-data-tables.json': buildBootstrapWorkflow(),
    '10-event-gateway.json': buildGatewayWorkflow(),
    '15-batch-processor.json': buildBatchProcessorWorkflow(),
    '20-digest-llm-publisher.json': buildDigestWorkflow(),
    '30-delivery-retry-cleanup.json': buildRetryWorkflow(),
    '90-error-alert.json': buildErrorWorkflow(),
  };
  for (const [fileName, data] of Object.entries(outputs)) writeWorkflow(fileName, data);
  return outputs;
}

if (require.main === module) {
  const outputs = buildAll();
  process.stdout.write(`Generated ${Object.keys(outputs).length} workflows in ${WORKFLOWS_DIR}\n`);
}

module.exports = {
  CREDENTIALS,
  POLICY,
  TABLES,
  buildAll,
  gatewayValidationCode,
  immediateDeliveryCode,
};
