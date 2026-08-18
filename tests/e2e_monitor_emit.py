from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from dayz_events.config import EventPipelineConfig
from dayz_events.pipeline import EventPipeline


PRIVATE_UID = "uid-e2e-private-987654"
PRIVATE_NAME = "E2E_Private_Player"
PRIVATE_X = "4250.5"
PRIVATE_Z = "8100.25"
WORK_METRICS = ("lines", "events", "batches", "delivered", "retried")


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_now(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def require_loopback(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--webhook must be a loopback HTTP URL")


def snapshot_payload(now: datetime, sequence: int, *, is_night: bool, rain: float) -> dict[str, object]:
    occurred = now - timedelta(seconds=70 - sequence * 20)
    return {
        "schema": 1,
        "type": "world.snapshot",
        "ts_utc": utc_iso(occurred),
        "server_id": "livonia-1",
        "boot_id": "e2e-local-boot",
        "seq": sequence,
        "catalog_revision": "sha256:" + "a" * 64,
        "visibility": "admin",
        "data": {
            "game_time": {
                "year": 2026,
                "month": 8,
                "day": 17,
                "hour": 21 if is_night else 19,
                "minute": 10,
                "decimal_hour": 21.17 if is_night else 19.17,
                "is_night": is_night,
            },
            "weather": {
                "rain": {"actual": rain, "forecast": rain, "next_change_seconds": 60},
                "fog": {"actual": 0.1, "forecast": 0.1, "next_change_seconds": 120},
            },
            "server": {
                "online_players": 7,
                "fps_min": 52.0,
                "fps_avg": 58.0,
                "fps_max": 60.0,
                "uptime_seconds": 900.0,
            },
        },
    }


def write_synthetic_adm(logs_dir: Path, now: datetime) -> Path:
    local = now.astimezone(ZoneInfo("Europe/Moscow"))
    file_name = f"DayZServer_{local:%Y-%m-%d_%H-%M-%S}.ADM"
    line_time = f"{local.hour}:{local:%M:%S}"
    lines = [
        (
            f'{line_time} | Player "{PRIVATE_NAME}" '
            f"(id={PRIVATE_UID} pos=<{PRIVATE_X}, {PRIVATE_Z}, 12>) is connected"
        ),
        f"{line_time} | RB_EVT v1 {json.dumps(snapshot_payload(now, 1, is_night=False, rain=0.1), separators=(',', ':'))}",
        f"{line_time} | RB_EVT v1 {json.dumps(snapshot_payload(now, 2, is_night=True, rain=0.8), separators=(',', ':'))}",
    ]
    path = logs_dir / file_name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return path


def config(logs_dir: Path, state_db: Path, webhook: str, key: str) -> EventPipelineConfig:
    return EventPipelineConfig(
        enabled=True,
        logs_dir=logs_dir,
        state_db=state_db,
        webhook_url=webhook,
        webhook_key=key,
        player_hmac_secret="e2e-private-hmac-secret-" + "s" * 32,
        server_id="livonia-1",
        server_timezone="Europe/Moscow",
        source_name="e2e-local",
        start_at_end=False,
        max_batch_events=100,
        max_batch_bytes=262_144,
        max_read_bytes=1_048_576,
        webhook_timeout=30,
        storage_dir=None,
        storage_check_seconds=300,
        telemetry_stale_seconds=180,
        catalog_file=None,
    )


def local_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def run(webhook: str, key: str, now: datetime) -> dict[str, object]:
    require_loopback(webhook)
    if len(key) < 32:
        raise ValueError("--key must contain at least 32 characters")

    with tempfile.TemporaryDirectory(prefix="dayz-monitor-e2e-") as temp:
        root = Path(temp)
        logs_dir = root / "logs"
        logs_dir.mkdir()
        write_synthetic_adm(logs_dir, now)
        pipeline = EventPipeline(
            config(logs_dir, root / "state" / "events.sqlite3", webhook, key),
            session=local_session(),
            logger=lambda *_: None,
            clock=lambda: now,
        )
        try:
            first = pipeline.poll()
            second = pipeline.poll()
            repeated = {name: second[name] for name in WORK_METRICS if second[name]}
            if repeated:
                raise RuntimeError(f"second poll repeated work: {repeated}")

            rows = pipeline.state.connection.execute(
                "SELECT status, payload FROM events ORDER BY event_id"
            ).fetchall()
            types: Counter[str] = Counter()
            statuses: Counter[str] = Counter()
            serialized = ""
            for row in rows:
                payload = str(row["payload"])
                event = json.loads(payload)
                types[str(event["type"])] += 1
                statuses[str(row["status"])] += 1
                serialized += payload
            if PRIVATE_UID in serialized:
                raise RuntimeError("raw player UID crossed the monitor privacy boundary")
            if types != {"player.connected": 1, "world.snapshot": 2}:
                raise RuntimeError(f"unexpected typed event set: {dict(types)}")
            if statuses != {"delivered": 3}:
                raise RuntimeError(f"unexpected delivery states: {dict(statuses)}")
            return {
                "first_poll": first,
                "second_poll_work": sum(second[name] for name in WORK_METRICS),
                "event_types": dict(sorted(types.items())),
                "event_statuses": dict(sorted(statuses.items())),
                "raw_uid_absent": True,
            }
        finally:
            pipeline.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit a synthetic monitor batch to a loopback E2E sink.")
    parser.add_argument("--webhook", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--now", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run(args.webhook, args.key, parse_now(args.now))
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
