from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class WorldCatalog:
    """Read-only static Livonia context; it never claims candidate points are active."""

    def __init__(self, payload: dict[str, Any]) -> None:
        if payload.get("schema") != 1:
            raise ValueError("world catalog schema must be 1")
        if payload.get("catalog_kind") != "monitor_world_reference":
            raise ValueError("world catalog kind must be monitor_world_reference")
        ce = payload.get("ce_spawn_points")
        if isinstance(ce, dict) and ce.get("active_state_known") is not False:
            raise ValueError("CE catalog must explicitly declare active_state_known=false")
        self.revision = str(payload.get("catalog_revision", ""))[:128]
        static = payload.get("static_contaminated_areas", {})
        self.static_areas = list(static.get("positions", [])) if isinstance(static, dict) else []
        self.player_spawns = list(payload.get("player_spawn_points", {}).get("positions", []))
        self.animal_candidates = list(ce.get("animal", {}).get("positions", [])) if isinstance(ce, dict) else []
        self.infected_candidates = list(ce.get("infected", {}).get("positions", [])) if isinstance(ce, dict) else []

    @classmethod
    def from_path(cls, path: Path) -> "WorldCatalog":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("world catalog root must be an object")
        return cls(payload)

    @staticmethod
    def _distance(x: float, z: float, item: dict[str, Any]) -> float:
        return math.hypot(x - float(item["x"]), z - float(item["z"]))

    def context(self, x: float, z: float) -> dict[str, Any]:
        context: dict[str, Any] = {"catalog_revision": self.revision}
        hazards = []
        for area in self.static_areas:
            try:
                distance = self._distance(x, z, area)
                radius = float(area.get("radius", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if distance <= radius:
                hazards.append(
                    {
                        "id": str(area.get("id", ""))[:128],
                        "name": str(area.get("name", ""))[:80],
                        "status": "configured_static",
                        "inside": True,
                    }
                )
        if hazards:
            context["static_hazards"] = hazards

        # Candidate territories are useful admin context only. They are explicitly
        # labelled configured_candidate and never interpreted as current wildlife.
        nearby_candidates: dict[str, list[str]] = {}
        for label, candidates in (
            ("animal", self.animal_candidates),
            ("infected", self.infected_candidates),
        ):
            names = set()
            for item in candidates:
                try:
                    distance = self._distance(x, z, item)
                    radius = max(100.0, float(item.get("radius", 0)))
                except (KeyError, TypeError, ValueError):
                    continue
                if distance <= radius:
                    names.add(str(item.get("source", item.get("zone_name", "unknown")))[:80])
            if names:
                nearby_candidates[label] = sorted(names)
        if nearby_candidates:
            context["configured_candidates"] = {
                "status": "candidate_not_active_state",
                "types": nearby_candidates,
            }
        return context

    def enrich_actions(self, actions: list[dict[str, Any]]) -> None:
        for action in actions:
            candidates: list[dict[str, Any]] = []
            if action.get("action") == "event":
                candidates.append(action["event"])
            elif action.get("action") in {"aggregate", "sos_signal"}:
                key = "seed_event" if action["action"] == "aggregate" else "trigger_event"
                candidates.append(action[key])
            for event in candidates:
                location = event.get("admin_view", {}).get("location")
                if not isinstance(location, dict) or "x" not in location or "z" not in location:
                    continue
                event.setdefault("facts", {})["world_context"] = self.context(
                    float(location["x"]), float(location["z"])
                )
