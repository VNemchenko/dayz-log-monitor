from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def fail(message: str) -> None:
    raise AssertionError(message)


def load_json(relative: str) -> Any:
    with (ROOT / relative).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def all_json_documents() -> dict[str, Any]:
    documents: dict[str, Any] = {}
    for path in sorted(ROOT.rglob("*.json")):
        relative = path.relative_to(ROOT).as_posix()
        with path.open("r", encoding="utf-8") as handle:
            documents[relative] = json.load(handle)
    return documents


def assert_generator_is_deterministic(manifest: dict[str, Any]) -> None:
    paths = [ROOT / item["file"] for item in manifest["workflows"]]
    before = {path: path.read_bytes() for path in paths}
    completed = subprocess.run(
        ["node", str(ROOT / manifest["generator"])],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        fail(f"workflow generator failed: {completed.stderr.strip()}")
    after = {path: path.read_bytes() for path in paths}
    if before != after:
        fail("generated workflow artifacts are not deterministic/canonical")


def assert_workflows(manifest: dict[str, Any], documents: dict[str, Any]) -> None:
    workflow_entries = manifest["workflows"]
    expected_files = [entry["file"] for entry in workflow_entries]
    actual_files = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "workflows").glob("*.json")
    )
    if sorted(expected_files) != actual_files:
        fail(f"manifest/workflow file mismatch: {expected_files!r} != {actual_files!r}")
    if [entry["order"] for entry in workflow_entries] != sorted(
        entry["order"] for entry in workflow_entries
    ):
        fail("workflow import order is not sorted")

    all_node_ids: set[str] = set()
    declared_tables = {entry["name"] for entry in manifest["data_tables"]}
    placeholder_credentials = {
        (entry["placeholder_id"], entry["type"], entry["name"])
        for entry in manifest["credentials"]
    }
    used_credentials: set[tuple[str, str, str]] = set()

    for relative in expected_files:
        workflow = documents[relative]
        for key in ("name", "nodes", "connections", "active", "settings", "versionId"):
            if key not in workflow:
                fail(f"{relative}: missing workflow key {key}")
        if workflow["active"] is not False:
            fail(f"{relative}: workflow must import inactive")
        settings = workflow["settings"]
        if settings.get("saveDataErrorExecution") != "none" or settings.get(
            "saveDataSuccessExecution"
        ) != "none":
            fail(f"{relative}: runtime execution payload storage is enabled")

        node_names: set[str] = set()
        for node in workflow["nodes"]:
            name = node["name"]
            node_id = node["id"]
            if name in node_names:
                fail(f"{relative}: duplicate node name {name}")
            if node_id in all_node_ids:
                fail(f"duplicate node id across bundle: {node_id}")
            node_names.add(name)
            all_node_ids.add(node_id)

            if node["type"] == "n8n-nodes-base.dataTable":
                parameters = node["parameters"]
                locator = parameters.get("dataTableId")
                if locator:
                    if locator.get("mode") != "name" or locator.get("value") not in declared_tables:
                        fail(f"{relative}/{name}: non-portable Data Table locator {locator!r}")

            for credential_type, reference in node.get("credentials", {}).items():
                used_credentials.add(
                    (reference.get("id", ""), credential_type, reference.get("name", ""))
                )

        for source, output_groups in workflow["connections"].items():
            if source not in node_names:
                fail(f"{relative}: connection source does not exist: {source}")
            for groups in output_groups.values():
                if not isinstance(groups, list):
                    fail(f"{relative}/{source}: invalid connection output groups")
                for group in groups:
                    for connection in group:
                        if connection["node"] not in node_names:
                            fail(
                                f"{relative}/{source}: target does not exist: {connection['node']}"
                            )

    if used_credentials != placeholder_credentials:
        fail(
            "workflow credential references differ from manifest placeholders: "
            f"used={used_credentials!r}, expected={placeholder_credentials!r}"
        )

    bootstrap = documents["workflows/00-bootstrap-data-tables.json"]
    created_tables = {
        node["parameters"]["tableName"]
        for node in bootstrap["nodes"]
        if node["type"] == "n8n-nodes-base.dataTable"
        and node["parameters"].get("resource") == "table"
        and node["parameters"].get("operation") == "create"
        and node["parameters"].get("options", {}).get("createIfNotExists") is True
    }
    if created_tables != declared_tables:
        fail(f"bootstrap tables differ from manifest: {created_tables!r}")
    if created_tables != {"dayz_routes", "dayz_events", "dayz_deliveries", "dayz_cooldowns"}:
        fail(f"bootstrap must provision the exact four-table contract: {created_tables!r}")
    event_table_node = next(node for node in bootstrap["nodes"] if node["name"] == "Create dayz_events")
    event_table_columns = {
        entry["name"]: entry["type"]
        for entry in event_table_node["parameters"]["columns"]["column"]
    }
    if event_table_columns.get("processing_expires_at") != "date" or event_table_columns.get("expires_at") != "date":
        fail("dayz_events must separate the processing deadline from retention expiry")

    generated_text = "\n".join(
        json.dumps(documents[relative], ensure_ascii=False, sort_keys=True)
        for relative in expected_files
    )
    for obsolete_term in ("dayz_batches", "game_ascii", "fallback_game", "telegram_ru"):
        if obsolete_term in generated_text:
            fail(f"generated workflows retain obsolete contract term: {obsolete_term}")

    gateway = documents["workflows/10-event-gateway.json"]
    webhook = next(node for node in gateway["nodes"] if node["type"] == "n8n-nodes-base.webhook")
    if webhook["parameters"].get("path") != manifest["contracts"]["ingest_path"]:
        fail("gateway webhook path differs from manifest")
    if webhook["parameters"].get("authentication") != "headerAuth":
        fail("gateway does not enforce Header Auth")
    if any(node["type"] == "n8n-nodes-base.removeDuplicates" for node in gateway["nodes"]):
        fail("gateway must not mark a batch seen before its durable ledger write")
    if not any(
        node["type"] == "n8n-nodes-base.dataTable"
        and node["parameters"].get("dataTableId", {}).get("value") == "dayz_events"
        and node["name"] in {"Get Existing Batch", "Persist New Batch"}
        for node in gateway["nodes"]
    ):
        fail("gateway lacks the namespaced batch ledger in dayz_events")
    persist_batch = next(node for node in gateway["nodes"] if node["name"] == "Persist New Batch")
    if persist_batch["parameters"].get("operation") != "insert":
        fail("new batches must be inserted only after the durable ledger absence check")
    if persist_batch["parameters"].get("filters"):
        fail("batch insert unexpectedly carries update/upsert filters")
    batch_decision = gateway["connections"].get("Batch Already Exists", {}).get("main")
    if batch_decision != [
        [{"node": "Acknowledge Duplicate Batch", "type": "main", "index": 0}],
        [{"node": "Persist New Batch", "type": "main", "index": 0}],
    ]:
        fail("batch existence is not decided by one fail-closed ledger lookup")
    persist_targets = gateway["connections"]["Persist New Batch"]["main"][0]
    if persist_targets != [{"node": "Acknowledge Accepted Batch", "type": "main", "index": 0}]:
        fail("batch is acknowledged before or independently of the durable ledger insert")

    for relative in expected_files:
        for node in documents[relative]["nodes"]:
            parameters = node.get("parameters", {})
            if node["type"] != "n8n-nodes-base.dataTable" or parameters.get("operation") != "upsert":
                continue
            table = parameters.get("dataTableId", {}).get("value")
            if table in {"dayz_events", "dayz_deliveries"}:
                fail(f"{relative}/{node['name']} can reset durable routing/terminal state via upsert")
            expected_key = {
                "dayz_events": "record_id",
                "dayz_deliveries": "delivery_id",
                "dayz_cooldowns": "scope_key",
            }.get(table)
            filter_keys = {
                condition.get("keyName")
                for condition in parameters.get("filters", {}).get("conditions", [])
            }
            if expected_key and filter_keys != {expected_key}:
                fail(f"{relative}/{node['name']} does not upsert by stable {expected_key}")

    publisher = documents["workflows/20-digest-llm-publisher.json"]
    processor = documents["workflows/15-batch-processor.json"]
    loop_contracts = [
        (processor, "Loop Event Candidates", "Build Batch Completion", "Get Existing Event Row"),
        (publisher, "Loop World Announcement Deliveries", "Mark World Announcement Queued", "Get Existing World Announcement Delivery"),
        (publisher, "Loop Safety Deliveries", "Mark Safety Event Queued", "Get Existing Safety Delivery"),
        (publisher, "Loop Command Deliveries", "Mark Command Event Queued", "Get Existing Command Delivery"),
        (publisher, "Loop Immediate Deliveries", "Mark Immediate Event Queued", "Get Existing Immediate Delivery"),
        (publisher, "Loop Digest Deliveries", "Expand Digested Event IDs", "Get Existing Digest Delivery"),
        (publisher, "Loop Chronicle Deliveries", "Expand Chronicled Event IDs", "Get Existing Chronicle Delivery"),
    ]
    for document, loop_name, done_target, item_target in loop_contracts:
        loop = next((node for node in document["nodes"] if node["name"] == loop_name), None)
        if (
            not loop
            or loop["type"] != "n8n-nodes-base.splitInBatches"
            or loop.get("typeVersion") != 3
            or loop["parameters"].get("batchSize") != 1
        ):
            fail(f"{loop_name} is not an n8n 2.6.4 one-item create-if-absent loop")
        expected_loop_outputs = [
            [{"node": done_target, "type": "main", "index": 0}],
            [{"node": item_target, "type": "main", "index": 0}],
        ]
        if document["connections"].get(loop_name, {}).get("main") != expected_loop_outputs:
            fail(f"{loop_name} can mark source state before all candidates are checked")

    command_branch = publisher["connections"].get("Command Delivery Is Valid", {}).get("main", [])
    if len(command_branch) != 2 or command_branch[1] != [
        {"node": "Mark Command Event Expired", "type": "main", "index": 0}
    ]:
        fail("invalid/near-expired command replies can reach delivery insertion")

    deadline_queries = {
        "Get Pending World Announcements", "Get Pending Safety Warnings",
        "Get Pending Player Commands", "Get Pending Immediate Events",
        "Get Pending Public Events", "Get Pending Chronicle Events",
    }
    for node in publisher["nodes"]:
        if node["name"] not in deadline_queries:
            continue
        filter_names = {
            condition.get("keyName")
            for condition in node["parameters"].get("filters", {}).get("conditions", [])
        }
        if "processing_expires_at" not in filter_names or "expires_at" in filter_names:
            fail(f"{node['name']} does not honor the wire event processing deadline")

    retry = documents["workflows/30-delivery-retry-cleanup.json"]
    retry_limits = documents["config/policy.v1.json"]["limits"]
    trigger = next(node for node in retry["nodes"] if node["name"] == "Every Minute")
    expected_interval = {
        "field": "minutes",
        "minutesInterval": retry_limits["delivery_scan_interval_seconds"] / 60,
    }
    if trigger["parameters"].get("rule", {}).get("interval") != [expected_interval]:
        fail("delivery trigger cadence differs from delivery_scan_interval_seconds")
    pending = next(node for node in retry["nodes"] if node["name"] == "Get Pending Deliveries")
    if pending["parameters"].get("returnAll") is not False or pending["parameters"].get("limit") != 1:
        fail("delivery scan must serialize claims so cooldown checks cannot race inside one run")
    eligibility_keys = {"status", "next_attempt_at", "expires_at", "attempt_count"}
    for document, names in (
        (processor, ("Get Queued Batches", "Claim Queued Batch")),
        (retry, ("Get Pending Deliveries", "Claim Due Delivery")),
    ):
        for name in names:
            lookup = next(node for node in document["nodes"] if node["name"] == name)
            keys = {
                condition.get("keyName")
                for condition in lookup["parameters"].get("filters", {}).get("conditions", [])
            }
            if not eligibility_keys.issubset(keys):
                fail(f"{name} permits head-of-line or late claims: {sorted(keys)}")

    stale_telegram = next(node for node in retry["nodes"] if node["name"] == "Recover Stale Telegram Claims")
    stale_telegram_values = stale_telegram["parameters"]["columns"]["value"]
    if (
        stale_telegram_values.get("status") != "delivery_unknown"
        or stale_telegram_values.get("message") != ""
    ):
        fail("ambiguous stale Telegram claims can be sent twice")
    stale_game = next(node for node in retry["nodes"] if node["name"] == "Recover Stale Game Claims")
    if stale_game["parameters"]["columns"]["value"].get("last_error") != "signal_lease_reconcile":
        fail("stale game claims do not enter deterministic Signal GET reconciliation")

    expected_reconciliation_branch = [
        [{"node": "Build Signal Command", "type": "main", "index": 0}],
        [
            {"node": "Game Cooldown Is Free", "type": "main", "index": 0},
            {"node": "Game Cooldown Is Active", "type": "main", "index": 0},
        ],
    ]
    if retry["connections"].get("Signal Reconciliation Required", {}).get("main") != expected_reconciliation_branch:
        fail("Signal reconciliation does not bypass every game cooldown/rate gate")
    expected_signal_window_branch = [
        [{"node": "Signal Status Poll", "type": "main", "index": 0}],
        [{"node": "Expire Unsafe Signal Submission", "type": "main", "index": 0}],
    ]
    if retry["connections"].get("Signal Attempt Has Recovery Window", {}).get("main") != expected_signal_window_branch:
        fail("Signal POST/GET can bypass the actual remaining-deadline gate")

    maintenance_pairs = [
        ("Recover Stale Telegram Claims", "Collapse Recovered Telegram Claims"),
        ("Recover Stale Game Claims", "Collapse Recovered Game Claims"),
        ("Expire Pending Deliveries", "Collapse Expired Deliveries"),
        ("Fail Exhausted Deliveries", "Collapse Exhausted Deliveries"),
        ("Purge Expired Delivery Messages", "Collapse Purged Delivery Messages"),
        ("Clear Expired Batch Payloads", "Collapse Cleared Batch Payloads"),
        ("Delete Expired Batches", "Collapse Deleted Batches"),
        ("Clear Expired Private Payloads", "Collapse Cleared Private Payloads"),
        ("Delete Expired Events", "Collapse Deleted Events"),
        ("Delete Expired Deliveries", "Collapse Deleted Deliveries"),
    ]
    for bulk, collapse in maintenance_pairs:
        targets = retry["connections"].get(bulk, {}).get("main", [[]])[0]
        if targets != [{"node": collapse, "type": "main", "index": 0}]:
            fail(f"{bulk} can multiply the following table-wide operation")
    for bulk, collapse in (
        ("Recover Stale Batch Claims", "Collapse Recovered Batch Claims"),
        ("Expire Queued Batches", "Collapse Expired Batches"),
        ("Fail Exhausted Batches", "Collapse Exhausted Batches"),
    ):
        targets = processor["connections"].get(bulk, {}).get("main", [[]])[0]
        if targets != [{"node": collapse, "type": "main", "index": 0}]:
            fail(f"{bulk} can multiply the following batch maintenance operation")

    route_lookup = next(node for node in retry["nodes"] if node["name"] == "Get Delivery Route")
    if route_lookup.get("alwaysOutputData") is not True:
        fail("missing delivery route cannot reach the fail-closed terminal path")

    telegram_nodes = {
        node["name"]: node
        for relative in expected_files
        for node in documents[relative]["nodes"]
        if node["type"] == "n8n-nodes-base.telegram"
    }
    expected_telegram_text = {
        "Send Telegram": "={{ $json.telegram_text_html }}",
        "Send Sanitized Error to Telegram": "={{ $('Build Sanitized Alert').item.json.message_html }}",
    }
    if set(telegram_nodes) != set(expected_telegram_text):
        fail(f"unexpected Telegram sendMessage nodes: {sorted(telegram_nodes)}")
    for name, node in telegram_nodes.items():
        parameters = node["parameters"]
        if node.get("typeVersion") != 1.2 or parameters.get("operation") != "sendMessage":
            fail(f"{name} is not the reviewed n8n 2.6.4 Telegram sendMessage shape")
        if parameters.get("additionalFields") != {"appendAttribution": False, "parse_mode": "HTML"}:
            fail(f"{name} does not explicitly disable unsafe implicit Markdown")
        if parameters.get("text") != expected_telegram_text[name]:
            fail(f"{name} bypasses the deterministic HTML-escaped text field")

    signal = next(node for node in retry["nodes"] if node["name"] == "Send Signal v1")
    if signal["parameters"].get("method") != "POST":
        fail("Signal command submission is not POST")
    headers = signal["parameters"].get("headerParameters", {}).get("parameters", [])
    if headers != [{"name": "Idempotency-Key", "value": "={{ $json.command_id }}"}]:
        fail("Signal v1 Idempotency-Key is missing or not derived from command_id")
    if signal["parameters"].get("jsonBody") != "={{ JSON.stringify($json.command) }}":
        fail("Signal v1 does not send the generated full command envelope")
    signal_poll = next(node for node in retry["nodes"] if node["name"] == "Get Signal v1 Status")
    if signal_poll["parameters"].get("method") != "GET":
        fail("Signal queued/sending states are not polled with GET")
    if signal_poll["parameters"].get("sendHeaders") or signal_poll["parameters"].get("headerParameters"):
        fail("Signal status GET must not send a stale Idempotency-Key")
    for node in (signal, signal_poll):
        timeout_ms = node["parameters"].get("options", {}).get("timeout")
        if not isinstance(timeout_ms, int) or timeout_ms >= retry_limits["signal_reconcile_buffer_seconds"] * 1000:
            fail(f"{node['name']} timeout consumes the Signal reconciliation buffer")
        if timeout_ms >= retry_limits["delivery_lease_seconds"] * 1000:
            fail(f"{node['name']} timeout is not shorter than the delivery lease")
    rate_slots = {
        node["name"]
        for node in retry["nodes"]
        if re.fullmatch(r"Auto Rate Slot [0-2] Is Free", node["name"])
    }
    if rate_slots != {"Auto Rate Slot 0 Is Free", "Auto Rate Slot 1 Is Free", "Auto Rate Slot 2 Is Free"}:
        fail("automatic game rolling limit must expose exactly three independent slots")
    signal_classifier = next(
        node for node in retry["nodes"] if node["name"] == "Classify Signal Result"
    )["parameters"]["jsCode"]
    for token in (
        "delivery.signal_method === 'GET'",
        "statusCode === 404",
        "body.error_code === 'not_found'",
        "attemptCount = Number(delivery.attempt_count || 0)",
    ):
        if token not in signal_classifier:
            fail("Signal stale-claim reconciliation is not fail-closed on exact GET not_found")
    signal_builder = next(
        node for node in retry["nodes"] if node["name"] == "Build Signal Command"
    )["parameters"]["jsCode"]
    for token in (
        "Boolean(delivery.signal_status_url)",
        "delivery_lease_seconds",
        "delivery_scan_interval_seconds",
        "signal_reconcile_buffer_seconds",
        "wireTtlMs >= 1000",
        "signalAttemptAllowed",
    ):
        if token not in signal_builder:
            fail(f"Signal actual-deadline/wire-TTL guard is missing {token}")

    gateway_code = next(
        node for node in gateway["nodes"] if node["name"] == "Validate and Normalize Batch"
    )["parameters"]["jsCode"]
    for token in (
        "normalizePublicView",
        "invalid_coarse_location",
        "coordinates",
        "location.size_m !== 2000",
        "safePublicView",
    ):
        if token not in gateway_code:
            fail(f"gateway strict public projection guard is missing {token}")
    announcement_code = next(
        node for node in publisher["nodes"] if node["name"] == "Build Delayed World Announcements"
    )["parameters"]["jsCode"]
    for kind in ("heli_crash", "military_convoy", "train", "police_situation"):
        if kind not in announcement_code:
            fail(f"canonical producer world kind is not announced: {kind}")
    for node_name in ("Combine Digest and Route", "Aggregate Hourly Chronicle"):
        code = next(node for node in publisher["nodes"] if node["name"] == node_name)["parameters"]["jsCode"]
        if "processing_expires_at" not in code and "processing_deadline" not in code:
            fail(f"{node_name} can extend source wire deadlines")

    command_schema = documents[manifest["contracts"]["command_schema"]]
    command_fields = {
        "schema", "command_id", "server_id", "created_at", "expires_at",
        "channel", "message", "metadata",
    }
    if (
        set(command_schema.get("required", [])) != command_fields
        or set(command_schema.get("properties", {})) != command_fields
        or command_schema.get("additionalProperties") is not False
    ):
        fail("dayz.command.v1 schema is not the exact full envelope")
    if command_schema["properties"]["command_id"].get("pattern") != r"^cmd_[A-Za-z0-9][A-Za-z0-9._:-]{3,123}$":
        fail("dayz.command.v1 command_id pattern differs from Signal v1")
    metadata_schema = command_schema["properties"]["metadata"]
    if (
        set(metadata_schema.get("required", [])) != {"event_id", "policy_version"}
        or set(metadata_schema.get("properties", {})) != {"event_id", "policy_version"}
        or metadata_schema.get("additionalProperties") is not False
    ):
        fail("dayz.command.v1 metadata is not the exact event_id/policy_version contract")
    command = documents["fixtures/signal/command.json"]
    if set(command) != set(command_schema["required"]):
        fail("Signal command fixture is not the exact full envelope")
    if not re.fullmatch(r"cmd_[A-Za-z0-9][A-Za-z0-9._:-]{3,123}", command["command_id"]):
        fail("Signal command fixture does not use cmd_ identity")
    created_at = datetime.fromisoformat(command["created_at"].replace("Z", "+00:00"))
    expires_at = datetime.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
    if not 1 <= (expires_at - created_at).total_seconds() <= 300:
        fail("Signal command fixture violates the 1..300 second TTL")
    if command.get("channel") != "global" or command.get("schema") != "dayz.command.v1":
        fail("Signal command fixture is not the global dayz.command.v1 envelope")
    if command.get("metadata") != {
        "event_id": command.get("metadata", {}).get("event_id"),
        "policy_version": command.get("metadata", {}).get("policy_version"),
    }:
        fail("Signal command metadata contains fields outside event_id/policy_version")
    if command["message"].isascii():
        fail("Signal command fixture must prove that Russian Unicode is passed through unchanged")
    response = documents["fixtures/signal/success.json"]["body"]
    if response.get("command_id") != command["command_id"] or response.get("request_id") != command["command_id"] or response.get("status") != "acknowledged":
        fail("Signal response fixture is not correlated by command_id/request_id/status")


def discover_monitor_event_types() -> set[str]:
    variable_names = {"event_type", "base_action", "aggregate_type"}
    result: set[str] = set()
    for relative in ("dayz_events/parser.py", "dayz_events/pipeline.py"):
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if any(
                    isinstance(target, ast.Name) and target.id in variable_names
                    for target in targets
                ):
                    for child in ast.walk(value):
                        if (
                            isinstance(child, ast.Constant)
                            and isinstance(child.value, str)
                            and "." in child.value
                        ):
                            result.add(child.value)
                if any(
                    isinstance(target, ast.Name) and target.id == "TELEMETRY_TYPES"
                    for target in targets
                ):
                    result.update(
                        child.value
                        for child in ast.walk(value)
                        if isinstance(child, ast.Constant) and isinstance(child.value, str)
                    )
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_event"
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    result.add(node.args[1].value)
                for keyword in node.keywords:
                    if (
                        keyword.arg == "event_type"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        result.add(keyword.value.value)
    return result


def assert_monitor_contract(policy: dict[str, Any]) -> None:
    sys.path.insert(0, str(REPO))
    from dayz_events.models import iso_utc, stable_id  # type: ignore[import-not-found]
    from dayz_events.parser import DayZEventParser, LineRecord  # type: ignore[import-not-found]
    from dayz_events.privacy import PrivacyProjector  # type: ignore[import-not-found]

    monitor_types = discover_monitor_event_types()
    policy_types = set(policy["event_types"])
    if monitor_types != policy_types:
        fail(
            "monitor/policy event type mismatch: "
            f"missing={sorted(monitor_types - policy_types)}, extra={sorted(policy_types - monitor_types)}"
        )

    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    parser = DayZEventParser(
        "livonia-1", PrivacyProjector("s" * 40, namespace="livonia-1")
    )

    def record(kind: str, file_name: str, text: str, offset: int) -> LineRecord:
        return LineRecord(
            kind=kind,
            file_key=f"fixture-{file_name}",
            file_name=file_name,
            offset_start=offset,
            offset_end=offset + len(text.encode("utf-8")) + 1,
            text=text,
            occurred_at=now,
            observed_at=now,
        )

    events: list[dict[str, Any]] = []
    # ADM order is world-x, world-z, altitude. The parser projects it to x, y, z.
    sos_line = '12:00:00 | Chat("Player One"(id=private-uid pos=<4250, 8100, 12>)): !sos'
    events.extend(
        action["event"]
        for action in parser.parse(
            record("adm", "DayZServer_2030-01-01_12-00-00.ADM", sos_line, 0)
        )
        if action["action"] == "event"
    )
    telemetry = {
        "schema": 1,
        "server_id": "livonia-1",
        "boot_id": "20300101T115500Z",
        "seq": 7,
        "catalog_revision": "sha256:" + "a" * 64,
        "visibility": "admin",
        "ts_utc": "2030-01-01T12:00:00Z",
        "type": "world.event.started",
        "data": {"kind": "heli_crash", "x": 4100, "y": 12, "z": 8700},
    }
    telemetry_line = "12:00:00 | RB_EVT v1 " + json.dumps(telemetry)
    events.extend(
        action["event"]
        for action in parser.parse(
            record("adm", "DayZServer_2030-01-01_12-00-00.ADM", telemetry_line, 200)
        )
        if action["action"] == "event"
    )
    restart_line = "12:00:00 | [Shutdown] Shutting down in 300 seconds"
    events.extend(
        action["event"]
        for action in parser.parse(
            record("rpt", "DayZServer_2030-01-01_12-00-00.RPT", restart_line, 500)
        )
        if action["action"] == "event"
    )
    batch_id = stable_id("batch", "livonia-1", *(event["event_id"] for event in events))
    generated = {
        "schema": "dayz.event-batch.v1",
        "batch_id": batch_id,
        "sent_at": iso_utc(now),
        "producer": {"name": "dayz-log-monitor", "source": "livonia"},
        "events": events,
    }
    fixture = load_json("fixtures/events/valid_monitor_batch.json")
    if generated != fixture:
        fail("valid_monitor_batch.json has drifted from current monitor parser/models")

    event_schema = load_json("schemas/dayz.event.v1.schema.json")
    required = set(event_schema["required"])
    properties = set(event_schema["properties"])
    wire_keys = set(events[0])
    if required != wire_keys or properties != wire_keys:
        fail(
            "dayz.event.v1 schema fields differ from monitor wire fields: "
            f"wire={sorted(wire_keys)}, required={sorted(required)}, properties={sorted(properties)}"
        )


def assert_config_and_safety(manifest: dict[str, Any], documents: dict[str, Any]) -> None:
    policy = documents[manifest["configuration"]["policy"]]
    routes = documents[manifest["configuration"]["routes_example"]]
    if policy["schema"] != "dayz.policy.v1" or policy["version"] != "dayz-policy-v1":
        fail("unexpected policy identity")
    limits = policy["limits"]
    if limits.get("command_player_cooldown_seconds") != 60 or limits.get("command_global_cooldown_seconds") != 15:
        fail("player/global command cooldown contract drifted")
    if limits.get("auto_game_min_interval_seconds") != 120 or limits.get("auto_game_max_per_10_minutes") != 3 or limits.get("auto_game_window_seconds") != 600:
        fail("automatic game rate policy drifted")
    if limits.get("game_global_cooldown_seconds") != 120 or limits.get("game_message_max_chars") != 160:
        fail("Signal-compatible game cooldown/message policy drifted")
    expected_signal_timing = {
        "signal_ttl_max_seconds": 300,
        "delivery_lease_seconds": 30,
        "delivery_scan_interval_seconds": 60,
        "signal_reconcile_buffer_seconds": 15,
        "delivery_max_attempts": 3,
        "delivery_retry_seconds": [30, 120],
    }
    for key, expected in expected_signal_timing.items():
        if limits.get(key) != expected:
            fail(f"Signal timing policy drifted for {key}: {limits.get(key)!r}")
    signal_submission_floor = (
        limits["delivery_lease_seconds"]
        + limits["delivery_scan_interval_seconds"]
        + limits["signal_reconcile_buffer_seconds"]
    )
    if signal_submission_floor != 105 or signal_submission_floor >= limits["signal_ttl_max_seconds"]:
        fail("Signal submission recovery floor is not safely below the wire TTL")
    if len(limits["delivery_retry_seconds"]) != limits["delivery_max_attempts"] - 1:
        fail("Signal retry schedule does not match total-attempt semantics")
    if limits.get("snapshot_fresh_seconds") != 180 or limits.get("public_delay_seconds") != 600:
        fail("snapshot freshness/world delay policy drifted")
    if limits.get("chronicle_min_significant_events") != 3 or limits.get("chronicle_window_seconds") != 3600:
        fail("hourly chronicle threshold/window drifted")
    for event_type in ("combat.infected_pressure", "combat.vehicle_incident", "combat.wildlife_pressure"):
        rule = policy["event_types"].get(event_type, {})
        if rule.get("max_audience") != "public" or rule.get("chronicle") is not True:
            fail(f"{event_type} is not admitted to the privacy-gated public chronicle")
    world_lifecycle_types = (
        "world.event.present", "world.event.started",
        "world.event.updated", "world.event.ended",
    )
    for event_type in world_lifecycle_types:
        rule = policy["event_types"].get(event_type, {})
        if (
            rule.get("max_audience") != "public_delayed"
            or rule.get("immediate_channels") != ["telegram"]
            or rule.get("digest") is not False
        ):
            fail(f"{event_type} must use only admin Telegram plus its dedicated public path")
    trap_rule = policy["event_types"].get("combat.trap", {})
    if trap_rule.get("max_audience") != "admin" or trap_rule.get("immediate_channels") != ["telegram"]:
        fail("combat.trap must remain an immediate admin-only Telegram signal")
    llm_schema = documents[manifest["contracts"]["llm_response_schema"]]
    if set(llm_schema.get("required", [])) != {"message_ru", "safety_flags"} or llm_schema.get("additionalProperties") is not False:
        fail("LLM response is not the strict message_ru/safety_flags contract")
    llm_message = llm_schema.get("properties", {}).get("message_ru", {})
    if (
        llm_message.get("minLength") != 1
        or llm_message.get("maxLength") != limits["public_message_max_chars"]
        or llm_message.get("pattern") != r"^[^\u0000-\u001F\u007F-\u009F]+$"
    ):
        fail("LLM message schema does not reject empty/control-bearing output at the policy limit")
    for row in routes["rows"]:
        if row.get("mode") != "shadow" or row.get("enabled") is not False:
            fail("route example is not fail-closed")
        for field in ("telegram_enabled", "game_enabled", "llm_enabled"):
            if row.get(field) is not False:
                fail(f"route example enables external channel {field}")
        for field in ("signal_base_url", "llm_base_url"):
            if ".invalid" not in row.get(field, ""):
                fail(f"route example contains a non-reserved URL in {field}")
        if not isinstance(row.get("rules_message_ru"), str) or len(row["rules_message_ru"]) > limits["game_message_max_chars"]:
            fail("route rules_message_ru must be an optional bounded string")

    allowed_url_hosts = {"json-schema.org", "schemas.example.invalid", "signal.example.invalid", "llm.example.invalid", "127.0.0.1"}
    url_pattern = re.compile(r"https?://([^/\s\"')]+)", re.IGNORECASE)
    token_patterns = [
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}"),
    ]
    ip_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".js", ".py", ".ps1", ".md", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in url_pattern.finditer(text):
            host = match.group(1).split(":", 1)[0].lower()
            if host not in allowed_url_hosts:
                fail(f"{path.relative_to(ROOT)} contains non-reserved URL host: {host}")
        for pattern in token_patterns:
            match = pattern.search(text)
            if match:
                fail(f"{path.relative_to(ROOT)} contains secret/IP-like material: {match.group(0)}")
        for match in ip_pattern.finditer(text):
            if match.group(0) != "127.0.0.1":
                fail(f"{path.relative_to(ROOT)} contains non-loopback IP-like material: {match.group(0)}")


def main() -> int:
    documents = all_json_documents()
    manifest = documents["manifest.json"]
    if manifest.get("schema") != "dayz.n8n-bundle.v1":
        fail("unexpected manifest schema")
    assert_generator_is_deterministic(manifest)
    documents = all_json_documents()
    assert_workflows(manifest, documents)
    policy = documents[manifest["configuration"]["policy"]]
    assert_monitor_contract(policy)
    assert_config_and_safety(manifest, documents)
    print(
        f"validate_bundle: ok ({len(manifest['workflows'])} workflows, "
        f"{len(documents)} JSON documents, {len(policy['event_types'])} monitor event types)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"validate_bundle: FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
