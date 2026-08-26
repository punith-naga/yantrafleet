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
  __main__.py    # CLI
sample.jsonl     # 5 realistic VDA 5050 v2.1 state messages (3 robots)
tests/           # fully offline (httpx.MockTransport; no network ever)
```

## Install

```bash
pip install httpx                    # core (only runtime dependency)
pip install 'paho-mqtt>=2.0'         # optional, for --mqtt-host mode
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
