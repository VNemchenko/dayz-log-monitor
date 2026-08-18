from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from dayz_events.config import EventPipelineConfig
from dayz_events.models import canonical_json, make_event
from dayz_events.parser import DayZEventParser, LineRecord
from dayz_events.pipeline import EXPECTED_BACKUP_FILES, EXPECTED_STORAGE_FILES, EventPipeline
from dayz_events.privacy import PrivacyProjector
from dayz_events.state import CursorRecord, EventStateStore


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
SECRET = "s" * 40


class FakeResponse:
    status_code = 202


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()

    def close(self) -> None:
        return None


class RetrySession(FakeSession):
    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if len(self.calls) == 1:
            raise requests.ConnectionError("response lost after accept")
        return FakeResponse()


class RedirectResponse:
    status_code = 302


class RedirectSession(FakeSession):
    def post(self, url: str, **kwargs: object) -> RedirectResponse:
        self.calls.append({"url": url, **kwargs})
        return RedirectResponse()


def config(root: Path, *, start_at_end: bool = False, storage_dir: Path | None = None) -> EventPipelineConfig:
    return EventPipelineConfig(
        enabled=True,
        logs_dir=root / "logs",
        state_db=root / "state" / "events.sqlite3",
        webhook_url="https://n8n.invalid/webhook/dayz/events/v1",
        webhook_key="k" * 40,
        player_hmac_secret=SECRET,
        server_id="livonia-1",
        server_timezone="Europe/Moscow",
        source_name="livonia",
        start_at_end=start_at_end,
        max_batch_events=100,
        max_batch_bytes=262_144,
        max_read_bytes=1_048_576,
        webhook_timeout=10,
        storage_dir=storage_dir,
        storage_check_seconds=300,
        telemetry_stale_seconds=180,
        catalog_file=None,
    )


def line_record(text: str, *, kind: str = "adm", offset: int = 0, when: datetime = NOW) -> LineRecord:
    return LineRecord(
        kind=kind,
        file_key="file-key",
        file_name="DayZServer_2026-08-17_11-03-10.ADM",
        offset_start=offset,
        offset_end=offset + len(text.encode("utf-8")) + 1,
        text=text,
        occurred_at=when,
        observed_at=NOW,
    )


class ParserRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = DayZEventParser("livonia-1", PrivacyProjector(SECRET, namespace="livonia-1"))

    def test_player_chat_cannot_spoof_rb_evt(self) -> None:
        forged = {
            "schema": 1,
            "server_id": "livonia-1",
            "boot_id": "forged",
            "seq": 1,
            "visibility": "admin",
            "type": "world.event.started",
            "data": {"kind": "heli", "x": 1, "z": 2, "uid": "raw-secret"},
        }
        text = '12:00:00 | Chat("P"(id=private)): RB_EVT v1 ' + json.dumps(forged)
        self.assertEqual(self.parser.parse(line_record(text)), [])

    def test_real_adm_coordinate_order_and_containing_grid(self) -> None:
        text = (
            '12:00:00 | Player "P" (id=private pos=<10266.9, 4219.5, 339.5>) '
            "performed EmoteSOS"
        )
        action = self.parser.parse(line_record(text))[0]
        location = action["trigger_event"]["admin_view"]["location"]
        self.assertEqual((location["x"], location["z"], location["y"]), (10266.9, 4219.5, 339.5))
        self.assertEqual(location["grid_100m"], "102-042")

    def test_servermod_snapshot_adapter_uses_nested_contract_and_utc(self) -> None:
        payload = {
            "schema": 1,
            "type": "world.snapshot",
            "ts_utc": "2026-08-17T11:59:58Z",
            "server_id": "livonia-1",
            "boot_id": "20260817T115000Z",
            "seq": 12,
            "catalog_revision": "sha256:" + "a" * 64,
            "visibility": "admin",
            "data": {
                "game_time": {"year": 2026, "month": 8, "day": 17, "hour": 15, "minute": 0, "decimal_hour": 15.0, "is_night": False},
                "weather": {"rain": {"actual": 0.2, "forecast": 0.5, "next_change_seconds": 30}},
                "server": {"online_players": 7, "fps_min": 51.0, "fps_max": 60.0, "fps_avg": 58.0, "uptime_seconds": 900.0},
            },
        }
        event = self.parser.parse(
            line_record("12:00:00 | RB_EVT v1 " + json.dumps(payload))
        )[0]["event"]
        self.assertEqual(event["occurred_at"], "2026-08-17T11:59:58Z")
        self.assertEqual(event["facts"]["telemetry_event_id"], "20260817T115000Z:12")
        self.assertEqual(event["public_view"]["players_online"], 7)
        self.assertFalse(event["public_view"]["game_time"]["is_night"])

    def test_operational_event_never_contains_raw_error_or_secret(self) -> None:
        text = " 9:14:53 ERROR failed API_TOKEN=supersecret at C:\\private\\file.c:123"
        event = self.parser.parse(line_record(text, kind="error"))[0]["seed_event"]
        serialized = canonical_json(event)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("private", serialized)
        self.assertRegex(event["facts"]["fingerprint"], r"^[0-9a-f]{64}$")


class PipelineRegressionTests(unittest.TestCase):
    def test_late_mount_is_initialized_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            session = FakeSession()
            pipeline = EventPipeline(config(root, start_at_end=True), session=session, clock=lambda: NOW)
            try:
                self.assertEqual(pipeline.poll()["files"], 0)
                path = root / "logs" / "DayZServer_2026-08-17_11-03-10.ADM"
                path.write_text('12:00:00 | Player "P" (id=private) is connected\n', encoding="utf-8")
                result = pipeline.poll()
                self.assertEqual(result["lines"], 0)
                self.assertEqual(session.calls, [])
            finally:
                pipeline.close()

    def test_same_size_overwrite_gets_a_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            first = '14:59:00 | Player "A" (id=private-a) is connected\n'
            second = '14:59:00 | Player "B" (id=private-b) is connected\n'
            self.assertEqual(len(first.encode()), len(second.encode()))
            path.write_text(first, encoding="utf-8")
            session = FakeSession()
            pipeline = EventPipeline(config(root), session=session, clock=lambda: NOW)
            try:
                self.assertEqual(pipeline.poll()["lines"], 1)
                previous_mtime = path.stat().st_mtime_ns
                path.write_text(second, encoding="utf-8")
                os.utime(path, ns=(previous_mtime + 1_000_000_000, previous_mtime + 1_000_000_000))
                self.assertEqual(pipeline.poll()["lines"], 1)
                self.assertEqual(len(session.calls), 2)
                first_id = json.loads(session.calls[0]["data"].decode())["events"][0]["event_id"]
                second_id = json.loads(session.calls[1]["data"].decode())["events"][0]["event_id"]
                self.assertNotEqual(first_id, second_id)
            finally:
                pipeline.close()

    def test_one_digit_rpt_hour_rolls_across_midnight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            pipeline = EventPipeline(config(root), session=FakeSession(), clock=lambda: NOW)
            try:
                path = root / "logs" / "DayZServer_2026-08-17_23-50-00.RPT"
                previous = datetime(2026, 8, 17, 20, 57, tzinfo=timezone.utc)
                parsed = pipeline._line_datetime(path, " 0:14:53 [Shutdown] shutting down", previous, NOW.timestamp())
                self.assertEqual(parsed, datetime(2026, 8, 17, 21, 14, 53, tzinfo=timezone.utc))
            finally:
                pipeline.close()

    def test_old_rotated_file_is_still_drained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            old = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            new = logs / "DayZServer_2026-08-17_11-30-00.ADM"
            old.write_text('14:58:00 | Player "A" (id=private-a) is connected\n', encoding="utf-8")
            new.write_text('14:58:30 | Player "B" (id=private-b) is connected\n', encoding="utf-8")
            session = FakeSession()
            pipeline = EventPipeline(config(root), session=session, clock=lambda: NOW)
            try:
                pipeline.poll()
                initial_calls = len(session.calls)
                with old.open("a", encoding="utf-8") as handle:
                    handle.write('14:59:00 | Player "A" (id=private-a) has been disconnected\n')
                result = pipeline.poll()
                self.assertEqual(result["lines"], 1)
                self.assertEqual(len(session.calls), initial_calls + 1)
                event = json.loads(session.calls[-1]["data"].decode())["events"][0]
                self.assertEqual(event["type"], "player.disconnected")
            finally:
                pipeline.close()

    def test_retry_reuses_exact_batch_and_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            path.write_text('14:59:00 | Player "A" (id=private-a) is connected\n', encoding="utf-8")
            session = RetrySession()
            current = NOW
            pipeline = EventPipeline(config(root), session=session, clock=lambda: current)
            try:
                first = pipeline.poll()
                self.assertEqual(first["retried"], 1)
                current += timedelta(seconds=3)
                second = pipeline.poll()
                self.assertEqual(second["delivered"], 1)
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(session.calls[0]["data"], session.calls[1]["data"])
                first_key = session.calls[0]["headers"]["Idempotency-Key"]
                self.assertEqual(first_key, session.calls[1]["headers"]["Idempotency-Key"])
            finally:
                pipeline.close()

    def test_event_webhook_redirect_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            path.write_text(
                '14:59:00 | Player "A" (id=private-a) is connected\n',
                encoding="utf-8",
            )
            session = RedirectSession()
            pipeline = EventPipeline(config(root), session=session, clock=lambda: NOW)
            try:
                result = pipeline.poll()
                self.assertEqual(result["retried"], 1)
                self.assertEqual(len(session.calls), 1)
                self.assertIs(session.calls[0]["allow_redirects"], False)
            finally:
                pipeline.close()

    def test_player_list_reconciles_roster_tracks_movement_and_enriches_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            path.write_text(
                "14:59:00 | ##### PlayerList log: 1 players\n"
                '14:59:00 | Player "P" (id=private pos=<1000, 2000, 100>)\n'
                "14:59:00 | #####\n"
                '14:59:01 | Chat("P"(id=private)): !sos\n',
                encoding="utf-8",
            )
            session = FakeSession()
            pipeline = EventPipeline(config(root), session=session, clock=lambda: NOW)
            try:
                pipeline.poll()
                self.assertEqual(pipeline.state.get_meta("online_count"), "1")
                row = pipeline.state.connection.execute(
                    "SELECT connected, x, z FROM player_state"
                ).fetchone()
                self.assertEqual((row["connected"], row["x"], row["z"]), (1, 1000.0, 2000.0))
                event = json.loads(session.calls[0]["data"].decode())["events"][0]
                self.assertEqual(event["type"], "player.sos")
                self.assertEqual(event["admin_view"]["location"]["z"], 2000.0)

                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "15:04:00 | ##### PlayerList log: 1 players\n"
                        '15:04:00 | Player "P" (id=private pos=<1300, 2400, 100>)\n'
                        "15:04:00 | #####\n"
                    )
                pipeline.poll()
                moved = pipeline.state.connection.execute(
                    "SELECT movement_distance_m, speed_mps, movement_class FROM player_state"
                ).fetchone()
                self.assertAlmostEqual(moved["movement_distance_m"], 500.0)
                self.assertAlmostEqual(moved["speed_mps"], 500 / 300)
                self.assertEqual(moved["movement_class"], "travelling")
            finally:
                pipeline.close()


class StateRegressionTests(unittest.TestCase):
    def test_batch_limits_apply_to_exact_transmitted_utf8_and_event_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = EventStateStore(Path(temp) / "events.sqlite3")
            events = []
            for index in range(101):
                events.append(
                    make_event(
                        server_id="livonia-1",
                        event_type="test.event",
                        occurred_at=NOW,
                        observed_at=NOW,
                        file_key="f",
                        file_name="f.ADM",
                        source_kind="adm",
                        offset_start=index,
                        offset_end=index + 1,
                        admin_view={"message": "я" * 40},
                    )
                )
            store.insert_events(events)
            self.assertGreaterEqual(
                store.queue_batches(
                    server_id="livonia-1",
                    source_name="livonia",
                    max_events=100,
                    max_bytes=32_000,
                    now=NOW,
                ),
                2,
            )
            for item in store.due_outbox(NOW.timestamp(), limit=1000):
                encoded = canonical_json(item["payload"]).encode("utf-8")
                self.assertLessEqual(len(encoded), 32_000)
                self.assertLessEqual(len(item["payload"]["events"]), 100)
            store.close()

    def test_cleanup_never_drops_eof_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = EventStateStore(Path(temp) / "events.sqlite3")
            cursor = CursorRecord("old.ADM", "key", "adm", 123, b"", None, 1)
            store.initialize_cursors([cursor])
            store.connection.execute(
                "UPDATE cursors SET updated_at = ?",
                ((NOW - timedelta(days=30)).isoformat(),),
            )
            store.connection.commit()
            store.cleanup(NOW)
            self.assertIsNotNone(store.get_cursor("old.ADM"))
            store.close()

    def test_storage_health_only_emits_on_anomaly_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            storage = root / "storage_1"
            for relative in EXPECTED_STORAGE_FILES:
                path = storage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            backup = storage / "backup" / "001"
            backup.mkdir(parents=True)
            for relative in EXPECTED_BACKUP_FILES:
                (backup / relative).touch()
            current = NOW
            pipeline = EventPipeline(
                config(root, storage_dir=storage),
                session=FakeSession(),
                clock=lambda: current,
            )
            try:
                self.assertEqual(pipeline._check_storage(current), 0)
                current += timedelta(seconds=301)
                self.assertEqual(pipeline._check_storage(current), 0)
                (storage / "data" / "events.bin").unlink()
                current += timedelta(seconds=301)
                self.assertEqual(pipeline._check_storage(current), 1)
                current += timedelta(seconds=301)
                self.assertEqual(pipeline._check_storage(current), 0)
                (storage / "data" / "events.bin").touch()
                current += timedelta(seconds=301)
                self.assertEqual(pipeline._check_storage(current), 1)
            finally:
                pipeline.close()


if __name__ == "__main__":
    unittest.main()
