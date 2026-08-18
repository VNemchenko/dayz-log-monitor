"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const dgram = require("node:dgram");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const util = require("node:util");
const vm = require("node:vm");


const execFile = util.promisify(childProcess.execFile);
const ROOT = path.resolve(__dirname, "..");
const PRIVATE_UID = "uid-e2e-private-987654";
const PRIVATE_NAME = "E2E_Private_Player";
const PRIVATE_COORDINATES = ["4250.5", "8100.25", "042-081"];
const EVENT_WEBHOOK_KEY = `e2e-event-key-${"k".repeat(32)}`;
const SIGNAL_API_KEY = `e2e-signal-key-${"a".repeat(32)}`;
const MOCK_LLM_MESSAGE = "На Ливонии начался дождь и наступила ночь. Соблюдайте осторожность.";


function workflow(file) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, "n8n", "workflows", file), "utf8"));
}


function workflowNode(file, name) {
  const node = workflow(file).nodes.find((candidate) => candidate.name === name);
  assert.ok(node, `${file}: node not found: ${name}`);
  return node;
}


async function runCode(file, name, context) {
  const code = workflowNode(file, name).parameters.jsCode;
  return vm.runInNewContext(`(async () => {${code}\n})()`, context, {
    filename: `${file}:${name}`,
    timeout: 3000,
  });
}


function waitFor(predicate, timeoutMs = 5000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const check = async () => {
      try {
        const value = await predicate();
        if (value) {
          resolve(value);
          return;
        }
      } catch (_error) {
        // A local endpoint may not be listening yet.
      }
      if (Date.now() - started >= timeoutMs) {
        reject(new Error("local E2E condition timed out"));
      } else {
        setTimeout(check, 20);
      }
    };
    void check();
  });
}


async function freeTcpPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}


async function closeServer(server) {
  if (!server || !server.listening) return;
  await new Promise((resolve) => server.close(resolve));
}


async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let body = {};
  if (text) body = JSON.parse(text);
  return { status: response.status, body, headers: response.headers };
}


function assertPrivateDataAbsent(value, label) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  for (const token of [PRIVATE_UID, PRIVATE_NAME, ...PRIVATE_COORDINATES]) {
    assert.ok(!serialized.includes(token), `${label} leaked private token ${token}`);
  }
  assert.ok(!/\b[xyz]\s*[=:]\s*-?\d/i.test(serialized), `${label} leaked exact coordinates`);
}


function parseArgs(argv) {
  let signalRoot = process.env.DAYZ_SIGNAL_ROOT
    ? path.resolve(process.env.DAYZ_SIGNAL_ROOT)
    : path.resolve(ROOT, "..", "dayz-signal");
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--signal-root" && argv[index + 1]) {
      signalRoot = path.resolve(argv[index + 1]);
      index += 1;
    } else {
      throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  return { signalRoot };
}


async function signalChildMain() {
  const signalRoot = path.resolve(process.env.DAYZ_SIGNAL_ROOT || "");
  const { start } = require(path.join(signalRoot, "src", "server.js"));
  const service = await start();
  if (process.send) process.send({ type: "ready" });
  let stopping = false;
  process.on("message", (message) => {
    if (!message || message.type !== "shutdown" || stopping) return;
    stopping = true;
    void service.shutdown("local_e2e").then(() => {
      if (process.send) process.send({ type: "stopped" });
      process.exit(process.exitCode || 0);
    });
  });
}


async function startSignalProcess(signalRoot, tempDirectory, rconPort, httpPort) {
  for (const relative of ["src/server.js", "src/rconClient.js", "src/transliterate.js"] ) {
    assert.ok(fs.existsSync(path.join(signalRoot, relative)), `dayz-signal file missing: ${relative}`);
  }
  const journalPath = path.join(tempDirectory, "broadcasts.jsonl");
  const child = childProcess.fork(__filename, ["--signal-child"], {
    cwd: signalRoot,
    env: {
      ...process.env,
      DAYZ_SIGNAL_ROOT: signalRoot,
      SERVICE_NAME: "dayz-signal-e2e",
      SERVER_ID: "livonia-1",
      HTTP_HOST: "127.0.0.1",
      HTTP_PORT: String(httpPort),
      HTTP_BODY_LIMIT_BYTES: "4096",
      HTTP_WAIT_MS: "1000",
      API_KEY: SIGNAL_API_KEY,
      RCON_HOST: "127.0.0.1",
      RCON_PORT: String(rconPort),
      RCON_PASSWORD: "e2e-rcon-password",
      RCON_CONNECTION_TYPE: "udp4",
      RCON_CONNECTION_TIMEOUT_MS: "1000",
      RCON_CONNECTION_INTERVAL_MS: "100",
      RCON_KEEPALIVE_MS: "1000",
      RCON_COMMAND_TIMEOUT_MS: "500",
      RCON_RECONNECT_BASE_MS: "50",
      RCON_RECONNECT_MAX_MS: "100",
      JOURNAL_PATH: journalPath,
      QUEUE_CAPACITY: "50",
      BROADCAST_TTL_SECONDS: "30",
      BROADCAST_MAX_TTL_SECONDS: "300",
      COMMAND_MAX_FUTURE_SKEW_SECONDS: "30",
      IDEMPOTENCY_RETENTION_MS: "60000",
      IDEMPOTENCY_MAX_RECORDS: "100",
      JOURNAL_MAX_BYTES: "1048576",
      RATE_LIMIT_PER_MINUTE: "30",
      RATE_LIMIT_BURST: "5",
      STARTUP_TIMEOUT_MS: "5000",
      SHUTDOWN_GRACE_MS: "1000",
    },
    silent: true,
  });
  const stdout = [];
  const stderr = [];
  child.stdout.on("data", (chunk) => stdout.push(chunk.toString("utf8")));
  child.stderr.on("data", (chunk) => stderr.push(chunk.toString("utf8")));

  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("dayz-signal startup timed out")), 8000);
      const onMessage = (message) => {
        if (message && message.type === "ready") {
          clearTimeout(timer);
          child.off("message", onMessage);
          child.off("exit", onExit);
          resolve();
        }
      };
      const onExit = (code) => {
        clearTimeout(timer);
        child.off("message", onMessage);
        reject(new Error(`dayz-signal exited during startup (${code}): ${stderr.join("").slice(-1000)}`));
      };
      child.on("message", onMessage);
      child.once("exit", onExit);
    });
  } catch (error) {
    if (child.exitCode === null) child.kill();
    throw error;
  }
  return { child, journalPath, stdout, stderr };
}


async function stopSignalProcess(signal) {
  if (!signal || signal.child.exitCode !== null) return;
  const exited = new Promise((resolve) => signal.child.once("exit", resolve));
  signal.child.send({ type: "shutdown" });
  const completed = await Promise.race([
    exited.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 5000)),
  ]);
  if (!completed && signal.child.exitCode === null) {
    signal.child.kill();
    await exited;
  }
}


async function startUdpEmulator(signalRoot) {
  const { buildPacket, parsePacket } = require(path.join(signalRoot, "src", "rconClient.js"));
  const socket = dgram.createSocket("udp4");
  const commands = [];
  let protocolError = null;
  socket.on("message", (message, rinfo) => {
    try {
      const packet = parsePacket(message);
      if (packet.type === 0) {
        socket.send(buildPacket(0, Buffer.from([1])), rinfo.port, rinfo.address);
      } else if (packet.type === 1) {
        const command = packet.data.toString("ascii");
        if (command) commands.push(command);
        socket.send(buildPacket(1, Buffer.from([packet.sequence])), rinfo.port, rinfo.address);
      }
    } catch (error) {
      protocolError = error;
    }
  });
  await new Promise((resolve, reject) => {
    socket.once("error", reject);
    socket.bind(0, "127.0.0.1", resolve);
  });
  return {
    socket,
    port: socket.address().port,
    commands,
    get protocolError() { return protocolError; },
  };
}


async function closeUdp(emulator) {
  if (!emulator) return;
  await new Promise((resolve) => emulator.socket.close(resolve));
}


async function processN8nPolicy(batch, headers, nowIso, signalBaseUrl) {
  const rawBatch = JSON.stringify(batch);
  assert.ok(!rawBatch.includes(PRIVATE_UID), "monitor batch leaked a raw UID");
  const connected = batch.events.find((event) => event.type === "player.connected");
  assert.ok(connected, "synthetic ADM event is missing");
  assert.equal(connected.admin_view.actor.display_name, PRIVATE_NAME);
  assert.equal(connected.admin_view.location.x, 4250.5);

  const validated = await runCode("10-event-gateway.json", "Validate and Normalize Batch", {
    $json: { body: batch, headers },
    __TEST_NOW__: nowIso,
  });
  assert.equal(validated.length, 1);
  assert.equal(validated[0].json.valid, true, validated[0].json.response && validated[0].json.response.message);
  const normalized = JSON.parse(validated[0].json.batch_row.payload_json);
  assert.deepEqual(
    normalized.events.map((event) => event.event_type).sort(),
    ["player.connected", "world.snapshot", "world.snapshot"],
  );

  const claimedBatch = { ...validated[0].json.batch_row, lease_token: "e2e-batch-lease" };
  const rows = await runCode("15-batch-processor.json", "Expand Normalized Batch", {
    $input: { all: () => [{ json: claimedBatch }] },
  });
  const immediate = await runCode("20-digest-llm-publisher.json", "Build Immediate Deliveries", {
    $input: { all: () => rows.filter((item) => item.json.immediate_state === "pending") },
    __TEST_NOW__: nowIso,
  });
  assert.equal(immediate.length, 0, "the selected synthetic events must not publish immediately");

  const aggregate = await runCode("20-digest-llm-publisher.json", "Aggregate Public Digest", {
    $input: { all: () => rows.filter((item) => item.json.digest_state === "pending") },
  });
  const route = {
    server_id: "livonia-1",
    mode: "active",
    enabled: true,
    telegram_enabled: true,
    telegram_chat_id: "mock-telegram",
    game_enabled: true,
    llm_enabled: true,
    public_delay_seconds: 600,
    game_cooldown_seconds: 120,
    signal_base_url: signalBaseUrl,
  };
  const combined = await runCode("20-digest-llm-publisher.json", "Combine Digest and Route", {
    $input: { all: () => [{ json: route }] },
    $: (name) => ({ all: () => (name === "Aggregate Public Digest" ? aggregate : []) }),
    __TEST_NOW__: nowIso,
  });
  assert.equal(combined.length, 1);
  assert.equal(combined[0].json.publish, true);
  const publicDigest = combined[0].json.public_digest_json;
  assertPrivateDataAbsent(publicDigest, "LLM public digest");
  assert.ok(publicDigest.includes("day_to_night"));
  assert.ok(publicDigest.includes('"phenomenon":"rain"'));

  const llmNode = workflowNode("20-digest-llm-publisher.json", "OpenAI Compatible Digest Wording");
  assert.ok(llmNode.parameters.jsonBody.includes("$json.public_digest_json"));
  assert.ok(!llmNode.parameters.jsonBody.includes("admin_json"));
  assert.ok(!llmNode.parameters.jsonBody.includes("facts"));
  const mockLlmResponse = {
    body: {
      choices: [{ message: { content: JSON.stringify({ message_ru: MOCK_LLM_MESSAGE, safety_flags: [] }) } }],
    },
  };
  const wording = await runCode("20-digest-llm-publisher.json", "Validate LLM or Use Fallback", {
    $json: mockLlmResponse,
    $: () => ({ item: { json: combined[0].json } }),
  });
  assert.equal(wording.json.message_ru, MOCK_LLM_MESSAGE);

  const deliveries = await runCode("20-digest-llm-publisher.json", "Build Digest Deliveries", {
    $input: { all: () => [wording] },
  });
  assert.deepEqual(Array.from(deliveries, (item) => item.json.channel), ["telegram", "game"]);
  assert.equal(deliveries[0].json.message, deliveries[1].json.message);
  assert.ok(/[А-Яа-яЁё]/u.test(deliveries[0].json.message), "shared delivery must remain Russian");

  const combinedDeliveries = [];
  for (const delivery of deliveries) {
    const item = await runCode("30-delivery-retry-cleanup.json", "Combine Delivery and Route", {
      $json: route,
      $: () => ({ item: { json: delivery.json } }),
    });
    assert.equal(item.json.deliverable, true);
    combinedDeliveries.push(item);
  }
  const telegram = combinedDeliveries.find((item) => item.json.channel === "telegram");
  const game = combinedDeliveries.find((item) => item.json.channel === "game");
  assert.ok(telegram && game);
  const telegramMessage = telegram.json.message;
  const gameMessage = game.json.message;
  assert.equal(telegramMessage, gameMessage);
  assertPrivateDataAbsent(telegramMessage, "mock Telegram delivery");
  assertPrivateDataAbsent(gameMessage, "game delivery");

  const telegramResult = await runCode("30-delivery-retry-cleanup.json", "Telegram Sent", {
    $: () => ({ item: { json: telegram.json } }),
  });
  assert.equal(telegramResult.json.status, "sent");

  const signalCommand = await runCode("30-delivery-retry-cleanup.json", "Build Signal Command", {
    $json: game.json,
  });
  assertPrivateDataAbsent(signalCommand.json.command, "Signal v1 command");
  const posted = await fetchJson(signalCommand.json.signal_url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${SIGNAL_API_KEY}`,
      "content-type": "application/json",
      "idempotency-key": signalCommand.json.command_id,
    },
    body: JSON.stringify(signalCommand.json.command),
  });
  assert.equal(posted.status, 202, JSON.stringify(posted.body));

  const finalStatus = await waitFor(async () => {
    const status = await fetchJson(signalCommand.json.status_url, {
      headers: { authorization: `Bearer ${SIGNAL_API_KEY}` },
    });
    return status.body.status === "acknowledged" ? status : null;
  }, 5000);
  const classified = await runCode("30-delivery-retry-cleanup.json", "Classify Signal Result", {
    $json: { statusCode: finalStatus.status, body: finalStatus.body },
    $: () => ({ item: { json: signalCommand.json } }),
  });
  assert.equal(classified.json.status, "sent");

  return {
    accepted_event_count: normalized.events.length,
    event_types: normalized.events.map((event) => event.event_type).sort(),
    llm_input_bytes: Buffer.byteLength(publicDigest),
    llm_received_public_only: true,
    telegram_message: telegramMessage,
    game_message: gameMessage,
    same_russian_message: telegramMessage === gameMessage,
    signal_http_post_status: posted.status,
    signal_http_final_status: finalStatus.status,
    signal_state: finalStatus.body.status,
    signal_sent_message: finalStatus.body.sent_message,
    command_id: finalStatus.body.command_id,
  };
}


async function startMockN8n(nowIso, signalBaseUrl) {
  let result = null;
  let processingError = null;
  let requestCount = 0;
  const server = http.createServer(async (req, res) => {
    try {
      assert.equal(req.method, "POST");
      assert.equal(req.url, "/webhook/dayz/events/v1");
      assert.equal(req.headers["x-dayz-key"], EVENT_WEBHOOK_KEY);
      requestCount += 1;
      assert.equal(requestCount, 1, "monitor retried a successful E2E batch");
      const chunks = [];
      let length = 0;
      for await (const chunk of req) {
        length += chunk.length;
        assert.ok(length <= 262_144, "monitor batch exceeded 256 KiB");
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      result = await processN8nPolicy(body, req.headers, nowIso, signalBaseUrl);
      const response = Buffer.from(JSON.stringify({ accepted: true }), "utf8");
      res.writeHead(202, { "content-type": "application/json", "content-length": response.length });
      res.end(response);
    } catch (error) {
      processingError = error;
      const response = Buffer.from(JSON.stringify({ accepted: false }), "utf8");
      res.writeHead(500, { "content-type": "application/json", "content-length": response.length });
      res.end(response);
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    server,
    url: `http://127.0.0.1:${server.address().port}/webhook/dayz/events/v1`,
    get result() { return result; },
    get processingError() { return processingError; },
    get requestCount() { return requestCount; },
  };
}


async function runMonitorEmitter(mockN8n, nowIso) {
  const python = process.env.PYTHON || "python";
  let output;
  try {
    output = await execFile(
      python,
      [
        "-m", "tests.e2e_monitor_emit",
        "--webhook", mockN8n.url,
        "--key", EVENT_WEBHOOK_KEY,
        "--now", nowIso,
      ],
      {
        cwd: ROOT,
        timeout: 60000,
        windowsHide: true,
        maxBuffer: 1024 * 1024,
        env: { ...process.env, NO_PROXY: "127.0.0.1,localhost,::1" },
      },
    );
  } catch (error) {
    const detail = String(error.stdout || error.stderr || error.message || error).trim();
    const policy = mockN8n.processingError
      ? `; mock n8n: ${mockN8n.processingError.stack || mockN8n.processingError.message}`
      : "";
    throw new Error(`monitor emitter failed: ${detail}${policy}`);
  }
  const { stdout, stderr } = output;
  assert.equal(stderr.trim(), "");
  const result = JSON.parse(stdout);
  assert.equal(result.ok, true, result.error);
  return result;
}


async function main() {
  const { signalRoot } = parseArgs(process.argv.slice(2));
  const tempDirectory = await fs.promises.mkdtemp(path.join(os.tmpdir(), "dayz-local-e2e-"));
  let udp = null;
  let signal = null;
  let mockN8n = null;
  try {
    udp = await startUdpEmulator(signalRoot);
    const signalPort = await freeTcpPort();
    signal = await startSignalProcess(signalRoot, tempDirectory, udp.port, signalPort);
    const signalBaseUrl = `http://127.0.0.1:${signalPort}`;
    await waitFor(async () => {
      const response = await fetch(`${signalBaseUrl}/readyz`);
      return response.status === 200;
    }, 8000);

    const nowIso = new Date().toISOString();
    mockN8n = await startMockN8n(nowIso, signalBaseUrl);
    const monitor = await runMonitorEmitter(mockN8n, nowIso);
    if (mockN8n.processingError) throw mockN8n.processingError;
    assert.ok(mockN8n.result, "mock n8n did not process a batch");
    assert.equal(mockN8n.requestCount, 1);
    await waitFor(() => udp.commands.length === 1, 3000);
    if (udp.protocolError) throw udp.protocolError;

    const n8n = mockN8n.result;
    const expectedUdp = `say -1 ${n8n.signal_sent_message}`;
    assert.deepEqual(udp.commands, [expectedUdp]);
    assert.match(n8n.signal_sent_message, /^[\x20-\x7e]+$/);
    assert.notEqual(n8n.signal_sent_message, n8n.game_message);
    assertPrivateDataAbsent(udp.commands[0], "UDP RCON command");

    const journalLines = fs.readFileSync(signal.journalPath, "utf8").trim().split("\n").filter(Boolean);
    const journalStates = journalLines.map((line) => JSON.parse(line).state);
    assert.deepEqual(journalStates, ["queued", "sending", "acknowledged"]);

    const summary = {
      schema: "dayz.local-e2e-summary.v1",
      ok: true,
      network_scope: "loopback-only",
      source_snapshot_touched: false,
      runtime: { node: process.version, python: process.env.PYTHON || "python" },
      monitor: {
        first_poll: monitor.first_poll,
        second_poll_work: monitor.second_poll_work,
        event_types: monitor.event_types,
        raw_uid_absent: monitor.raw_uid_absent,
      },
      mock_n8n: {
        accepted_event_count: n8n.accepted_event_count,
        event_types: n8n.event_types,
        llm_input_bytes: n8n.llm_input_bytes,
        llm_received_public_only: n8n.llm_received_public_only,
        telegram_count: 1,
        game_count: 1,
        same_russian_message: n8n.same_russian_message,
        message_ru: n8n.telegram_message,
      },
      signal: {
        post_status: n8n.signal_http_post_status,
        final_http_status: n8n.signal_http_final_status,
        state: n8n.signal_state,
        journal_states: journalStates,
      },
      rcon: {
        command_count: udp.commands.length,
        transliterated_message: n8n.signal_sent_message,
        printable_ascii: true,
      },
      privacy: {
        raw_uid_absent: true,
        exact_coordinates_absent_from_llm_telegram_game_udp: true,
      },
    };
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
  } finally {
    await closeServer(mockN8n && mockN8n.server);
    await stopSignalProcess(signal);
    await closeUdp(udp);
    await fs.promises.rm(tempDirectory, { recursive: true, force: true });
  }
}


if (process.argv.includes("--signal-child")) {
  signalChildMain().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exitCode = 1;
  });
} else {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exitCode = 1;
  });
}
