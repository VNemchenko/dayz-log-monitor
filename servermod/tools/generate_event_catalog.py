#!/usr/bin/env python3
"""Generate a deterministic, read-only DayZ dynamic-event observation catalog.

The generator never reads persistence binaries.  It combines the public mission
configuration files that describe enabled events, their possible positions and
a stable anchor object associated with each event group.

An optional, separate monitor-facing catalog can also be generated from public
mission configuration.  Its CE territory and player-spawn positions are
explicitly labelled as candidates; they are not runtime-active-state claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
DEFAULT_SERVER_ID = "livonia-1"
DEFAULT_EVENT_NAMES = (
    "StaticHeliCrash",
    "StaticMilitaryConvoy",
    "StaticTrain",
    "StaticPoliceSituation",
    "StaticContaminatedArea",
)
MONITOR_CATALOG_KIND = "monitor_world_reference"
EVENT_KINDS = {
    "StaticHeliCrash": "heli_crash",
    "StaticMilitaryConvoy": "military_convoy",
    "StaticTrain": "train",
    "StaticPoliceSituation": "police_situation",
    "StaticContaminatedArea": "contaminated_area",
}


class CatalogError(RuntimeError):
    """Mission XML is incomplete or ambiguous for safe observation."""


@dataclass(frozen=True)
class GroupDefinition:
    name: str
    anchor_type: str
    anchor_distance: float


def _parse_xml(path: Path) -> ET.Element:
    if not path.is_file():
        raise CatalogError(f"required mission file does not exist: {path}")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise CatalogError(f"invalid XML in {path}: {exc}") from exc


def _parse_json(path: Path) -> object:
    if not path.is_file():
        raise CatalogError(f"required mission file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CatalogError(f"invalid JSON in {path}: {exc}") from exc


def _float_attr(element: ET.Element, name: str, context: str) -> float:
    raw = element.get(name)
    if raw is None:
        raise CatalogError(f"{context} is missing attribute {name!r}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise CatalogError(f"{context} has invalid {name}={raw!r}") from exc
    if not (-20_000.0 <= value <= 20_000.0):
        raise CatalogError(f"{context} has out-of-bounds {name}={value}")
    return value


def _event_nodes(root: ET.Element) -> dict[str, ET.Element]:
    nodes: dict[str, ET.Element] = {}
    for node in root.findall("./event"):
        name = node.get("name", "").strip()
        if not name:
            raise CatalogError("events.xml contains an event without a name")
        if name in nodes:
            raise CatalogError(f"events.xml contains duplicate event {name!r}")
        nodes[name] = node
    return nodes


def _spawn_nodes(root: ET.Element) -> dict[str, ET.Element]:
    nodes: dict[str, ET.Element] = {}
    for node in root.findall("./event"):
        name = node.get("name", "").strip()
        if not name:
            raise CatalogError("cfgeventspawns.xml contains an event without a name")
        if name in nodes:
            raise CatalogError(
                f"cfgeventspawns.xml contains duplicate event {name!r}"
            )
        nodes[name] = node
    return nodes


def _group_definitions(root: ET.Element) -> dict[str, GroupDefinition]:
    definitions: dict[str, GroupDefinition] = {}
    for group in root.findall("./group"):
        name = group.get("name", "").strip()
        if not name:
            raise CatalogError("cfgeventgroups.xml contains a group without a name")
        if name in definitions:
            raise CatalogError(f"duplicate event group {name!r}")

        candidates: list[tuple[int, float, int, str]] = []
        for index, child in enumerate(group.findall("./child")):
            anchor_type = child.get("type", "").strip()
            if not anchor_type:
                continue
            try:
                x = float(child.get("x", "0"))
                z = float(child.get("z", "0"))
            except ValueError as exc:
                raise CatalogError(f"group {name!r} contains invalid child offsets") from exc
            distance = math.hypot(x, z)
            # Prefer CE loot-bearing children.  They are stable event objects,
            # whereas some groups put a decorative decal at their origin.
            # When the attribute is absent, prefer a non-decal object, then
            # fall back to the nearest typed child.  Document order is the
            # final deterministic tie-breaker.
            if child.get("deloot") is not None:
                priority = 0
            elif "decal" not in anchor_type.lower():
                priority = 1
            else:
                priority = 2
            candidates.append((priority, distance, index, anchor_type))

        if not candidates:
            raise CatalogError(f"group {name!r} has no typed child to observe")

        _, anchor_distance, _, anchor_type = min(candidates)
        if anchor_distance > 56.0:
            raise CatalogError(
                f"group {name!r} anchor {anchor_type!r} is too far from its "
                f"event position ({anchor_distance:.3f} m)"
            )
        definitions[name] = GroupDefinition(
            name=name,
            anchor_type=anchor_type,
            anchor_distance=anchor_distance,
        )
    return definitions


def _direct_anchor_types(event: ET.Element, event_name: str) -> list[str]:
    anchors = sorted(
        {
            child.get("type", "").strip()
            for child in event.findall("./children/child")
            if child.get("type", "").strip()
        }
    )
    if not anchors:
        raise CatalogError(
            f"event {event_name!r} has direct positions but no child anchor types"
        )
    return anchors


def _active(event: ET.Element, event_name: str) -> bool:
    raw = (event.findtext("active") or "").strip()
    if raw not in {"0", "1"}:
        raise CatalogError(f"event {event_name!r} has invalid active={raw!r}")
    return raw == "1"


def _safe_slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in value]
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _point_id(kind: str, group_name: str, ordinal: int) -> str:
    if group_name:
        return f"{kind}:{_safe_slug(group_name)}"
    return f"{kind}:{ordinal:03d}"


def _canonical_catalog_bytes(points: Sequence[dict[str, object]]) -> bytes:
    return json.dumps(
        points,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_catalog(
    mission_dir: Path,
    server_id: str = DEFAULT_SERVER_ID,
    event_names: Iterable[str] = DEFAULT_EVENT_NAMES,
) -> dict[str, Any]:
    """Build the runtime configuration without modifying ``mission_dir``."""

    mission_dir = mission_dir.resolve()
    event_root = _parse_xml(mission_dir / "db" / "events.xml")
    spawn_root = _parse_xml(mission_dir / "cfgeventspawns.xml")
    group_root = _parse_xml(mission_dir / "cfgeventgroups.xml")

    events = _event_nodes(event_root)
    spawns = _spawn_nodes(spawn_root)
    groups = _group_definitions(group_root)
    points: list[dict[str, object]] = []

    for event_name in event_names:
        if event_name not in EVENT_KINDS:
            raise CatalogError(f"no stable kind mapping for event {event_name!r}")
        event = events.get(event_name)
        spawn = spawns.get(event_name)
        if event is None:
            raise CatalogError(f"events.xml is missing event {event_name!r}")
        if spawn is None:
            raise CatalogError(
                f"cfgeventspawns.xml is missing event {event_name!r}"
            )
        if not _active(event, event_name):
            continue

        kind = EVENT_KINDS[event_name]
        raw_positions = list(spawn.findall("./pos"))
        normalized: list[tuple[float, float, str, float, list[str]]] = []
        direct_anchors: list[str] | None = None

        for index, position in enumerate(raw_positions):
            context = f"{event_name} position #{index}"
            x = _float_attr(position, "x", context)
            z = _float_attr(position, "z", context)
            group_name = position.get("group", "").strip()
            if group_name:
                definition = groups.get(group_name)
                if definition is None:
                    raise CatalogError(
                        f"{context} references unknown event group {group_name!r}"
                    )
                anchors = [definition.anchor_type]
                radius = max(8.0, round(definition.anchor_distance + 4.0, 3))
            else:
                if direct_anchors is None:
                    direct_anchors = _direct_anchor_types(event, event_name)
                anchors = direct_anchors
                radius = 8.0
            normalized.append((x, z, group_name, radius, anchors))

        normalized.sort(key=lambda item: (item[0], item[1], item[2]))
        for ordinal, (x, z, group_name, radius, anchors) in enumerate(normalized):
            points.append(
                {
                    "id": _point_id(kind, group_name, ordinal),
                    "kind": kind,
                    "event_name": event_name,
                    "group_name": group_name,
                    "x": x,
                    "z": z,
                    "radius": radius,
                    "anchor_types": anchors,
                }
            )

    ids = [str(point["id"]) for point in points]
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise CatalogError(f"duplicate generated point ids: {', '.join(duplicates)}")
    if not points:
        raise CatalogError("no active event observation points were generated")

    revision = hashlib.sha256(_canonical_catalog_bytes(points)).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "server_id": server_id,
        "catalog_revision": f"sha256:{revision}",
        "snapshot_interval_ms": 60_000,
        "snapshot_start_delay_ms": 10_000,
        "scanner_start_delay_ms": 60_000,
        "scan_tick_ms": 1_000,
        "scan_batch_size": 4,
        "missing_scans_to_end": 2,
        "event_points": points,
    }


def _json_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{context} must be numeric")
    result = float(value)
    if not (-20_000.0 <= result <= 20_000.0):
        raise CatalogError(f"{context} is out of bounds: {result}")
    return result


def _static_contaminated_areas(mission_dir: Path) -> dict[str, object]:
    document = _parse_json(mission_dir / "cfgeffectarea.json")
    if not isinstance(document, dict) or not isinstance(document.get("Areas"), list):
        raise CatalogError("cfgeffectarea.json must contain an Areas array")

    positions: list[dict[str, object]] = []
    for index, raw_area in enumerate(document["Areas"]):
        context = f"cfgeffectarea.json Areas[{index}]"
        if not isinstance(raw_area, dict):
            raise CatalogError(f"{context} must be an object")
        area_type = str(raw_area.get("Type", "")).strip()
        if area_type != "ContaminatedArea_Static":
            continue
        name = str(raw_area.get("AreaName", "")).strip()
        data = raw_area.get("Data")
        if not name or not isinstance(data, dict):
            raise CatalogError(f"{context} has no AreaName or Data object")
        raw_position = data.get("Pos")
        if not isinstance(raw_position, list) or len(raw_position) != 3:
            raise CatalogError(f"{context} Data.Pos must contain x, y and z")
        positions.append(
            {
                "id": f"static_contaminated_area:{_safe_slug(name)}",
                "status": "configured_static",
                "name": name,
                "type": area_type,
                "trigger_type": str(raw_area.get("TriggerType", "")),
                "x": _json_number(raw_position[0], f"{context} Data.Pos[0]"),
                "y": _json_number(raw_position[1], f"{context} Data.Pos[1]"),
                "z": _json_number(raw_position[2], f"{context} Data.Pos[2]"),
                "radius": _json_number(data.get("Radius"), f"{context} Data.Radius"),
                "positive_height": _json_number(
                    data.get("PosHeight"), f"{context} Data.PosHeight"
                ),
                "negative_height": _json_number(
                    data.get("NegHeight"), f"{context} Data.NegHeight"
                ),
            }
        )
    positions.sort(key=lambda item: (str(item["name"]), item["x"], item["z"]))
    ids = [str(point["id"]) for point in positions]
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise CatalogError(
            "duplicate static contaminated area ids: " + ", ".join(duplicates)
        )
    return {
        "semantics": "configured_static_area_not_runtime_presence",
        "count": len(positions),
        "positions": positions,
    }


def _player_spawn_candidates(mission_dir: Path) -> dict[str, object]:
    root = _parse_xml(mission_dir / "cfgplayerspawnpoints.xml")
    raw_points: list[tuple[str, str, float, float]] = []
    for mode in list(root):
        mode_name = mode.tag.strip()
        generator = mode.find("./generator_posbubbles")
        if not mode_name or generator is None:
            continue
        for group in generator.findall("./group"):
            group_name = group.get("name", "").strip()
            if not group_name:
                raise CatalogError(
                    f"cfgplayerspawnpoints.xml mode {mode_name!r} has an unnamed group"
                )
            for index, position in enumerate(group.findall("./pos")):
                context = f"player spawn {mode_name}/{group_name} #{index}"
                raw_points.append(
                    (
                        mode_name,
                        group_name,
                        _float_attr(position, "x", context),
                        _float_attr(position, "z", context),
                    )
                )

    raw_points.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    ordinals: Counter[tuple[str, str]] = Counter()
    positions: list[dict[str, object]] = []
    for mode_name, group_name, x, z in raw_points:
        key = (mode_name, group_name)
        ordinal = ordinals[key]
        ordinals[key] += 1
        positions.append(
            {
                "id": f"player_spawn:{_safe_slug(mode_name)}:{_safe_slug(group_name)}:{ordinal:03d}",
                "status": "candidate",
                "mode": mode_name,
                "group": group_name,
                "x": x,
                "z": z,
            }
        )

    mode_counts = Counter(str(point["mode"]) for point in positions)
    return {
        "semantics": "configured_generator_candidate_not_selected_spawn",
        "count": len(positions),
        "counts_by_mode": {
            name: mode_counts[name] for name in sorted(mode_counts)
        },
        "positions": positions,
    }


def _resolve_mission_file(mission_dir: Path, relative_path: str) -> Path:
    resolved = (mission_dir / relative_path).resolve()
    try:
        resolved.relative_to(mission_dir)
    except ValueError as exc:
        raise CatalogError(
            f"cfgenvironment.xml path escapes mission directory: {relative_path!r}"
        ) from exc
    return resolved


def _territory_candidates(mission_dir: Path) -> dict[str, object]:
    environment = _parse_xml(mission_dir / "cfgenvironment.xml")
    relative_paths = sorted(
        {
            node.get("path", "").strip()
            for node in environment.findall("./territories/file")
            if node.get("path", "").strip()
        }
    )
    if not relative_paths:
        raise CatalogError("cfgenvironment.xml has no territory files")

    categories: dict[str, list[dict[str, object]]] = {
        "animal": [],
        "infected": [],
    }
    for relative_path in relative_paths:
        source_path = _resolve_mission_file(mission_dir, relative_path)
        source_name = source_path.stem
        lowered = source_name.lower()
        category = "infected" if "zombie" in lowered or "infected" in lowered else "animal"
        root = _parse_xml(source_path)
        source_points: list[dict[str, object]] = []
        for territory_index, territory in enumerate(root.findall("./territory")):
            territory_color = territory.get("color", "").strip()
            for zone_index, zone in enumerate(territory.findall("./zone")):
                context = (
                    f"{relative_path} territory #{territory_index} zone #{zone_index}"
                )
                zone_name = zone.get("name", "").strip()
                if not zone_name:
                    raise CatalogError(f"{context} has no name")
                source_points.append(
                    {
                        "status": "candidate",
                        "source": source_name,
                        "territory_color": territory_color,
                        "zone_name": zone_name,
                        "x": _float_attr(zone, "x", context),
                        "z": _float_attr(zone, "z", context),
                        "radius": _float_attr(zone, "r", context),
                        "smin": _float_attr(zone, "smin", context),
                        "smax": _float_attr(zone, "smax", context),
                        "dmin": _float_attr(zone, "dmin", context),
                        "dmax": _float_attr(zone, "dmax", context),
                    }
                )
        source_points.sort(
            key=lambda item: (
                str(item["zone_name"]),
                item["x"],
                item["z"],
                item["radius"],
                str(item["territory_color"]),
            )
        )
        for ordinal, point in enumerate(source_points):
            point["id"] = f"ce_candidate:{_safe_slug(source_name)}:{ordinal:04d}"
        categories[category].extend(source_points)

    result: dict[str, object] = {
        "semantics": "configured_ce_spawn_candidate_not_active_state",
        "active_state_known": False,
    }
    for category in ("animal", "infected"):
        positions = categories[category]
        positions.sort(key=lambda item: str(item["id"]))
        source_counts = Counter(str(point["source"]) for point in positions)
        result[category] = {
            "count": len(positions),
            "counts_by_source": {
                name: source_counts[name] for name in sorted(source_counts)
            },
            "positions": positions,
        }
    return result


def build_monitor_catalog(
    mission_dir: Path,
    server_id: str = DEFAULT_SERVER_ID,
) -> dict[str, Any]:
    """Build static/candidate map context without making runtime claims."""

    mission_dir = mission_dir.resolve()
    sections = {
        "static_contaminated_areas": _static_contaminated_areas(mission_dir),
        "player_spawn_points": _player_spawn_candidates(mission_dir),
        "ce_spawn_points": _territory_candidates(mission_dir),
    }
    revision = hashlib.sha256(
        json.dumps(
            sections,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "catalog_kind": MONITOR_CATALOG_KIND,
        "server_id": server_id,
        "catalog_revision": f"sha256:{revision}",
        **sections,
    }


def render_catalog(catalog: dict[str, object]) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--server-id", default=DEFAULT_SERVER_ID)
    parser.add_argument(
        "--monitor-output",
        type=Path,
        help=(
            "optionally write/check a separate monitor-facing catalog of "
            "static areas and candidate spawn positions"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if output differs instead of writing it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = build_catalog(args.mission_dir, server_id=args.server_id)
        rendered = render_catalog(catalog)
        monitor_catalog = None
        monitor_rendered = None
        if args.monitor_output:
            monitor_catalog = build_monitor_catalog(
                args.mission_dir, server_id=args.server_id
            )
            monitor_rendered = render_catalog(monitor_catalog)
        if args.check:
            stale = False
            if not args.output.is_file():
                print(f"catalog is missing: {args.output}", file=sys.stderr)
                stale = True
            elif args.output.read_text(encoding="utf-8") != rendered:
                print(
                    f"catalog is stale: regenerate {args.output} from {args.mission_dir}",
                    file=sys.stderr,
                )
                stale = True
            if args.monitor_output and monitor_rendered is not None:
                if not args.monitor_output.is_file():
                    print(
                        f"monitor catalog is missing: {args.monitor_output}",
                        file=sys.stderr,
                    )
                    stale = True
                elif args.monitor_output.read_text(encoding="utf-8") != monitor_rendered:
                    print(
                        "monitor catalog is stale: regenerate "
                        f"{args.monitor_output} from {args.mission_dir}",
                        file=sys.stderr,
                    )
                    stale = True
            if stale:
                return 1
            print(
                f"catalog is current: {len(catalog['event_points'])} points, "
                f"{catalog['catalog_revision']}"
            )
            if args.monitor_output and monitor_catalog is not None:
                print(
                    "monitor catalog is current: "
                    f"static_areas={monitor_catalog['static_contaminated_areas']['count']}, "
                    f"player_spawns={monitor_catalog['player_spawn_points']['count']}, "
                    f"animal_candidates={monitor_catalog['ce_spawn_points']['animal']['count']}, "
                    f"infected_candidates={monitor_catalog['ce_spawn_points']['infected']['count']}, "
                    f"{monitor_catalog['catalog_revision']}"
                )
            return 0

        write_atomic(args.output, rendered)
        if args.monitor_output and monitor_rendered is not None:
            write_atomic(args.monitor_output, monitor_rendered)
        counts = Counter(str(point["kind"]) for point in catalog["event_points"])
        summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        print(
            f"wrote {args.output}: {len(catalog['event_points'])} points "
            f"({summary}), {catalog['catalog_revision']}"
        )
        if args.monitor_output and monitor_catalog is not None:
            print(
                f"wrote {args.monitor_output}: "
                f"static_areas={monitor_catalog['static_contaminated_areas']['count']}, "
                f"player_spawns={monitor_catalog['player_spawn_points']['count']}, "
                f"animal_candidates={monitor_catalog['ce_spawn_points']['animal']['count']}, "
                f"infected_candidates={monitor_catalog['ce_spawn_points']['infected']['count']}, "
                f"{monitor_catalog['catalog_revision']}"
            )
        return 0
    except (CatalogError, OSError) as exc:
        print(f"catalog generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
