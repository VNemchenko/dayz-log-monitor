#!/usr/bin/env python3
from __future__ import annotations
import glob
import os
import re
import time
from datetime import datetime
from typing import Optional, Tuple

import requests


LOGS_DIR = os.getenv("LOGS_DIR", "/logs")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
SOURCE_NAME = os.getenv("SOURCE_NAME", "dayz-server").strip() or "dayz-server"
RAW_STATE_FILE = os.getenv("STATE_FILE", "/state/position.txt")
RAW_BATCH_DIR = os.getenv("BATCH_DIR", "/state/batches")
QUIET_HOURS_RANGE_RAW = os.getenv("QUIET_HOURS_RANGE", "").strip()
SEND_INCLUDE_GROUPS_RAW = os.getenv("SEND_INCLUDE_GROUPS", "").strip()
FALLBACK_STATE_FILE = "/tmp/dayz-log-monitor/position.txt"
FALLBACK_BATCH_DIR = "/tmp/dayz-log-monitor/batches"

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
]


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
WEBHOOK_TIMEOUT = read_int_env("WEBHOOK_TIMEOUT", 10, 1)
WEBHOOK_RETRIES = read_int_env("WEBHOOK_RETRIES", 3, 1)
WEBHOOK_RETRY_BACKOFF = read_int_env("WEBHOOK_RETRY_BACKOFF", 2, 1)
SEND_INTERVAL_SECONDS = read_int_env("SEND_INTERVAL_SECONDS", 1200, 1)
CUSTOM_EXCLUDE_SUBSTRINGS = read_list_env("FILTER_EXCLUDE_SUBSTRINGS")
QUIET_HOURS_RANGE = parse_quiet_hours_range(QUIET_HOURS_RANGE_RAW)
SEND_INCLUDE_GROUPS = parse_send_include_groups(SEND_INCLUDE_GROUPS_RAW)

EXCLUDE_SUBSTRINGS = []
seen_tokens = set()
for token in DEFAULT_EXCLUDE_SUBSTRINGS + CUSTOM_EXCLUDE_SUBSTRINGS:
    token_cf = token.casefold()
    if token_cf in seen_tokens:
        continue
    seen_tokens.add(token_cf)
    EXCLUDE_SUBSTRINGS.append(token)

EXCLUDE_SUBSTRINGS_CASEFOLD = [token.casefold() for token in EXCLUDE_SUBSTRINGS]
SAFE_SOURCE_NAME = sanitize_source_name(SOURCE_NAME)
STATE_FILE = resolve_state_file(RAW_STATE_FILE)
BATCH_DIR = resolve_writable_dir(RAW_BATCH_DIR, FALLBACK_BATCH_DIR, "BATCH_DIR")
BATCH_FILENAME_RE = re.compile(
    rf"^{re.escape(SAFE_SOURCE_NAME)}_(\d{{8}}_\d{{6}})(?:_\d+)?\.log$"
)

if QUIET_HOURS_RANGE:
    QUIET_HOURS_LABEL = f"{QUIET_HOURS_RANGE[0]:02d}-{QUIET_HOURS_RANGE[1]:02d}"
else:
    QUIET_HOURS_LABEL = "<disabled>"


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


def load_state() -> Tuple[Optional[str], int]:
    """Load monitor state (file path + byte offset)."""
    if not os.path.exists(STATE_FILE):
        return None, 0

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_handle:
            rows = state_handle.read().splitlines()

        if not rows:
            return None, 0

        filepath = rows[0].strip() or None
        if len(rows) >= 2 and rows[1].strip():
            position = int(rows[1].strip())
        else:
            position = 0

        if position < 0:
            raise ValueError("position must be non-negative")

        return filepath, position
    except (OSError, ValueError) as exc:
        print(f"[warn] Failed to load state from {STATE_FILE}: {exc}. Starting from 0.")
        return None, 0


def save_state(filepath: str, position: int) -> None:
    """Save monitor state atomically."""
    if not filepath:
        return

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temp_state_file = f"{STATE_FILE}.tmp"

    with open(temp_state_file, "w", encoding="utf-8") as state_handle:
        state_handle.write(f"{filepath}\n{position}\n")

    os.replace(temp_state_file, STATE_FILE)


def send_to_webhook(lines: list[str]) -> bool:
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


def list_batch_files() -> list[str]:
    pattern = os.path.join(BATCH_DIR, f"{SAFE_SOURCE_NAME}_*.log")
    return sorted(glob.glob(pattern))


def parse_batch_started_at(filepath: str) -> datetime:
    filename = os.path.basename(filepath)
    match = BATCH_FILENAME_RE.match(filename)
    if match:
        timestamp_raw = match.group(1)
        try:
            return datetime.strptime(timestamp_raw, "%Y%m%d_%H%M%S")
        except ValueError:
            pass

    return datetime.fromtimestamp(os.path.getmtime(filepath))


def batch_age_seconds(filepath: str, now_dt: datetime) -> int:
    started_at = parse_batch_started_at(filepath)
    return int((now_dt - started_at).total_seconds())


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

    latest_batch = batch_files[-1]
    if batch_age_seconds(latest_batch, now_dt) >= SEND_INTERVAL_SECONDS:
        return create_batch_file(now_dt)

    return latest_batch


def append_lines_to_batch(lines: list[str], now_dt: datetime) -> str:
    batch_file = get_batch_file_for_append(now_dt)

    with open(batch_file, "a", encoding="utf-8") as batch_handle:
        for line in lines:
            batch_handle.write(f"{line}\n")

    return batch_file


def read_batch_lines(batch_file: str) -> list[str]:
    with open(batch_file, "r", encoding="utf-8", errors="ignore") as batch_handle:
        lines = [line.rstrip("\n\r") for line in batch_handle if line.strip()]

    return lines


def apply_send_include_filter(lines: list[str]) -> Tuple[list[str], int]:
    if not SEND_INCLUDE_GROUPS:
        return lines, 0

    kept_lines = []
    dropped_count = 0

    for line in lines:
        line_cf = line.casefold()
        matched = any(all(term in line_cf for term in group) for group in SEND_INCLUDE_GROUPS)

        if matched:
            kept_lines.append(line)
        else:
            dropped_count += 1

    return kept_lines, dropped_count


def send_due_batches(now_dt: datetime, force_send_all: bool = False) -> None:
    for batch_file in list_batch_files():
        age_seconds = batch_age_seconds(batch_file, now_dt)
        if not force_send_all and age_seconds < SEND_INTERVAL_SECONDS:
            continue

        batch_lines = read_batch_lines(batch_file)
        if not batch_lines:
            os.remove(batch_file)
            print(f"[info] Removed empty batch file: {os.path.basename(batch_file)}")
            continue

        filtered_batch_lines, filtered_out_count = apply_send_include_filter(batch_lines)
        if filtered_out_count:
            print(
                f"[info] Send-filter excluded {filtered_out_count} lines from "
                f"{os.path.basename(batch_file)}"
            )

        if not filtered_batch_lines:
            os.remove(batch_file)
            print(
                f"[info] Removed batch {os.path.basename(batch_file)}: "
                "no lines matched SEND_INCLUDE_GROUPS"
            )
            continue

        send_reason = "forced_after_quiet_hours" if force_send_all else "due_interval"
        print(
            f"[info] Sending batch {os.path.basename(batch_file)} "
            f"({len(filtered_batch_lines)} lines, age={age_seconds}s, reason={send_reason})"
        )

        delivered = send_to_webhook(filtered_batch_lines)
        if not delivered:
            print(f"[warn] Keeping batch file for retry: {os.path.basename(batch_file)}")
            break

        os.remove(batch_file)
        print(f"[ok] Batch sent and removed: {os.path.basename(batch_file)}")


def monitor_logs() -> None:
    """Main monitoring loop."""
    print("=== DayZ Log Monitor started ===")
    print(f"Logs directory: {LOGS_DIR}")
    print(f"Source name: {SOURCE_NAME}")
    print(f"Webhook URL: {mask_secret(WEBHOOK_URL)}")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print(f"Send interval: {SEND_INTERVAL_SECONDS}s")
    print(f"Quiet hours: {QUIET_HOURS_LABEL}")
    print(f"State file: {STATE_FILE}")
    print(f"Batch dir: {BATCH_DIR}")
    print(
        f"Webhook timeout: {WEBHOOK_TIMEOUT}s, "
        f"retries: {WEBHOOK_RETRIES}, "
        f"backoff: {WEBHOOK_RETRY_BACKOFF}s"
    )
    print(f"Filter excludes: {len(EXCLUDE_SUBSTRINGS)} substrings")
    if SEND_INCLUDE_GROUPS:
        print(f"Send include groups: {len(SEND_INCLUDE_GROUPS)}")
    else:
        print("Send include groups: <disabled> (all lines pass)")
    print()

    last_file, last_position = load_state()
    was_in_quiet_hours = False

    while True:
        now_dt = datetime.now()

        try:
            in_quiet_hours = is_in_quiet_hours(now_dt)

            if in_quiet_hours:
                if not was_in_quiet_hours:
                    print(
                        "[info] Entered quiet hours window "
                        f"({QUIET_HOURS_LABEL}); sending paused."
                    )
            else:
                if was_in_quiet_hours:
                    print(
                        "[info] Quiet hours ended; sending all accumulated batches now."
                    )
                    send_due_batches(now_dt, force_send_all=True)
                else:
                    send_due_batches(now_dt)

            was_in_quiet_hours = in_quiet_hours

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
                        kept_lines, dropped_count = filter_log_lines(new_lines)
                        if dropped_count:
                            print(
                                f"[info] Filtered out {dropped_count} lines "
                                f"by exclude substrings"
                            )

                        if kept_lines:
                            batch_file = append_lines_to_batch(kept_lines, now_dt)
                            print(
                                f"[info] Appended {len(kept_lines)} lines to "
                                f"{os.path.basename(batch_file)}"
                            )

                    last_position = new_position
                    save_state(last_file, last_position)

        except Exception as exc:
            print(f"[error] Unexpected error: {exc}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if not WEBHOOK_URL:
        print("[error] WEBHOOK_URL is required")
        raise SystemExit(1)

    monitor_logs()

