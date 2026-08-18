from __future__ import annotations

import json
import hashlib
import unittest
from pathlib import Path


SERVERMOD_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVERMOD_ROOT / "source" / "RB_Telemetry"


class ServerModContractTests(unittest.TestCase):
    def test_servermod_source_has_no_world_mutation_calls(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCE_ROOT.rglob("*.c"))
        )
        forbidden = (
            "GetCEApi(",
            "SpawnDE(",
            "EconomyOutput(",
            "CreateObject(",
            "CreateObjectEx(",
            "ObjectDelete(",
            "MissionWeather(",
            "SetDate(",
            "SetDecayState(",
            "OpenFile(",
            "FPrint(",
            "FPrintln(",
            "SaveFile(",
            "GetRestApi(",
        )
        for call in forbidden:
            with self.subTest(call=call):
                self.assertNotIn(call, source)

    def test_admin_log_contract_is_present(self) -> None:
        writer = (
            SOURCE_ROOT / "scripts" / "5_Mission" / "RBAdminLogWriter.c"
        ).read_text(encoding="utf-8")
        self.assertIn('AdminLog("RB_EVT v1 " + serialized)', writer)
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCE_ROOT.rglob("*.c"))
        )
        for event_type in (
            "telemetry.started",
            "telemetry.stopped",
            "telemetry.error",
            "world.snapshot",
            "world.event.present",
            "world.event.started",
            "world.event.updated",
            "world.event.ended",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(f'"{event_type}"', source)

    def test_generated_runtime_config_is_schema_one(self) -> None:
        path = SERVERMOD_ROOT / "config" / "livonia.generated.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["server_id"], "livonia-1")
        self.assertEqual(len(document["event_points"]), 177)
        canonical = json.dumps(
            document["event_points"],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            document["catalog_revision"],
            f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        )

    def test_monitor_catalog_never_claims_ce_candidates_are_active(self) -> None:
        path = SERVERMOD_ROOT / "config" / "livonia.world.generated.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["catalog_kind"], "monitor_world_reference")
        self.assertEqual(document["static_contaminated_areas"]["count"], 8)
        self.assertEqual(document["player_spawn_points"]["count"], 151)
        self.assertEqual(document["ce_spawn_points"]["animal"]["count"], 1925)
        self.assertEqual(document["ce_spawn_points"]["infected"]["count"], 328)
        self.assertFalse(document["ce_spawn_points"]["active_state_known"])
        self.assertTrue(
            all(
                point["status"] == "candidate"
                for point in document["player_spawn_points"]["positions"]
            )
        )
        for category in ("animal", "infected"):
            self.assertTrue(
                all(
                    point["status"] == "candidate"
                    for point in document["ce_spawn_points"][category]["positions"]
                )
            )
        sections = {
            "static_contaminated_areas": document["static_contaminated_areas"],
            "player_spawn_points": document["player_spawn_points"],
            "ce_spawn_points": document["ce_spawn_points"],
        }
        canonical = json.dumps(
            sections,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            document["catalog_revision"],
            f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        )

    def test_contamination_bridge_is_read_only(self) -> None:
        bridge = (
            SOURCE_ROOT / "scripts" / "4_World" / "RBContaminationAccess.c"
        ).read_text(encoding="utf-8")
        self.assertIn("return m_DecayState;", bridge)
        self.assertNotIn("m_DecayState =", bridge)

    def test_mission_init_is_not_part_of_servermod(self) -> None:
        names = {path.name.lower() for path in SERVERMOD_ROOT.rglob("*")}
        self.assertNotIn("init.c", names)

    def test_scanner_source_encodes_present_two_misses_ended_then_started(self) -> None:
        scanner = (
            SOURCE_ROOT / "scripts" / "5_Mission" / "RBEventScanner.c"
        ).read_text(encoding="utf-8")
        for contract_fragment in (
            "state.missing_scans++;",
            "state.missing_scans < m_Config.missing_scans_to_end",
            '"world.event.present"',
            '"world.event.started"',
            '"world.event.ended"',
        ):
            self.assertIn(contract_fragment, scanner)

        initialized = False
        present = False
        misses = 0
        emitted: list[str] = []

        def observe(found: bool) -> None:
            nonlocal initialized, present, misses
            if not found:
                if not initialized:
                    initialized = True
                    present = False
                elif present:
                    misses += 1
                    if misses >= 2:
                        emitted.append("ended")
                        present = False
                        misses = 0
                return
            misses = 0
            if not initialized:
                initialized = True
                present = True
                emitted.append("present")
            elif not present:
                present = True
                emitted.append("started")

        observe(True)
        observe(False)
        observe(False)
        observe(True)
        self.assertEqual(emitted, ["present", "ended", "started"])


if __name__ == "__main__":
    unittest.main()
