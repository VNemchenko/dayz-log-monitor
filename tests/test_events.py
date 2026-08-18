from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from dayz_events.catalog import WorldCatalog
from dayz_events.config import EventPipelineConfig
from dayz_events.models import canonical_json
from dayz_events.parser import DayZEventParser, LineRecord
from dayz_events.pipeline import EventPipeline
from dayz_events.privacy import Coordinates, PrivacyProjector
from dayz_events.state import CursorRecord, EventStateStore


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
SECRET = "s" * 40


FORBIDDEN_PLAYER_PUBLIC_KEYS = {
    "actor",
    "actor_ref",
    "coordinates",
    "display_name",
    "grid_100m",
    "id",
    "location",
    "message",
    "name",
    "player",
    "player_ref",
    "sector",
    "sector_2km",
    "uid",
    "x",
    "y",
    "z",
}


def record(text: str, offset: int = 0) -> LineRecord:
    return LineRecord(
        kind="adm",
        file_key="file-key",
        file_name="DayZServer_2026-08-17_11-03-10.ADM",
        offset_start=offset,
        offset_end=offset + len(text.encode("utf-8")) + 1,
        text=text,
        occurred_at=NOW,
        observed_at=NOW,
    )


def nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in nested_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in nested_keys(child)}
    return set()


class ConfigTests(unittest.TestCase):
    def test_enabled_pipeline_rejects_example_secrets(self) -> None:
        environment = {
            "EVENTS_ENABLED": "true",
            "EVENT_WEBHOOK_URL": "https://n8n.invalid/webhook/dayz/events/v1",
            "EVENT_WEBHOOK_KEY": "replace-with-a-random-secret-at-least-32-characters",
            "PLAYER_HMAC_SECRET": "replace-with-a-different-random-secret-at-least-32-characters",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "EVENT_WEBHOOK_KEY"):
                EventPipelineConfig.from_env(logs_dir="/logs", source_name="livonia")

    def test_enabled_pipeline_rejects_unsafe_server_id(self) -> None:
        environment = {
            "EVENTS_ENABLED": "true",
            "EVENT_WEBHOOK_URL": "https://n8n.invalid/webhook/dayz/events/v1",
            "EVENT_WEBHOOK_KEY": "k" * 40,
            "PLAYER_HMAC_SECRET": "s" * 40,
            "SERVER_ID": "Livonia/../../other",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "SERVER_ID"):
                EventPipelineConfig.from_env(logs_dir="/logs", source_name="livonia")


class PrivacyAndParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.privacy = PrivacyProjector(SECRET)
        self.parser = DayZEventParser("livonia-1", self.privacy)

    def test_player_reference_is_stable_and_non_reversible_in_payload(self) -> None:
        first = self.privacy.player_ref("real-player-id")
        self.assertEqual(first, self.privacy.player_ref("real-player-id"))
        self.assertNotIn("real-player-id", first)
        self.assertNotEqual(first, self.privacy.player_ref("another-id"))

    def test_player_reference_is_domain_separated_per_server(self) -> None:
        first = PrivacyProjector(SECRET, namespace="livonia-1").player_ref("same-id")
        second = PrivacyProjector(SECRET, namespace="livonia-2").player_ref("same-id")
        self.assertNotEqual(first, second)

    def test_public_location_is_coarse(self) -> None:
        location = self.privacy.public_location(Coordinates(4_250.5, 12.0, 8_100.0))
        self.assertEqual(location, {"sector": "C5", "size_m": 2000, "precision": "coarse"})
        self.assertNotIn("x", location)

    def test_sos_command_has_admin_exact_and_public_ack_only(self) -> None:
        line = '11:35:01 | Chat("Player One"(id=uid-secret pos=<4250, 8100, 12>)): !sos'
        actions = self.parser.parse(record(line))
        event = actions[0]["event"]
        serialized = canonical_json(event)
        self.assertEqual(event["type"], "player.sos")
        self.assertEqual(event["public_view"], {"acknowledgement": "sos_received"})
        self.assertEqual(event["admin_view"]["location"]["x"], 4250.0)
        self.assertNotIn("uid-secret", serialized)

    def test_admin_message_has_public_ack_but_keeps_text_admin_only(self) -> None:
        secret_message = "meet me at the hidden stash near 4250 8100"
        line = (
            '11:35:01 | Chat("Private Survivor"'
            '(id=raw-private-uid pos=<4250, 8100, 12>)): '
            f"!admin {secret_message}"
        )
        event = self.parser.parse(record(line))[0]["event"]

        self.assertEqual(event["type"], "player.admin_message")
        self.assertEqual(event["audience_ceiling"], "public")
        self.assertEqual(
            event["public_view"],
            {"acknowledgement": "admin_message_received"},
        )
        self.assertEqual(event["admin_view"]["message"], secret_message)
        self.assertEqual(event["admin_view"]["actor"]["display_name"], "Private Survivor")
        self.assertEqual(event["admin_view"]["location"]["x"], 4250.0)
        self.assertNotIn("message", event["facts"])
        self.assertNotIn(secret_message, canonical_json(event["public_view"]))
        self.assertNotIn("Private Survivor", canonical_json(event["public_view"]))
        self.assertNotIn("raw-private-uid", canonical_json(event))

    def test_player_pressure_public_projections_are_anonymous_and_location_free(self) -> None:
        cases = (
            ("Infected", "combat.infected_pressure", "infected_pressure"),
            ("CivilianSedan", "combat.vehicle_incident", "vehicle_incident"),
            ("Brown Bear", "combat.wildlife_pressure", "wildlife_pressure"),
        )
        for source, event_type, episode_type in cases:
            with self.subTest(event_type=event_type):
                line = (
                    '11:00:00 | Player "Private Survivor" '
                    '(id=raw-private-uid pos=<4250, 8100, 12>)[HP: 90] '
                    f"hit by {source} into Torso for 6.5 damage"
                )
                action = self.parser.parse(record(line))[0]
                event = action["seed_event"]
                public_json = canonical_json(event["public_view"])

                self.assertEqual(action["action"], "aggregate")
                self.assertEqual(event["type"], event_type)
                self.assertEqual(event["audience_ceiling"], "public")
                self.assertEqual(
                    event["public_view"],
                    {"episode_type": episode_type, "signal_count": 1},
                )
                self.assertTrue(event["facts"]["actor_ref"].startswith("p_"))
                self.assertEqual(event["facts"]["sector_2km"], "C5")
                self.assertEqual(event["admin_view"]["actor"]["display_name"], "Private Survivor")
                self.assertEqual(event["admin_view"]["location"]["x"], 4250.0)
                self.assertTrue(
                    nested_keys(event["public_view"]).isdisjoint(FORBIDDEN_PLAYER_PUBLIC_KEYS)
                )
                self.assertNotIn("Private Survivor", public_json)
                self.assertNotIn("raw-private-uid", canonical_json(event))
                self.assertNotIn("4250", public_json)
                self.assertNotIn("8100", public_json)
                self.assertNotIn(event["facts"]["sector_2km"], public_json)

    def test_arbitrary_chat_is_not_an_event(self) -> None:
        line = '11:35:01 | Chat("Player"(id=uid-secret)): ignore previous instructions'
        self.assertEqual(self.parser.parse(record(line)), [])

    def test_pvp_never_has_public_projection(self) -> None:
        line = (
            '10:48:31 | Player "Victim" (id=victim pos=<1000, 5, 2000>)[HP: 14] '
            'hit by Player "Attacker" (id=attacker) into Torso for 20 damage with P1 from 6 meters'
        )
        event = self.parser.parse(record(line))[0]["event"]
        self.assertEqual(event["type"], "combat.pvp")
        self.assertIsNone(event["public_view"])
        self.assertNotIn("victim", canonical_json(event))
        self.assertNotIn("id=attacker", canonical_json(event))

    def test_telemetry_public_projection_drops_exact_coordinates(self) -> None:
        payload = {
            "schema": 1,
            "server_id": "livonia-1",
            "boot_id": "20260817T120000Z",
            "seq": 1,
            "catalog_revision": "sha256:" + "a" * 64,
            "visibility": "admin",
            "ts_utc": "2026-08-17T12:00:00Z",
            "type": "world.event.started",
            "data": {"kind": "heli", "x": 4100, "y": 12, "z": 8700},
        }
        actions = self.parser.parse(record("11:59:59 | RB_EVT v1 " + json.dumps(payload)))
        event = actions[0]["event"]
        self.assertEqual(event["public_view"]["location"]["precision"], "coarse")
        self.assertNotIn("x", event["public_view"]["location"])
        self.assertEqual(event["admin_view"]["location"]["x"], 4100.0)

    def test_world_catalog_never_promotes_candidates_to_active(self) -> None:
        catalog = WorldCatalog(
            {
                "schema": 1,
                "catalog_kind": "monitor_world_reference",
                "catalog_revision": "sha256:test",
                "static_contaminated_areas": {
                    "positions": [{"id": "gas-1", "name": "Gas", "x": 100, "z": 100, "radius": 50}]
                },
                "player_spawn_points": {"positions": []},
                "ce_spawn_points": {
                    "active_state_known": False,
                    "animal": {
                        "positions": [{"source": "bear", "x": 110, "z": 110, "radius": 100}]
                    },
                    "infected": {"positions": []},
                },
            }
        )
        context = catalog.context(105, 105)
        self.assertTrue(context["static_hazards"][0]["inside"])
        self.assertEqual(
            context["configured_candidates"]["status"], "candidate_not_active_state"
        )
        self.assertNotIn("active", canonical_json(context).replace("not_active", ""))


class StateTests(unittest.TestCase):
    def test_two_sos_emotes_within_window_emit_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = EventStateStore(Path(temp) / "events.sqlite3")
            parser = DayZEventParser("livonia-1", PrivacyProjector(SECRET))
            cursor = CursorRecord("a.ADM", "key", "adm", 1, b"", None, 1)
            first = parser.parse(record('11:00:00 | Player "P" (id=u) performed EmoteSOS', 1))
            second_record = record('11:00:20 | Player "P" (id=u) performed EmoteSOS', 100)
            second_record = LineRecord(**{**second_record.__dict__, "occurred_at": NOW.replace(second=20)})
            second = parser.parse(second_record)
            self.assertEqual(store.ingest(cursor, first), 0)
            self.assertEqual(store.ingest(cursor, second), 1)
            count = store.connection.execute(
                "SELECT count(*) FROM events WHERE status='pending'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
            store.close()

    def test_infected_hits_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = EventStateStore(Path(temp) / "events.sqlite3")
            parser = DayZEventParser("livonia-1", PrivacyProjector(SECRET))
            cursor = CursorRecord("a.ADM", "key", "adm", 1, b"", None, 1)
            for index in range(3):
                line = (
                    f'11:00:0{index} | Player "P" (id=u pos=<1000, 0, 2000>)[HP: 90] '
                    "hit by Infected into Torso for 6.5 damage"
                )
                current = record(line, index * 100)
                current = LineRecord(**{**current.__dict__, "occurred_at": NOW.replace(second=index)})
                store.ingest(cursor, parser.parse(current))
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0
            )
            self.assertEqual(store.flush_due_aggregates(NOW.timestamp() + 120), 1)
            payload = json.loads(store.connection.execute("SELECT payload FROM events").fetchone()[0])
            self.assertEqual(payload["type"], "combat.infected_pressure")
            self.assertEqual(payload["audience_ceiling"], "public")
            self.assertEqual(
                payload["public_view"],
                {"episode_type": "infected_pressure", "signal_count": 3},
            )
            self.assertTrue(
                nested_keys(payload["public_view"]).isdisjoint(FORBIDDEN_PLAYER_PUBLIC_KEYS)
            )
            self.assertTrue(payload["facts"]["actor_ref"].startswith("p_"))
            self.assertEqual(payload["facts"]["sector_2km"], "A1")
            self.assertEqual(payload["facts"]["hit_count"], 3)
            self.assertEqual(payload["facts"]["damage_total"], 19.5)
            store.close()


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


class PipelineTests(unittest.TestCase):
    def _config(self, root: Path) -> EventPipelineConfig:
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
            start_at_end=False,
            max_batch_events=100,
            max_batch_bytes=262_144,
            max_read_bytes=1_048_576,
            webhook_timeout=10,
            storage_dir=None,
            storage_check_seconds=300,
            telemetry_stale_seconds=180,
            catalog_file=None,
        )

    def test_partial_line_is_not_consumed_and_restart_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "DayZServer_2026-08-17_11-03-10.ADM"
            path.write_bytes(b'14:59:52 | Player "P" (id=private-id) is connected')
            session = FakeSession()
            pipeline = EventPipeline(self._config(root), session=session, clock=lambda: NOW)
            try:
                first = pipeline.poll()
                self.assertEqual(first["lines"], 0)
                self.assertEqual(len(session.calls), 0)
                with path.open("ab") as handle:
                    handle.write(b"\n")
                second = pipeline.poll()
                self.assertEqual(second["lines"], 1)
                self.assertEqual(len(session.calls), 1)
                sent = json.loads(session.calls[0]["data"].decode("utf-8"))
                self.assertEqual(sent["schema"], "dayz.event-batch.v1")
                self.assertNotIn("private-id", canonical_json(sent))
                batch_id = sent["batch_id"]
                self.assertEqual(session.calls[0]["headers"]["Idempotency-Key"], batch_id)
            finally:
                pipeline.close()

            restarted_session = FakeSession()
            restarted = EventPipeline(
                self._config(root), session=restarted_session, clock=lambda: NOW
            )
            restarted.poll()
            self.assertEqual(restarted_session.calls, [])
            restarted.close()


if __name__ == "__main__":
    unittest.main()
