from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:32]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_event(
    *,
    server_id: str,
    event_type: str,
    occurred_at: datetime,
    observed_at: datetime,
    file_key: str,
    file_name: str,
    source_kind: str,
    offset_start: int,
    offset_end: int,
    severity: str = "info",
    audience_ceiling: str = "admin",
    admin_view: dict[str, Any] | None = None,
    public_view: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
    expires_seconds: int = 1_800,
) -> dict[str, Any]:
    event_id = stable_id(
        "evt",
        server_id,
        file_key,
        offset_start,
        offset_end,
        event_type,
    )
    expires_at = occurred_at.timestamp() + expires_seconds
    return {
        "schema": "dayz.event.v1",
        "event_id": event_id,
        "server_id": server_id,
        "type": event_type,
        "occurred_at": iso_utc(occurred_at),
        "observed_at": iso_utc(observed_at),
        "expires_at": iso_utc(datetime.fromtimestamp(expires_at, timezone.utc)),
        "severity": severity,
        "audience_ceiling": audience_ceiling,
        "admin_view": admin_view or {},
        "public_view": public_view,
        "facts": facts or {},
        "source": {
            "kind": source_kind,
            "file": file_name,
            "offset_start": offset_start,
            "offset_end": offset_end,
        },
    }
