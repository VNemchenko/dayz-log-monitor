# Red Bastion server-only telemetry

`@RedBastionTelemetry` is a read-only DayZ server mod. It samples world/server
state and observes selected Central Economy (CE) dynamic events, then writes one
compact JSON document per signal to the native admin log:

```text
RB_EVT v1 {"schema":1,"type":"world.snapshot",...}
```

It does not edit the mission `init.c`, spawn or delete objects, call CE debug or
mutation APIs, change time/weather, decode persistence `.bin` files, make HTTP
requests, or write a second telemetry file. The existing admin-log reader can
route the `RB_EVT v1 ` records to n8n without another file-tail transport.

## Baseline and layout

The source targets DayZ `1.29.163709`. API names were checked against the
official Bohemia Interactive `DayZ-Script-Diff` build `1.29.163709`, scripts
revision `125372` ([Git commit
`86974a0f`](https://github.com/BohemiaInteractive/DayZ-Script-Diff/commit/86974a0f5bd16b1ee3e334ad828133c93dca80a1)).

```text
config/
  config.sample.json          minimal hand-editable example
  livonia.generated.json      deterministic catalog for the supplied snapshot
  livonia.world.generated.json  monitor-facing static/candidate world reference
source/RB_Telemetry/          Addon Builder input
package/mod.cpp               `@RedBastionTelemetry` metadata
tools/generate_event_catalog.py
tools/build.ps1               baseline/catalog/tests/builder preflight + package
tests/
```

The runtime reads `$profile:RBTelemetry/config.json`. The generated Livonia
catalog contains 177 possible positions for enabled events:

- 79 helicopter crashes
- 14 military convoys
- 14 trains
- 33 police situations
- 37 dynamic contaminated areas

`config.sample.json` documents the shape only; its placeholder server ID and
revision are intentionally not deployment values. Use the generated config.

The runtime scanner catalog only reads `db/events.xml`, `cfgeventspawns.xml`,
and `cfgeventgroups.xml`. For grouped events it selects a CE loot-bearing child as
the observation anchor when possible, otherwise the nearest non-decal typed
child, and expands the point radius to cover its relative offset. Output order
and `catalog_revision` are deterministic.

The optional world-reference output is deliberately separate from the 177-point
runtime scanner config and has its own revision. It contains eight configured
static contaminated areas, 151 configured player-spawn generator candidates,
1,925 animal territory candidates, and 328 infected territory candidates in
the supplied snapshot. `status: "candidate"` and
`active_state_known: false` are part of the data contract: territory XML and
player spawn bubbles do not prove what CE has active or which spawn it selected.

## Event contract

Every JSON record has `schema`, `type`, UTC timestamp, `server_id`, per-boot
`boot_id`, monotonic `seq`, `catalog_revision`, `visibility: "admin"`, and
type-specific `data`.

Emitted types:

- `telemetry.started`, `telemetry.stopped`, `telemetry.error`
- `world.snapshot`
- `world.event.present` — anchor exists on its first observation after startup
- `world.event.started` — an initially absent/ended anchor later appears
- `world.event.updated` — dynamic contamination decay stage changes
- `world.event.ended` — anchor misses the configured consecutive-scan threshold

Snapshots include game date/time/night state, actual/forecast/next-change for
overcast, rain, fog, snowfall, wind magnitude and direction, base environment
temperature, online player count, FPS statistics, and process uptime.

Event coordinates are intentionally admin-only. Downstream n8n/LLM prompts
must not expose exact coordinates unless that is an explicit administrator
workflow.

## Generate and verify

From the `dayz-log-monitor` repository:

```powershell
python servermod\tools\generate_event_catalog.py `
  --mission-dir F:\src\livonia\mpmissions\enoch_rb.enoch `
  --output servermod\config\livonia.generated.json `
  --monitor-output servermod\config\livonia.world.generated.json `
  --server-id livonia-1

python servermod\tools\generate_event_catalog.py `
  --mission-dir F:\src\livonia\mpmissions\enoch_rb.enoch `
  --output servermod\config\livonia.generated.json `
  --monitor-output servermod\config\livonia.world.generated.json `
  --server-id livonia-1 --check

python -m unittest discover -s servermod\tests -v
```

The build entry point first verifies the latest server RPT reports the exact
baseline, verifies (or explicitly refreshes) the catalog, runs tests, and then
requires DayZ Tools Addon Builder:

```powershell
& servermod\tools\build.ps1 -PreflightOnly
& servermod\tools\build.ps1 -RefreshCatalog -PreflightOnly
& servermod\tools\build.ps1 -AddonBuilderPath 'D:\DayZ Tools\Bin\AddonBuilder\AddonBuilder.exe'
```

An absent Addon Builder is a hard preflight failure. A successful local build
creates `servermod/dist/@RedBastionTelemetry/Addons/RB_Telemetry.pbo` and
`servermod/dist/profiles/RBTelemetry/config.json`, plus the non-runtime
`servermod/dist/monitor/livonia.world.json`; it never deploys them.

## Manual deployment (not performed by the build)

1. Back up the current server launch configuration and profile directory.
2. Copy `@RedBastionTelemetry` beside the other server mods.
3. Copy the generated config to `<profiles>/RBTelemetry/config.json`.
4. Add `@RedBastionTelemetry` to `-serverMod`, and keep `-adminlog` enabled.
5. Restart in a controlled window and inspect RPT/script errors first.
6. Verify one `telemetry.started`, periodic `world.snapshot`, then event records
   in the ADM log before enabling downstream automation.

## Observation limits

DayZ 1.29 does not expose a supported structured query for all active CE
events. The scanner therefore observes configured anchor object types around
known possible positions. It reports observed presence, not authoritative CE
queue state. With the default 177 points, batch size 4, and one-second tick, a
full sweep takes about 45 seconds; the two-miss end rule therefore trades false
end suppression for roughly 45–90 seconds of end-detection latency. Events that
start and end entirely between scans cannot be observed. Persistence binaries
remain deliberately out of scope.
