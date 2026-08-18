from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}, got {value}")
    return value


@dataclass(frozen=True)
class EventPipelineConfig:
    enabled: bool
    logs_dir: Path
    state_db: Path
    webhook_url: str
    webhook_key: str
    player_hmac_secret: str
    server_id: str
    server_timezone: str
    source_name: str
    start_at_end: bool
    max_batch_events: int
    max_batch_bytes: int
    max_read_bytes: int
    webhook_timeout: int
    storage_dir: Path | None
    storage_check_seconds: int
    telemetry_stale_seconds: int
    catalog_file: Path | None

    @classmethod
    def from_env(cls, *, logs_dir: str, source_name: str) -> "EventPipelineConfig":
        webhook_url = os.getenv("EVENT_WEBHOOK_URL", "").strip()
        enabled = _bool_env("EVENTS_ENABLED", bool(webhook_url))
        state_db = Path(os.getenv("EVENT_DB_FILE", "/state/events.sqlite3").strip())
        server_id = os.getenv("SERVER_ID", source_name).strip() or source_name
        webhook_key = os.getenv("EVENT_WEBHOOK_KEY", "").strip()
        player_hmac_secret = os.getenv("PLAYER_HMAC_SECRET", "").strip()

        if enabled:
            if not webhook_url:
                raise ValueError("EVENT_WEBHOOK_URL is required when EVENTS_ENABLED=true")
            if len(webhook_key) < 32 or webhook_key.casefold().startswith(("change_me", "replace-")):
                raise ValueError("EVENT_WEBHOOK_KEY must be a non-placeholder secret of at least 32 characters")
            if len(player_hmac_secret) < 32 or player_hmac_secret.casefold().startswith(("change_me", "replace-")):
                raise ValueError("PLAYER_HMAC_SECRET must be a non-placeholder secret of at least 32 characters")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,47}", server_id):
                raise ValueError("SERVER_ID must match [a-z0-9][a-z0-9_-]{1,47}")

        storage_raw = os.getenv("STORAGE_DIR", "").strip()
        catalog_raw = os.getenv("EVENT_CATALOG_FILE", "").strip()

        return cls(
            enabled=enabled,
            logs_dir=Path(logs_dir),
            state_db=state_db,
            webhook_url=webhook_url,
            webhook_key=webhook_key,
            player_hmac_secret=player_hmac_secret,
            server_id=server_id,
            server_timezone=os.getenv("SERVER_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
            source_name=source_name,
            start_at_end=_bool_env("EVENT_START_AT_END", True),
            max_batch_events=_int_env("EVENT_BATCH_MAX_EVENTS", 100, 1, 100),
            max_batch_bytes=_int_env("EVENT_BATCH_MAX_BYTES", 262_144, 4_096, 262_144),
            max_read_bytes=_int_env("EVENT_MAX_READ_BYTES", 1_048_576, 4_096, 16_777_216),
            webhook_timeout=_int_env("EVENT_WEBHOOK_TIMEOUT", 10, 1, 120),
            storage_dir=Path(storage_raw) if storage_raw else None,
            storage_check_seconds=_int_env("STORAGE_CHECK_SECONDS", 300, 30, 86_400),
            telemetry_stale_seconds=_int_env("TELEMETRY_STALE_SECONDS", 180, 60, 86_400),
            catalog_file=Path(catalog_raw) if catalog_raw else None,
        )
