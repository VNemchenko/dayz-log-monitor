from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .catalog import WorldCatalog
from .config import EventPipelineConfig
from .models import canonical_json, iso_utc, make_event, stable_id, utc_now
from .parser import DayZEventParser, LineRecord
from .privacy import Coordinates, PrivacyProjector
from .state import CursorRecord, EventStateStore


FILE_DATE_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[_-](?P<hour>\d{2})[-:](?P<minute>\d{2})[-:](?P<second>\d{2})"
)
LINE_TIME_RE = re.compile(r"^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.\d+)?(?:\s*\||\s)")

EXPECTED_STORAGE_FILES = (
    "players.db",
    "spawnpoints.bin",
    "data/animals.bin",
    "data/building.bin",
    "data/events.bin",
    "data/types.bin",
    "data/vehicles.bin",
    "data/zombies.bin",
    *(f"data/dynamic_{index:03d}.bin" for index in range(12)),
)
EXPECTED_BACKUP_FILES = (
    "building.bin",
    "events.bin",
    "types.bin",
    "vehicles.bin",
    *(f"dynamic_{index:03d}.bin" for index in range(12)),
)


class EventPipeline:
    def __init__(
        self,
        config: EventPipelineConfig,
        *,
        session: requests.Session | None = None,
        logger: Callable[..., None] = print,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not config.enabled:
            raise ValueError("EventPipeline cannot be created while disabled")
        self.config = config
        self.logger = logger
        self.clock = clock
        try:
            self.server_timezone = ZoneInfo(config.server_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown SERVER_TIMEZONE: {config.server_timezone}") from exc
        self.state = EventStateStore(config.state_db)
        self.privacy = PrivacyProjector(config.player_hmac_secret, namespace=config.server_id)
        self.parser = DayZEventParser(config.server_id, self.privacy)
        self.session = session or requests.Session()
        self.catalog = self._load_catalog(config.catalog_file)
        if self.state.get_meta("pipeline_started_at") is None:
            self.state.set_meta("pipeline_started_at", iso_utc(self.clock()))

    def close(self) -> None:
        self.state.close()
        self.session.close()

    def poll(self) -> dict[str, int]:
        now = self.clock().astimezone(timezone.utc)
        metrics = {
            "files": 0,
            "lines": 0,
            "events": 0,
            "batches": 0,
            "delivered": 0,
            "retried": 0,
        }
        paths = self._discover_files()
        metrics["files"] = len(paths)
        if not self.state.get_meta("cursors_initialized"):
            if not paths:
                self._post_scan(now, metrics)
                return metrics
            cursors = []
            for path in paths:
                stat = path.stat()
                cursors.append(
                    CursorRecord(
                        path=str(path),
                        file_key=self._file_key(path, stat),
                        kind=self._kind(path),
                        offset=stat.st_size if self.config.start_at_end else 0,
                        partial=b"",
                        last_occurred_at=None,
                        mtime_ns=stat.st_mtime_ns,
                        fingerprint=self._content_fingerprint(path, stat.st_size),
                    )
                )
            self.state.initialize_cursors(cursors)
            if self.config.start_at_end:
                self.logger(
                    f"[info] Typed event pipeline initialized at EOF for {len(cursors)} existing files"
                )
                self._post_scan(now, metrics)
                return metrics

        for path in paths:
            lines, inserted = self._poll_file(path, now)
            metrics["lines"] += lines
            metrics["events"] += inserted

        metrics["events"] += self.state.flush_due_aggregates(now.timestamp())
        self._post_scan(now, metrics)
        return metrics

    def _post_scan(self, now: datetime, metrics: dict[str, int]) -> None:
        metrics["events"] += self._check_telemetry_freshness(now)
        metrics["events"] += self._check_storage(now)
        metrics["batches"] += self.state.queue_batches(
            server_id=self.config.server_id,
            source_name=self.config.source_name,
            max_events=self.config.max_batch_events,
            max_bytes=self.config.max_batch_bytes,
            now=now,
        )
        delivery = self._deliver_due(now.timestamp())
        metrics["delivered"] += delivery["delivered"]
        metrics["retried"] += delivery["retried"]
        self.state.cleanup(now)

    def _discover_files(self) -> list[Path]:
        patterns = (
            "DayZServer_*.ADM",
            "DayZServer_*.RPT",
            "script_*.log",
            "error.log",
        )
        unique: dict[str, Path] = {}
        for pattern in patterns:
            for raw_path in glob.glob(str(self.config.logs_dir / pattern)):
                path = Path(raw_path)
                if path.is_file():
                    unique[str(path.resolve())] = path.resolve()
        return sorted(unique.values(), key=lambda value: (value.stat().st_mtime_ns, value.name.casefold()))

    @staticmethod
    def _kind(path: Path) -> str:
        suffix = path.suffix.casefold()
        name = path.name.casefold()
        if suffix == ".adm":
            return "adm"
        if suffix == ".rpt":
            return "rpt"
        if name == "error.log":
            return "error"
        return "script"

    @staticmethod
    def _file_key(path: Path, stat: os.stat_result) -> str:
        # ctime changes on every append on Linux, so it cannot identify a log generation.
        # inode/device plus the unique DayZ filename remains stable while the file grows.
        material = f"{path.name}\x1f{stat.st_dev}\x1f{stat.st_ino}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _poll_file(self, path: Path, observed_at: datetime) -> tuple[int, int]:
        stat = path.stat()
        base_file_key = self._file_key(path, stat)
        cursor = self.state.get_cursor(str(path))
        if cursor is None or not self._same_file_generation(cursor.file_key, base_file_key):
            cursor = CursorRecord(
                path=str(path),
                file_key=base_file_key,
                kind=self._kind(path),
                offset=0,
                partial=b"",
                last_occurred_at=None,
                mtime_ns=stat.st_mtime_ns,
            )
        else:
            current_fingerprint = self._content_fingerprint(
                path, min(cursor.offset, stat.st_size)
            )
            overwritten = bool(
                cursor.fingerprint
                and stat.st_mtime_ns != cursor.mtime_ns
                and current_fingerprint != cursor.fingerprint
            )
            if stat.st_size < cursor.offset or overwritten:
                generation = hashlib.sha256(
                    (
                        f"{cursor.file_key}\x1f{stat.st_mtime_ns}\x1f{stat.st_size}"
                        f"\x1f{current_fingerprint}"
                    ).encode("utf-8")
                ).hexdigest()[:16]
                cursor = CursorRecord(
                    path=str(path),
                    file_key=f"{base_file_key}:{generation}",
                    kind=self._kind(path),
                    offset=0,
                    partial=b"",
                    last_occurred_at=None,
                    mtime_ns=stat.st_mtime_ns,
                )

        file_key = cursor.file_key

        if stat.st_size <= cursor.offset:
            if not cursor.fingerprint and cursor.offset > 0:
                self.state.ingest(
                    CursorRecord(
                        path=cursor.path,
                        file_key=cursor.file_key,
                        kind=cursor.kind,
                        offset=cursor.offset,
                        partial=cursor.partial,
                        last_occurred_at=cursor.last_occurred_at,
                        mtime_ns=stat.st_mtime_ns,
                        fingerprint=self._content_fingerprint(path, cursor.offset),
                    ),
                    [],
                )
            return 0, 0

        with path.open("rb") as handle:
            handle.seek(cursor.offset)
            new_data = handle.read(self.config.max_read_bytes)
        if not new_data:
            return 0, 0

        combined = cursor.partial + new_data
        combined_base = cursor.offset - len(cursor.partial)
        segments = combined.split(b"\n")
        complete = segments[:-1]
        trailing = segments[-1]
        if len(trailing) > 65_536:
            trailing = b""
            self.logger(f"[warn] Dropped oversized partial line in {path.name}")

        previous = self._parse_iso(cursor.last_occurred_at)
        actions: list[dict[str, Any]] = []
        recent_positions: dict[str, dict[str, Any]] = {}
        relative = 0
        line_count = 0
        for raw_segment in complete:
            consumed = len(raw_segment) + 1
            clean_bytes = raw_segment[:-1] if raw_segment.endswith(b"\r") else raw_segment
            line = clean_bytes.decode("utf-8", errors="replace")
            start = combined_base + relative
            end = start + consumed
            relative += consumed
            if not line.strip():
                continue
            occurred_at = self._line_datetime(path, line, previous, stat.st_mtime)
            previous = occurred_at
            record = LineRecord(
                kind=cursor.kind,
                file_key=file_key,
                file_name=path.name,
                offset_start=start,
                offset_end=end,
                text=line,
                occurred_at=occurred_at,
                observed_at=observed_at,
            )
            line_actions = self.parser.parse(record)
            self._enrich_from_player_state(line_actions, recent_positions)
            if self.catalog:
                self.catalog.enrich_actions(line_actions)
            actions.extend(line_actions)
            line_count += 1

        updated_cursor = CursorRecord(
            path=str(path),
            file_key=file_key,
            kind=cursor.kind,
            offset=cursor.offset + len(new_data),
            partial=trailing,
            last_occurred_at=iso_utc(previous) if previous else cursor.last_occurred_at,
            mtime_ns=stat.st_mtime_ns,
            fingerprint=self._content_fingerprint(path, cursor.offset + len(new_data)),
        )
        inserted = self.state.ingest(updated_cursor, actions)
        return line_count, inserted

    def _line_datetime(
        self,
        path: Path,
        line: str,
        previous: datetime | None,
        fallback_mtime: float,
    ) -> datetime:
        time_match = LINE_TIME_RE.match(line)
        file_match = FILE_DATE_RE.search(path.name)
        if file_match:
            base_date = date.fromisoformat(file_match.group("date"))
            file_start = datetime(
                base_date.year,
                base_date.month,
                base_date.day,
                int(file_match.group("hour")),
                int(file_match.group("minute")),
                int(file_match.group("second")),
                tzinfo=self.server_timezone,
            )
        else:
            file_start = datetime.fromtimestamp(fallback_mtime, self.server_timezone)
            base_date = file_start.date()

        if not time_match:
            return file_start.astimezone(timezone.utc)

        candidate = datetime.combine(
            base_date,
            datetime_time(
                int(time_match.group("hour")),
                int(time_match.group("minute")),
                int(time_match.group("second")),
            ),
            tzinfo=self.server_timezone,
        )
        if candidate < file_start - timedelta(hours=12):
            candidate += timedelta(days=1)
        if previous is not None:
            previous_local = previous.astimezone(self.server_timezone)
            while candidate < previous_local - timedelta(hours=12):
                candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _same_file_generation(file_key: str, base_file_key: str) -> bool:
        return file_key == base_file_key or file_key.startswith(f"{base_file_key}:")

    @staticmethod
    def _content_fingerprint(path: Path, offset: int) -> str:
        if offset <= 0:
            return ""
        probe_size = min(4096, offset)
        with path.open("rb") as handle:
            head = handle.read(probe_size)
            handle.seek(max(0, offset - probe_size))
            tail = handle.read(probe_size)
        digest = hashlib.sha256()
        digest.update(str(offset).encode("ascii"))
        digest.update(b"\x00")
        digest.update(head)
        digest.update(b"\x00")
        digest.update(tail)
        return digest.hexdigest()

    def _enrich_from_player_state(
        self,
        actions: list[dict[str, Any]],
        recent_positions: dict[str, dict[str, Any]],
    ) -> None:
        for action in actions:
            if action.get("action") == "event":
                event = action.get("event")
            elif action.get("action") == "sos_signal":
                event = action.get("trigger_event")
            else:
                continue
            if not isinstance(event, dict) or event.get("type") not in {
                "player.command",
                "player.sos",
                "player.admin_message",
            }:
                continue
            admin_view = event.get("admin_view")
            if not isinstance(admin_view, dict) or admin_view.get("location"):
                continue
            facts = event.get("facts")
            player_ref = facts.get("actor_ref") if isinstance(facts, dict) else None
            if not isinstance(player_ref, str):
                continue
            assert isinstance(facts, dict)
            player = recent_positions.get(player_ref) or self.state.get_player_position(player_ref)
            if player is None:
                continue
            observed = self._parse_iso(player["last_seen"])
            occurred = self._parse_iso(event.get("occurred_at"))
            if observed is None or occurred is None or abs((occurred - observed).total_seconds()) > 900:
                continue
            coordinates = Coordinates(player["x"], player["y"], player["z"])
            admin_view["location"] = self.privacy.admin_location(coordinates)
            facts["position_observed_at"] = iso_utc(observed)
        for action in actions:
            if action.get("action") not in {"player_position", "player_connection"}:
                continue
            player = action.get("player")
            action_coordinates = action.get("coordinates")
            if not isinstance(player, dict) or not isinstance(action_coordinates, Coordinates):
                continue
            player_ref = player.get("ref")
            if isinstance(player_ref, str):
                recent_positions[player_ref] = {
                    "x": action_coordinates.x,
                    "y": action_coordinates.y,
                    "z": action_coordinates.z,
                    "last_seen": str(action.get("observed_at", "")),
                }

    def _check_telemetry_freshness(self, now: datetime) -> int:
        last_raw = self.state.get_meta("last_world_snapshot")
        base_raw = last_raw or self.state.get_meta("pipeline_started_at")
        base = self._parse_iso(base_raw)
        if base is None:
            return 0
        stale = (now - base).total_seconds() >= self.config.telemetry_stale_seconds
        prior_state = self.state.get_meta("telemetry_freshness")
        if not stale:
            if prior_state and prior_state.startswith("stale:"):
                self.state.set_meta("telemetry_freshness", "fresh")
            return 0
        marker = base_raw or "never"
        if prior_state == f"stale:{marker}":
            return 0
        event = make_event(
            server_id=self.config.server_id,
            event_type="server.telemetry_stale",
            occurred_at=now,
            observed_at=now,
            file_key="monitor.telemetry",
            file_name="monitor",
            source_kind="monitor",
            offset_start=int(base.timestamp()),
            offset_end=int(base.timestamp()) + 1,
            severity="warning",
            admin_view={
                "last_snapshot_at": last_raw,
                "stale_seconds": int((now - base).total_seconds()),
            },
            facts={"stale_after_seconds": self.config.telemetry_stale_seconds},
            expires_seconds=3_600,
        )
        inserted = self.state.insert_events([event])
        self.state.set_meta("telemetry_freshness", f"stale:{marker}")
        return inserted

    def _check_storage(self, now: datetime) -> int:
        storage_dir = self.config.storage_dir
        if storage_dir is None:
            return 0
        last_check_raw = self.state.get_meta("last_storage_check")
        try:
            last_check = float(last_check_raw) if last_check_raw else 0
        except ValueError:
            last_check = 0
        if now.timestamp() - last_check < self.config.storage_check_seconds:
            return 0
        self.state.set_meta("last_storage_check", str(now.timestamp()))

        missing = [relative for relative in EXPECTED_STORAGE_FILES if not (storage_dir / relative).is_file()]
        data_paths = [storage_dir / relative for relative in EXPECTED_STORAGE_FILES[2:] if (storage_dir / relative).is_file()]
        mtime_skew = 0
        if len(data_paths) > 1:
            mtimes = [path.stat().st_mtime for path in data_paths]
            mtime_skew = int(max(mtimes) - min(mtimes))

        backup_dir = storage_dir / "backup"
        backup_generations: list[tuple[float, Path, list[Path]]] = []
        if backup_dir.is_dir():
            for generation in backup_dir.iterdir():
                if not generation.is_dir():
                    continue
                files = [path for path in generation.rglob("*") if path.is_file()]
                if files:
                    backup_generations.append(
                        (max(path.stat().st_mtime for path in files), generation, files)
                    )
        backup_generations.sort(key=lambda item: item[0], reverse=True)
        backup_files = backup_generations[0][2] if backup_generations else []
        backup_generation = backup_generations[0][1] if backup_generations else None
        backup_age = None
        backup_missing: list[str] = list(EXPECTED_BACKUP_FILES)
        backup_mtime_skew = 0
        if backup_files:
            mtimes = [path.stat().st_mtime for path in backup_files]
            newest = max(mtimes)
            backup_age = int(now.timestamp() - newest)
            backup_mtime_skew = int(max(mtimes) - min(mtimes))
            present_names = {
                str(path.relative_to(backup_generation)).replace("\\", "/")
                for path in backup_files
                if backup_generation is not None
            }
            backup_missing = [name for name in EXPECTED_BACKUP_FILES if name not in present_names]

        status = {
            "missing": missing,
            "data_mtime_skew_seconds": mtime_skew,
            "backup_age_seconds": backup_age,
            "backup_present": bool(backup_files),
            "backup_generation": backup_generation.name if backup_generation else None,
            "backup_missing": backup_missing,
            "backup_mtime_skew_seconds": backup_mtime_skew,
        }
        unhealthy = bool(
            missing
            or mtime_skew > 300
            or backup_age is None
            or backup_age > 172_800
            or backup_missing
            or backup_mtime_skew > 300
        )
        condition = {
            "missing": missing,
            "data_mtime_skewed": mtime_skew > 300,
            "backup_state": "missing" if backup_age is None else "stale" if backup_age > 172_800 else "fresh",
            "backup_missing": backup_missing,
            "backup_mtime_skewed": backup_mtime_skew > 300,
        }
        state_hash = stable_id("storage", json.dumps(condition, sort_keys=True), unhealthy)
        current_state = f"{'unhealthy' if unhealthy else 'healthy'}:{state_hash}"
        previous_state = self.state.get_meta("storage_health_state")
        if previous_state == current_state:
            return 0
        self.state.set_meta("storage_health_state", current_state)
        if previous_state is None and not unhealthy:
            return 0
        if previous_state and previous_state.startswith("healthy:") and not unhealthy:
            return 0
        event_type = "storage.health_anomaly" if unhealthy else "storage.health_recovered"
        event = make_event(
            server_id=self.config.server_id,
            event_type=event_type,
            occurred_at=now,
            observed_at=now,
            file_key="monitor.storage",
            file_name="storage_1",
            source_kind="storage",
            offset_start=int(now.timestamp() // self.config.storage_check_seconds),
            offset_end=int(now.timestamp() // self.config.storage_check_seconds) + 1,
            severity="warning" if unhealthy else "info",
            admin_view=status,
            facts={"healthy": not unhealthy},
            expires_seconds=3_600,
        )
        return self.state.insert_events([event])

    def _deliver_due(self, now_epoch: float) -> dict[str, int]:
        result = {"delivered": 0, "retried": 0}
        for item in self.state.due_outbox(now_epoch):
            batch_id = item["batch_id"]
            attempts = int(item["attempts"]) + 1
            try:
                encoded_payload = canonical_json(item["payload"]).encode("utf-8")
                response = self.session.post(
                    self.config.webhook_url,
                    data=encoded_payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-DayZ-Key": self.config.webhook_key,
                        "Idempotency-Key": batch_id,
                    },
                    timeout=self.config.webhook_timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                self.state.mark_retry(batch_id, attempts, exc.__class__.__name__, now_epoch)
                result["retried"] += 1
                continue

            if 200 <= response.status_code < 300:
                self.state.mark_delivered(batch_id)
                result["delivered"] += 1
            elif response.status_code in {400, 401, 403, 409, 413, 422}:
                self.state.mark_dead(batch_id, f"HTTP {response.status_code}")
                self.logger(f"[error] Event batch {batch_id} rejected with HTTP {response.status_code}")
            else:
                self.state.mark_retry(batch_id, attempts, f"HTTP {response.status_code}", now_epoch)
                result["retried"] += 1
        return result

    def _load_catalog(self, path: Path | None) -> WorldCatalog | None:
        if path is None:
            return None
        try:
            return WorldCatalog.from_path(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Cannot load EVENT_CATALOG_FILE {path}: {exc}") from exc
