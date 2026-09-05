# YantraFleet — Feature Contracts (migrations 0009–0016)

Every table, column, view and RPC added by `supabase/0009_*.sql` through
`supabase/0016_*.sql`, with exact names, exact argument names and types,
exact return shapes, and which role may call each one.

This file is the contract. If something is not written here, do not assume it.

---

## 0. How to read this file

* **Roles** are 0007's hierarchy: `operator < engineer < manager < admin`,
  enforced by `public.yf_has_role(<role>, <site>)` / `public.yf_rank()`. An
  `admin` at *any* site is treated as admin everywhere.
* **"anon"** means callable with the publishable/anon key, no session.
* **"service_role"** means callable only with the service key — never from a
  browser. A grant to `authenticated` is *absent*, not merely unused.
* Every RPC returns `json` and is called over PostgREST as
  `POST /rest/v1/rpc/<name>` with a JSON object body whose keys are the
  argument names *exactly as spelled here* (`p_site`, not `site`).
* Timestamps are ISO-8601 strings with an offset (`timestamptz`).
  Arguments typed `timestamptz` accept an ISO-8601 string.
* Fields documented as nullable really can be `null`; render for that.
* **Apply order matters.** All eight migrations are `OPT-IN` (skipped by a
  plain `yantraops migrate`; pass `--include-opt-in`). All of them require
  `0007_rbac.sql`; 0011–0016 additionally require `public.yf_can_read_site()`
  and `public.yf_is_service_role()`, which are defined in **0009**; 0016
  requires `missions.completed_at`, defined in **0012**. In short: apply
  0007 → 0008 → 0009 → … → 0016 in filename order.

### The shared site-read gate

```
public.yf_can_read_site(p_site text) -> boolean     -- 0009
```

True when the caller holds `operator` or higher at `p_site` (admins: every
site), **or** when the request carries a live demo token whose sandbox *is*
`p_site`. Every read-oriented RPC in 0011–0013 uses it, which is why those
RPCs are granted to `anon`: the grant alone authorises nothing, and an anon
caller without a live demo token gets an exception.

### The demo token header

A demo sandbox is addressed by sending

```
x-yf-demo-token: <the 64-hex token from demo_mint_session>
```

on **every** PostgREST request (table reads/writes and RPC calls alike). It is
read by `public.yf_demo_token()` from `current_setting('request.headers')`.
It is only honoured when there is no authenticated JWT — 0009's policies are
declared `to anon`.

### Offline testing

`e2e/fakerest.py` emulates every table and RPC below. Start it with
`FakePostgREST(rbac=True, service_key="sk-test")` to get the role checks;
build caller tokens with `make_test_jwt(email, yf_role, site)`; seed roles for
users who are not the caller (0014 needs this) with `fake.set_user_role(...)`.
Differences from the real database are called out per feature under
**Fake divergences**.

---

## 1. Ephemeral demo sandbox — `0009_demo_sandbox.sql`

### Tables

#### `public.demo_sessions`
No grants to `anon` or `authenticated`; reachable only through the RPCs.

| column | type | notes |
|---|---|---|
| `token` | `text` **PK** | 64 hex chars, bearer secret |
| `site_id` | `text` **UNIQUE, NOT NULL** | `DEMO-<12 uppercase hex>` |
| `created_at` | `timestamptz NOT NULL` | default `now()` |
| `expires_at` | `timestamptz NOT NULL` | |
| `claimed` | `boolean NOT NULL` | default `false` |
| `claimed_at` | `timestamptz` | |
| `reaped_at` | `timestamptz` | non-null ⇒ rows purged, token dead |
| `origin` | `text` | free-text marker, e.g. `'marketing'` |

#### `public.demo_limits`
Single row, `id = 1`. No grants to `anon`/`authenticated`.

| column | type | default |
|---|---|---|
| `id` | `int` **PK** `check (id = 1)` | `1` |
| `enabled` | `boolean NOT NULL` | `true` |
| `ttl_minutes` | `int NOT NULL` (5..1440) | `60` |
| `max_live_sessions` | `int NOT NULL` (0..10000) | `25` |
| `max_seed_robots` | `int NOT NULL` (0..50) | `12` |
| `updated_at` | `timestamptz NOT NULL` | `now()` |
| `updated_by` | `text` | |

### What a demo token may touch

Tables: `robots`, `alerts`, `incidents`, `missions`, `robot_telemetry`,
`maintenance_findings`, `commands` — **SELECT / INSERT / UPDATE only, and only
rows whose `site_id` equals the token's sandbox site.** Every insert and update
must carry that `site_id`; an attempt to write any other site is rejected with
`42501 new row violates row-level security policy`.

* **No DELETE**, anywhere, for `anon` (privilege revoked *and* no policy).
* **No access** to `fleet_meta`, `app_settings`, `user_roles`,
  `academy_progress`, `certificates`, `demo_sessions`, `demo_limits`,
  `share_links`, `site_cost_settings`, `maintenance_feedback`,
  `channel_identities`, `inbound_actions`, `site_status_pages`.
* Column-level UPDATE limits inside the sandbox (mirroring 0007's operator
  posture):
  * `alerts` — only `ack`
  * `commands` — only `status`, `decided_by`, `decided_at`, `note`, `executed_at`

### Helper functions

| function | returns | who |
|---|---|---|
| `yf_is_demo_site(p_site text)` | `boolean` — `p_site LIKE 'DEMO-%'` | anon+ |
| `yf_demo_token()` | `text` — this request's token, or `null` | anon+ |
| `yf_demo_site()` | `text` — the one site the token may touch, or `null` | anon+ |
| `yf_can_read_site(p_site text)` | `boolean` | anon+ |
| `yf_is_service_role()` | `boolean` | authenticated, service_role |

### RPCs

#### `demo_mint_session(p_ttl_minutes int = null, p_seed_robots int = 6, p_origin text = null)` — **anon**
Clamps `p_ttl_minutes` to `[5, demo_limits.ttl_minutes]` and `p_seed_robots` to
`[0, demo_limits.max_seed_robots]`. Reaps expired sandboxes first, then refuses
when the live count is at `max_live_sessions`. Seeds `p_seed_robots` robots
named `<site>-R01`…, plus one mission `<site>-M01` and one alert `<site>-A01`
when `p_seed_robots > 0`.

```json
{"token":"…64 hex…","site_id":"DEMO-0A1B2C3D4E5F",
 "created_at":"…","expires_at":"…","ttl_minutes":60,
 "claimed":false,"seeded_robots":6}
```
Raises when the sandbox is disabled or the cap is reached.

#### `demo_claim_session(p_token text)` — **anon**
Marks the session claimed (idempotent). Returns the session-info shape below
with `"live": true`. **Raises** for an unknown, reaped or expired token.

#### `demo_session_info(p_token text)` — **anon**
Never raises — an ended demo is a normal state, not an error.

```json
{"live":true,"reason":"ok","token":"…","site_id":"DEMO-…",
 "created_at":"…","expires_at":"…","claimed":true,"claimed_at":"…",
 "seconds_remaining":3401}
```
`reason` ∈ `"ok" | "expired" | "reaped" | "unknown"`. For `"unknown"`,
`token`/`site_id`/`expires_at` are `null` and `seconds_remaining` is `0`.

#### `demo_end_session(p_token text)` — **anon**
Purges that sandbox immediately.

```json
{"site_id":"DEMO-…","already_reaped":false,
 "deleted":{"robot_telemetry":0,"maintenance_findings":0,"commands":0,
            "alerts":1,"incidents":0,"missions":1,"robots":6},
 "total":8}
```
Raises for an unknown token.

#### `demo_reap_expired(p_grace_minutes int = 0)` — **authenticated, service_role**
```json
{"sessions_reaped":2,"sites":["DEMO-…","DEMO-…"],
 "deleted":{…same 7 keys…},"total":17}
```
With nothing to reap: `{"sessions_reaped":0,"sites":[],"deleted":{},"total":0}`.

#### `demo_limits_public()` — **anon**
`{"enabled":true,"ttl_minutes":60}` — enough for the marketing page to show or
hide the "try it" button.

#### `admin_list_demo_sessions(p_limit int = 100)` — **admin**
JSON array, newest first. **Tokens are deliberately omitted.**
```json
[{"site_id":"DEMO-…","created_at":"…","expires_at":"…","claimed":true,
  "claimed_at":"…","reaped_at":null,"origin":"marketing","live":true}]
```

#### `admin_set_demo_limits(p_enabled boolean = null, p_ttl_minutes int = null, p_max_live_sessions int = null, p_max_seed_robots int = null)` — **admin**
`null` means "leave as is". Returns the whole `demo_limits` row.

### Fake divergences
* The fake mints deterministic sites (`DEMO-000000000001`, …) and a
  deterministic starter fleet (fixed battery/pos/health), so tests can assert
  exact values. The SQL randomises those fields.
* `demo_reap_expired` in the fake accepts any non-anon caller; the SQL grants
  it to `authenticated` and `service_role`.

---

## 2. Expiring read-only share links — `0010_share_links.sql`

### Table `public.share_links`
No grants to `anon`/`authenticated`; RPC-only.

| column | type | notes |
|---|---|---|
| `token` | `text` **PK** | 64 hex chars |
| `kind` | `text NOT NULL` | `'fleet'` or `'incident'` |
| `target_id` | `text` | incident id when `kind='incident'`, else `null` |
| `site_id` | `text NOT NULL` | |
| `created_by` | `uuid → auth.users(id)` | |
| `created_email` | `text` | snapshot for the minter's own list |
| `label` | `text` | |
| `created_at` | `timestamptz NOT NULL` | |
| `expires_at` | `timestamptz NOT NULL` | |
| `revoked_at` / `revoked_by` | `timestamptz` / `text` | |
| `last_viewed_at` | `timestamptz` | |
| `view_count` | `int NOT NULL` | default `0` |

Constraint `share_links_target_shape`: `kind='incident'` ⇒ `target_id NOT NULL`;
`kind='fleet'` ⇒ `target_id IS NULL`.

### RPCs

#### `share_mint_link(p_kind text, p_target_id text = null, p_site text = 'BLR-DC1', p_ttl_hours int = 168, p_label text = null)` — **manager+ at `p_site`**
`p_ttl_hours` clamped to `[1, 2160]` (90 days). For `kind='incident'` the
incident must exist at that site.
```json
{"token":"…","kind":"fleet","target_id":null,"site_id":"BLR-DC1",
 "label":"MiR support","created_at":"…","expires_at":"…",
 "ttl_hours":24,"revoked_at":null,"view_count":0}
```

#### `share_resolve(p_token text)` — **anon**
Strictly read-only. Never raises. Increments `view_count`.

Invalid / dead token:
```json
{"valid":false,"status":"not_found"}
{"valid":false,"status":"expired","expires_at":"…"}
{"valid":false,"status":"revoked","expires_at":"…"}
{"valid":false,"status":"target_missing"}
```

`kind='fleet'`:
```json
{"valid":true,"status":"ok","kind":"fleet","site_id":"BLR-DC1",
 "label":"MiR support","expires_at":"…","generated_at":"…",
 "counts":{"robots_total":12,"robots_active":7,"robots_idle":3,
           "robots_charging":1,"robots_faulted":1},
 "robots":[{"id","vendor","status","battery","pos","speed","task_kind",
            "health","motor_temp","tasks_done","fault_msg","updated_at"}],
 "alerts":[{"id","sev","msg","src","tlabel","ack","created_at"}],
 "open_incidents":[{"id","sev","title","src","tlabel","state","created_at"}]}
```
`robots` is ordered by `id`; `alerts` and `open_incidents` are newest-first,
capped at 50 each.

`kind='incident'`:
```json
{"valid":true,"status":"ok","kind":"incident","site_id":"BLR-DC1",
 "label":"ticket 41822","expires_at":"…","generated_at":"…",
 "incident":{"id","sev","title","src","tlabel","state","impact","rca","fix",
             "dur","created_at"},
 "robot":{…same 12 robot columns…}|null,
 "maintenance_findings":[{"id","robot_id","component","finding","rul_days",
                          "confidence","action","state","created_at"}]}
```
`robot` is the robot named by `incident.tlabel`, or `null`.
`maintenance_findings` are that robot's **Open** findings, newest first, ≤ 20.

**Never returned by either kind:** `commands` (they carry `requested_by` /
`decided_by` emails), user identities, `app_settings`, other share links, any
other site's rows.

#### `share_revoke(p_token text)` — **manager+ at the link's site, or the link's creator**
```json
{"token":"…","kind":"fleet","site_id":"BLR-DC1",
 "revoked_at":"…","revoked_by":"mgr@example.com"}
```

#### `share_list_links(p_site text = 'BLR-DC1', p_include_dead boolean = false, p_limit int = 100)` — **manager+ at `p_site`**
Array, newest first. Includes `token` (the manager's own credential to manage).
```json
[{"token","kind","target_id","site_id","label","created_email","created_at",
  "expires_at","revoked_at","revoked_by","last_viewed_at","view_count","live"}]
```

---

## 3. Time-travel replay — `0011_replay.sql`

### Schema changes

`public.incidents` gains:

| column | type | filled by |
|---|---|---|
| `closed_at` | `timestamptz` | trigger `yf_incidents_stamp_trg` |
| `closed_at_estimated` | `boolean NOT NULL default false` | `true` only for the one-shot backfill of rows closed before this migration |
| `updated_at` | `timestamptz` | trigger, on every write |

**Trigger `yf_incidents_stamp_trg` (BEFORE INSERT OR UPDATE):** sets
`updated_at = now()`; if `state = 'Open'` clears `closed_at`; otherwise stamps
`closed_at = now()` when it is null. **Writers need no code change** — set
`state` and the close time appears. (`state` vocabulary in this repo:
`'Open'` and `'Resolved'`.)

New index `robot_telemetry_site_robot_ts` on `(site_id, robot_id, ts desc)`
and `incidents_site_closed` on `(site_id, closed_at)`.

### RPCs

#### `replay_time_range(p_site text = 'BLR-DC1')` — **operator+ at site, or demo token**
```json
{"site_id":"BLR-DC1","from":"…","to":"…",
 "telemetry_from":"…","telemetry_to":"…",
 "incidents_from":"…","incidents_to":"…",
 "sample_count":4800,"robot_count":12,"incident_count":9,
 "now":"…","has_history":true}
```
All the timestamp fields are `null` when the site has no data of that kind.

#### `replay_state_at(p_site text, p_at timestamptz, p_max_age_seconds int = 120, p_limit int = 200)` — **operator+ at site, or demo token**
`p_at` null ⇒ `now()`. `p_limit` clamped to `[1, 1000]`.
```json
{"site_id":"BLR-DC1","at":"…","max_age_seconds":120,"generated_at":"…",
 "counts":{"robots":12,"robots_stale":1,"open_incidents":2,
           "alerts":30,"commands":8},
 "robots":[{"robot_id","ts","pos","battery","speed","motor_temp","status",
            "stale","age_seconds","vendor"}],
 "open_incidents":[{"id","sev","title","src","tlabel","created_at",
                    "closed_at","closed_at_estimated","impact"}],
 "alerts":[{"id","sev","msg","src","tlabel","created_at","ack_now"}],
 "commands":[{"id","robot_id","cmd","requested_by","created_at",
              "status_at","status_now"}]}
```
* `robots` — one row per robot with a sample at or before `p_at`, ordered by
  `robot_id`. `stale` is `age_seconds > p_max_age_seconds`; a stale robot is
  still returned (last known pose, honestly labelled), never dropped.
* `open_incidents` — `created_at <= p_at AND (closed_at IS NULL OR closed_at > p_at)`.
* `alerts` — created at or before `p_at`, newest first, ≤ 100. **`ack_now` is
  the CURRENT ack value, not its value at `p_at`** — `alerts` carries no ack
  timestamp. Name it that way in the UI.
* `commands` — `status_at` is point-in-time correct (`'pending'` when
  `decided_at` is null or after `p_at`); `status_now` is today's value.

#### `replay_robot_track(p_site text, p_robot text, p_from timestamptz, p_to timestamptz, p_limit int = 2000)` — **operator+ at site, or demo token**
`p_to` null ⇒ `now()`; `p_from` null ⇒ unbounded. `p_limit` clamped to
`[1, 20000]`.
```json
{"site_id":"BLR-DC1","robot_id":"R-01","from":"…","to":"…",
 "point_count":481,"truncated":false,
 "points":[{"ts","pos","battery","speed","motor_temp","status"}]}
```
`points` ascending by `ts`.

---

## 4. Utilization and cost rollups — `0012_utilization.sql`

### Schema changes

* `public.robot_telemetry.tasks_done int` (nullable). **Writers should start
  stamping the robot's cumulative task counter on each sample.** While it is
  null, `tasks_done_delta` is reported as `null` (unknown), never `0`.
* `public.missions.completed_at timestamptz` and `missions.updated_at`,
  filled by **trigger `yf_missions_stamp_trg` (BEFORE INSERT OR UPDATE)**:
  `state = 'Done'` stamps `completed_at = now()` when null; any other state
  clears it. (`missions.state` vocabulary: `'Queued' | 'Running' | 'Done'`.)
* Index `missions_site_completed` on `(site_id, completed_at)`.

### Table `public.site_cost_settings`
`SELECT` granted to `authenticated`, gated by policy `cost_settings_read`
(`yf_has_role('operator', site_id)`). Writes only through the admin RPC.
**Every value column is nullable with NO default** — there is no built-in
currency and no built-in rate.

| column | type |
|---|---|
| `site_id` | `text` **PK** |
| `currency` | `text` (operator-chosen label, e.g. `'INR'`) |
| `robot_hour_cost` | `numeric` `>= 0` |
| `operator_hour_cost` | `numeric` `>= 0` |
| `target_utilization_pct` | `numeric` 0..100 |
| `shift_hours_per_day` | `numeric` 0 < x ≤ 24 |
| `notes` | `text` |
| `updated_at` | `timestamptz NOT NULL` |
| `updated_by` | `text` |

Money is a **UI-layer multiplication**: take `fleet.idle_robot_hours` from the
rollup and multiply by `cost.robot_hour_cost`, and show `cost.currency` next to
it. When `cost.configured` is `false`, show "set your hourly cost", not a zero.

### RPCs

#### `get_cost_settings(p_site text = 'BLR-DC1')` — **operator+ at site, or demo token**
```json
{"site_id":"BLR-DC1","configured":true,"currency":"INR",
 "robot_hour_cost":240,"operator_hour_cost":180,
 "target_utilization_pct":80,"shift_hours_per_day":16,
 "notes":"FY26 assumption","updated_at":"…","updated_by":"admin@example.com"}
```
`configured` is `true` iff `robot_hour_cost` or `operator_hour_cost` is set.
When no row exists every value field is `null` and `configured` is `false`.

#### `admin_set_cost_settings(p_site text, p_currency text = null, p_robot_hour_cost numeric = null, p_operator_hour_cost numeric = null, p_target_utilization_pct numeric = null, p_shift_hours_per_day numeric = null, p_notes text = null)` — **admin**
Upsert; `null` means "leave as is". Returns the same shape as
`get_cost_settings` with `"configured": true`.

#### `utilization_rollup(p_site text, p_from timestamptz = null, p_to timestamptz = null, p_max_gap_seconds int = 60)` — **operator+ at site, or demo token**
`p_to` null ⇒ `now()`; `p_from` null ⇒ `p_to - 24h`. `p_max_gap_seconds`
clamped to `[1, 3600]`. Raises when `p_from >= p_to`.

Each telemetry sample is credited with the time until the next sample for that
robot, **capped at `p_max_gap_seconds`**, so a writer outage shows up as
missing coverage rather than as fake idle time. Percentages are computed
against `covered_seconds`, never wall clock.

Buckets: `active` ← `active`; `idle` ← `idle`, `paused`; `charging` ←
`charging`; `faulted` ← `fault`, `estop`, `degraded`; `other` ← anything else.

```json
{"site_id":"BLR-DC1","from":"…","to":"…","window_seconds":1800.0,
 "max_gap_seconds":60,"generated_at":"…",
 "robots":[{"robot_id":"R-01",
            "active_seconds":1170.0,"idle_seconds":0.0,
            "charging_seconds":0.0,"faulted_seconds":0.0,
            "other_seconds":0.0,"covered_seconds":1170.0,
            "utilization_pct":100.00,"coverage_pct":65.00,
            "samples":40,"first_ts":"…","last_ts":"…",
            "tasks_done_delta":39,
            "by_status":{"active":1170.0}}],
 "fleet":{"robot_count":3,
          "active_seconds":…,"idle_seconds":…,"charging_seconds":…,
          "faulted_seconds":…,"other_seconds":…,"covered_seconds":…,
          "active_robot_hours":…,"idle_robot_hours":…,
          "charging_robot_hours":…,"faulted_robot_hours":…,
          "covered_robot_hours":…,
          "utilization_pct":…,"coverage_pct":…},
 "throughput":{"completed_missions":1,"tasks_done_delta":39,
               "tasks_done_total":16,"tasks_per_hour":2.0},
 "cost":{…exactly the get_cost_settings shape…}}
```
* Seconds are rounded to 1 dp, robot-hours to 3 dp, percentages to 2 dp.
* `utilization_pct`, `coverage_pct` and `tasks_per_hour` are `null` when their
  denominator is zero. `tasks_done_delta` is `null` when no sample in the
  window carried `tasks_done`.
* `robots` is ordered by `robot_id`; a robot with no samples in the window is
  absent from the array entirely.

#### `utilization_series(p_site text, p_from timestamptz = null, p_to timestamptz = null, p_bucket text = 'day', p_max_gap_seconds int = 60)` — **operator+ at site, or demo token**
`p_bucket` ∈ `'hour' | 'day' | 'week'` (anything else raises). `p_from` null ⇒
`p_to - 7 days`.
```json
{"site_id":"BLR-DC1","from":"…","to":"…","bucket":"day",
 "max_gap_seconds":60,"generated_at":"…",
 "buckets":[{"bucket":"2026-09-04T00:00:00+00:00",
             "active_seconds":…,"idle_seconds":…,"charging_seconds":…,
             "faulted_seconds":…,"covered_seconds":…,
             "utilization_pct":…}]}
```
`buckets` ascending; buckets with no samples are absent (not zero-filled).

---

## 5. Explainable predictive maintenance + feedback — `0013_maintenance_explain.sql`

### Schema changes on `public.maintenance_findings`

| column | type | notes |
|---|---|---|
| `factors` | `jsonb NOT NULL default '[]'` | `CHECK jsonb_typeof(factors) = 'array'` |
| `model_version` | `text` | which model/heuristic produced this |
| `score` | `numeric` | the model's raw output |
| `severity` | `text` | `null` or `'info'|'warn'|'serious'|'critical'` |

**`factors` element shape** (written by the detector, read by the UI):

```json
{"factor":    "motor_temp_slope",          // REQUIRED, stable machine key
 "label":     "Motor temperature trend",   // optional, human title
 "weight":    0.42,                        // REQUIRED, numeric
 "value":     3.8,                         // optional
 "unit":      "degC/h",                    // optional
 "direction": "up",                        // optional: up | down | flat
 "threshold": 2.0,                         // optional
 "window":    "24h",                       // optional
 "detail":    "rose 3.8 degC/h over 41 samples"}  // optional
```
Only `factor` and `weight` are required. **Weights need not sum to 1** — the
reader normalises for display (`weight_pct` below). `factor` must be a stable
key, not a sentence: `maintenance_accuracy().by_factor` groups on it.

`yf_factors_problem(p_factors jsonb) -> text` (**anon+**) returns `null` when
the array is well-formed, else a one-line description of the first problem.
Detectors should call it before writing.

### Table `public.maintenance_feedback`
`SELECT` granted to `authenticated`, gated by policy
`maintenance_feedback_read` (`yf_has_role('operator', site_id)`). Writes only
through the RPC.

| column | type | notes |
|---|---|---|
| `id` | `uuid` **PK** | |
| `finding_id` | `text NOT NULL → maintenance_findings(id) ON DELETE CASCADE` | |
| `site_id` | `text NOT NULL` | default `'BLR-DC1'` |
| `verdict` | `text NOT NULL` | `'correct' | 'incorrect' | 'unclear'` |
| `actual_fault` | `text` | what was really wrong |
| `action_taken` | `text` | |
| `parts_replaced` | `text` | |
| `downtime_minutes` | `numeric >= 0` | |
| `note` | `text` | |
| `submitted_by` | `text` | `auth.email()` snapshot |
| `submitted_by_uid` | `uuid → auth.users(id)` | |
| `created_at`, `updated_at` | `timestamptz NOT NULL` | |

Unique index `maintenance_feedback_one_per_user` on
`(finding_id, submitted_by_uid)` — one standing verdict per technician per
finding, upsertable. **The finding itself is never edited**: what the model
said at the time is the evidence.

### RPCs

#### `maintenance_explain(p_finding_id text)` — **operator+ at the finding's site, or demo token**
```json
{"id":"MF-0001","robot_id":"R-01","site_id":"BLR-DC1",
 "component":"drive motor","finding":"Motor temp trending up",
 "rul_days":12,"confidence":0.82,"score":0.71,"severity":"warn",
 "action":"Inspect bearing","state":"Open","model_version":"heur-1",
 "created_at":"…","cleared_at":null,
 "factors":[{"factor":"motor_temp_slope","label":"Motor temperature trend",
             "weight":0.6,"weight_pct":75.0,"value":"3.8","unit":"degC/h",
             "direction":"up","threshold":null,"window":"24h","detail":null}],
 "factors_problem":null,
 "feedback":[{"verdict","actual_fault","action_taken","parts_replaced",
              "downtime_minutes","note","submitted_by","created_at",
              "updated_at"}],
 "feedback_count":1}
```
* `factors` is sorted by `weight` descending. `weight_pct` is
  `100 * |weight| / Σ|weight|`, rounded to 1 dp — the number to draw a bar
  with. `value` and `threshold` come back as **strings** (they may be any JSON
  scalar in the source).
* When `factors_problem` is non-null the `factors` array is `[]` and the UI
  should show the problem instead of a chart. Raises only for an unknown
  `p_finding_id` or a site the caller may not read.

#### `maintenance_submit_feedback(p_finding_id text, p_verdict text, p_actual_fault text = null, p_action_taken text = null, p_parts_replaced text = null, p_downtime_minutes numeric = null, p_note text = null)` — **engineer+ at the finding's site**
Upserts this technician's verdict.
```json
{"id":"…","finding_id":"MF-0001","site_id":"BLR-DC1","verdict":"correct",
 "actual_fault":"bearing wear","action_taken":"replaced bearing",
 "parts_replaced":"BRG-9","downtime_minutes":45,"note":"ok",
 "submitted_by":"eng@example.com","created_at":"…","updated_at":"…"}
```

#### `maintenance_accuracy(p_site text = 'BLR-DC1', p_from timestamptz = null, p_to timestamptz = null)` — **engineer+ at `p_site`**
`p_to` null ⇒ `now()`; `p_from` null ⇒ `p_to - 90 days`. Window is applied to
`maintenance_findings.created_at`.
```json
{"site_id":"BLR-DC1","from":"…","to":"…","generated_at":"…",
 "overall":{"total":18,"correct":12,"incorrect":4,"unclear":2,
            "precision":75.00,
            "mean_confidence_correct":0.8125,
            "mean_confidence_incorrect":0.6400},
 "by_component":[{"component":"drive motor","total":9,"correct":7,
                  "incorrect":1,"unclear":1,"precision":87.50}],
 "by_factor":[{"factor":"motor_temp_slope","appearances":14,
               "on_correct":11,"on_incorrect":2,
               "mean_weight_correct":0.5800,
               "mean_weight_incorrect":0.3100,
               "precision":84.62}]}
```
`precision` = `100 * correct / (correct + incorrect)`, `null` when nothing has
been decided. `by_component` is ordered by `total` desc, `by_factor` by
`appearances` desc. **`by_factor` is the tuning input**: a factor whose
`mean_weight_incorrect` exceeds its `mean_weight_correct` is a candidate to
down-weight.

---

## 6. WhatsApp / inbound ops actions — `0014_inbound_ops.sql`

> **An inbound message is not a credential.** `inbound_perform()` and
> `inbound_resolve_sender()` are granted to **service_role only** — EXECUTE is
> revoked from `public`, `anon` **and `authenticated`** — and they additionally
> assert `yf_is_service_role()` internally. Call them from the webhook
> receiver, never from a browser. Everything they do is re-checked against the
> **mapped user's** role using the console's own thresholds.

### Schema changes on `public.incidents`

| column | type | notes |
|---|---|---|
| `assignee` | `text` | display name or free-text name |
| `assignee_uid` | `uuid → auth.users(id)` | set only on self-assign |
| `assigned_at` | `timestamptz` | |

### Table `public.channel_identities`
No grants to `anon`/`authenticated`; RPC-only (it is PII plus an
authorisation mapping).

| column | type | notes |
|---|---|---|
| `id` | `uuid` **PK** | |
| `channel` | `text NOT NULL` | `'whatsapp'|'sms'|'telegram'|'email'|'voice'` |
| `external_id` | `text NOT NULL` | E.164 phone, chat id, address |
| `user_id` | `uuid NOT NULL → auth.users(id)` | |
| `site_id` | `text NOT NULL` | default `'BLR-DC1'` |
| `display_name` | `text` | |
| `verified_at`, `revoked_at`, `created_at` | `timestamptz` | |
| `created_by` | `text` | |

Unique index `channel_identities_unique` on `(channel, external_id)` — one
external identity maps to at most one platform user, so the ambiguity a
spoofer would need simply cannot exist in the data.

### Table `public.inbound_actions`
No grants to `anon`/`authenticated`; RPC-only. **Every attempt is logged,
including the refusals.**

| column | type | notes |
|---|---|---|
| `id` | `uuid` **PK** | |
| `channel` | `text NOT NULL` | |
| `external_id` | `text NOT NULL` | as received, unmodified |
| `user_id` | `uuid` | `null` when unmapped |
| `mapped_as` | `text` | display name at the time |
| `site_id` | `text` | |
| `action` | `text NOT NULL` | `'ack_alert'|'assign_incident'|'approve_command'|'reject_command'|'status'|'unknown'` |
| `target_id` | `text` | |
| `raw_text` | `text` | |
| `result` | `text NOT NULL` | `'ok'|'denied'|'not_found'|'invalid'|'unmapped'|'error'` |
| `detail` | `text` | |
| `message_id` | `text` | provider id; unique when non-null |
| `received_at`, `processed_at` | `timestamptz NOT NULL` | |

### Helper

`yf_rank_at(p_user uuid, p_site text) -> int` (**authenticated, service_role**)
— the 0007 rank (`0` none, `1` operator … `4` admin) of an **arbitrary** user
at a site; admin at any site returns `4` everywhere.

`_yf_mask_identity(p_value text) -> text` — `••••••` + last 4 chars.

### RPCs

#### `inbound_map_identity(p_channel text, p_external_id text, p_user_email text, p_site text = 'BLR-DC1', p_display_name text = null)` — **admin**
Upsert on `(channel, external_id)`; re-mapping also clears `revoked_at`.
Raises when no `auth.users` row has that email (they must sign in once first).
```json
{"id":"…","channel":"whatsapp","external_id":"+911234567890",
 "user_id":"…uuid…","site_id":"BLR-DC1","display_name":"Asha",
 "verified_at":"…","revoked_at":null,"created_at":"…",
 "created_by":"admin@example.com"}
```

#### `inbound_unmap_identity(p_channel text, p_external_id text)` — **admin at the mapping's site**
Revokes (never deletes — the audit trail must keep pointing at a mapping that
once existed).
`{"id":"…","channel":"whatsapp","external_id":"+91…","revoked_at":"…"}`

#### `inbound_list_identities(p_site text = null)` — **admin**
`p_site` null lists every site. Newest first. Unmasked `external_id` (it is the
admin's own team roster).
```json
[{"id","channel","external_id","user_id","site_id","display_name",
  "verified_at","revoked_at","created_at","rank_at_site","active"}]
```

#### `inbound_resolve_sender(p_channel text, p_external_id text)` — **service_role only**
Look-up without performing anything. **Deliberately does not return the mapped
user's email.**
```json
{"mapped":true,"channel":"whatsapp","user_id":"…uuid…","site_id":"BLR-DC1",
 "display_name":"Asha","rank":1}
```
Unmapped: `{"mapped":false,"channel":"whatsapp","user_id":null,"site_id":null,"display_name":null,"rank":0}`

#### `inbound_perform(p_channel text, p_external_id text, p_action text, p_target_id text = null, p_note text = null, p_raw_text text = null, p_message_id text = null)` — **service_role only**

Returns the same envelope for every outcome and **never raises for an
authorisation failure** — so the receiver can reply politely and the refusal
still lands in `inbound_actions`:

```json
{"ok":true,"result":"ok","action":"ack_alert","target_id":"A-1",
 "site_id":"BLR-DC1","mapped_as":"Asha","rank":1,
 "detail":"alert A-1 acknowledged by Asha",
 "payload":{…action-specific, see below…},
 "log_id":"…uuid…","replayed":false}
```

| `p_action` | required rank | effect | `payload` |
|---|---|---|---|
| `'ack_alert'` | operator (1) | `alerts.ack = true` for `p_target_id` at the sender's site | `{"id","sev","msg","ack"}` |
| `'assign_incident'` | operator (1) self-assign; **manager (3)** to assign somebody else | self-assign sets `assignee`=display name, `assignee_uid`=sender; with a non-empty `p_note` and manager+ it sets `assignee = trim(p_note)` and `assignee_uid = null` | `{"id","title","sev","state"}` |
| `'approve_command'` / `'reject_command'` | manager (3) | same state machine as `decide_command()`: **pending only**; sets `status`, `decided_by = '<channel>:<name>'`, `decided_at`; `p_note` overwrites `note` when non-empty | `{"id","robot_id","cmd","status"}` |
| `'status'` | operator (1) | read-only snapshot | `{"site_id","robots_total","robots_active","robots_faulted","open_incidents","unacked_alerts","pending_commands"}` |

`result` values: `'ok'`; `'unmapped'` (sender not mapped — nothing is
performed); `'denied'` (mapped but insufficient rank); `'not_found'` (no such
target at the sender's site); `'invalid'` (unrecognised action, or a command
that is not pending); `'error'`.

The envelope's **key set never varies** — same eleven keys for every outcome,
so a client can destructure it unconditionally.

**Replay safety:** when `p_message_id` is supplied and already present, the
original outcome is returned with `"replayed": true` and nothing is performed
a second time. A replayed response carries `"rank": null` and
`"payload": null` — neither is persisted, only `result`/`detail` are — but the
keys are still present.

#### `inbound_recent_actions(p_site text = 'BLR-DC1', p_limit int = 50)` — **manager+ at `p_site`**
Newest first. **`external_id` is masked** (`external_id_masked`); the unmasked
roster is admin-only via `inbound_list_identities`.
```json
[{"id","channel","external_id_masked","mapped_as","site_id","action",
  "target_id","result","detail","received_at","processed_at"}]
```

### Fake divergences
`inbound_map_identity` in the fake derives the user id from the email the same
way `make_test_jwt` does (`uuid5(NAMESPACE_URL, "yf-test-user:<email>")`) and
does not require a pre-existing user row. Seed the mapped user's role with
`fake.set_user_role(email, role, site)` (or authenticate once as that user, so
the fake learns it from the JWT) before calling `inbound_perform`.

---

## 7. Operator certification — `0015_certification.sql`

### Schema changes on `public.certificates` (from 0007)

| column | type | notes |
|---|---|---|
| `level` | `text` | `null` or `'bronze'|'silver'|'gold'|'platinum'` |
| `holder_name` | `text` | **snapshot at issue time**, not a live join |
| `issued_for` | `text` | human title of the track |
| `site_id` | `text` | private — never returned by `certificate_verify` |
| `expires_at` | `timestamptz` | |
| `revoked_at`, `revoked_by`, `revoke_reason` | | |

**Trigger `yf_certificates_fill_trg` (BEFORE INSERT)** fills in whatever the
caller left out: `level` from `score`, `verification_code` when null,
`holder_name` from `auth.users.raw_user_meta_data->>'full_name'` / `'name'` /
the email **local part** (never the domain) / `'YantraFleet operator'`, and
`issued_for` from `track`. **0007's `issue_certificate(p_track, p_score,
p_code)` keeps its exact signature and keeps working** — it now produces
verifiable certificates with no caller change.

Level ladder (`yf_certificate_level(p_score numeric) -> text`, **anon+**):
`>= 95` platinum · `>= 85` gold · `>= 75` silver · `>= 60` bronze · below that
`null` (no level).

Codes generated by `yf_make_certificate_code(p_track)` look like
`YF-<TRACKSLUG>-<12 chars from [0-9HJKMNP]>`.

### RPCs

#### `issue_certificate_v2(p_track text, p_score numeric, p_code text = null, p_level text = null, p_holder_name text = null, p_issued_for text = null, p_site text = null, p_expires_at timestamptz = null)` — **authenticated** (issues for `auth.uid()`)
A separate name, not an overload of `issue_certificate` — two same-named
functions differing only in arity make PostgREST's RPC resolution ambiguous.
```json
{"id":"…uuid…","track":"safety-checkride","score":96,"level":"platinum",
 "holder_name":"Asha Operator","issued_for":"safety-checkride",
 "verification_code":"YF-SAFETYCHECKRIDE-3M9KP0N2HJ44",
 "issued_at":"…","expires_at":null}
```
Raises on a duplicate `p_code` (`23505`), a missing track, a score outside
0..100, or an unknown level.

#### `certificate_verify(p_code text)` — **anon**
**THE PUBLIC ENDPOINT.** Never raises. Returns exactly these eight keys and
nothing else:
```json
{"valid":true,"status":"valid","verification_code":"YF-…",
 "holder_name":"Asha Operator","track":"safety-checkride",
 "level":"platinum","issued_at":"…","expires_at":null}
```
`status` ∈ `'valid' | 'revoked' | 'expired' | 'not_found'`. For
`'not_found'` only `{"valid":false,"status":"not_found"}` is returned.
`track` is `issued_for` falling back to `track`.

**Never returned:** email, `user_id`, `site_id`, **`score`**, any other
certificate held by the same person.

#### `my_certificates()` — **authenticated**
The holder's own wallet, newest first — includes the score.
```json
[{"id","track","issued_for","score","level","holder_name",
  "verification_code","issued_at","expires_at","revoked_at"}]
```

#### `certificate_revoke(p_code text, p_reason text = null)` — **admin**
```json
{"verification_code":"…","holder_name":"…","track":"…",
 "revoked_at":"…","revoked_by":"admin@example.com","revoke_reason":"fraud"}
```

---

## 8. Public fleet status page — `0016_public_status.sql`

### Table `public.site_status_pages`
No grants to `anon`/`authenticated`; RPC-only — so it can never be used to
enumerate which sites exist.

| column | type | default |
|---|---|---|
| `site_id` | `text` **PK** | |
| `enabled` | `boolean NOT NULL` | **`false`** |
| `display_name` | `text` | public label, admin-chosen |
| `blurb` | `text` | one public sentence, admin-chosen |
| `show_throughput` | `boolean NOT NULL` | `true` |
| `show_incidents` | `boolean NOT NULL` | `true` |
| `online_grace_seconds` | `int NOT NULL` (30..86400) | `300` |
| `updated_at` | `timestamptz NOT NULL` | `now()` |
| `updated_by` | `text` | |

### RPCs

#### `public_fleet_status(p_site text)` — **anon**
**Off by default.** A site with no row, a site with `enabled = false`, a blank
`p_site` and a site that never existed all return the IDENTICAL shape, so this
endpoint is not a site-name oracle:
```json
{"site_id":"BLR-DC1","enabled":false,"status":"not_published"}
```

Published:
```json
{"site_id":"BLR-DC1","enabled":true,"status":"published",
 "display_name":"Bengaluru DC1","blurb":"Live fleet status",
 "generated_at":"…",
 "robots_total":12,"robots_online":11,"robots_online_pct":91.7,
 "robots_active":7,"robots_charging":1,"robots_faulted":1,
 "uptime_pct_24h":98.42,"uptime_covered_seconds":84120,
 "active_incidents":2,"active_incidents_by_sev":{"warn":1,"serious":1},
 "unacked_alerts":3,"throughput_today":41,
 "last_robot_update":"…"}
```
* `robots_online` = robots whose `updated_at` is newer than
  `now() - online_grace_seconds`.
* `uptime_pct_24h` = share of covered telemetry time in the last 24h **not**
  in `fault`/`estop`/`degraded`, using 0012's gap-capped span arithmetic with
  a fixed 60s cap. `null` when there is no telemetry.
* `throughput_today` = missions with `completed_at >= date_trunc('day', now())`.
* `active_incidents`, `active_incidents_by_sev` and `unacked_alerts` are
  `null` when `show_incidents` is false; `throughput_today` is `null` when
  `show_throughput` is false.

**Never returned:** robot ids/names/vendors, incident ids/titles/text, alert
messages, poses or telemetry samples, any user identity, anything about any
other site. Only counts, percentages and timestamps — plus the two free-text
fields the admin deliberately published.

#### `admin_set_status_page(p_site text, p_enabled boolean = null, p_display_name text = null, p_blurb text = null, p_show_throughput boolean = null, p_show_incidents boolean = null, p_online_grace_seconds int = null)` — **admin at `p_site`**
Upsert; `null` means "leave as is" on an existing row. On first insert the
omitted fields take the table defaults (`enabled = false`). Returns the whole
`site_status_pages` row.

#### `admin_list_status_pages()` — **admin**
Array ordered by `site_id`:
```json
[{"site_id","enabled","display_name","blurb","show_throughput",
  "show_incidents","online_grace_seconds","updated_at","updated_by"}]
```

---

## 9. Quick index

| RPC | migration | who may call |
|---|---|---|
| `demo_mint_session` | 0009 | anon |
| `demo_claim_session` | 0009 | anon |
| `demo_session_info` | 0009 | anon |
| `demo_end_session` | 0009 | anon |
| `demo_limits_public` | 0009 | anon |
| `demo_reap_expired` | 0009 | authenticated, service_role |
| `admin_list_demo_sessions` | 0009 | admin |
| `admin_set_demo_limits` | 0009 | admin |
| `share_resolve` | 0010 | anon |
| `share_mint_link` | 0010 | manager+ |
| `share_revoke` | 0010 | manager+ or creator |
| `share_list_links` | 0010 | manager+ |
| `replay_time_range` | 0011 | operator+ / demo token |
| `replay_state_at` | 0011 | operator+ / demo token |
| `replay_robot_track` | 0011 | operator+ / demo token |
| `get_cost_settings` | 0012 | operator+ / demo token |
| `utilization_rollup` | 0012 | operator+ / demo token |
| `utilization_series` | 0012 | operator+ / demo token |
| `admin_set_cost_settings` | 0012 | admin |
| `maintenance_explain` | 0013 | operator+ / demo token |
| `maintenance_submit_feedback` | 0013 | engineer+ |
| `maintenance_accuracy` | 0013 | engineer+ |
| `inbound_perform` | 0014 | **service_role only** |
| `inbound_resolve_sender` | 0014 | **service_role only** |
| `inbound_map_identity` | 0014 | admin |
| `inbound_unmap_identity` | 0014 | admin |
| `inbound_list_identities` | 0014 | admin |
| `inbound_recent_actions` | 0014 | manager+ |
| `certificate_verify` | 0015 | anon |
| `issue_certificate_v2` | 0015 | authenticated |
| `my_certificates` | 0015 | authenticated |
| `certificate_revoke` | 0015 | admin |
| `public_fleet_status` | 0016 | anon |
| `admin_set_status_page` | 0016 | admin |
| `admin_list_status_pages` | 0016 | admin |

### Error conventions

PostgREST surfaces a `raise exception` as HTTP 400 with
`{"message": "<fn>: <reason>", "code": "P0001"}`; a permission failure inside
one of these functions carries the same shape with the function name first, so
`message.startsWith('utilization_rollup:')` is a reliable test. The
never-raising endpoints — `share_resolve`, `certificate_verify`,
`public_fleet_status`, `demo_session_info`, `inbound_perform` — return HTTP 200
with a `status`/`result` field instead; branch on that, not on the HTTP code.
