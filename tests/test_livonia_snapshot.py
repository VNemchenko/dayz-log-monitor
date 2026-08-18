from __future__ import annotations

import hashlib
import os
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dayz_events.parser import DayZEventParser, LineRecord
from dayz_events.privacy import PrivacyProjector


DEFAULT_SNAPSHOT = Path(r"F:\src\livonia\profiles")


def digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


class LivoniaSnapshotReplayTests(unittest.TestCase):
    def test_adm_snapshot_counts_and_read_only_invariant(self) -> None:
        if os.getenv("RUN_LIVONIA_SNAPSHOT_TEST", "").strip().casefold() not in {
            "1",
            "true",
            "yes",
        }:
            self.skipTest("set RUN_LIVONIA_SNAPSHOT_TEST=1 for the optional snapshot replay")
        snapshot = Path(os.getenv("LIVONIA_SNAPSHOT", str(DEFAULT_SNAPSHOT)))
        files = sorted(snapshot.glob("DayZServer_*.ADM")) if snapshot.is_dir() else []
        if not files:
            self.skipTest("Livonia ADM snapshot is not available")

        before = digest_files(files)
        parser = DayZEventParser("livonia-replay", PrivacyProjector("replay-only-secret-" * 3))
        counts: Counter[str] = Counter()
        action_counts: Counter[str] = Counter()
        placeholder_time = datetime(2026, 8, 17, tzinfo=timezone.utc)

        for path in files:
            offset = 0
            with path.open("rb") as handle:
                for raw_line in handle:
                    text = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
                    record = LineRecord(
                        kind="adm",
                        file_key=path.name,
                        file_name=path.name,
                        offset_start=offset,
                        offset_end=offset + len(raw_line),
                        text=text,
                        occurred_at=placeholder_time,
                        observed_at=placeholder_time,
                    )
                    for action in parser.parse(record):
                        action_counts[action["action"]] += 1
                        if action["action"] == "event":
                            counts[action["event"]["type"]] += 1
                        elif action["action"] == "aggregate":
                            counts[action["seed_event"]["type"]] += 1
                        elif action["action"] == "sos_signal":
                            counts["player.sos_signal"] += 1
                    offset += len(raw_line)

        expected = {
            "player.connected": 524,
            "player.disconnected": 497,
            "combat.infected_pressure": 3299,
            "combat.vehicle_incident": 304,
            "combat.pvp": 55,
            "combat.explosion": 14,
            "player.death": 79,
            "player.suicide": 32,
            "player.unconscious": 70,
            "player.recovered": 28,
            "base.placed": 229,
            "base.built": 40,
            "base.dismantled": 7,
            "player.sos_signal": 7,
        }
        for event_type, expected_count in expected.items():
            self.assertEqual(counts[event_type], expected_count, event_type)
        self.assertEqual(action_counts["player_position"], 5372)
        self.assertEqual(before, digest_files(files), "read-only replay changed source ADM files")


if __name__ == "__main__":
    unittest.main()
