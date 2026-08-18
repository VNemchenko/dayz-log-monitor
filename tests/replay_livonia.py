from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dayz_events.config import EventPipelineConfig
from dayz_events.pipeline import EventPipeline, FILE_DATE_RE


DEFAULT_SOURCE = Path(r"F:\src\livonia")
LOG_PATTERNS = ("DayZServer_*.ADM", "DayZServer_*.RPT", "script_*.log", "error.log")
WORK_METRICS = ("lines", "events", "batches", "delivered", "retried")
EVENT_ID_RE = re.compile(r"evt_[0-9a-f]{32}")


class ReplayFailure(RuntimeError):
    pass


class FakeResponse:
    status_code = 202


class FakeEventSink:
    """Validates deliveries while retaining only non-sensitive aggregate data."""

    def __init__(self, *, expected_key: str, max_events: int, max_bytes: int) -> None:
        self.expected_key = expected_key
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.batch_ids: set[str] = set()
        self.event_ids: set[str] = set()
        self.event_types: Counter[str] = Counter()
        self.batch_count = 0
        self.event_count = 0
        self.total_bytes = 0
        self.max_batch_events = 0
        self.max_batch_bytes = 0

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        if url != "https://replay.invalid/dayz/events/v1":
            raise ReplayFailure("event pipeline attempted an unexpected destination")
        data = kwargs.get("data")
        headers = kwargs.get("headers")
        if not isinstance(data, (bytes, bytearray)) or not isinstance(headers, dict):
            raise ReplayFailure("fake sink received an invalid HTTP request")
        if headers.get("X-DayZ-Key") != self.expected_key:
            raise ReplayFailure("fake sink received an invalid authentication header")
        if len(data) > self.max_bytes:
            raise ReplayFailure("fake sink received an oversized batch")

        body = json.loads(bytes(data).decode("utf-8"))
        if not isinstance(body, dict) or body.get("schema") != "dayz.event-batch.v1":
            raise ReplayFailure("fake sink received an invalid batch envelope")
        batch_id = body.get("batch_id")
        if not isinstance(batch_id, str) or headers.get("Idempotency-Key") != batch_id:
            raise ReplayFailure("fake sink received an unstable idempotency key")
        if batch_id in self.batch_ids:
            raise ReplayFailure("fake sink received a duplicate batch")

        events = body.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= self.max_events:
            raise ReplayFailure("fake sink received an invalid event count")
        for event in events:
            if not isinstance(event, dict):
                raise ReplayFailure("fake sink received a non-object event")
            event_id = event.get("event_id")
            event_type = event.get("type")
            if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
                raise ReplayFailure("fake sink received an invalid event id")
            if event_id in self.event_ids:
                raise ReplayFailure("fake sink received a duplicate event id")
            if not isinstance(event_type, str):
                raise ReplayFailure("fake sink received an invalid event type")
            self.event_ids.add(event_id)
            self.event_types[event_type] += 1

        self.batch_ids.add(batch_id)
        self.batch_count += 1
        self.event_count += len(events)
        self.total_bytes += len(data)
        self.max_batch_events = max(self.max_batch_events, len(events))
        self.max_batch_bytes = max(self.max_batch_bytes, len(data))
        return FakeResponse()

    def close(self) -> None:
        return None


def discover_log_files(directory: Path) -> list[Path]:
    unique: dict[str, Path] = {}
    for pattern in LOG_PATTERNS:
        for path in directory.glob(pattern):
            if path.is_file():
                unique[str(path.resolve())] = path.resolve()
    return sorted(unique.values(), key=lambda path: path.name.casefold())


def resolve_logs_dir(source: Path) -> tuple[Path, list[Path]]:
    source = source.resolve()
    direct = discover_log_files(source) if source.is_dir() else []
    profiles = source / "profiles"
    nested = discover_log_files(profiles) if profiles.is_dir() else []
    if direct and nested:
        raise ReplayFailure("both source and source/profiles contain logs; choose one explicitly")
    if direct:
        return source, direct
    if nested:
        return profiles, nested
    raise ReplayFailure("no ADM/RPT/script/error logs were found")


def snapshot_digest(root: Path, paths: list[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    total_bytes = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\x00")
        total_bytes += size
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\x00")
    return digest.hexdigest(), total_bytes


def file_kind(path: Path) -> str:
    if path.suffix.casefold() == ".adm":
        return "adm"
    if path.suffix.casefold() == ".rpt":
        return "rpt"
    if path.name.casefold() == "error.log":
        return "error"
    return "script"


def replay_clock(paths: list[Path], server_timezone: str) -> datetime:
    zone = ZoneInfo(server_timezone)
    starts: list[datetime] = []
    for path in paths:
        match = FILE_DATE_RE.search(path.name)
        if match:
            year, month, day = map(int, match.group("date").split("-"))
            starts.append(
                datetime(
                    year,
                    month,
                    day,
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.group("second")),
                    tzinfo=zone,
                ).astimezone(timezone.utc)
            )
    if not starts:
        raise ReplayFailure("dated logs are required for a deterministic replay clock")
    return min(starts) - timedelta(days=1)


def pipeline_config(logs_dir: Path, state_db: Path) -> EventPipelineConfig:
    return EventPipelineConfig(
        enabled=True,
        logs_dir=logs_dir,
        state_db=state_db,
        webhook_url="https://replay.invalid/dayz/events/v1",
        webhook_key="replay-webhook-key-v1-" + "k" * 32,
        player_hmac_secret="replay-player-hmac-v1-" + "s" * 32,
        server_id="livonia-replay",
        server_timezone="Europe/Moscow",
        source_name="livonia-replay",
        start_at_end=False,
        max_batch_events=100,
        max_batch_bytes=262_144,
        max_read_bytes=16_777_216,
        webhook_timeout=10,
        storage_dir=None,
        storage_check_seconds=300,
        telemetry_stale_seconds=180,
        catalog_file=None,
    )


def assert_zero_work(metrics: dict[str, int], phase: str) -> None:
    nonzero = {key: metrics.get(key, 0) for key in WORK_METRICS if metrics.get(key, 0)}
    if nonzero:
        raise ReplayFailure(f"{phase} poll repeated work: {nonzero}")


def drain_outbox(pipeline: EventPipeline, now_epoch: float) -> int:
    delivered = 0
    for _ in range(10_000):
        due = pipeline.state.due_outbox(now_epoch)
        if not due:
            return delivered
        result = pipeline._deliver_due(now_epoch)
        if result["retried"] or result["delivered"] == 0:
            raise ReplayFailure("fake sink could not drain the reliable outbox")
        delivered += result["delivered"]
    raise ReplayFailure("outbox drain exceeded its safety bound")


def state_summary(pipeline: EventPipeline, paths: list[Path]) -> dict[str, Any]:
    rows = pipeline.state.connection.execute(
        "SELECT event_id, status, payload FROM events ORDER BY event_id"
    ).fetchall()
    event_ids: list[str] = []
    event_types: Counter[str] = Counter()
    source_kinds: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for row in rows:
        event_id = str(row["event_id"])
        if EVENT_ID_RE.fullmatch(event_id) is None:
            raise ReplayFailure("SQLite contains an invalid event id")
        payload = json.loads(row["payload"])
        event_type = payload.get("type")
        source = payload.get("source")
        source_kind = source.get("kind") if isinstance(source, dict) else None
        if not isinstance(event_type, str) or not isinstance(source_kind, str):
            raise ReplayFailure("SQLite contains an invalid event projection")
        event_ids.append(event_id)
        event_types[event_type] += 1
        source_kinds[source_kind] += 1
        statuses[str(row["status"])] += 1

    if len(event_ids) != len(set(event_ids)):
        raise ReplayFailure("SQLite contains duplicate event ids")
    id_digest = hashlib.sha256("\n".join(event_ids).encode("ascii")).hexdigest()

    cursor_rows = pipeline.state.connection.execute(
        "SELECT path, kind, offset, partial FROM cursors ORDER BY path"
    ).fetchall()
    expected_sizes = {str(path): path.stat().st_size for path in paths}
    cursor_kinds: Counter[str] = Counter()
    partial_cursors = 0
    partial_bytes = 0
    for row in cursor_rows:
        path = str(row["path"])
        if path not in expected_sizes or int(row["offset"]) != expected_sizes[path]:
            raise ReplayFailure("a replay cursor did not reach source EOF")
        cursor_kinds[str(row["kind"])] += 1
        partial = bytes(row["partial"])
        partial_cursors += bool(partial)
        partial_bytes += len(partial)

    aggregates_open = int(
        pipeline.state.connection.execute("SELECT count(*) FROM aggregates").fetchone()[0]
    )
    outbox_open = int(
        pipeline.state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0]
    )
    return {
        "cursor_count": len(cursor_rows),
        "cursor_kinds": dict(sorted(cursor_kinds.items())),
        "partial_cursor_count": partial_cursors,
        "partial_bytes": partial_bytes,
        "event_count": len(event_ids),
        "event_id_set_sha256": id_digest,
        "event_types": dict(sorted(event_types.items())),
        "event_source_kinds": dict(sorted(source_kinds.items())),
        "event_statuses": dict(sorted(statuses.items())),
        "open_aggregates": aggregates_open,
        "open_outbox": outbox_open,
    }


def run_replay(source: Path, expected_files: int) -> dict[str, Any]:
    logs_dir, paths = resolve_logs_dir(source)
    if expected_files > 0 and len(paths) != expected_files:
        raise ReplayFailure(f"expected {expected_files} logs, found {len(paths)}")
    before_digest, source_bytes = snapshot_digest(logs_dir, paths)
    names_before = [path.name for path in paths]
    kind_counts = Counter(file_kind(path) for path in paths)
    # Keep historic events eligible for the fake outbox instead of expiring them
    # against wall-clock time. Source timestamps and stable event IDs are unchanged.
    fixed_now = replay_clock(paths, "Europe/Moscow")

    with tempfile.TemporaryDirectory(prefix="dayz-livonia-replay-") as temp:
        state_db = Path(temp) / "events.sqlite3"
        config = pipeline_config(logs_dir, state_db)
        sink = FakeEventSink(
            expected_key=config.webhook_key,
            max_events=config.max_batch_events,
            max_bytes=config.max_batch_bytes,
        )
        pipeline = EventPipeline(config, session=sink, logger=lambda *_: None, clock=lambda: fixed_now)
        try:
            first_poll = pipeline.poll()
            cursor_times = [
                datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                for row in pipeline.state.connection.execute(
                    "SELECT last_occurred_at FROM cursors WHERE last_occurred_at IS NOT NULL"
                ).fetchall()
            ]
            if not cursor_times:
                raise ReplayFailure("the replay did not parse any dated log lines")
            # Close the final per-player/fingerprint episodes after every source
            # file has reached EOF, then queue them under the deterministic clock.
            flushed_aggregates = pipeline.state.flush_due_aggregates(
                (max(cursor_times) + timedelta(days=1)).timestamp()
            )
            queued_after_flush = pipeline.state.queue_batches(
                server_id=config.server_id,
                source_name=config.source_name,
                max_events=config.max_batch_events,
                max_bytes=config.max_batch_bytes,
                now=fixed_now,
            )
            drained_batches = drain_outbox(pipeline, fixed_now.timestamp())
            baseline = state_summary(pipeline, paths)
            if baseline["cursor_count"] != len(paths):
                raise ReplayFailure("cursor count does not match source file count")
            if baseline["open_aggregates"] or baseline["open_outbox"]:
                raise ReplayFailure("replay left aggregate or outbox state open")
            if baseline["event_statuses"] != {"delivered": baseline["event_count"]}:
                raise ReplayFailure("not every replay event reached the fake sink")
            if sink.event_ids != {
                str(row[0])
                for row in pipeline.state.connection.execute("SELECT event_id FROM events").fetchall()
            }:
                raise ReplayFailure("fake sink and SQLite event id sets differ")

            second_poll = pipeline.poll()
            assert_zero_work(second_poll, "second")
            if state_summary(pipeline, paths) != baseline:
                raise ReplayFailure("second poll changed stable replay state")
        finally:
            pipeline.close()

        reopened = EventPipeline(config, session=sink, logger=lambda *_: None, clock=lambda: fixed_now)
        try:
            reopen_poll = reopened.poll()
            assert_zero_work(reopen_poll, "reopen")
            reopened_summary = state_summary(reopened, paths)
            if reopened_summary != baseline:
                raise ReplayFailure("reopen poll changed stable replay state")
        finally:
            reopened.close()

        paths_after = discover_log_files(logs_dir)
        after_digest, after_bytes = snapshot_digest(logs_dir, paths_after)
        if [path.name for path in paths_after] != names_before:
            raise ReplayFailure("source log set changed during read-only replay")
        if after_digest != before_digest or after_bytes != source_bytes:
            raise ReplayFailure("source log bytes changed during read-only replay")

        return {
            "schema": "dayz.replay-summary.v1",
            "ok": True,
            "source": {
                "file_count": len(paths),
                "file_kinds": dict(sorted(kind_counts.items())),
                "bytes": source_bytes,
                "sha256_before": before_digest,
                "sha256_after": after_digest,
                "unchanged": True,
            },
            "pipeline": {
                "first_poll": first_poll,
                "aggregates_flushed": flushed_aggregates,
                "batches_queued_after_flush": queued_after_flush,
                "batches_drained_after_first_poll": drained_batches,
                "second_poll_work": sum(second_poll[key] for key in WORK_METRICS),
                "reopen_poll_work": sum(reopen_poll[key] for key in WORK_METRICS),
            },
            "state": baseline,
            "fake_sink": {
                "batch_count": sink.batch_count,
                "event_count": sink.event_count,
                "event_types": dict(sorted(sink.event_types.items())),
                "total_bytes": sink.total_bytes,
                "max_batch_events": sink.max_batch_events,
                "max_batch_bytes": sink.max_batch_bytes,
            },
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only full replay of a DayZ Livonia log snapshot."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Livonia root or profiles directory (default: F:\\src\\livonia)",
    )
    parser.add_argument(
        "--expected-files",
        type=int,
        default=283,
        help="exact expected log count; use 0 to accept any positive count",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = run_replay(args.source, args.expected_files)
    except (OSError, ReplayFailure, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "dayz.replay-summary.v1", "ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
