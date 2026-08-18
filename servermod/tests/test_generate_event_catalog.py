from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


SERVERMOD_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = SERVERMOD_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from generate_event_catalog import (  # noqa: E402
    CatalogError,
    build_catalog,
    build_monitor_catalog,
    main,
    render_catalog,
)


EVENTS_XML = """<?xml version="1.0"?>
<events>
  <event name="StaticHeliCrash">
    <active>1</active>
    <children><child type="Wreck_Mi8_Crashed"/></children>
  </event>
  <event name="StaticMilitaryConvoy">
    <active>1</active><children/>
  </event>
  <event name="StaticTrain">
    <active>0</active><children/>
  </event>
  <event name="StaticPoliceSituation">
    <active>0</active><children/>
  </event>
  <event name="StaticContaminatedArea">
    <active>1</active>
    <children><child type="ContaminatedArea_Dynamic"/></children>
  </event>
</events>
"""

SPAWNS_XML = """<?xml version="1.0"?>
<eventposdef>
  <event name="StaticHeliCrash">
    <pos x="200" z="300"/><pos x="100" z="400"/>
  </event>
  <event name="StaticMilitaryConvoy">
    <pos x="500" z="600" group="Convoy_One"/>
  </event>
  <event name="StaticTrain"><pos x="700" z="800" group="Train_One"/></event>
  <event name="StaticPoliceSituation"><pos x="900" z="1000" group="Police_One"/></event>
  <event name="StaticContaminatedArea"><pos x="1100" z="1200"/></event>
</eventposdef>
"""

GROUPS_XML = """<?xml version="1.0"?>
<eventgroupdef>
  <group name="Convoy_One">
    <child type="StaticObj_Wreck_Decal_F" x="0" z="0"/>
    <child type="ConvoyAnchor" x="5" z="5" deloot="1"/>
  </group>
  <group name="Train_One"><child type="TrainAnchor" x="0" z="0"/></group>
  <group name="Police_One"><child type="PoliceAnchor" x="0" z="0"/></group>
</eventgroupdef>
"""

EFFECT_AREAS_JSON = """{
  "Areas": [
    {
      "AreaName": "Static One",
      "Type": "ContaminatedArea_Static",
      "TriggerType": "ContaminatedTrigger",
      "Data": {"Pos": [10, 0, 20], "Radius": 100, "PosHeight": 30, "NegHeight": 10}
    },
    {
      "AreaName": "Dynamic Template",
      "Type": "ContaminatedArea_Dynamic",
      "Data": {"Pos": [30, 0, 40], "Radius": 50, "PosHeight": 10, "NegHeight": 10}
    }
  ]
}"""

PLAYER_SPAWNS_XML = """<?xml version="1.0"?>
<playerspawnpoints>
  <fresh><generator_posbubbles><group name="Coast">
    <pos x="20" z="30"/><pos x="10" z="40"/>
  </group></generator_posbubbles></fresh>
  <hop><generator_posbubbles><group name="North">
    <pos x="50" z="60"/>
  </group></generator_posbubbles></hop>
</playerspawnpoints>
"""

ENVIRONMENT_XML = """<?xml version="1.0"?>
<env><territories>
  <file path="env/bear_territories.xml"/>
  <file path="env/zombie_territories.xml"/>
</territories></env>
"""

BEAR_TERRITORIES_XML = """<?xml version="1.0"?>
<territory-type><territory color="1">
  <zone name="Graze" smin="0" smax="1" dmin="2" dmax="3" x="100" z="200" r="50"/>
  <zone name="Graze" smin="0" smax="1" dmin="2" dmax="3" x="300" z="400" r="60"/>
</territory></territory-type>
"""

ZOMBIE_TERRITORIES_XML = """<?xml version="1.0"?>
<territory-type><territory color="2">
  <zone name="InfectedCity" smin="0" smax="0" dmin="5" dmax="10" x="500" z="600" r="70"/>
</territory></territory-type>
"""


class CatalogFixture:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "db").mkdir()
        (self.root / "db" / "events.xml").write_text(EVENTS_XML, encoding="utf-8")
        (self.root / "cfgeventspawns.xml").write_text(
            SPAWNS_XML, encoding="utf-8"
        )
        (self.root / "cfgeventgroups.xml").write_text(
            GROUPS_XML, encoding="utf-8"
        )
        (self.root / "cfgeffectarea.json").write_text(
            EFFECT_AREAS_JSON, encoding="utf-8"
        )
        (self.root / "cfgplayerspawnpoints.xml").write_text(
            PLAYER_SPAWNS_XML, encoding="utf-8"
        )
        (self.root / "cfgenvironment.xml").write_text(
            ENVIRONMENT_XML, encoding="utf-8"
        )
        (self.root / "env").mkdir()
        (self.root / "env" / "bear_territories.xml").write_text(
            BEAR_TERRITORIES_XML, encoding="utf-8"
        )
        (self.root / "env" / "zombie_territories.xml").write_text(
            ZOMBIE_TERRITORIES_XML, encoding="utf-8"
        )

    def close(self) -> None:
        self._temporary.cleanup()


class GenerateEventCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CatalogFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_builds_direct_and_group_points_and_skips_inactive_events(self) -> None:
        catalog = build_catalog(self.fixture.root, server_id="test-server")

        self.assertEqual(catalog["schema"], 1)
        self.assertEqual(catalog["server_id"], "test-server")
        points = catalog["event_points"]
        self.assertEqual(len(points), 4)
        self.assertEqual(
            Counter(point["kind"] for point in points),
            {
                "heli_crash": 2,
                "military_convoy": 1,
                "contaminated_area": 1,
            },
        )
        convoy = next(point for point in points if point["kind"] == "military_convoy")
        self.assertEqual(convoy["anchor_types"], ["ConvoyAnchor"])
        self.assertEqual(convoy["radius"], 11.071)
        self.assertEqual(convoy["id"], "military_convoy:convoy-one")
        heli = [point for point in points if point["kind"] == "heli_crash"]
        self.assertEqual([point["x"] for point in heli], [100.0, 200.0])

    def test_output_and_revision_are_deterministic(self) -> None:
        first = render_catalog(build_catalog(self.fixture.root))
        second = render_catalog(build_catalog(self.fixture.root))
        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertRegex(document["catalog_revision"], r"^sha256:[0-9a-f]{64}$")

    def test_monitor_catalog_separates_configured_and_candidate_positions(self) -> None:
        runtime_revision = build_catalog(self.fixture.root)["catalog_revision"]
        first = build_monitor_catalog(self.fixture.root, server_id="test-server")
        second = build_monitor_catalog(self.fixture.root, server_id="test-server")

        self.assertEqual(render_catalog(first), render_catalog(second))
        self.assertEqual(first["catalog_kind"], "monitor_world_reference")
        self.assertEqual(first["static_contaminated_areas"]["count"], 1)
        self.assertEqual(first["player_spawn_points"]["count"], 3)
        self.assertEqual(first["ce_spawn_points"]["animal"]["count"], 2)
        self.assertEqual(first["ce_spawn_points"]["infected"]["count"], 1)
        self.assertFalse(first["ce_spawn_points"]["active_state_known"])
        self.assertTrue(
            all(
                point["status"] == "candidate"
                for point in first["ce_spawn_points"]["animal"]["positions"]
            )
        )
        (self.fixture.root / "cfgeffectarea.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            build_catalog(self.fixture.root)["catalog_revision"], runtime_revision
        )

    def test_unknown_group_fails_closed(self) -> None:
        spawn_path = self.fixture.root / "cfgeventspawns.xml"
        spawn_path.write_text(
            SPAWNS_XML.replace("Convoy_One", "Missing_Group", 1), encoding="utf-8"
        )
        with self.assertRaisesRegex(CatalogError, "unknown event group"):
            build_catalog(self.fixture.root)

    def test_group_without_typed_children_fails_closed(self) -> None:
        group_path = self.fixture.root / "cfgeventgroups.xml"
        group_path.write_text(
            GROUPS_XML.replace(
                '<child type="ConvoyAnchor" x="5" z="5" deloot="1"/>',
                '<child x="5" z="5"/>',
            ).replace(
                '<child type="StaticObj_Wreck_Decal_F" x="0" z="0"/>',
                '<child x="0" z="0"/>',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CatalogError, "no typed child"):
            build_catalog(self.fixture.root)

    def test_check_mode_detects_current_and_stale_output(self) -> None:
        output = self.fixture.root / "catalog.json"
        monitor_output = self.fixture.root / "monitor.json"
        self.assertEqual(
            main(
                [
                    "--mission-dir",
                    str(self.fixture.root),
                    "--output",
                    str(output),
                    "--monitor-output",
                    str(monitor_output),
                ]
            ),
            0,
        )
        self.assertEqual(
            main(
                [
                    "--mission-dir",
                    str(self.fixture.root),
                    "--output",
                    str(output),
                    "--monitor-output",
                    str(monitor_output),
                    "--check",
                ]
            ),
            0,
        )
        monitor_output.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            main(
                [
                    "--mission-dir",
                    str(self.fixture.root),
                    "--output",
                    str(output),
                    "--monitor-output",
                    str(monitor_output),
                    "--check",
                ]
            ),
            1,
        )
        self.assertEqual(
            main(
                [
                    "--mission-dir",
                    str(self.fixture.root),
                    "--output",
                    str(output),
                    "--monitor-output",
                    str(monitor_output),
                ]
            ),
            0,
        )
        output.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            main(
                [
                    "--mission-dir",
                    str(self.fixture.root),
                    "--output",
                    str(output),
                    "--monitor-output",
                    str(monitor_output),
                    "--check",
                ]
            ),
            1,
        )

    def test_real_livonia_snapshot_has_expected_catalog_shape(self) -> None:
        mission = Path(r"F:\src\livonia\mpmissions\enoch_rb.enoch")
        if not mission.is_dir():
            self.skipTest("the read-only Livonia snapshot is not available")

        catalog = build_catalog(mission)
        counts = Counter(point["kind"] for point in catalog["event_points"])
        self.assertEqual(len(catalog["event_points"]), 177)
        self.assertEqual(
            counts,
            {
                "heli_crash": 79,
                "military_convoy": 14,
                "train": 14,
                "police_situation": 33,
                "contaminated_area": 37,
            },
        )

        monitor = build_monitor_catalog(mission)
        self.assertEqual(monitor["static_contaminated_areas"]["count"], 8)
        self.assertEqual(monitor["player_spawn_points"]["count"], 151)
        self.assertEqual(monitor["ce_spawn_points"]["animal"]["count"], 1925)
        self.assertEqual(monitor["ce_spawn_points"]["infected"]["count"], 328)
        self.assertFalse(monitor["ce_spawn_points"]["active_state_known"])


if __name__ == "__main__":
    unittest.main()
