#!/usr/bin/env python3
import glob
import os
import time
from datetime import datetime
from typing import Optional, Tuple

import requests


LOGS_DIR = os.getenv("LOGS_DIR", "/logs")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RAW_STATE_FILE = os.getenv("STATE_FILE", "/state/position.txt")
FALLBACK_STATE_FILE = "/tmp/dayz-log-monitor/position.txt"


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


CHECK_INTERVAL = read_int_env("CHECK_INTERVAL", 30, 1)
WEBHOOK_TIMEOUT = read_int_env("WEBHOOK_TIMEOUT", 10, 1)
WEBHOOK_RETRIES = read_int_env("WEBHOOK_RETRIES", 3, 1)
WEBHOOK_RETRY_BACKOFF = read_int_env("WEBHOOK_RETRY_BACKOFF", 2, 1)


def resolve_state_file(path: str) -> str:
    state_dir = os.path.dirname(path) or "."
    probe_file = os.path.join(state_dir, ".write-test")

    try:
        os.makedirs(state_dir, exist_ok=True)
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

        fallback_dir = os.path.dirname(FALLBACK_STATE_FILE)
        os.makedirs(fallback_dir, exist_ok=True)
        print(
            f"[warn] STATE_FILE {path} is not writable ({exc}). "
            f"Using volatile fallback {FALLBACK_STATE_FILE}."
        )
        return FALLBACK_STATE_FILE


STATE_FILE = resolve_state_file(RAW_STATE_FILE)


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


def monitor_logs() -> None:
    """Main monitoring loop."""
    print("=== DayZ Log Monitor started ===")
    print(f"Logs directory: {LOGS_DIR}")
    print(f"Webhook URL: {mask_secret(WEBHOOK_URL)}")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print(f"State file: {STATE_FILE}")
    print(
        f"Webhook timeout: {WEBHOOK_TIMEOUT}s, "
        f"retries: {WEBHOOK_RETRIES}, "
        f"backoff: {WEBHOOK_RETRY_BACKOFF}s"
    )
    print()

    last_file, last_position = load_state()

    while True:
        try:
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
                        print(f"[info] Found {len(new_lines)} new non-empty log lines")
                        delivered = send_to_webhook(new_lines)
                        if not delivered:
                            print("[warn] Keeping current offset to retry delivery next cycle")
                            time.sleep(CHECK_INTERVAL)
                            continue

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
