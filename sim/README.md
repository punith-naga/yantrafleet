# yantrasim — Yantrika warehouse AMR simulator

Simulates a fleet of **10 AMRs from 3 vendors** (`nexomotion`, `agilus`,
`boturo`) navigating a warehouse waypoint graph (8×5 grid, 4 m spacing, two
chargers, two docks), and emits **VDA 5050 v2.1-compliant state messages**
each tick. Two transports, selected by CLI flag:

- `--supabase` (default): translates each state to a `robots`-table row and
  bulk-upserts via PostgREST (`httpx`), inserts `alerts` rows on faults /
  low battery, and heartbeats `fleet_meta` (id=1).
- `--mqtt`: publishes proper VDA topics
  (`uagv/v2/<manufacturer>/<serialNumber>/state`, plus retained QoS-1
  `connection` ONLINE/OFFLINE). `paho-mqtt` is an **optional** dependency.

## Quick start

```bash
pip install httpx                  # only hard dependency
python -m yantrasim --supabase --interval 2        # default mode
python -m yantrasim --mqtt --broker localhost      # needs: pip install paho-mqtt
python -m yantrasim --stdout --ticks 30 --interval 0   # offline fast-forward
```

Useful flags: `--ticks N` (stop after N ticks; 0 = forever), `--seed`
(deterministic runs), `--time-scale` (simulated seconds per wall second,
default 5), `--url/--key` or env `SUPABASE_URL`/`SUPABASE_KEY` (override the
embedded client-safe anon key), `--verbose`.

## Simulation model

Per tick (default 10 simulated seconds) each robot runs a small state
machine: `idle → moving → working → idle`, diverting `to_charger →
charging` below 20% battery (leaves at 90%). Battery drains by activity;
motor temp rises while driving; health dips on faults and recovers.
Random faults (`motor_overtemp`, `obstacle_blocked`, `estop`) occur with
low probability, and **AMR-07 has a scripted localization fault at tick
20** (FATAL `localizationError`, `positionInitialized:false`) lasting 12
ticks. All randomness flows through one seeded RNG, so runs reproduce.

## VDA 5050 compliance notes

- Header (`headerId` per-robot increment, ISO-8601 ms UTC timestamp,
  `version: "2.1.0"`, manufacturer, serialNumber) matches the topic path.
- Serial numbers are sanitized to the spec charset (`AMR-07` → `AMR_07`
  on the wire); the Supabase `robots.id` keeps the raw fleet id `AMR-07`.
- `errors[]` is always present (empty when healthy) with 2.1's
  `errorHint`; `safetyState`, `batteryState`, `operatingMode`,
  `nodeStates`/`edgeStates` (edges odd / nodes even sequenceIds),
  `actionStates` with `RUNNING` during pick/drop.
- v3.0 renames (`positionInitialized→localized`, `batteryState→powerSupply`)
  are confined to `vda.py`/`translate.py`, so a v3 builder is a table
  change, not a sim change.
- MQTT: `state` QoS 0 non-retained; `connection` QoS 1 retained. One
  process multiplexes 10 vehicles over one client, so per-vehicle
  last-wills are not registered (documented deviation in `transports/mqtt.py`).

## Supabase mapping

Status is derived dashboard-style from the VDA message alone: FATAL error
→ `fault`; eStop/fieldViolation → `safety_stop`; WARNING errors →
`degraded`; then `charging` / `working` (RUNNING action) / `moving` /
`paused` / `idle`. Upserts use `?on_conflict=id` with
`Prefer: resolution=merge-duplicates, return=minimal` (one bulk POST per
table per tick); alerts use `ignore-duplicates` with deterministic ids
(`al-<robot>-<kind>-<tick>`) so retries are idempotent.

## Layout

```
yantrasim/
  world.py        # waypoint graph (pure)
  sim.py          # simulation core (pure, seeded RNG)
  vda.py          # VDA 5050 v2.1 builders + topic/serial sanitizers (pure)
  translate.py    # VDA state -> Supabase rows (pure)
  transports/
    base.py       # Transport protocol + stdout transport
    supabase.py   # httpx bulk upsert (injectable client)
    mqtt.py       # optional paho; injectable fake client
  __main__.py     # CLI
tests/            # fully offline: httpx.MockTransport + fake MQTT client
```

## Tests

```bash
pip install pytest
python -m pytest        # 43 tests, no network required
```
