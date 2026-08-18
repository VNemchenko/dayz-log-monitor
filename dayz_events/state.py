from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import canonical_json, iso_utc, stable_id, utc_now
from .privacy import Coordinates


MAX_AGGREGATE_SECONDS = 300


@dataclass(frozen=True)
class CursorRecord:
    path: str
    file_key: str
    kind: str
    offset: int
    partial: bytes
    last_occurred_at: str | None
    mtime_ns: int
    fingerprint: str = ""


class EventStateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                path TEXT PRIMARY KEY,
                file_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                offset INTEGER NOT NULL,
                partial BLOB NOT NULL DEFAULT X'',
                last_occurred_at TEXT,
                mtime_ns INTEGER NOT NULL,
                fingerprint TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                batch_id TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS events_status_idx ON events(status, created_at);
            CREATE TABLE IF NOT EXISTS outbox (
                batch_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS player_state (
                player_ref TEXT PRIMARY KEY,
                display_name TEXT,
                x REAL,
                y REAL,
                z REAL,
                last_seen TEXT,
                connected INTEGER,
                previous_x REAL,
                previous_y REAL,
                previous_z REAL,
                movement_distance_m REAL,
                speed_mps REAL,
                movement_class TEXT,
                sos_window_start REAL,
                sos_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS player_list_snapshot (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                list_key TEXT NOT NULL,
                expected_count INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS player_list_members (
                list_key TEXT NOT NULL,
                player_ref TEXT NOT NULL,
                PRIMARY KEY(list_key, player_ref)
            );
            CREATE TABLE IF NOT EXISTS aggregates (
                aggregate_key TEXT PRIMARY KEY,
                window_seconds INTEGER NOT NULL,
                started_at REAL NOT NULL,
                last_at REAL NOT NULL,
                hit_count INTEGER NOT NULL,
                damage_sum REAL NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        cursor_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(cursors)").fetchall()
        }
        if "fingerprint" not in cursor_columns:
            self.connection.execute(
                "ALTER TABLE cursors ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"
            )
        player_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(player_state)").fetchall()
        }
        for column, sql_type in (
            ("previous_x", "REAL"),
            ("previous_y", "REAL"),
            ("previous_z", "REAL"),
            ("movement_distance_m", "REAL"),
            ("speed_mps", "REAL"),
            ("movement_class", "TEXT"),
        ):
            if column not in player_columns:
                self.connection.execute(
                    f"ALTER TABLE player_state ADD COLUMN {column} {sql_type}"
                )
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_cursor(self, path: str) -> CursorRecord | None:
        row = self.connection.execute("SELECT * FROM cursors WHERE path = ?", (path,)).fetchone()
        if not row:
            return None
        return CursorRecord(
            path=str(row["path"]),
            file_key=str(row["file_key"]),
            kind=str(row["kind"]),
            offset=int(row["offset"]),
            partial=bytes(row["partial"]),
            last_occurred_at=row["last_occurred_at"],
            mtime_ns=int(row["mtime_ns"]),
            fingerprint=str(row["fingerprint"] or ""),
        )

    def get_player_position(self, player_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT x, y, z, last_seen FROM player_state WHERE player_ref = ?",
            (player_ref,),
        ).fetchone()
        if not row or row["x"] is None or row["y"] is None or row["z"] is None:
            return None
        return {
            "x": float(row["x"]),
            "y": float(row["y"]),
            "z": float(row["z"]),
            "last_seen": str(row["last_seen"] or ""),
        }

    def initialize_cursors(self, cursors: Iterable[CursorRecord]) -> None:
        now = iso_utc(utc_now())
        with self.connection:
            for cursor in cursors:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO cursors(
                        path, file_key, kind, offset, partial, last_occurred_at, mtime_ns,
                        fingerprint, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cursor.path,
                        cursor.file_key,
                        cursor.kind,
                        cursor.offset,
                        cursor.partial,
                        cursor.last_occurred_at,
                        cursor.mtime_ns,
                        cursor.fingerprint,
                        now,
                    ),
                )
            self.connection.execute(
                "INSERT INTO meta(key, value) VALUES('cursors_initialized', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )

    def ingest(self, cursor: CursorRecord, actions: list[dict[str, Any]]) -> int:
        inserted = 0
        now_text = iso_utc(utc_now())
        with self.connection:
            for action in actions:
                kind = action.get("action")
                if kind == "event":
                    inserted += self._insert_event(self.connection, action["event"], now_text)
                elif kind == "player_position":
                    self._update_player_position(self.connection, action)
                elif kind == "player_connection":
                    self._update_player_connection(self.connection, action)
                elif kind == "sos_signal":
                    inserted += self._register_sos(self.connection, action, now_text)
                elif kind == "aggregate":
                    inserted += self._register_aggregate(self.connection, action, now_text)
                elif kind == "telemetry_snapshot":
                    self.connection.execute(
                        "INSERT INTO meta(key, value) VALUES('last_world_snapshot', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(action["observed_at"]),),
                    )
                elif kind == "player_list_start":
                    self._start_player_list(self.connection, action)
                elif kind == "player_list_end":
                    self._finish_player_list(self.connection, action)

            self.connection.execute(
                """
                INSERT INTO cursors(
                    path, file_key, kind, offset, partial, last_occurred_at, mtime_ns,
                    fingerprint, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    file_key = excluded.file_key,
                    kind = excluded.kind,
                    offset = excluded.offset,
                    partial = excluded.partial,
                    last_occurred_at = excluded.last_occurred_at,
                    mtime_ns = excluded.mtime_ns,
                    fingerprint = excluded.fingerprint,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.path,
                    cursor.file_key,
                    cursor.kind,
                    cursor.offset,
                    cursor.partial,
                    cursor.last_occurred_at,
                    cursor.mtime_ns,
                    cursor.fingerprint,
                    now_text,
                ),
            )
        return inserted

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: dict[str, Any], now_text: str) -> int:
        result = connection.execute(
            "INSERT OR IGNORE INTO events(event_id, payload, status, created_at) VALUES(?, ?, 'pending', ?)",
            (event["event_id"], canonical_json(event), now_text),
        )
        return int(result.rowcount > 0)

    @staticmethod
    def _update_player_position(connection: sqlite3.Connection, action: dict[str, Any]) -> None:
        player = action["player"]
        coordinates: Coordinates = action["coordinates"]
        previous = connection.execute(
            "SELECT x, y, z, last_seen FROM player_state WHERE player_ref = ?",
            (player["ref"],),
        ).fetchone()
        previous_x = float(previous["x"]) if previous and previous["x"] is not None else None
        previous_y = float(previous["y"]) if previous and previous["y"] is not None else None
        previous_z = float(previous["z"]) if previous and previous["z"] is not None else None
        distance = None
        speed = None
        movement_class = None
        if previous_x is not None and previous_z is not None:
            distance = math.hypot(coordinates.x - previous_x, coordinates.z - previous_z)
            try:
                current_time = datetime.fromisoformat(str(action["observed_at"]).replace("Z", "+00:00"))
                prior_time = datetime.fromisoformat(str(previous["last_seen"]).replace("Z", "+00:00"))
                elapsed = (current_time - prior_time).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed > 0:
                speed = distance / elapsed
            if distance < 10:
                movement_class = "stationary"
            elif distance < 500:
                movement_class = "moving"
            else:
                movement_class = "travelling"
        connection.execute(
            """
            INSERT INTO player_state(
                player_ref, display_name, x, y, z, last_seen,
                previous_x, previous_y, previous_z,
                movement_distance_m, speed_mps, movement_class
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_ref) DO UPDATE SET
                display_name = excluded.display_name,
                x = excluded.x,
                y = excluded.y,
                z = excluded.z,
                last_seen = excluded.last_seen,
                previous_x = excluded.previous_x,
                previous_y = excluded.previous_y,
                previous_z = excluded.previous_z,
                movement_distance_m = excluded.movement_distance_m,
                speed_mps = excluded.speed_mps,
                movement_class = excluded.movement_class
            """,
            (
                player["ref"],
                player.get("display_name"),
                coordinates.x,
                coordinates.y,
                coordinates.z,
                action["observed_at"],
                previous_x,
                previous_y,
                previous_z,
                distance,
                speed,
                movement_class,
            ),
        )
        snapshot = connection.execute(
            "SELECT list_key FROM player_list_snapshot WHERE singleton = 1"
        ).fetchone()
        if snapshot:
            connection.execute(
                "INSERT OR IGNORE INTO player_list_members(list_key, player_ref) VALUES(?, ?)",
                (snapshot["list_key"], player["ref"]),
            )

    @staticmethod
    def _start_player_list(connection: sqlite3.Connection, action: dict[str, Any]) -> None:
        connection.execute("DELETE FROM player_list_members")
        connection.execute(
            """
            INSERT INTO player_list_snapshot(singleton, list_key, expected_count, observed_at)
            VALUES(1, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                list_key = excluded.list_key,
                expected_count = excluded.expected_count,
                observed_at = excluded.observed_at
            """,
            (action["list_key"], int(action["expected_count"]), action["observed_at"]),
        )

    @staticmethod
    def _finish_player_list(connection: sqlite3.Connection, action: dict[str, Any]) -> None:
        snapshot = connection.execute(
            "SELECT list_key, expected_count FROM player_list_snapshot WHERE singleton = 1"
        ).fetchone()
        if not snapshot:
            return
        members = connection.execute(
            "SELECT player_ref FROM player_list_members WHERE list_key = ?",
            (snapshot["list_key"],),
        ).fetchall()
        member_refs = [str(row["player_ref"]) for row in members]
        actual_count = len(member_refs)
        expected_count = int(snapshot["expected_count"])
        if actual_count == expected_count:
            connection.execute("UPDATE player_state SET connected = 0 WHERE connected = 1")
            if member_refs:
                placeholders = ",".join("?" for _ in member_refs)
                connection.execute(
                    f"UPDATE player_state SET connected = 1 WHERE player_ref IN ({placeholders})",
                    member_refs,
                )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('online_count', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(actual_count),),
            )
            connection.execute("DELETE FROM meta WHERE key = 'player_list_mismatch'")
        else:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('player_list_mismatch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"expected={expected_count},actual={actual_count},at={action['observed_at']}",),
            )
        connection.execute("DELETE FROM player_list_members")
        connection.execute("DELETE FROM player_list_snapshot WHERE singleton = 1")

    @staticmethod
    def _update_player_connection(connection: sqlite3.Connection, action: dict[str, Any]) -> None:
        player = action["player"]
        coordinates = action.get("coordinates")
        xyz = (
            (coordinates.x, coordinates.y, coordinates.z)
            if isinstance(coordinates, Coordinates)
            else (None, None, None)
        )
        connection.execute(
            """
            INSERT INTO player_state(player_ref, display_name, x, y, z, last_seen, connected)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_ref) DO UPDATE SET
                display_name = excluded.display_name,
                x = COALESCE(excluded.x, player_state.x),
                y = COALESCE(excluded.y, player_state.y),
                z = COALESCE(excluded.z, player_state.z),
                last_seen = excluded.last_seen,
                connected = excluded.connected
            """,
            (
                player["ref"],
                player.get("display_name"),
                xyz[0],
                xyz[1],
                xyz[2],
                action["observed_at"],
                1 if action["connected"] else 0,
            ),
        )

    def _register_sos(
        self,
        connection: sqlite3.Connection,
        action: dict[str, Any],
        now_text: str,
    ) -> int:
        player_ref = action["player_ref"]
        occurred_at = float(action["occurred_at"])
        row = connection.execute(
            "SELECT sos_window_start, sos_count FROM player_state WHERE player_ref = ?",
            (player_ref,),
        ).fetchone()
        window_start = float(row["sos_window_start"]) if row and row["sos_window_start"] else occurred_at
        count = int(row["sos_count"]) if row else 0
        if occurred_at - window_start > 30 or occurred_at < window_start:
            window_start = occurred_at
            count = 0
        count += 1
        should_emit = count >= 2
        connection.execute(
            """
            INSERT INTO player_state(player_ref, sos_window_start, sos_count)
            VALUES(?, ?, ?)
            ON CONFLICT(player_ref) DO UPDATE SET
                sos_window_start = excluded.sos_window_start,
                sos_count = excluded.sos_count
            """,
            (player_ref, None if should_emit else window_start, 0 if should_emit else count),
        )
        if should_emit:
            return self._insert_event(connection, action["trigger_event"], now_text)
        return 0

    def _register_aggregate(
        self,
        connection: sqlite3.Connection,
        action: dict[str, Any],
        now_text: str,
    ) -> int:
        key = action["key"]
        occurred_at = float(action["occurred_at"])
        damage = float(action.get("damage", 0))
        window_seconds = int(action["window_seconds"])
        row = connection.execute(
            "SELECT * FROM aggregates WHERE aggregate_key = ?", (key,)
        ).fetchone()
        inserted = 0

        if row and (
            occurred_at < float(row["last_at"])
            or occurred_at - float(row["last_at"]) > int(row["window_seconds"])
            or occurred_at - float(row["started_at"]) >= MAX_AGGREGATE_SECONDS
        ):
            payload = json.loads(row["payload"])
            self._finalize_aggregate_payload(
                payload, int(row["hit_count"]), float(row["damage_sum"])
            )
            inserted += self._insert_event(connection, payload, now_text)
            connection.execute("DELETE FROM aggregates WHERE aggregate_key = ?", (key,))
            row = None

        if row:
            hit_count = int(row["hit_count"]) + 1
            damage_sum = float(row["damage_sum"]) + damage
            payload = json.loads(row["payload"])
            seed = action["seed_event"]
            if seed.get("admin_view", {}).get("location"):
                payload.setdefault("admin_view", {})["location"] = seed["admin_view"]["location"]
            seed_facts = seed.get("facts")
            if isinstance(seed_facts, dict) and isinstance(seed_facts.get("sector_2km"), str):
                payload.setdefault("facts", {})["sector_2km"] = seed_facts["sector_2km"]
            connection.execute(
                "UPDATE aggregates SET last_at = ?, hit_count = ?, damage_sum = ?, payload = ? "
                "WHERE aggregate_key = ?",
                (occurred_at, hit_count, damage_sum, canonical_json(payload), key),
            )
        else:
            hit_count = 1
            damage_sum = damage
            payload = action["seed_event"]
            connection.execute(
                """
                INSERT INTO aggregates(
                    aggregate_key, window_seconds, started_at, last_at, hit_count, damage_sum, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (key, window_seconds, occurred_at, occurred_at, hit_count, damage_sum, canonical_json(payload)),
            )

        return inserted

    def flush_due_aggregates(self, now_epoch: float) -> int:
        rows = self.connection.execute(
            "SELECT * FROM aggregates WHERE last_at + window_seconds <= ?", (now_epoch,)
        ).fetchall()
        if not rows:
            return 0
        inserted = 0
        now_text = iso_utc(utc_now())
        with self.connection:
            for row in rows:
                payload = json.loads(row["payload"])
                self._finalize_aggregate_payload(
                    payload, int(row["hit_count"]), float(row["damage_sum"])
                )
                inserted += self._insert_event(self.connection, payload, now_text)
                self.connection.execute(
                    "DELETE FROM aggregates WHERE aggregate_key = ?", (row["aggregate_key"],)
                )
        return inserted

    @staticmethod
    def _finalize_aggregate_payload(
        payload: dict[str, Any], count: int, value_sum: float
    ) -> None:
        facts = payload.setdefault("facts", {})
        if str(payload.get("type", "")).startswith("combat."):
            facts["hit_count"] = count
            facts["damage_total"] = round(value_sum, 3)
            public_view = payload.get("public_view")
            if isinstance(public_view, dict) and "signal_count" in public_view:
                public_view["signal_count"] = count
        else:
            facts["occurrence_count"] = count

    def insert_events(self, events: Iterable[dict[str, Any]]) -> int:
        now_text = iso_utc(utc_now())
        inserted = 0
        with self.connection:
            for event in events:
                inserted += self._insert_event(self.connection, event, now_text)
        return inserted

    def queue_batches(
        self,
        *,
        server_id: str,
        source_name: str,
        max_events: int,
        max_bytes: int,
        now: datetime | None = None,
    ) -> int:
        queued = 0
        now = (now or utc_now()).astimezone(timezone.utc)
        now_text = iso_utc(now)
        with self.connection:
            rows = self.connection.execute(
                "SELECT event_id, payload FROM events WHERE status = 'pending' ORDER BY created_at, event_id"
            ).fetchall()
            pending: list[tuple[str, dict[str, Any]]] = []
            for row in rows:
                payload = json.loads(row["payload"])
                try:
                    expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    expires_at = now + timedelta(minutes=1)
                if expires_at <= now:
                    self.connection.execute(
                        "UPDATE events SET status = 'expired' WHERE event_id = ?", (row["event_id"],)
                    )
                else:
                    pending.append((row["event_id"], payload))

            while pending:
                selected: list[tuple[str, dict[str, Any]]] = []
                for candidate in pending[:max_events]:
                    proposed = selected + [candidate]
                    event_ids = [item[0] for item in proposed]
                    batch_id = stable_id("batch", server_id, *event_ids)
                    body = {
                        "schema": "dayz.event-batch.v1",
                        "batch_id": batch_id,
                        "sent_at": now_text,
                        "producer": {"name": "dayz-log-monitor", "source": source_name},
                        "events": [item[1] for item in proposed],
                    }
                    if len(canonical_json(body).encode("utf-8")) > max_bytes:
                        if not selected:
                            self.connection.execute(
                                "UPDATE events SET status = 'failed' WHERE event_id = ?",
                                (candidate[0],),
                            )
                            pending.pop(0)
                        break
                    selected = proposed
                if not selected:
                    continue
                event_ids = [item[0] for item in selected]
                batch_id = stable_id("batch", server_id, *event_ids)
                body = {
                    "schema": "dayz.event-batch.v1",
                    "batch_id": batch_id,
                    "sent_at": now_text,
                    "producer": {"name": "dayz-log-monitor", "source": source_name},
                    "events": [item[1] for item in selected],
                }
                self.connection.execute(
                    "INSERT OR IGNORE INTO outbox(batch_id, payload, created_at) VALUES(?, ?, ?)",
                    (batch_id, canonical_json(body), now_text),
                )
                placeholders = ",".join("?" for _ in event_ids)
                self.connection.execute(
                    f"UPDATE events SET status = 'batched', batch_id = ? WHERE event_id IN ({placeholders})",
                    (batch_id, *event_ids),
                )
                queued += 1
                selected_ids = set(event_ids)
                pending = [item for item in pending if item[0] not in selected_ids]
        return queued

    def due_outbox(self, now_epoch: float, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT batch_id, payload, attempts FROM outbox
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY created_at LIMIT ?
            """,
            (now_epoch, limit),
        ).fetchall()
        return [
            {
                "batch_id": str(row["batch_id"]),
                "payload": json.loads(row["payload"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def mark_delivered(self, batch_id: str) -> None:
        now_text = iso_utc(utc_now())
        with self.connection:
            self.connection.execute(
                "UPDATE events SET status = 'delivered', delivered_at = ? WHERE batch_id = ?",
                (now_text, batch_id),
            )
            self.connection.execute("DELETE FROM outbox WHERE batch_id = ?", (batch_id,))

    def mark_retry(self, batch_id: str, attempts: int, error: str, now_epoch: float) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        with self.connection:
            self.connection.execute(
                """
                UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE batch_id = ?
                """,
                (attempts, now_epoch + delay, error[:300], batch_id),
            )

    def mark_dead(self, batch_id: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE outbox SET status = 'dead', last_error = ? WHERE batch_id = ?",
                (error[:300], batch_id),
            )
            self.connection.execute(
                "UPDATE events SET status = 'failed' WHERE batch_id = ?", (batch_id,)
            )

    def cleanup(self, now: datetime) -> None:
        private_cutoff = iso_utc(now - timedelta(hours=24))
        delivered_cutoff = iso_utc(now - timedelta(hours=72))
        failed_cutoff = iso_utc(now - timedelta(days=14))
        with self.connection:
            self.connection.execute(
                "UPDATE events SET payload = '{}' "
                "WHERE status = 'delivered' AND delivered_at < ? AND payload <> '{}'",
                (private_cutoff,),
            )
            self.connection.execute(
                "DELETE FROM events WHERE status IN ('delivered', 'expired') AND created_at < ?",
                (delivered_cutoff,),
            )
            self.connection.execute(
                "DELETE FROM events WHERE status = 'failed' AND created_at < ?",
                (failed_cutoff,),
            )
            self.connection.execute(
                "DELETE FROM outbox WHERE status = 'dead' AND created_at < ?",
                (failed_cutoff,),
            )
            self.connection.execute(
                "DELETE FROM player_state WHERE COALESCE(connected, 0) = 0 AND last_seen < ?",
                (private_cutoff,),
            )

    def counts(self) -> dict[str, int]:
        return {
            "pending_events": int(
                self.connection.execute("SELECT count(*) FROM events WHERE status='pending'").fetchone()[0]
            ),
            "outbox": int(
                self.connection.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0]
            ),
        }
