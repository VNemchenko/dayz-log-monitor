#!/usr/bin/env python3
from __future__ import annotations
import builtins
import glob
import json
import os
import re
import time
from datetime import datetime
from typing import Optional, Tuple

import requests


def print_with_timestamp(*args, **kwargs) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    builtins.print(f"[{timestamp}]", *args, **kwargs)


print = print_with_timestamp


LOGS_DIR = os.getenv("LOGS_DIR", "/logs")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RAW_WEBHOOK_URL = os.getenv("RAW_WEBHOOK_URL", "").strip()
SOURCE_NAME = os.getenv("SOURCE_NAME", "dayz-server").strip() or "dayz-server"
RAW_STATE_FILE = os.getenv("STATE_FILE", "/state/position.txt")
RAW_BATCH_DIR = os.getenv("BATCH_DIR", "/state/batches")
RAW_PLAYERS_DB_FILE = os.getenv("PLAYERS_DB_FILE", "/state/players.json")
QUIET_HOURS_RANGE_RAW = os.getenv("QUIET_HOURS_RANGE", "").strip()
SEND_INCLUDE_GROUPS_RAW = os.getenv("SEND_INCLUDE_GROUPS", "").strip()
FALLBACK_STATE_FILE = "/tmp/dayz-log-monitor/position.txt"
FALLBACK_BATCH_DIR = "/tmp/dayz-log-monitor/batches"
FALLBACK_PLAYERS_DB_FILE = "/tmp/dayz-log-monitor/players.json"

DEFAULT_EXCLUDE_SUBSTRINGS = [
    "****************",
    "#####",
    "connected",
    "EmoteSitA",
    "AdminLog",
    "built",
    "placed",
    ")):",
    "packed",
    "Dug in",
    "Dug out",
    "choosing to respawn",
    "Dismantled",
    "teleported",
    "folded",
    "has raised",
    "Mounted",
]
# Despite legacy name, RAW_EXCLUDE here works as required include tokens
# for RAW_WEBHOOK_URL stream: a line is sent only if it matches at least
# one token from this list.
DEFAULT_RAW_EXCLUDE_SUBSTRINGS: list[str] = ["connect", ")):", "killed by Player"]


def read_int_env(name: str, default: int, min_value: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        print(f"[warn] {name}={raw_value!r} is not an integer, using {default}")
        return default

    if value < min_value:
        print(f"[warn] {name}={value} must be >= {min_value}, using {default}")
        return default

    return value


def read_list_env(name: str) -> list[str]:
    raw_value = os.getenv(name, "")
    if not raw_value:
        return []

    values = []
    for item in re.split(r"[,;\n]", raw_value):
        cleaned = item.strip()
        if cleaned:
            values.append(cleaned)

    return values


def build_unique_substrings(*token_groups: list[str]) -> list[str]:
    merged_tokens = []
    seen_tokens = set()

    for token_group in token_groups:
        for token in token_group:
            token_cf = token.casefold()
            if token_cf in seen_tokens:
                continue
            seen_tokens.add(token_cf)
            merged_tokens.append(token)

    return merged_tokens


def resolve_writable_dir(path: str, fallback_path: str, label: str) -> str:
    probe_file = os.path.join(path, ".write-test")

    try:
        os.makedirs(path, exist_ok=True)
        with open(probe_file, "w", encoding="utf-8"):
            pass
        os.remove(probe_file)
        return path
    except OSError as exc:
        try:
            if os.path.exists(probe_file):
                os.remove(probe_file)
        except OSError:
            pass

        os.makedirs(fallback_path, exist_ok=True)
        print(
            f"[warn] {label} {path} is not writable ({exc}). "
            f"Using fallback {fallback_path}."
        )
        return fallback_path


def resolve_state_file(path: str) -> str:
    state_dir = os.path.dirname(path) or "."
    fallback_state_dir = os.path.dirname(FALLBACK_STATE_FILE)
    resolved_state_dir = resolve_writable_dir(state_dir, fallback_state_dir, "STATE_FILE dir")

    if os.path.normpath(resolved_state_dir) == os.path.normpath(state_dir):
        return path

    return FALLBACK_STATE_FILE


def resolve_players_db_file(path: str) -> str:
    db_dir = os.path.dirname(path) or "."
    fallback_db_dir = os.path.dirname(FALLBACK_PLAYERS_DB_FILE)
    resolved_db_dir = resolve_writable_dir(db_dir, fallback_db_dir, "PLAYERS_DB_FILE dir")

    if os.path.normpath(resolved_db_dir) == os.path.normpath(db_dir):
        return path

    return FALLBACK_PLAYERS_DB_FILE


def sanitize_source_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return sanitized or "dayz-server"


def parse_quiet_hours_range(value: str) -> Optional[Tuple[int, int]]:
    if not value:
        return None

    match = re.match(r"^\s*([01]?\d|2[0-3])\s*-\s*([01]?\d|2[0-3])\s*$", value)
    if not match:
        print(
            f"[warn] QUIET_HOURS_RANGE={value!r} has invalid format. "
            "Expected 'HH-HH' (e.g. '1-6' or '23-7')."
        )
        return None

    start_hour = int(match.group(1))
    end_hour = int(match.group(2))

    if start_hour == end_hour:
        print(
            f"[warn] QUIET_HOURS_RANGE={value!r} has zero length. "
            "Quiet hours disabled."
        )
        return None

    return start_hour, end_hour


def parse_send_include_groups(value: str) -> list[list[str]]:
    if not value:
        return []

    groups = []
    raw_groups = [item.strip() for item in re.split(r"[,;\n|]", value) if item.strip()]

    for raw_group in raw_groups:
        terms = [term.strip().casefold() for term in re.split(r"\s*\+\s*", raw_group) if term.strip()]
        if not terms:
            continue
        groups.append(terms)

    return groups


CHECK_INTERVAL = read_int_env("CHECK_INTERVAL", 30, 1)
ROTATE_MINUTES = read_int_env("ROTATE_MINUTES", 60, 1)
WEBHOOK_TIMEOUT = read_int_env("WEBHOOK_TIMEOUT", 10, 1)
WEBHOOK_RETRIES = read_int_env("WEBHOOK_RETRIES", 3, 1)
WEBHOOK_RETRY_BACKOFF = read_int_env("WEBHOOK_RETRY_BACKOFF", 2, 1)
CUSTOM_EXCLUDE_SUBSTRINGS = read_list_env("FILTER_EXCLUDE_SUBSTRINGS")
CUSTOM_RAW_EXCLUDE_SUBSTRINGS = read_list_env("RAW_FILTER_EXCLUDE_SUBSTRINGS")
QUIET_HOURS_RANGE = parse_quiet_hours_range(QUIET_HOURS_RANGE_RAW)
SEND_INCLUDE_GROUPS = parse_send_include_groups(SEND_INCLUDE_GROUPS_RAW)
SEND_INCLUDE_GROUPS_ENABLED = bool(SEND_INCLUDE_GROUPS)

EXCLUDE_SUBSTRINGS = build_unique_substrings(
    DEFAULT_EXCLUDE_SUBSTRINGS, CUSTOM_EXCLUDE_SUBSTRINGS
)
RAW_REQUIRED_SUBSTRINGS = build_unique_substrings(
    DEFAULT_RAW_EXCLUDE_SUBSTRINGS, CUSTOM_RAW_EXCLUDE_SUBSTRINGS
)
EXCLUDE_SUBSTRINGS_CASEFOLD = [token.casefold() for token in EXCLUDE_SUBSTRINGS]
RAW_REQUIRED_SUBSTRINGS_CASEFOLD = [token.casefold() for token in RAW_REQUIRED_SUBSTRINGS]
SAFE_SOURCE_NAME = sanitize_source_name(SOURCE_NAME)
STATE_FILE = resolve_state_file(RAW_STATE_FILE)
BATCH_DIR = resolve_writable_dir(RAW_BATCH_DIR, FALLBACK_BATCH_DIR, "BATCH_DIR")
PLAYERS_DB_FILE = resolve_players_db_file(RAW_PLAYERS_DB_FILE)

if QUIET_HOURS_RANGE:
    QUIET_HOURS_LABEL = f"{QUIET_HOURS_RANGE[0]:02d}-{QUIET_HOURS_RANGE[1]:02d}"
else:
    QUIET_HOURS_LABEL = "<disabled>"

TRIGGER_READY_TO_SEND = 2
BATCH_ROTATE_SECONDS = ROTATE_MINUTES * 60
LOG_LINE_TS_RE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2})\s*\|\s*(?P<body>.*)$")
HP_VALUE_RE = re.compile(r"\[HP:\s*(-?\d+(?:\.\d+)?)\]")
POS_TOKEN_RE = re.compile(r"pos=<[^>]+>")
PLAYER_ID_PAIR_RE = re.compile(
    r'Player\s+"(?P<name>[^"]+)"\s*(?:\([^)]*\)\s*)*\(id=(?P<id>[^)\s]+)'
)
PLAYER_TOKEN_RE = re.compile(
    r'Player\s+"(?P<name>[^"]+)"(?P<prefix>\s*(?:\([^)]*\)\s*)*)'
    r'\(id=(?P<id>[^)\s]+)(?P<tail>[^)]*)\)'
)


def mask_secret(secret: str) -> str:
    if not secret:
        return "<not set>"
    if len(secret) <= 8:
        return "********"
    return f"{secret[:4]}...{secret[-4:]}"


def get_latest_log_file() -> Optional[str]:
    """Find the latest DayZ ADM log file."""
    pattern = os.path.join(LOGS_DIR, "DayZServer_*.ADM")
    files = glob.glob(pattern)

    if not files:
        return None

    return max(files, key=os.path.getmtime)


def load_state() -> Tuple[Optional[str], int, int, bool]:
    """Load monitor state (file path + byte offset + trigger state + sleepy flag)."""
    if not os.path.exists(STATE_FILE):
        return None, 0, 0, False

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_handle:
            rows = state_handle.read().splitlines()

        if not rows:
            return None, 0, 0, False

        filepath = rows[0].strip() or None
        if len(rows) >= 2 and rows[1].strip():
            position = int(rows[1].strip())
        else:
            position = 0

        if len(rows) >= 3 and rows[2].strip():
            trigger_state = int(rows[2].strip())
        else:
            trigger_state = 0

        if len(rows) >= 4 and rows[3].strip():
            sleepy_pending = bool(int(rows[3].strip()))
        else:
            sleepy_pending = False

        if position < 0:
            raise ValueError("position must be non-negative")
        if trigger_state < 0:
            raise ValueError("trigger_state must be non-negative")
        if len(rows) >= 4 and rows[3].strip() and rows[3].strip() not in {"0", "1"}:
            raise ValueError("sleepy flag must be 0 or 1")

        return filepath, position, trigger_state, sleepy_pending
    except (OSError, ValueError) as exc:
        print(f"[warn] Failed to load state from {STATE_FILE}: {exc}. Starting from 0.")
        return None, 0, 0, False


def save_state(filepath: str, position: int, trigger_state: int, sleepy_pending: bool) -> None:
    """Save monitor state atomically."""
    if not filepath:
        return

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temp_state_file = f"{STATE_FILE}.tmp"

    with open(temp_state_file, "w", encoding="utf-8") as state_handle:
        state_handle.write(
            f"{filepath}\n{position}\n{trigger_state}\n{1 if sleepy_pending else 0}\n"
        )

    os.replace(temp_state_file, STATE_FILE)


def load_players_db() -> dict[str, dict[str, object]]:
    if not os.path.exists(PLAYERS_DB_FILE):
        return {}

    try:
        with open(PLAYERS_DB_FILE, "r", encoding="utf-8") as db_handle:
            raw_data = json.load(db_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] Failed to load players DB from {PLAYERS_DB_FILE}: {exc}. Starting empty.")
        return {}

    if not isinstance(raw_data, dict):
        print(f"[warn] Invalid players DB format in {PLAYERS_DB_FILE}: root must be object.")
        return {}

    raw_players = raw_data.get("players", raw_data)
    if not isinstance(raw_players, dict):
        print(f"[warn] Invalid players DB format in {PLAYERS_DB_FILE}: 'players' must be object.")
        return {}

    players_db: dict[str, dict[str, object]] = {}
    used_indexes: set[int] = set()
    for player_id, raw_entry in raw_players.items():
        if not isinstance(player_id, str):
            continue
        normalized_id = player_id.strip()
        if not normalized_id:
            continue

        if isinstance(raw_entry, str):
            player_name = raw_entry.strip()
            if not player_name:
                continue
            players_db[normalized_id] = {
                "name": player_name,
                "raw_name": player_name,
                "aliases": [player_name],
            }
            continue

        if not isinstance(raw_entry, dict):
            continue

        raw_name = raw_entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        player_name = raw_name.strip()

        aliases: list[str] = []
        raw_aliases = raw_entry.get("aliases")
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str):
                    alias_clean = alias.strip()
                    if alias_clean and alias_clean not in aliases:
                        aliases.append(alias_clean)
        if player_name not in aliases:
            aliases.insert(0, player_name)

        entry: dict[str, object] = {
            "name": player_name,
            "raw_name": str(raw_entry.get("raw_name", player_name)).strip() or player_name,
            "aliases": aliases,
        }

        raw_index = raw_entry.get("index")
        if isinstance(raw_index, int) and raw_index > 0 and raw_index not in used_indexes:
            entry["index"] = raw_index
            used_indexes.add(raw_index)

        last_seen = raw_entry.get("last_seen")
        if isinstance(last_seen, str) and last_seen.strip():
            entry["last_seen"] = last_seen.strip()

        players_db[normalized_id] = entry

    next_index = 1
    for player_id in sorted(players_db):
        entry = players_db[player_id]
        if isinstance(entry.get("index"), int) and entry["index"] > 0:
            continue

        while next_index in used_indexes:
            next_index += 1
        entry["index"] = next_index
        used_indexes.add(next_index)
        next_index += 1

    return players_db


def save_players_db(players_db: dict[str, dict[str, object]], now_dt: datetime) -> None:
    os.makedirs(os.path.dirname(PLAYERS_DB_FILE) or ".", exist_ok=True)
    temp_db_file = f"{PLAYERS_DB_FILE}.tmp"

    normalized_players: dict[str, dict[str, object]] = {}
    for player_id in sorted(players_db):
        entry = players_db[player_id]
        player_name = str(entry.get("name", "")).strip()
        if not player_name:
            continue

        aliases: list[str] = []
        for alias in entry.get("aliases", []):
            alias_str = str(alias).strip()
            if alias_str and alias_str not in aliases:
                aliases.append(alias_str)
        if player_name not in aliases:
            aliases.insert(0, player_name)

        normalized_entry: dict[str, object] = {
            "name": player_name,
            "aliases": aliases,
        }

        player_index = entry.get("index")
        if isinstance(player_index, int) and player_index > 0:
            normalized_entry["index"] = player_index

        raw_name = str(entry.get("raw_name", "")).strip()
        if raw_name:
            normalized_entry["raw_name"] = raw_name

        last_seen = entry.get("last_seen")
        if isinstance(last_seen, str) and last_seen.strip():
            normalized_entry["last_seen"] = last_seen.strip()

        normalized_players[player_id] = normalized_entry

    payload = {
        "source": SOURCE_NAME,
        "updated_at": now_dt.isoformat(),
        "count": len(normalized_players),
        "players": normalized_players,
    }

    with open(temp_db_file, "w", encoding="utf-8") as db_handle:
        json.dump(payload, db_handle, ensure_ascii=False, indent=2)
        db_handle.write("\n")

    os.replace(temp_db_file, PLAYERS_DB_FILE)


def extract_player_id_name_pairs(lines: list[str]) -> list[Tuple[str, str]]:
    pairs: list[Tuple[str, str]] = []
    for line in lines:
        for match in PLAYER_ID_PAIR_RE.finditer(line):
            player_name = match.group("name").strip()
            player_id = match.group("id").strip()
            if not player_name or not player_id:
                continue
            pairs.append((player_id, player_name))
    return pairs


def get_next_player_index(players_db: dict[str, dict[str, object]]) -> int:
    max_index = 0
    for entry in players_db.values():
        entry_index = entry.get("index")
        if isinstance(entry_index, int) and entry_index > max_index:
            max_index = entry_index
    return max_index + 1


def is_survivor_name(player_name: str) -> bool:
    return "survivor" in player_name.casefold()


def select_persisted_player_name(
    observed_name: str,
    player_index: int,
    players_db: dict[str, dict[str, object]],
) -> str:
    if is_survivor_name(observed_name):
        return f"Survivor{player_index}"

    used_names = {
        str(entry.get("name", "")).strip()
        for entry in players_db.values()
        if str(entry.get("name", "")).strip()
    }

    candidate = observed_name
    if candidate in used_names:
        candidate = f"{observed_name}{player_index}"

    suffix = 2
    while candidate in used_names:
        candidate = f"{observed_name}{player_index}_{suffix}"
        suffix += 1

    return candidate


def get_persisted_player_name(
    players_db: dict[str, dict[str, object]],
    player_id: str,
    fallback_name: str,
) -> str:
    entry = players_db.get(player_id)
    if not entry:
        return fallback_name

    resolved_name = str(entry.get("name", "")).strip()
    if not resolved_name:
        return fallback_name

    return resolved_name


def update_players_db(
    players_db: dict[str, dict[str, object]],
    pairs: list[Tuple[str, str]],
    now_dt: datetime,
) -> Tuple[int, int]:
    if not pairs:
        return 0, 0

    latest_by_id: dict[str, str] = {}
    for player_id, player_name in pairs:
        latest_by_id[player_id] = player_name

    new_ids = 0
    alias_updates = 0
    now_iso = now_dt.isoformat()

    for player_id, player_name in latest_by_id.items():
        existing = players_db.get(player_id)
        if existing is None:
            player_index = get_next_player_index(players_db)
            persisted_name = select_persisted_player_name(player_name, player_index, players_db)
            players_db[player_id] = {
                "index": player_index,
                "name": persisted_name,
                "raw_name": player_name,
                "aliases": [player_name],
                "last_seen": now_iso,
            }
            new_ids += 1
            continue

        existing["last_seen"] = now_iso
        existing["raw_name"] = player_name

        current_name = str(existing.get("name", "")).strip()
        aliases = [str(alias).strip() for alias in existing.get("aliases", []) if str(alias).strip()]
        alias_added = False
        if player_name not in aliases:
            aliases.append(player_name)
            alias_added = True
        existing["aliases"] = aliases

        if alias_added and player_name != current_name:
            alias_updates += 1

    return new_ids, alias_updates


def sanitize_line_for_batch(
    line: str,
    players_db: dict[str, dict[str, object]],
) -> Tuple[str, int]:
    replacements = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1

        original_name = match.group("name").strip()
        player_id = match.group("id").strip()
        prefix = (match.group("prefix") or "").rstrip()
        tail = (match.group("tail") or "").strip()

        persisted_name = get_persisted_player_name(players_db, player_id, original_name)
        tail_block = f" ({tail})" if tail else ""

        return f'Player "{persisted_name}"{prefix}{tail_block}'

    sanitized = PLAYER_TOKEN_RE.sub(_replace, line)
    return sanitized, replacements


def sanitize_lines_for_batch(
    lines: list[str],
    players_db: dict[str, dict[str, object]],
) -> Tuple[list[str], int]:
    sanitized_lines = []
    total_replacements = 0

    for line in lines:
        sanitized_line, replacements = sanitize_line_for_batch(line, players_db)
        sanitized_lines.append(sanitized_line)
        total_replacements += replacements

    return sanitized_lines, total_replacements


def send_raw_lines_to_webhook(lines: list[str]) -> bool:
    """Send raw lines (before filtering) to optional common webhook.

    This path is intentionally independent from quiet-hours/SLEEPY logic.
    """
    if not lines:
        return True

    if not RAW_WEBHOOK_URL:
        return True

    payload = {
        "timestamp": datetime.now().isoformat(),
        "source": SOURCE_NAME,
        "count": len(lines),
        "logs": lines,
    }

    for attempt in range(1, WEBHOOK_RETRIES + 1):
        try:
            response = requests.post(
                RAW_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=WEBHOOK_TIMEOUT,
            )

            if 200 <= response.status_code < 300:
                print(
                    f"[ok] Delivered {len(lines)} raw pre-filter lines "
                    f"(HTTP {response.status_code})"
                )
                return True

            print(
                f"[warn] Raw webhook returned HTTP {response.status_code} "
                f"(attempt {attempt}/{WEBHOOK_RETRIES})"
            )
        except requests.RequestException as exc:
            print(
                f"[warn] Raw webhook request failed on attempt "
                f"{attempt}/{WEBHOOK_RETRIES}: {exc}"
            )

        if attempt < WEBHOOK_RETRIES:
            sleep_seconds = WEBHOOK_RETRY_BACKOFF * attempt
            print(f"[info] Retrying raw webhook in {sleep_seconds}s")
            time.sleep(sleep_seconds)

    print(f"[error] Raw pre-filter delivery failed after {WEBHOOK_RETRIES} attempts")
    return False


def send_to_webhook(lines: list[str], sleepy: bool) -> bool:
    """Send lines to webhook and return delivery status."""
    if not lines:
        return True

    if not WEBHOOK_URL:
        print("[error] WEBHOOK_URL is not set")
        return False

    payload = {
        "timestamp": datetime.now().isoformat(),
        "source": SOURCE_NAME,
        "count": len(lines),
        "SLEEPY": sleepy,
        "logs": lines,
    }

    for attempt in range(1, WEBHOOK_RETRIES + 1):
        try:
            response = requests.post(
                WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=WEBHOOK_TIMEOUT,
            )

            if 200 <= response.status_code < 300:
                print(f"[ok] Delivered {len(lines)} lines (HTTP {response.status_code})")
                return True

            print(
                f"[warn] Webhook returned HTTP {response.status_code} "
                f"(attempt {attempt}/{WEBHOOK_RETRIES})"
            )
        except requests.RequestException as exc:
            print(
                f"[warn] Webhook request failed on attempt "
                f"{attempt}/{WEBHOOK_RETRIES}: {exc}"
            )

        if attempt < WEBHOOK_RETRIES:
            sleep_seconds = WEBHOOK_RETRY_BACKOFF * attempt
            print(f"[info] Retrying webhook in {sleep_seconds}s")
            time.sleep(sleep_seconds)

    print(f"[error] Delivery failed after {WEBHOOK_RETRIES} attempts")
    return False


def read_new_content(filepath: str, last_position: int) -> Tuple[list[str], int]:
    """Read new file content from byte offset and return non-empty lines + new offset."""
    with open(filepath, "r", encoding="utf-8", errors="ignore") as log_handle:
        log_handle.seek(last_position)
        new_lines = []

        for line in log_handle:
            cleaned_line = line.rstrip("\n\r")
            if cleaned_line:
                new_lines.append(cleaned_line)

        new_position = log_handle.tell()

    return new_lines, new_position


def filter_log_lines(lines: list[str]) -> Tuple[list[str], int]:
    kept_lines = []
    dropped_count = 0

    for line in lines:
        line_cf = line.casefold()
        if any(token in line_cf for token in EXCLUDE_SUBSTRINGS_CASEFOLD):
            dropped_count += 1
            continue
        kept_lines.append(line)

    return kept_lines, dropped_count


def filter_raw_webhook_lines(lines: list[str]) -> Tuple[list[str], int]:
    if not RAW_REQUIRED_SUBSTRINGS_CASEFOLD:
        return lines, 0

    kept_lines = []
    dropped_count = 0

    for line in lines:
        line_cf = line.casefold()
        if any(token in line_cf for token in RAW_REQUIRED_SUBSTRINGS_CASEFOLD):
            kept_lines.append(line)
            continue
        dropped_count += 1

    return kept_lines, dropped_count


def dedupe_lines_by_tail(lines: list[str]) -> Tuple[list[str], int]:
    unique_lines = []
    seen = set()
    dropped_duplicates = 0

    for line in lines:
        if "|" in line:
            _, tail = line.split("|", 1)
            dedupe_key = tail.strip()
        else:
            dedupe_key = line

        if dedupe_key in seen:
            dropped_duplicates += 1
            continue

        seen.add(dedupe_key)
        unique_lines.append(line)

    return unique_lines, dropped_duplicates


def format_hp_sum(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def compact_hp_burst_lines(lines: list[str]) -> Tuple[list[str], int, int]:
    """Collapse same-second HP burst lines that only differ by pos/HP."""
    groups: dict[tuple[str, str], dict[str, object]] = {}
    ordered_items: list[Tuple[str, object]] = []

    for line in lines:
        ts_match = LOG_LINE_TS_RE.match(line)
        if not ts_match:
            ordered_items.append(("plain", line))
            continue

        body = ts_match.group("body")
        hp_match = HP_VALUE_RE.search(body)
        if not hp_match:
            ordered_items.append(("plain", line))
            continue

        try:
            hp_value = float(hp_match.group(1))
        except ValueError:
            ordered_items.append(("plain", line))
            continue

        normalized_body = POS_TOKEN_RE.sub("pos=<POS>", body)
        normalized_body = HP_VALUE_RE.sub("[HP:<SUM>]", normalized_body, count=1)
        key = (ts_match.group("ts"), normalized_body)

        group = groups.get(key)
        if group is None:
            groups[key] = {
                "first_line": line,
                "hp_sum": hp_value,
                "count": 1,
            }
            ordered_items.append(("group", key))
            continue

        group["hp_sum"] = float(group["hp_sum"]) + hp_value
        group["count"] = int(group["count"]) + 1

    compacted_lines: list[str] = []
    collapsed_groups = 0
    removed_lines = 0

    for item_type, payload in ordered_items:
        if item_type == "plain":
            compacted_lines.append(str(payload))
            continue

        group = groups[payload]  # type: ignore[index]
        count = int(group["count"])
        first_line = str(group["first_line"])

        if count <= 1:
            compacted_lines.append(first_line)
            continue

        collapsed_groups += 1
        removed_lines += count - 1
        hp_sum_text = format_hp_sum(float(group["hp_sum"]))
        collapsed_line = HP_VALUE_RE.sub(
            f"[HP: {hp_sum_text}]",
            first_line,
            count=1,
        )
        compacted_lines.append(f"{collapsed_line} [collapsed x{count}]")

    return compacted_lines, collapsed_groups, removed_lines


def list_batch_files() -> list[str]:
    pattern = os.path.join(BATCH_DIR, f"{SAFE_SOURCE_NAME}_*.log")
    return sorted(glob.glob(pattern))


def is_in_quiet_hours(now_dt: datetime) -> bool:
    if not QUIET_HOURS_RANGE:
        return False

    start_hour, end_hour = QUIET_HOURS_RANGE
    current_hour = now_dt.hour

    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour

    return current_hour >= start_hour or current_hour < end_hour


def create_batch_file(now_dt: datetime) -> str:
    timestamp = now_dt.strftime("%Y%m%d_%H%M%S")
    base_path = os.path.join(BATCH_DIR, f"{SAFE_SOURCE_NAME}_{timestamp}")
    batch_path = f"{base_path}.log"
    suffix = 1

    while os.path.exists(batch_path):
        batch_path = f"{base_path}_{suffix:02d}.log"
        suffix += 1

    with open(batch_path, "a", encoding="utf-8"):
        pass

    return batch_path


def get_batch_file_for_append(now_dt: datetime) -> str:
    batch_files = list_batch_files()

    if not batch_files:
        return create_batch_file(now_dt)

    return batch_files[-1]


def append_lines_to_batch(lines: list[str], now_dt: datetime) -> str:
    batch_file = get_batch_file_for_append(now_dt)
    line_ts = int(now_dt.timestamp())

    with open(batch_file, "a", encoding="utf-8") as batch_handle:
        for line in lines:
            batch_handle.write(f"{line_ts}\t{line}\n")

    return batch_file


def parse_batch_line(line: str) -> Tuple[Optional[int], str]:
    if "\t" in line:
        ts_raw, payload = line.split("\t", 1)
        if ts_raw.isdigit():
            return int(ts_raw), payload

    return None, line


def read_batch_entries(batch_file: str) -> list[Tuple[Optional[int], str]]:
    with open(batch_file, "r", encoding="utf-8", errors="ignore") as batch_handle:
        entries = []

        for raw_line in batch_handle:
            line = raw_line.rstrip("\n\r")
            if not line:
                continue

            ts, payload = parse_batch_line(line)
            if not payload.strip():
                continue
            entries.append((ts, payload))

    return entries


def write_batch_entries(batch_file: str, entries: list[Tuple[int, str]]) -> None:
    with open(batch_file, "w", encoding="utf-8") as batch_handle:
        for ts, payload in entries:
            batch_handle.write(f"{ts}\t{payload}\n")


def read_batch_lines(batch_file: str) -> list[str]:
    entries = read_batch_entries(batch_file)
    lines = [payload for _, payload in entries]

    return lines


def prune_expired_batch_lines(now_dt: datetime, reason: str) -> None:
    cutoff_ts = int(now_dt.timestamp()) - BATCH_ROTATE_SECONDS
    if cutoff_ts <= 0:
        return

    files_removed = 0
    files_rewritten = 0
    lines_pruned = 0

    for batch_file in list_batch_files():
        entries = read_batch_entries(batch_file)
        if not entries:
            os.remove(batch_file)
            files_removed += 1
            continue

        fallback_ts = int(os.path.getmtime(batch_file))
        kept_entries: list[Tuple[int, str]] = []
        pruned_from_file = 0

        for ts, payload in entries:
            entry_ts = fallback_ts if ts is None else ts
            if entry_ts < cutoff_ts:
                pruned_from_file += 1
                continue
            kept_entries.append((entry_ts, payload))

        if pruned_from_file == 0:
            continue

        lines_pruned += pruned_from_file
        if kept_entries:
            write_batch_entries(batch_file, kept_entries)
            files_rewritten += 1
        else:
            os.remove(batch_file)
            files_removed += 1

    if lines_pruned or files_removed:
        print(
            f"[info] Batch cleanup ({reason}): "
            f"pruned {lines_pruned} old lines, "
            f"rewrote {files_rewritten} files, "
            f"removed {files_removed} files"
        )


def batch_has_send_include_match(lines: list[str]) -> bool:
    if not SEND_INCLUDE_GROUPS_ENABLED:
        # Include filter disabled: all lines are eligible for trigger matching.
        return True

    for line in lines:
        line_cf = line.casefold()
        if any(all(term in line_cf for term in group) for group in SEND_INCLUDE_GROUPS):
            return True

    return False


def update_trigger_state(trigger_state: int, batch_has_match: bool) -> int:
    if not SEND_INCLUDE_GROUPS_ENABLED:
        # No include groups configured: any non-empty processed batch should be sent.
        return TRIGGER_READY_TO_SEND

    if trigger_state <= 0:
        return 1 if batch_has_match else 0

    if trigger_state == 1:
        return 1 if batch_has_match else TRIGGER_READY_TO_SEND

    return trigger_state


def log_trigger_state(trigger_state: int, sleepy_pending: bool, in_quiet_hours: bool, reason: str) -> None:
    print(
        "[info] Trigger state: "
        f"value={trigger_state}, "
        f"SLEEPY={sleepy_pending}, "
        f"quiet={in_quiet_hours}, "
        f"reason={reason}"
    )


def flush_all_batches(send_reason: str, sleepy: bool) -> bool:
    batch_files = list_batch_files()
    if not batch_files:
        print("[info] Trigger fired but no batch files found.")
        return True

    files_with_data = []
    all_lines = []

    for batch_file in batch_files:
        batch_lines = read_batch_lines(batch_file)
        if not batch_lines:
            os.remove(batch_file)
            print(f"[info] Removed empty batch file: {os.path.basename(batch_file)}")
            continue

        files_with_data.append(batch_file)
        all_lines.extend(batch_lines)

    if not all_lines:
        return True

    print(
        "[info] Sending accumulated logs "
        f"({len(all_lines)} lines, files={len(files_with_data)}, reason={send_reason}, "
        f"SLEEPY={sleepy})"
    )
    delivered = send_to_webhook(all_lines, sleepy=sleepy)
    if not delivered:
        print("[warn] Keeping accumulated batch files for retry")
        return False

    for batch_file in files_with_data:
        os.remove(batch_file)

    print(f"[ok] Sent and removed {len(files_with_data)} batch files")
    return True


def monitor_logs() -> None:
    """Main monitoring loop."""
    print("=== DayZ Log Monitor started ===")
    print(f"Logs directory: {LOGS_DIR}")
    print(f"Source name: {SOURCE_NAME}")
    print(f"Webhook URL: {mask_secret(WEBHOOK_URL)}")
    if RAW_WEBHOOK_URL:
        print(f"Raw webhook URL: {mask_secret(RAW_WEBHOOK_URL)}")
        print("Raw webhook mode: always send on new lines (independent from SLEEPY/quiet hours)")
    else:
        print("Raw webhook URL: <disabled>")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print(
        "Batch retention window: "
        f"{ROTATE_MINUTES} minute(s) when trigger=0 and SLEEPY=false"
    )
    print("Send mode: trigger-based (send when trigger reaches 2)")
    print(f"Quiet hours: {QUIET_HOURS_LABEL}")
    print("Sleepy trigger: enabled (SLEEPY=true after quiet hours, resets after first send)")
    print(f"State file: {STATE_FILE}")
    print(f"Batch dir: {BATCH_DIR}")
    print(f"Players DB file: {PLAYERS_DB_FILE}")
    print(
        f"Webhook timeout: {WEBHOOK_TIMEOUT}s, "
        f"retries: {WEBHOOK_RETRIES}, "
        f"backoff: {WEBHOOK_RETRY_BACKOFF}s"
    )
    print(f"Raw filter required tokens: {len(RAW_REQUIRED_SUBSTRINGS)} substrings")
    print(f"Filter excludes: {len(EXCLUDE_SUBSTRINGS)} substrings")
    print("Deduplicate mode: enabled (unique by text after first '|')")
    if SEND_INCLUDE_GROUPS_ENABLED:
        print(f"Send include groups: {len(SEND_INCLUDE_GROUPS)}")
    else:
        print("Send include groups: <disabled> (all lines can trigger send)")
    print()

    last_file, last_position, trigger_state, sleepy_pending = load_state()
    players_db = load_players_db()
    print(f"[info] Players DB loaded: {len(players_db)} ids")
    startup_now = datetime.now()
    prune_expired_batch_lines(startup_now, reason="startup")
    was_in_quiet_hours = False
    trigger_waiting_in_quiet_logged = False
    log_trigger_state(
        trigger_state,
        sleepy_pending,
        in_quiet_hours=is_in_quiet_hours(startup_now),
        reason="startup",
    )

    while True:
        now_dt = datetime.now()

        try:
            in_quiet_hours = is_in_quiet_hours(now_dt)

            if (
                not SEND_INCLUDE_GROUPS_ENABLED
                and trigger_state < TRIGGER_READY_TO_SEND
                and list_batch_files()
            ):
                trigger_state = TRIGGER_READY_TO_SEND
                print(
                    "[info] Include groups are disabled and batch files exist; "
                    "trigger set to 2."
                )

            log_trigger_state(trigger_state, sleepy_pending, in_quiet_hours, "loop")

            if in_quiet_hours:
                if not was_in_quiet_hours:
                    sleepy_pending = True
                    print(
                        "[info] Entered quiet hours window "
                        f"({QUIET_HOURS_LABEL}); sending paused."
                    )
                    print("[info] SLEEPY set to true for next successful send.")
                    save_state(last_file, last_position, trigger_state, sleepy_pending)
            else:
                if was_in_quiet_hours:
                    print(
                        "[info] Quiet hours ended."
                    )

            if not in_quiet_hours and trigger_state == 0 and sleepy_pending:
                sleepy_pending = False
                print(
                    "[info] SLEEPY reset to false "
                    "(quiet=false and trigger=0)."
                )
                save_state(last_file, last_position, trigger_state, sleepy_pending)

            was_in_quiet_hours = in_quiet_hours

            if trigger_state >= TRIGGER_READY_TO_SEND:
                if in_quiet_hours:
                    if not trigger_waiting_in_quiet_logged:
                        print(
                            "[info] Trigger is armed (2), waiting for quiet hours to end "
                            "before sending."
                        )
                        trigger_waiting_in_quiet_logged = True
                else:
                    trigger_waiting_in_quiet_logged = False
                    delivered = flush_all_batches("trigger_ready", sleepy=sleepy_pending)
                    if delivered:
                        if sleepy_pending:
                            print("[info] SLEEPY reset to false after successful send.")
                            sleepy_pending = False
                        trigger_state = 0
                        print("[info] Trigger reset: 2 -> 0")
                        save_state(last_file, last_position, trigger_state, sleepy_pending)
                    else:
                        print("[warn] Trigger remains armed due to delivery failure")
            else:
                trigger_waiting_in_quiet_logged = False

            if trigger_state == 0 and not sleepy_pending:
                prune_expired_batch_lines(now_dt, reason="loop")

            current_file = get_latest_log_file()
            if not current_file:
                print("[warn] No DayZ log files found, waiting...")
                time.sleep(CHECK_INTERVAL)
                continue

            if last_file != current_file:
                print(f"[info] Switched to new log file: {os.path.basename(current_file)}")
                last_file = current_file
                last_position = 0

            file_size = os.path.getsize(current_file)

            if file_size < last_position:
                print("[warn] Log file was truncated, resetting position to 0")
                last_position = 0

            if file_size > last_position:
                new_lines, new_position = read_new_content(current_file, last_position)

                if new_position > last_position:
                    if new_lines:
                        raw_lines_for_player_db = new_lines

                        compacted_lines, collapsed_groups, collapsed_lines = compact_hp_burst_lines(
                            new_lines
                        )
                        if collapsed_groups:
                            print(
                                "[info] Collapsed HP burst lines: "
                                f"groups={collapsed_groups}, removed={collapsed_lines}"
                            )
                        new_lines = compacted_lines

                        raw_lines, raw_dropped_count = filter_raw_webhook_lines(new_lines)
                        if raw_dropped_count:
                            print(
                                f"[info] Filtered out {raw_dropped_count} raw lines "
                                "for RAW_WEBHOOK_URL: no required raw token match"
                            )

                        # Raw pre-filter stream is delivered regardless of SLEEPY/quiet-hours state.
                        raw_delivered = send_raw_lines_to_webhook(raw_lines)
                        if not raw_delivered:
                            print(
                                "[warn] Raw pre-filter webhook delivery failed; "
                                "continuing normal pipeline."
                            )

                        id_name_pairs = extract_player_id_name_pairs(raw_lines_for_player_db)
                        if id_name_pairs:
                            new_ids, alias_updates = update_players_db(
                                players_db, id_name_pairs, now_dt
                            )
                            if new_ids or alias_updates:
                                try:
                                    save_players_db(players_db, now_dt)
                                    print(
                                        "[info] Players DB updated: "
                                        f"new_ids={new_ids}, alias_updates={alias_updates}, "
                                        f"total_ids={len(players_db)}"
                                    )
                                except OSError as exc:
                                    print(f"[warn] Failed to save players DB: {exc}")

                        deduped_raw_lines, duplicate_count = dedupe_lines_by_tail(new_lines)
                        if duplicate_count:
                            print(
                                f"[info] Deduplicated {duplicate_count} raw lines "
                                "by message tail (ignoring timestamp before '|')"
                            )
                        new_lines = deduped_raw_lines

                        kept_lines, dropped_count = filter_log_lines(new_lines)
                        if dropped_count:
                            print(
                                f"[info] Filtered out {dropped_count} lines "
                                f"by exclude substrings"
                            )

                        if kept_lines:
                            sanitized_lines, sanitized_tokens = sanitize_lines_for_batch(
                                kept_lines, players_db
                            )
                            if sanitized_tokens:
                                print(
                                    "[info] Sanitized log lines for batch: "
                                    f"player_tokens={sanitized_tokens} "
                                    "(replaced names by DB and removed id)"
                                )

                            batch_file = append_lines_to_batch(sanitized_lines, now_dt)
                            print(
                                f"[info] Appended {len(sanitized_lines)} lines to "
                                f"{os.path.basename(batch_file)}"
                            )

                            has_trigger_match = batch_has_send_include_match(sanitized_lines)
                            previous_trigger = trigger_state
                            trigger_state = update_trigger_state(trigger_state, has_trigger_match)
                            if trigger_state != previous_trigger:
                                print(
                                    f"[info] Trigger updated: {previous_trigger} -> {trigger_state} "
                                    f"(matched={has_trigger_match})"
                                )

                            if trigger_state >= TRIGGER_READY_TO_SEND:
                                if in_quiet_hours:
                                    print(
                                        "[info] Trigger reached 2 during quiet hours; "
                                        "sending will start after quiet window."
                                    )
                                else:
                                    delivered = flush_all_batches(
                                        "trigger_reached", sleepy=sleepy_pending
                                    )
                                    if delivered:
                                        if sleepy_pending:
                                            print(
                                                "[info] SLEEPY reset to false after successful send."
                                            )
                                            sleepy_pending = False
                                        trigger_state = 0
                                        print("[info] Trigger reset: 2 -> 0")
                                    else:
                                        print(
                                            "[warn] Trigger remains armed due to delivery failure"
                                        )

                    last_position = new_position
                    save_state(last_file, last_position, trigger_state, sleepy_pending)

        except Exception as exc:
            print(f"[error] Unexpected error: {exc}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if not WEBHOOK_URL:
        print("[error] WEBHOOK_URL is required")
        raise SystemExit(1)

    monitor_logs()
