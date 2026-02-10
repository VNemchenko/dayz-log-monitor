# DayZ Log Monitor

DayZ ADM log monitor for multiple servers with webhook delivery.

The service tails `DayZServer_*.ADM` files, filters noisy lines, accumulates clean lines into batch files, and sends accumulated batches to webhook by a trigger workflow.

## Pipeline Overview

1. Every `CHECK_INTERVAL` seconds, the monitor checks the newest `DayZServer_*.ADM` file.
2. It resumes reading from saved byte position (`STATE_FILE`) and keeps trigger state there.
3. Empty lines are removed.
4. Same-second HP burst lines are compacted when they differ only by `pos`/`HP`:
   - these lines become one line
   - `HP` is replaced by summed value
5. Before raw webhook send, lines are checked against required raw tokens:
   - built-in tokens from `DEFAULT_RAW_EXCLUDE_SUBSTRINGS` (in code)
   - extra tokens from `RAW_FILTER_EXCLUDE_SUBSTRINGS` (env)
   A line is sent to raw webhook only if it contains at least one required token.
6. New raw lines are sent to `RAW_WEBHOOK_URL` (if configured) with `source` and `logs`.
   This raw stream is not paused by quiet hours and does not depend on `SLEEPY`.
7. Raw lines are scanned for player pairs like `Player "Name"(id=HASH)` and written into JSON player DB (`PLAYERS_DB_FILE`).
8. Remaining lines are deduplicated by message tail:
   - if line has `|`, only text after the first `|` is used as dedupe key
   - if line has no `|`, full line is used
9. Lines are filtered by `FILTER_EXCLUDE_SUBSTRINGS` + built-in exclude tokens (case-insensitive substring match).
10. Before appending to batch, each `Player "Name"(id=...)` token is normalized using the DB:
   - name is replaced with persisted DB name
   - `id=...` is removed from the log line
11. Kept unique lines are appended into a batch file in `BATCH_DIR`.
12. On service startup, lines older than `ROTATE_MINUTES` are pruned from batch storage before any send attempt.
13. During runtime, while `trigger=0` and `SLEEPY=false`, lines older than `ROTATE_MINUTES` are pruned from batch storage.
14. Trigger state is updated from the new batch using `SEND_INCLUDE_GROUPS`:
   - Trigger starts at `0`.
   - If batch has include-group match:
     - `0 -> 1`
     - `1 -> 1`
   - If batch has no include-group match:
     - `0 -> 0`
     - `1 -> 2`
15. When trigger reaches `2`, all accumulated batch files are sent in one webhook request and then deleted.
16. Trigger resets to `0` after successful send.
17. If current local server time is inside `QUIET_HOURS_RANGE`, sending is paused and batches keep accumulating.
18. On entering quiet hours, internal `SLEEPY` is set to `true`.
19. Whenever `quiet=false` and trigger is `0`, `SLEEPY` is reset to `false` immediately.
20. Otherwise, first successful send after quiet hours includes `SLEEPY=true`; after that it is reset to `false`.

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

Include matching is evaluated against the whole processed batch for the current poll cycle.
So terms inside one `+` group may appear in different lines of that batch.

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

## Raw Pre-Filter Webhook Payload (optional)

Used only when `RAW_WEBHOOK_URL_*` is set for a service. This payload is sent every poll cycle with newly read non-empty lines, after raw webhook filtering but before main pipeline filtering and trigger logic.

```json
{
  "timestamp": "2026-02-10T12:34:56.789012",
  "source": "cherno",
  "count": 42,
  "logs": [
    "raw line 1",
    "raw line 2"
  ]
}
```

## Players DB

The service keeps a JSON DB keyed by player ID. It is updated from raw lines containing patterns like `Player "Name"(id=HASH)`.

Naming rules for new IDs:

- If observed name contains `Survivor`, persisted name is `Survivor{index}`.
- If observed name does not contain `Survivor`, persisted name is original name.
- If original name is already used by another ID, persisted name is `{name}{index}`.

Example:

```json
{
  "source": "cherno",
  "updated_at": "2026-02-09T12:34:56.789012",
  "count": 2,
  "players": {
    "HASH_1": {
      "index": 1,
      "name": "PlayerOne",
      "raw_name": "PlayerOne",
      "aliases": ["PlayerOne"]
    },
    "HASH_2": {
      "index": 2,
      "name": "Survivor2",
      "raw_name": "Survivor (2)",
      "aliases": ["Survivor", "Survivor (2)"]
    }
  }
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
- `RAW_WEBHOOK_URL_*` - optional raw pre-filter webhook for this service
- `SOURCE_NAME_*` - `source` field in payload
- `CHECK_INTERVAL_*` - poll interval in seconds
- `QUIET_HOURS_RANGE_*` - quiet window in `HH-HH` format, empty to disable
- `SEND_INCLUDE_GROUPS_*` - trigger include groups (OR-of-AND), empty to disable include filter

### Shared (optional)

- `TZ` - container timezone used for quiet hours and timestamps (example: `Europe/Moscow`)
- `RAW_FILTER_EXCLUDE_SUBSTRINGS` - extra required tokens for `RAW_WEBHOOK_URL` stream, comma/semicolon/newline separated (legacy variable name)
- `ROTATE_MINUTES` - retention window for unsent batch lines when `trigger=0` and `SLEEPY=false` (default `60`)
- `PLAYERS_DB_FILE` - path to JSON file with player ID/name mapping (default `/state/players.json`)
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
Whenever quiet mode is inactive (`quiet=false`) and trigger is `0`, `SLEEPY` is reset immediately.
Quiet-hours evaluation uses container local time configured by `TZ`.

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
