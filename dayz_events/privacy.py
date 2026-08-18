from __future__ import annotations

import hashlib
import hmac
import math
import re
import unicodedata
from dataclasses import dataclass


CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Coordinates:
    x: float
    y: float
    z: float


class PrivacyProjector:
    def __init__(self, hmac_secret: str, sector_size: int = 2_000, namespace: str = "") -> None:
        self._secret = hmac_secret.encode("utf-8")
        self._namespace = namespace.strip().encode("utf-8")
        self.sector_size = sector_size

    def player_ref(self, player_id: str) -> str:
        digest = hmac.new(
            self._secret,
            self._namespace + b"\x00" + player_id.strip().encode("utf-8", errors="ignore"),
            hashlib.sha256,
        ).hexdigest()
        return f"p_{digest[:20]}"

    @staticmethod
    def clean_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        normalized = CONTROL_RE.sub(" ", normalized)
        return WHITESPACE_RE.sub(" ", normalized).strip()[:64] or "Player"

    @staticmethod
    def clean_admin_message(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        normalized = CONTROL_RE.sub(" ", normalized)
        return WHITESPACE_RE.sub(" ", normalized).strip()[:300]

    def admin_location(self, coordinates: Coordinates | None) -> dict[str, object] | None:
        if coordinates is None:
            return None
        return {
            "x": round(coordinates.x, 1),
            "y": round(coordinates.y, 1),
            "z": round(coordinates.z, 1),
            "grid_100m": f"{math.floor(coordinates.x / 100):03d}-{math.floor(coordinates.z / 100):03d}",
            "precision": "exact",
        }

    def public_location(self, coordinates: Coordinates | None) -> dict[str, object] | None:
        if coordinates is None:
            return None
        column = max(0, int(coordinates.x // self.sector_size))
        row = max(0, int(coordinates.z // self.sector_size)) + 1
        if column < 26:
            label = f"{chr(ord('A') + column)}{row}"
        else:
            label = f"X{column}-{row}"
        return {
            "sector": label,
            "size_m": self.sector_size,
            "precision": "coarse",
        }
