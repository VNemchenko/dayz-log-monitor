from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import make_event
from .privacy import Coordinates, PrivacyProjector


PLAYER_RE = re.compile(
    r'Player\s+"(?P<name>[^"]+)"(?P<middle>.*?)'
    r'\(id=(?P<id>[^\s,)]+)(?P<tail>[^)]*)\)',
    re.IGNORECASE,
)
CHAT_RE = re.compile(
    r'Chat\("(?P<name>[^"]+)"\s*\(id=(?P<id>[^\s,)]+)[^)]*\)\)\s*:\s*(?P<message>.*)$',
    re.IGNORECASE,
)
POSITION_RE = re.compile(
    r"pos=<\s*(?P<x>-?\d+(?:\.\d+)?)\s*,\s*"
    r"(?P<y>-?\d+(?:\.\d+)?)\s*,\s*"
    r"(?P<z>-?\d+(?:\.\d+)?)\s*>",
    re.IGNORECASE,
)
PLAYER_LIST_POSITION_RE = re.compile(r"pos=<[^>]+>\)\s*$", re.IGNORECASE)
PLAYER_LIST_HEADER_RE = re.compile(r"#####\s+PlayerList log:\s*(?P<count>\d+)\s+players?", re.IGNORECASE)
PLAYER_LIST_END_RE = re.compile(r"\|\s*#####\s*$")
DAMAGE_RE = re.compile(r"\bfor\s+(?P<damage>-?\d+(?:\.\d+)?)\s+damage\b", re.IGNORECASE)
DISTANCE_RE = re.compile(r"\bfrom\s+(?P<distance>\d+(?:\.\d+)?)\s+meters?\b", re.IGNORECASE)
WEAPON_RE = re.compile(r"\bwith\s+(?P<weapon>[^|]+?)(?:\s+from\s+\d|\s*$)", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HEX_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
RB_EVT_LINE_RE = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*\|\s*)?RB_EVT v1 (?P<payload>.*)\s*$"
)

TELEMETRY_TYPES = {
    "telemetry.started",
    "telemetry.stopped",
    "telemetry.error",
    "world.snapshot",
    "world.event.present",
    "world.event.started",
    "world.event.updated",
    "world.event.ended",
}

PUBLIC_COMMANDS = {"status", "weather", "time", "rules"}


@dataclass(frozen=True)
class LineRecord:
    kind: str
    file_key: str
    file_name: str
    offset_start: int
    offset_end: int
    text: str
    occurred_at: datetime
    observed_at: datetime


class DayZEventParser:
    def __init__(self, server_id: str, privacy: PrivacyProjector) -> None:
        self.server_id = server_id
        self.privacy = privacy

    def parse(self, record: LineRecord) -> list[dict[str, Any]]:
        telemetry = self._parse_telemetry(record) if record.kind == "adm" else None
        if telemetry is not None:
            actions = [{"action": "event", "event": telemetry}]
            if telemetry["type"] == "world.snapshot":
                actions.append(
                    {
                        "action": "telemetry_snapshot",
                        "observed_at": telemetry["observed_at"],
                    }
                )
            return actions

        if record.kind in {"rpt", "script", "error"}:
            operational = self._parse_operational(record)
            if not operational:
                return []
            if operational["type"] in {"server.script_error", "server.economy_anomaly"}:
                fingerprint = operational["facts"]["fingerprint"]
                return [
                    {
                        "action": "aggregate",
                        "key": f"{operational['type']}:{fingerprint}",
                        "window_seconds": 600,
                        "occurred_at": record.occurred_at.timestamp(),
                        "damage": 0,
                        "seed_event": operational,
                    }
                ]
            return [{"action": "event", "event": operational}]

        return self._parse_adm(record)

    def _event(self, record: LineRecord, event_type: str, **kwargs: Any) -> dict[str, Any]:
        return make_event(
            server_id=self.server_id,
            event_type=event_type,
            occurred_at=record.occurred_at,
            observed_at=record.observed_at,
            file_key=record.file_key,
            file_name=record.file_name,
            source_kind=record.kind,
            offset_start=record.offset_start,
            offset_end=record.offset_end,
            **kwargs,
        )

    @staticmethod
    def _coordinates(text: str) -> Coordinates | None:
        match = POSITION_RE.search(text)
        if not match:
            return None
        return Coordinates(
            x=float(match.group("x")),
            # DayZ ADM uses pos=<world-x, world-z, altitude>.
            y=float(match.group("z")),
            z=float(match.group("y")),
        )

    def _players(self, text: str) -> list[dict[str, Any]]:
        players: list[dict[str, Any]] = []
        for match in PLAYER_RE.finditer(text):
            player_id = match.group("id").strip()
            players.append(
                {
                    "ref": self.privacy.player_ref(player_id),
                    "display_name": self.privacy.clean_name(match.group("name")),
                }
            )
        return players

    def _admin_view(
        self,
        player: dict[str, Any] | None,
        coordinates: Coordinates | None,
        **extra: Any,
    ) -> dict[str, Any]:
        view: dict[str, Any] = {}
        if player:
            view["actor"] = player
        location = self.privacy.admin_location(coordinates)
        if location:
            view["location"] = location
        view.update(extra)
        return view

    def _parse_adm(self, record: LineRecord) -> list[dict[str, Any]]:
        text = record.text
        text_cf = text.casefold()
        coordinates = self._coordinates(text)
        players = self._players(text)
        actor = players[0] if players else None

        player_list_header = PLAYER_LIST_HEADER_RE.search(text)
        if player_list_header:
            return [
                {
                    "action": "player_list_start",
                    "list_key": f"{record.file_key}:{record.offset_start}",
                    "expected_count": int(player_list_header.group("count")),
                    "observed_at": record.occurred_at.isoformat(),
                }
            ]
        if PLAYER_LIST_END_RE.search(text):
            return [{"action": "player_list_end", "observed_at": record.occurred_at.isoformat()}]

        chat = CHAT_RE.search(text)
        if chat:
            player = {
                "ref": self.privacy.player_ref(chat.group("id")),
                "display_name": self.privacy.clean_name(chat.group("name")),
            }
            message = self.privacy.clean_admin_message(chat.group("message"))
            if not message.startswith("!"):
                return []
            command, _, argument = message[1:].partition(" ")
            command = command.casefold()
            facts = {"actor_ref": player["ref"], "command": command}
            if command in PUBLIC_COMMANDS:
                event = self._event(
                    record,
                    "player.command",
                    audience_ceiling="public",
                    admin_view=self._admin_view(player, coordinates, command=command),
                    public_view={"command": command},
                    facts=facts,
                    expires_seconds=120,
                )
                return [{"action": "event", "event": event}]
            if command == "sos":
                event = self._event(
                    record,
                    "player.sos",
                    severity="warning",
                    audience_ceiling="public",
                    admin_view=self._admin_view(player, coordinates, method="command"),
                    public_view={"acknowledgement": "sos_received"},
                    facts={**facts, "method": "command"},
                    expires_seconds=300,
                )
                return [{"action": "event", "event": event}]
            if command == "admin" and argument.strip():
                event = self._event(
                    record,
                    "player.admin_message",
                    severity="warning",
                    audience_ceiling="public",
                    admin_view=self._admin_view(
                        player,
                        coordinates,
                        message=self.privacy.clean_admin_message(argument),
                    ),
                    public_view={"acknowledgement": "admin_message_received"},
                    facts=facts,
                    expires_seconds=900,
                )
                return [{"action": "event", "event": event}]
            return []

        if actor and coordinates and PLAYER_LIST_POSITION_RE.search(text):
            return [
                {
                    "action": "player_position",
                    "player": actor,
                    "coordinates": coordinates,
                    "observed_at": record.occurred_at.isoformat(),
                }
            ]

        if actor and " is connected" in text_cf:
            return [
                {
                    "action": "player_connection",
                    "player": actor,
                    "connected": True,
                    "coordinates": coordinates,
                    "observed_at": record.occurred_at.isoformat(),
                },
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        "player.connected",
                        admin_view=self._admin_view(actor, coordinates),
                        facts={"actor_ref": actor["ref"]},
                    ),
                },
            ]

        if actor and "has been disconnected" in text_cf:
            return [
                {
                    "action": "player_connection",
                    "player": actor,
                    "connected": False,
                    "coordinates": coordinates,
                    "observed_at": record.occurred_at.isoformat(),
                },
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        "player.disconnected",
                        admin_view=self._admin_view(actor, coordinates),
                        facts={"actor_ref": actor["ref"]},
                    ),
                },
            ]

        if actor and "performed emotesos" in text_cf:
            trigger_event = self._event(
                record,
                "player.sos",
                severity="warning",
                audience_ceiling="public",
                admin_view=self._admin_view(actor, coordinates, method="double_emote"),
                public_view={"acknowledgement": "sos_received"},
                facts={"actor_ref": actor["ref"], "method": "double_emote"},
                expires_seconds=300,
            )
            return [
                {
                    "action": "sos_signal",
                    "player_ref": actor["ref"],
                    "occurred_at": record.occurred_at.timestamp(),
                    "trigger_event": trigger_event,
                }
            ]

        if actor and " is unconscious" in text_cf:
            event_type = "player.unconscious"
        elif actor and "regained consciousness" in text_cf:
            event_type = "player.recovered"
        elif actor and "committed suicide" in text_cf:
            event_type = "player.suicide"
        else:
            event_type = ""
        if event_type and actor:
            return [
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        event_type,
                        severity="warning" if event_type != "player.recovered" else "info",
                        admin_view=self._admin_view(actor, coordinates),
                        facts={"actor_ref": actor["ref"]},
                    ),
                }
            ]

        if actor and (" hit by " in text_cf or "hit by explosion" in text_cf):
            return self._parse_hit(record, actor, players, coordinates)

        if actor and "killed by player" in text_cf and len(players) >= 2:
            attacker = players[1]
            common = {
                "admin_view": self._admin_view(
                    actor,
                    coordinates,
                    attacker=attacker,
                ),
                "facts": {"actor_ref": actor["ref"], "attacker_ref": attacker["ref"]},
                "severity": "critical",
            }
            return [
                {"action": "event", "event": self._event(record, "combat.pvp_kill", **common)},
            ]

        if actor and " died." in text_cf:
            cause = "unknown"
            return [
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        "player.death",
                        severity="critical",
                        admin_view=self._admin_view(actor, coordinates, cause=cause),
                        facts={"actor_ref": actor["ref"], "cause_class": cause},
                    ),
                }
            ]

        if actor and " killed by " in text_cf:
            cause = re.split(r"\s+killed by\s+", text, maxsplit=1, flags=re.IGNORECASE)[-1]
            cause = cause.split()[0][:80]
            return [
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        "player.killed",
                        severity="critical",
                        admin_view=self._admin_view(actor, coordinates, cause=cause),
                        facts={"actor_ref": actor["ref"], "cause_class": cause},
                    ),
                }
            ]

        base_action = None
        if actor and "dismantled" in text_cf:
            base_action = "base.dismantled"
        elif actor and re.search(r"\bbuilt\b", text, re.IGNORECASE):
            base_action = "base.built"
        elif actor and " placed " in text_cf:
            base_action = "base.placed"
        if base_action and actor:
            object_name = self._extract_object_name(text, base_action)
            return [
                {
                    "action": "event",
                    "event": self._event(
                        record,
                        base_action,
                        severity="warning" if base_action == "base.dismantled" else "info",
                        admin_view=self._admin_view(actor, coordinates, object=object_name),
                        facts={"actor_ref": actor["ref"], "object_class": object_name},
                    ),
                }
            ]

        return []

    def _parse_hit(
        self,
        record: LineRecord,
        actor: dict[str, Any],
        players: list[dict[str, Any]],
        coordinates: Coordinates | None,
    ) -> list[dict[str, Any]]:
        text_cf = record.text.casefold()
        damage_match = DAMAGE_RE.search(record.text)
        damage = float(damage_match.group("damage")) if damage_match else 0.0
        common_facts: dict[str, Any] = {"actor_ref": actor["ref"], "damage": round(damage, 3)}

        if "hit by player" in text_cf and len(players) >= 2:
            attacker = players[1]
            distance_match = DISTANCE_RE.search(record.text)
            weapon_match = WEAPON_RE.search(record.text)
            if distance_match:
                common_facts["distance_m"] = round(float(distance_match.group("distance")), 1)
            if weapon_match:
                common_facts["weapon"] = weapon_match.group("weapon").strip()[:80]
            common_facts["attacker_ref"] = attacker["ref"]
            event = self._event(
                record,
                "combat.pvp",
                severity="critical",
                admin_view=self._admin_view(actor, coordinates, attacker=attacker),
                facts=common_facts,
                expires_seconds=900,
            )
            return [{"action": "event", "event": event}]

        if "hit by explosion" in text_cf:
            event = self._event(
                record,
                "combat.explosion",
                severity="critical",
                admin_view=self._admin_view(actor, coordinates),
                facts=common_facts,
                expires_seconds=900,
            )
            return [{"action": "event", "event": event}]

        if "beartrap" in text_cf or "bear trap" in text_cf:
            event = self._event(
                record,
                "combat.trap",
                severity="warning",
                admin_view=self._admin_view(actor, coordinates),
                facts=common_facts,
            )
            return [{"action": "event", "event": event}]

        if "hit by infected" in text_cf or "hit by zmb" in text_cf:
            aggregate_type = "combat.infected_pressure"
            window = 60
        elif any(
            token in text_cf
            for token in (
                "civiliansedan",
                "offroadhatchback",
                "hatchback",
                "sedan",
                "truck_",
                "vehicle",
                "bus",
            )
        ):
            aggregate_type = "combat.vehicle_incident"
            window = 90
        elif any(token in text_cf for token in ("bear", "wolf", "animal")):
            aggregate_type = "combat.wildlife_pressure"
            window = 60
        else:
            return []

        pressure_facts = {**common_facts, "hit_count": 1}
        private_sector = self.privacy.public_location(coordinates)
        if private_sector and isinstance(private_sector.get("sector"), str):
            pressure_facts["sector_2km"] = private_sector["sector"]
        seed_event = self._event(
            record,
            aggregate_type,
            severity="warning",
            audience_ceiling="public",
            admin_view=self._admin_view(actor, coordinates),
            public_view={
                "episode_type": aggregate_type.removeprefix("combat."),
                "signal_count": 1,
            },
            facts=pressure_facts,
            expires_seconds=1_800,
        )
        return [
            {
                "action": "aggregate",
                "key": f"{aggregate_type}:{actor['ref']}",
                "window_seconds": window,
                "occurred_at": record.occurred_at.timestamp(),
                "damage": damage,
                "seed_event": seed_event,
            }
        ]

    @staticmethod
    def _extract_object_name(text: str, event_type: str) -> str:
        marker = {
            "base.dismantled": r"\bdismantled\s+",
            "base.built": r"\bbuilt\s+",
            "base.placed": r"\bplaced\s+",
        }[event_type]
        parts = re.split(marker, text, maxsplit=1, flags=re.IGNORECASE)
        tail = parts[-1].strip()
        if event_type == "base.dismantled":
            tail = tail.split(" from ", 1)[0]
        else:
            tail = tail.split(" with ", 1)[0]
        return tail[:100]

    def _parse_telemetry(self, record: LineRecord) -> dict[str, Any] | None:
        match = RB_EVT_LINE_RE.fullmatch(record.text)
        if not match:
            return None
        raw_json = match.group("payload").strip()
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            return self._event(
                record,
                "telemetry.error",
                severity="warning",
                admin_view={"reason": "invalid_rb_evt_json"},
                facts={"reason": "invalid_json"},
            )
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            return self._event(
                record,
                "telemetry.error",
                severity="warning",
                admin_view={"reason": "unsupported_rb_evt_schema"},
                facts={"reason": "unsupported_schema"},
            )
        if payload.get("server_id") != self.server_id or payload.get("visibility") != "admin":
            return self._event(
                record,
                "telemetry.error",
                severity="warning",
                admin_view={"reason": "untrusted_rb_evt_envelope"},
                facts={"reason": "server_or_visibility_mismatch"},
            )
        event_type = payload.get("type")
        if event_type not in TELEMETRY_TYPES:
            return self._event(
                record,
                "telemetry.error",
                severity="warning",
                admin_view={"reason": "unsupported_rb_evt_type"},
                facts={"reason": "unsupported_type"},
            )

        boot_id = payload.get("boot_id")
        sequence = payload.get("seq")
        if not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 80 or not isinstance(sequence, int) or sequence < 1:
            return self._event(
                record,
                "telemetry.error",
                severity="warning",
                admin_view={"reason": "invalid_rb_evt_identity"},
                facts={"reason": "invalid_identity"},
            )

        raw_data_value = payload.get("data")
        raw_data: dict[str, Any] = raw_data_value if isinstance(raw_data_value, dict) else {}
        if event_type == "world.snapshot":
            data = self._sanitize_world_snapshot(raw_data)
        elif event_type.startswith("world.event."):
            data = self._sanitize_world_event(raw_data)
        else:
            data = self._sanitize_lifecycle(raw_data)
        occurred_at = record.occurred_at
        observed_raw = payload.get("ts_utc", payload.get("observed_at"))
        if isinstance(observed_raw, str):
            try:
                occurred_at = datetime.fromisoformat(observed_raw.replace("Z", "+00:00")).astimezone(
                    timezone.utc
                )
            except ValueError:
                pass

        coordinates = None
        position = data.get("position")
        if isinstance(position, dict):
            try:
                coordinates = Coordinates(float(position["x"]), float(position.get("y", 0)), float(position["z"]))
            except (KeyError, TypeError, ValueError):
                coordinates = None
        elif all(key in data for key in ("x", "z")):
            try:
                coordinates = Coordinates(float(data["x"]), float(data.get("y", 0)), float(data["z"]))
            except (TypeError, ValueError):
                coordinates = None

        public_view = None
        audience = "admin"
        if event_type == "world.snapshot":
            public_view = {}
            if "game_time" in data:
                public_view["game_time"] = data["game_time"]
            if "weather" in data:
                public_view["weather"] = data["weather"]
            server = data.get("server")
            if isinstance(server, dict) and "online_players" in server:
                public_view["players_online"] = server["online_players"]
            audience = "public"
        elif event_type.startswith("world.event."):
            public_view = {
                "lifecycle": event_type.rsplit(".", 1)[-1],
                "kind": data.get("kind", "unknown"),
            }
            public_location = self.privacy.public_location(coordinates)
            if public_location:
                public_view["location"] = public_location
            audience = "public_delayed"

        return make_event(
            server_id=self.server_id,
            event_type=event_type,
            occurred_at=occurred_at,
            observed_at=record.observed_at,
            file_key=record.file_key,
            file_name=record.file_name,
            source_kind=record.kind,
            offset_start=record.offset_start,
            offset_end=record.offset_end,
            severity="warning" if event_type in {"telemetry.error", "world.event.started"} else "info",
            audience_ceiling=audience,
            admin_view={"telemetry": data, "location": self.privacy.admin_location(coordinates)},
            public_view=public_view,
            facts={
                "telemetry_event_id": f"{boot_id}:{sequence}"[:160],
                "catalog_revision": str(payload.get("catalog_revision", ""))[:128],
            },
            expires_seconds=3_600,
        )

    @staticmethod
    def _finite_number(value: Any, minimum: float, maximum: float) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number) or number < minimum or number > maximum:
            return None
        return number

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]

    def _sanitize_world_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        game = data.get("game_time")
        if isinstance(game, dict):
            safe_game: dict[str, Any] = {}
            for key, minimum, maximum in (
                ("year", 2000, 2200),
                ("month", 1, 12),
                ("day", 1, 31),
                ("hour", 0, 23),
                ("minute", 0, 59),
                ("decimal_hour", 0, 24),
            ):
                value = self._finite_number(game.get(key), minimum, maximum)
                if value is not None:
                    safe_game[key] = int(value) if key != "decimal_hour" else value
            if isinstance(game.get("is_night"), bool):
                safe_game["is_night"] = game["is_night"]
            if safe_game:
                result["game_time"] = safe_game

        weather = data.get("weather")
        if isinstance(weather, dict):
            safe_weather: dict[str, Any] = {}
            for name in ("overcast", "rain", "fog", "snowfall", "wind_magnitude", "wind_direction"):
                phenomenon = weather.get(name)
                if not isinstance(phenomenon, dict):
                    continue
                safe_phenomenon: dict[str, float] = {}
                for field, minimum, maximum in (
                    ("actual", -1_000, 1_000),
                    ("forecast", -1_000, 1_000),
                    ("next_change_seconds", 0, 86_400),
                ):
                    value = self._finite_number(phenomenon.get(field), minimum, maximum)
                    if value is not None:
                        safe_phenomenon[field] = value
                if safe_phenomenon:
                    safe_weather[name] = safe_phenomenon
            temperature = self._finite_number(
                weather.get("base_environment_temperature_c"), -100, 100
            )
            if temperature is not None:
                safe_weather["base_environment_temperature_c"] = temperature
            if safe_weather:
                result["weather"] = safe_weather

        server = data.get("server")
        if isinstance(server, dict):
            safe_server: dict[str, Any] = {}
            online = self._finite_number(server.get("online_players"), 0, 500)
            if online is not None:
                safe_server["online_players"] = int(online)
            for field in ("fps_min", "fps_max", "fps_avg", "uptime_seconds"):
                value = self._finite_number(server.get(field), 0, 10_000_000)
                if value is not None:
                    safe_server[field] = value
            if safe_server:
                result["server"] = safe_server
        return result

    def _sanitize_world_event(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field, limit in (
            ("point_id", 128),
            ("kind", 64),
            ("event_name", 128),
            ("group_name", 128),
            ("anchor_type", 160),
            ("contamination_stage", 32),
        ):
            text_value = self._bounded_text(data.get(field), limit)
            if text_value:
                result[field] = text_value
        for field, minimum, maximum in (
            ("x", -20_000, 20_000),
            ("y", -2_000, 5_000),
            ("z", -20_000, 20_000),
            ("radius", 0, 10_000),
            ("remaining_seconds", -1, 10_000_000),
        ):
            number_value = self._finite_number(data.get(field), minimum, maximum)
            if number_value is not None:
                result[field] = number_value
        return result

    def _sanitize_lifecycle(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field, limit in (
            ("status", 32),
            ("code", 64),
            ("message", 300),
            ("dayz_baseline", 32),
        ):
            value = self._bounded_text(data.get(field), limit)
            if value:
                result[field] = value
        return result

    def _parse_operational(self, record: LineRecord) -> dict[str, Any] | None:
        text_cf = record.text.casefold()
        if record.text.strip() == "== DayZServer" or any(
            token in text_cf for token in ("mission started", "game started", "missionserver onmissionstart")
        ):
            event_type = "server.started"
            severity = "info"
        elif "[shutdown]" in text_cf and "shutting down in" in text_cf:
            event_type = "server.restart_due"
            severity = "warning"
        elif "missionserver onmissionfinish" in text_cf:
            event_type = "server.stopping"
            severity = "warning"
        elif any(token in text_cf for token in ("no points", "wrong points", "cannot spawn", "unspawnable")):
            event_type = "server.economy_anomaly"
            severity = "warning"
        elif any(token in text_cf for token in ("error", "exception", "null pointer", "cannot load", "does not exist")):
            event_type = "server.script_error"
            severity = "warning"
        else:
            return None

        normalized = record.text.strip()
        normalized = IP_RE.sub("<ip>", normalized)
        normalized = HEX_RE.sub("<id>", normalized)
        normalized = PLAYER_RE.sub('Player "<redacted>" (id=<redacted>)', normalized)
        normalized = POSITION_RE.sub("pos=<redacted>", normalized)
        normalized = re.sub(r"[\x00-\x1f\x7f]", " ", normalized)
        normalized = NUMBER_RE.sub("#", re.sub(r"\s+", " ", normalized).casefold())
        fingerprint = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
        if event_type == "server.economy_anomaly":
            error_class = "ce_spawn_failure"
        elif "null pointer" in text_cf:
            error_class = "null_pointer"
        elif "exception" in text_cf:
            error_class = "exception"
        elif "cannot load" in text_cf or "does not exist" in text_cf:
            error_class = "missing_resource"
        else:
            error_class = event_type.removeprefix("server.")
        public_view: dict[str, Any] | None = None
        audience = "admin"
        if event_type == "server.restart_due":
            seconds_match = re.search(r"shutting down in\s+(\d+)\s+seconds", text_cf)
            seconds = int(seconds_match.group(1)) if seconds_match else None
            public_view = {"restart_in_seconds": seconds}
            audience = "public"
        elif event_type == "server.started":
            public_view = {"status": "started"}
            audience = "public"

        return self._event(
            record,
            event_type,
            severity=severity,
            audience_ceiling=audience,
            admin_view={"error_class": error_class},
            public_view=public_view,
            facts={"fingerprint": fingerprint, "error_class": error_class},
            expires_seconds=3_600,
        )
