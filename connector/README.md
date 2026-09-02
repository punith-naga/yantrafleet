# yantrabridge — VDA 5050 → Supabase connector

Consumes **VDA 5050 v2.1 `state` messages** (live from MQTT, or replayed from
a JSONL file) and translates them into the YantraFleet Supabase tables:

| Output | Table | Write mode |
|---|---|---|
| Robot telemetry row | `robots` | upsert (`on_conflict=id`, merge-duplicates) |
| Alerts from `errors[]` + low battery | `alerts` | insert (ignore-duplicates → retry-safe) |
| Connector heartbeat | `fleet_meta` (id=1) | upsert (only `writer_id`, `updated_at`) |

## Layout

```
yantrabridge/
  translate.py   # PURE: state msg -> robots row; AlertDeduper; Translator facade
  sink.py        # SupabaseSink: PostgREST writes via httpx (transport injectable)
  sources.py     # read_jsonl() + optional MqttSource (paho-mqtt)
  importer.py    # bring-your-own-recording: MCAP/JSONL -> history (optional mcap)
  __main__.py    # CLI
sample.jsonl     # 5 realistic VDA 5050 v2.1 state messages (3 robots)
tests/           # fully offline (httpx.MockTransport; no network ever)
```

## Install

```bash
pip install httpx                    # core (only runtime dependency)
pip install 'paho-mqtt>=2.0'         # optional, for --mqtt-host mode
pip install 'mcap>=1.0'              # optional, for `import --mcap` (or: pip install 'yantrabridge[import]')
pip install pytest                   # dev/tests
```

## Usage

```bash
# Dry-run: translate the bundled sample and print rows (no network)
python -m yantrabridge --file sample.jsonl --dry-run

# Replay a JSONL file into Supabase
python -m yantrabridge --file sample.jsonl

# Live MQTT bridge (subscribes uagv/v2/+/+/state by default)
python -m yantrabridge --mqtt-host broker.local --mqtt-port 1883
```

Configuration (env vars override the embedded client-safe defaults):

| Env var | Default |
|---|---|
| `SUPABASE_URL` | `https://flwyvhsmgrrqpmhcqlzd.supabase.co` |
| `SUPABASE_KEY` | embedded publishable anon key (client-safe) |

Flags: `--battery-threshold` (default 20%), `--mqtt-topic`, `--mqtt-username`,
`--mqtt-password`, `--supabase-url`, `--supabase-key`.

## Translation rules

**`robots` row** (from each state message; identity keyed by
`serialNumber`, `vendor` = `manufacturer`):

- `status`: priority order — FATAL/CRITICAL/URGENT error → `fault`;
  `eStop != NONE` or `fieldViolation` → `estop`; `batteryState.charging` →
  `charging`; then `paused` / `active` (driving) / `idle`.
- `battery` = `batteryState.batteryCharge`; `pos` = `[x, y]` from
  `agvPosition`; `speed` = `hypot(vx, vy)` from `velocity`.
- `task_kind`: first RUNNING/INITIALIZING action's `actionType`, else
  `transit` while driving on an order, else null.
- `health`: heuristic 0–100 (−40 per fatal, −10 per warning, −15 safety
  stop, −10 low battery).
- `fault_msg`: first fatal error's description.
- `motor_temp` / `tasks_done`: not part of core VDA 5050 — passed through
  only when the vendor extends the payload (`motorTemp`/`motorTemperature`,
  `tasksDone`/`tasksCompleted`); otherwise the columns are **omitted** so the
  upsert never clobbers another writer's values.

**Alerts + dedup** (`AlertDeduper`):

- Each VDA error keyed by `(serial, errorType, errorLevel)`. An alert fires
  on the inactive→active transition only; while the error persists across
  state messages, nothing is re-emitted. When it disappears from `errors[]`
  the key retires, so a later recurrence raises a **new** alert (new id).
- Low battery: fires once when `batteryCharge ≤ 20%` (crit at ≤ 10%), resets
  only after recovery above 25% (5-point hysteresis to prevent flapping).
- Severity map: `WARNING → warn`, `FATAL → crit` (v3.0's `CRITICAL`/`URGENT`
  also map to `crit` for forward-compat).
- Alert ids are deterministic (`al-<serial>-<type>-<hash(serial,type,level,ts)>`),
  and the sink inserts with `resolution=ignore-duplicates`, so retrying a
  failed batch never duplicates rows.

**Dedup scope**: the ledger is in-memory per process. Restarting the
connector while an error is still active will re-alert once. Acceptable for
v0.1; persist the ledger (small JSON file) if that becomes noisy.

## Import a recording

Bring-your-own-recording: point the connector at an **MCAP** file from any
robot — a ROS 2 rosbag recorded with JSON-encoded topics, a Foxglove
recording, or anything else writing raw JSON channels — and it becomes
fleet history in Supabase. No ROS installation is needed; the importer reads
raw channels/messages with the `mcap` Python package and decodes JSON
payloads directly.

```bash
pip install 'yantrabridge[import]'      # pulls mcap>=1.0

# Inspect first: topics found, message counts, time range, rows that would
# be written — no network
python -m yantrabridge import --mcap run.mcap --dry-run

# Bulk import as fast as possible (default, --rate 0): batched
# robot_telemetry inserts, final robots state, alerts from errors[]
python -m yantrabridge import --mcap run.mcap

# Demo mode: replay at 4x real time, updating the robots table live so the
# console dashboard animates the recording
python -m yantrabridge import --mcap run.mcap --rate 4

# JSONL recordings go through the same pipeline
python -m yantrabridge import --jsonl sample.jsonl
```

What gets written:

| Output | Table | Notes |
|---|---|---|
| Final state per robot | `robots` | upsert, last message wins |
| One history row per message | `robot_telemetry` | **original recording timestamps preserved in `ts`** |
| Alerts from VDA `errors[]` | `alerts` | same dedup as the live bridge |

Messages on topics that look like VDA 5050 `state` (auto-detected per
payload: `serialNumber` + state-schema fields) go through the exact same
translation code as the live MQTT bridge. Everything else is skipped —
unless you map it.

### Topic map: import any schema

`--topic-map map.json` maps arbitrary topic names/fields to the fleet
columns. The spec is a JSON object of `{"<topic-pattern>": {<target>:
<source>, ...}}`:

```json
{
  "/acme/*/telemetry": {
    "robot_id": "topic[1]",
    "battery": "power.soc",
    "pos.x": "pose.x",
    "pos.y": "pose.y",
    "speed": "vel",
    "status": "mode",
    "status_map": {"MOVING": "active", "DOCKED": "charging"}
  }
}
```

- **Topic patterns** match exactly, or with `*` wildcards (fnmatch).
- **Targets**: `robot_id` (required), `battery`, `pos.x`, `pos.y`, `speed`,
  `status`, `motor_temp`, `ts` (an ISO timestamp inside the payload;
  defaults to the MCAP log time).
- **Sources** are one of:
  - `"a.b.c"` — dotted path into the JSON payload;
  - `"topic[N]"` — Nth segment of the topic (0-based, leading `/` stripped),
    handy when the robot id lives in the topic name;
  - `"=literal"` — the literal string after `=`.
- Optional `status_map` renames raw status values; unmapped values pass
  through unchanged.
- A matching topic-map entry always wins over VDA auto-detection; messages
  whose `robot_id` resolves to nothing are counted as skipped.

### Replay vs bulk

`--rate 0` (default) is a **bulk import**: the whole file is translated in
one pass, telemetry rows are inserted in batches of 500, robots get one
final-state upsert each. Use it to backfill history. `--rate N` is a
**replay**: messages are sorted by timestamp and pushed one at a time at Nx
real time, so the `robots` table (and any dashboard watching it) updates
live — a recorded incident plays back like it's happening now.

### Why bother

`robot_telemetry` is what the console's **incident replay** scrubs through:
once a recording is imported with its original timestamps, that shift's
battery sag, speed profile and fault appear on the timeline exactly when
they happened — bring a bag file from any robot, get a forensic replay of
the incident in the console. The FATAL error in the recording lands in
`alerts` with the original `created_at`, so it lines up with the telemetry
around it.

Notes: imported rows get `site_id` from the DB default (the connector never
stamps it); `robot_telemetry` has an identity primary key, so re-importing
the same file duplicates history rows — dry-run first, or purge with
`purge_old_telemetry()`.

## Testing (fully offline)

Supabase is intentionally never contacted by the tests — `SupabaseSink`
accepts an injectable `httpx` transport, and all sink tests run against
`httpx.MockTransport`, asserting URL paths, `on_conflict` params, `Prefer`
headers and JSON payloads. Translation/dedup tests are pure.

```bash
cd connector && python -m pytest
```

## Notes / limitations

- One PostgREST batch must have identical keys on every row; the sink groups
  rows by key-set automatically (matters when some robots carry vendor
  extensions and others don't).
- Writes use `Prefer: return=minimal` to conserve free-tier egress.
- The anon key is publishable/client-safe by design; writes rely on the
  project's current demo-open RLS. Harden RLS before production (see the
  project's Supabase notes).
- v3.0 forward-compat: severity map already handles the new error levels;
  field renames (`batteryState`→`powerSupply` etc.) would be added as a
  per-majorVersion mapping table in `translate.py`.
