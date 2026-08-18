# DayZ Log Monitor

DayZ log monitor for multiple servers with two parallel delivery contracts:

- the existing filtered/raw webhook pipeline, kept unchanged for migration;
- an opt-in typed `dayz.event-batch.v1` pipeline for n8n, Telegram and game-chat automation.

## Typed Event Pipeline (v1)

When `EVENTS_ENABLED=true`, the monitor tails all matching ADM, RPT, script and
error logs with independent SQLite cursors. Cursor advancement, parsed events and
the durable webhook outbox are committed transactionally. Incomplete final lines
remain buffered, old files are drained after rotation, and retrying a batch keeps
the same `batch_id` and `Idempotency-Key`.

The new path parses and derives:

- sessions, deaths, unconscious/recovered state, allowlisted chat commands and SOS;
- PvP, explosions and traps as immediate events;
- infected, wildlife and vehicle hits as bounded incident aggregates;
- placement, construction and dismantling;
- restart countdowns, RPT/script error fingerprints and telemetry freshness;
- `RB_EVT v1` world snapshots and observed CE-event lifecycle;
- optional read-only `storage_1` health based on names, timestamps and backup age.

Complete PlayerList frames reconcile the authoritative online roster and update
last position, movement distance/speed and movement class; none of those rows is
emitted as a standalone coordinate event. UID values are replaced with
server-scoped HMAC references. Exact coordinates and names
exist only in `admin_view`; `public_view` contains no raw log line and at most a
2x2 km sector. Static CE territory points are always labelled as configured
candidates, never as current activity.

The generated Livonia reference catalog is baked into the image for server 1.
It comes from `servermod/config/livonia.world.generated.json`; other maps must use
their own catalog or leave `EVENT_CATALOG_FILE_*` empty.

### Event webhook

The monitor sends at most 100 events and 256 KiB per request:

```json
{
  "schema": "dayz.event-batch.v1",
  "batch_id": "batch_<stable-hash>",
  "sent_at": "2026-08-17T12:00:00Z",
  "producer": {"name": "dayz-log-monitor", "source": "livonia"},
  "events": [{
    "schema": "dayz.event.v1",
    "event_id": "evt_<stable-hash>",
    "server_id": "livonia-1",
    "type": "player.sos",
    "occurred_at": "2026-08-17T11:59:58Z",
    "observed_at": "2026-08-17T12:00:00Z",
    "expires_at": "2026-08-17T12:04:58Z",
    "severity": "warning",
    "audience_ceiling": "public",
    "admin_view": {},
    "public_view": {"acknowledgement": "sos_received"},
    "facts": {},
    "source": {"kind": "adm", "file": "DayZServer_....ADM", "offset_start": 1, "offset_end": 2}
  }]
}
```

Requests use `X-DayZ-Key` and `Idempotency-Key: <batch_id>`. Both
`EVENT_WEBHOOK_KEY` and `PLAYER_HMAC_SECRET` must be independent, non-placeholder
secrets of at least 32 characters. The n8n import bundle, policy and fixtures are
documented in [`n8n/README.md`](n8n/README.md).

### Player commands

Only these commands are parsed; arbitrary chat is discarded by the event path:

- `!status`, `!weather`, `!time`, `!rules` — public-safe global response request;
- `!sos` — exact admin alert plus anonymous global acknowledgement;
- two `EmoteSOS` actions within 30 seconds — the same SOS workflow;
- `!admin <message>` — bounded admin-only text, never sent to the LLM.

All game replies remain global because BattlEye uses `say -1`.

### Enable safely

Keep `EVENT_START_AT_END=true` for first production startup so historical logs are
registered at EOF instead of replayed to n8n. Configure `EVENT_WEBHOOK_URL_*`,
`SERVER_ID_*`, the two secrets, then enable only the intended service with
`EVENTS_ENABLED_1=true` (or the corresponding `_2`/`_TEST` variable). The first
empty poll does not consume this guard: a late-mounted log directory is still
registered at EOF. After migration, the process may run typed-only with an empty
legacy `WEBHOOK_URL_*`.

To enable `storage_1` health, add a read-only mount in a local Compose override:

```yaml
services:
  dayz-log-monitor-1:
    volumes:
      - /path/to/mission/storage_1:/storage:ro
    environment:
      STORAGE_DIR: /storage
```

Never mount the persistence directory writable.

The server-only telemetry source and build preflight are documented in
[`servermod/README.md`](servermod/README.md). It does not patch the mission and it
does not interpret `events.bin`.

The service tails `DayZServer_*.ADM` files, filters noisy lines, accumulates clean lines into batch files, and sends accumulated batches to webhook by a trigger workflow.

## Pipeline Overview

1. Every `CHECK_INTERVAL` seconds, the monitor checks the newest `DayZServer_*.ADM` file.
2. It resumes reading from saved byte position (`STATE_FILE`), but resets trigger state to `0` on every startup.
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
    If the `1 -> 2` transition was caused by a non-matching chunk, that chunk is not included in this send and is appended after flush as the start of the next batch.
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
  "source": "livonia",
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
  "source": "livonia",
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
  "source": "livonia",
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
- `dayz_events/` - typed parsers, privacy projection, SQLite cursor/outbox and delivery
- `n8n/` - importable n8n workflows, policies, schemas and offline fixtures
- `servermod/` - `@RedBastionTelemetry` source and deterministic Livonia catalogs
- `tests/` - unit tests plus optional read-only Livonia snapshot replay
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
- `EVENTS_ENABLED_*` - enable the typed path independently for this service
- `EVENT_WEBHOOK_URL_*` - authenticated n8n event gateway
- `SERVER_ID_*` - stable lowercase server identifier
- `EVENT_CATALOG_FILE_*` - per-server generated static world reference
- `STORAGE_DIR_*` - optional read-only `storage_1` path inside the container
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
- `EVENT_WEBHOOK_KEY` - inbound n8n shared secret, minimum 32 characters
- `PLAYER_HMAC_SECRET` - independent HMAC secret for stable player references
- `SERVER_TIMEZONE` - IANA timezone used to convert ADM timestamps to UTC
- `EVENT_DB_FILE` - SQLite cursor/event/outbox state (default `/state/events.sqlite3`)
- `EVENT_START_AT_END` - skip existing bytes on the first typed-pipeline startup
- `EVENT_BATCH_MAX_EVENTS` / `EVENT_BATCH_MAX_BYTES` - hard payload limits
- `TELEMETRY_STALE_SECONDS` - alert threshold without a `world.snapshot`

## Verification

Run offline tests without calling webhooks or a game server:

```bash
python -m unittest discover -s tests -v
```

The 283-file Livonia replay is intentionally excluded from normal unit-test
discovery. Run it explicitly; it uses the real event pipeline, temporary SQLite
state and an in-memory fake webhook, and never writes to the snapshot:

```powershell
python -m tests.replay_livonia --source F:\src\livonia --expected-files 283
```

The command verifies source SHA256 before/after, cursor EOF state, stable unique
event IDs, no work on a second or reopened poll, and prints only aggregate
counters (never event payloads, player names or UIDs). It uses a deterministic
pre-snapshot clock so historic events exercise the fake outbox instead of being
discarded by runtime TTLs. The older ADM-only parser fixture is also opt-in with
`RUN_LIVONIA_SNAPSHOT_TEST=1`. n8n and servermod have their own offline validation
commands in their README files.

Run the opt-in local end-to-end smoke test from this repository with the sibling
`dayz-signal` checkout and its installed dependencies:

```powershell
node tests/e2e_local.js --signal-root C:\Users\VNemchenko\dayz-signal
```

The harness uses only loopback sockets and temporary files. It sends synthetic
ADM and `RB_EVT` lines through the real monitor SQLite/outbox, executes policy
code directly from the generated n8n workflow JSON with mock LLM and Telegram
destinations, starts the real `dayz-signal` HTTP service, and finishes at a local
UDP RCON emulator. It asserts one shared Russian Telegram/game message, Signal's
ASCII transliteration, and absence of raw UID or exact coordinates from every
public delivery surface. It never reads or writes `F:\src\livonia`.

This is a bounded success-path smoke test, not an embedded n8n installation: it
does not exercise Data Tables, credentials, a real LLM/Telegram API, RCON packet
loss, reconnects or multipart replies. Those remain covered by the component
test suites and disposable n8n import check. `dayz-signal` declares Node.js 24;
the summary prints the actual local Node version so an older-runtime run cannot
be mistaken for the production Node 24 gate.

Recommended rollout:

1. Import n8n workflows disabled, configure Data Tables and credentials.
2. Enable the event webhook in shadow mode for 48 hours.
3. Enable deterministic admin Telegram delivery.
4. Disable `RAW_WEBHOOK_URL` after parity is confirmed; it contains legacy raw data.
5. Enable public/game routes and their cooldowns.
6. Keep the legacy main webhook for seven more days, then disable it.

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
