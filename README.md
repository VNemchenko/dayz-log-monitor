# DayZ Log Monitor

DayZ ADM log monitor for multiple servers with webhook delivery.

The service tails `DayZServer_*.ADM` files, filters noisy lines, accumulates clean lines into batch files, and sends accumulated batches to webhook by a trigger workflow.

## Pipeline Overview

1. Every `CHECK_INTERVAL` seconds, the monitor checks the newest `DayZServer_*.ADM` file.
2. It resumes reading from saved byte position (`STATE_FILE`) and keeps trigger state there.
3. Empty lines are removed.
4. Lines are filtered by `FILTER_EXCLUDE_SUBSTRINGS` + built-in exclude tokens (case-insensitive substring match).
5. Kept lines are appended into a batch file in `BATCH_DIR`.
6. Trigger state is updated from the new batch using `SEND_INCLUDE_GROUPS`:
   - Trigger starts at `0`.
   - If batch has include-group match:
     - `0 -> 1`
     - `1 -> 1`
   - If batch has no include-group match:
     - `0 -> 0`
     - `1 -> 2`
7. When trigger reaches `2`, all accumulated batch files are sent in one webhook request and then deleted.
8. Trigger resets to `0` after successful send.
9. If current local server time is inside `QUIET_HOURS_RANGE`, sending is paused and batches keep accumulating.
10. On entering quiet hours, internal `SLEEPY` is set to `true`.
11. First successful send after quiet hours includes `SLEEPY=true`; after that it is reset to `false`.

## Include Groups Syntax

Use `SEND_INCLUDE_GROUPS` as OR-of-AND groups:

- Group separators (`OR`): `,` `;` newline `|`
- Term separator inside group (`AND`): `+`

Example:

```env
SEND_INCLUDE_GROUPS_1=kill+player,raid+base,helicrash
```

Meaning:
- `kill` and `player`, OR
- `raid` and `base`, OR
- `helicrash`

If `SEND_INCLUDE_GROUPS_*` is empty, include filter is disabled and all processed logs are eligible for sending (still respecting quiet hours).

## Webhook Payload

```json
{
  "timestamp": "2026-02-09T12:34:56.789012",
  "source": "cherno",
  "count": 42,
  "SLEEPY": false,
  "logs": [
    "line 1",
    "line 2"
  ]
}
```

## Project Files

- `monitor.py` - log read/filter/batch/trigger/send logic
- `docker-compose.yml` - multi-server deployment
- `Dockerfile` - container image
- `docker-entrypoint.sh` - runtime startup user/permissions wrapper
- `.env.example` - configuration template

## Requirements

- Docker Desktop or Docker Engine + Compose
- Host access to DayZ ADM logs

## Quick Start

1. Create `.env` from template:

```bash
cp .env.example .env
```

2. Fill per-server required values:
- `LOGS_HOST_PATH_*`
- `WEBHOOK_URL_*`
- `SOURCE_NAME_*`

3. Run:

```bash
docker compose up -d --build
```

4. Watch logs:

```bash
docker compose logs -f
```

## Environment Variables

### Per service

- `LOGS_HOST_PATH_*` - host path with DayZ ADM logs
- `WEBHOOK_URL_*` - destination webhook
- `SOURCE_NAME_*` - `source` field in payload
- `CHECK_INTERVAL_*` - poll interval in seconds
- `QUIET_HOURS_RANGE_*` - quiet window in `HH-HH` format, empty to disable
- `SEND_INCLUDE_GROUPS_*` - trigger include groups (OR-of-AND), empty to disable include filter

### Shared (optional)

- `WEBHOOK_TIMEOUT` - HTTP timeout seconds (default `10`)
- `WEBHOOK_RETRIES` - retries per webhook request (default `3`)
- `WEBHOOK_RETRY_BACKOFF` - linear retry backoff base seconds (default `2`)
- `FILTER_EXCLUDE_SUBSTRINGS` - extra exclude tokens, comma/semicolon/newline separated

## Quiet Hours

`QUIET_HOURS_RANGE` format is `HH-HH` (24h):

- `1-8` means pause from `01:00` inclusive to `08:00` exclusive
- `23-7` means pause from `23:00` to `07:00` (cross-midnight)

Sending resumes at the end hour exactly.
The first successful webhook after the quiet window carries `SLEEPY=true`.

## Operations

Rebuild after code updates:

```bash
docker compose up -d --build
```

Restart without rebuild:

```bash
docker compose restart
```

Start only one service:

```bash
docker compose up -d --build dayz-log-monitor-test
```

## Troubleshooting

### Build looks stuck on `apt-get`

First build can be slow when image packages are installed. This is normal.

### `/state` permission problems

If `/state` is not writable, monitor falls back to `/tmp/dayz-log-monitor`.

### No logs found

Check bind mount path and filename pattern `DayZServer_*.ADM`.

### Data not sent yet

Check:
- quiet hours are currently inactive
- trigger reached `2` (look at service logs)
- webhook is reachable and returns `2xx`
