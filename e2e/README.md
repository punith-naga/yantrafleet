# e2e — offline end-to-end loopback harness

Suites in this directory: `test_e2e.py` (sim → gate → copilot story),
`test_mqtt_e2e.py` (MQTT connector loop), `test_rbac_e2e.py`
(rbac-0007 auth emulation — see below).

Proves the whole YantraFleet stack **without Supabase**: a tiny in-process
fake PostgREST (`fakerest.py`, stdlib `http.server` on a `127.0.0.1`
ephemeral port, in-memory dict tables) stands in for the cloud, and the
*real* transports talk genuine httpx to it over localhost sockets. No
mocking of the components under test — only the database is faked.

```
yantrasim.FleetSim ── SupabaseTransport (real httpx) ──┐
                                                       ▼
                                    fakerest.FakePostgREST  (127.0.0.1:ephemeral)
                                                       ▲    tables: robots, alerts,
sarathi.Toolbox ── sarathi SupabaseTransport (httpx) ──┘    incidents, commands,
        │                                                   fleet_meta, robot_telemetry
   OfflineEngine (tier-3 keyword intents, no LLM)
```

## What `fakerest.py` implements

The PostgREST subset the components actually use:

| Verb  | Feature |
|-------|---------|
| POST  | bulk insert; `?on_conflict=col` + `Prefer: resolution=merge-duplicates` (upsert) or `resolution=ignore-duplicates` (insert-if-absent); plain insert auto-assigns a serial `id` |
| GET   | filters `eq. neq. in. lt. lte. gt. gte. is.`, `order=col.asc|desc`, `limit=N`, `select=` projection |
| PATCH | same filters select rows; JSON body merged in; 204 |
| POST `/rest/v1/rpc/{fn}` | the 0007 RPCs: `decide_command`, `save_progress`, `issue_certificate` |

## RBAC emulation (`supabase/0007_rbac.sql`) — opt-in

`FakePostgREST(rbac=True, service_key="sk-test", anon_key="anon-key")`
switches the fake into rbac-0007 mode so console / academy / browser
tests can exercise auth flows fully offline. **The default
(`rbac=False`) is byte-for-byte the old behaviour — auth headers are
ignored entirely** (the RPC routes exist in both modes; in default mode
they skip the role checks but keep the state machine).

**Test tokens** are deliberately *not* real JWTs — unsigned
`yf-test.<base64url json claims>.sig` strings with claims
`{sub, email, role: 'authenticated', yf_role, site_id}`. Build one with:

```python
from fakerest import make_test_jwt
headers = {"Authorization": f"Bearer {make_test_jwt('op@example.com', 'operator', 'BLR-DC1')}"}
```

What rbac mode enforces (an approximation of the 0007 policies, RLS
semantics preserved — invisible rows read as absent, not as errors):

| Caller | Reads | Writes |
|--------|-------|--------|
| anon (`apikey` only, no/bad Bearer) | `200 []` on every table | 401 `{message, code}` |
| authenticated, no known `yf_role` | `200 []` | 403 |
| operator / engineer / manager | rows where `site_id` = claim's `site_id` (tables without `site_id`, e.g. `fleet_meta`: all) | PATCH `alerts` with body exactly `{"ack": …}` (own site); POST `commands` only when `status='pending'`, `requested_by` = claim email, no `decided_by`, own site — everything else 403 |
| admin (any site) | every site | same write paths as operator, any site |
| `service_key` (Bearer *or* `apikey`) | everything | everything (RLS bypass — the writer path) |

**RPCs** (`POST /rest/v1/rpc/<fn>`, PostgREST-style; errors are JSON
`{message, code}` mirroring the SQL exception texts):

* `decide_command(p_id, p_decision, p_note)` — manager+ at the command's
  site (admins global); pending rows only; stamps `decided_by` (claim
  email) + `decided_at`, returns the row json. Errors: 401 not signed in,
  403 "not authorized", 400 "command not pending" / "unknown command" /
  "invalid decision".
* `save_progress(p_pack, p_data)` — per-user upsert into
  `fake.progress[email][pack]`; 401 anon, 400 missing pack.
* `issue_certificate(p_track, p_score, p_code)` — appends to
  `fake.certificates`; duplicate `verification_code` → 409 / `23505`;
  score bounds → 400.

`e2e/test_rbac_e2e.py` (17 tests) drives all of the above over real
localhost HTTP with httpx — anon lockout, site-scoped vs admin reads,
ack column-grant, command request/spoof, decide happy/denied/double,
service-key bypass, academy progress + certificates.

## What `test_e2e.py` proves (one shared world, tests in story order)

1. **Sim publish loop** — 8 ticks of `FleetSim(seed=9)` through
   `yantrasim.transports.supabase.SupabaseTransport`:
   * `robots` holds exactly 10 upserted rows, every `status` in the
     canonical vocabulary (`yantracore.CANONICAL`);
   * `robot_telemetry` accumulates downsampled history (every 3rd tick →
     20 rows), readable with `eq.`/`in.` filters (the replay query shape);
   * `alerts` are present (seed 9 deterministically faults AMR-09 at tick
     4 and clears it at tick 8) and re-posting them is idempotent under
     `ignore-duplicates`;
   * `fleet_meta` stays a single `id=1` heartbeat row.
2. **Human-in-the-loop gate** — a `pending` `pause AMR-01` command is
   inserted (console path), ignored by `poll_commands` while pending,
   approved via PATCH, then executed via `poll_commands` +
   `yantrasim.commands.apply_command`: the sim robot is `paused` *and*
   the row becomes `executed` with a grounded `note`; one more published
   tick round-trips `paused` into the `robots` table through the VDA
   translation.
3. **Copilot** — `sarathi.Toolbox` over sarathi's own httpx
   `SupabaseTransport` pointed at the fake, answered by `OfflineEngine`:
   * “what is waiting for approval?” lists the still-pending
     `charge AMR-02` and counts the executed pause;
   * “fleet status” numbers match the fake's tables row-for-row;
   * robot detail and alert questions are grounded too.

## Run

```sh
pip install --break-system-packages -r e2e/requirements.txt
pip install --break-system-packages -e ./core -e ./sim
python3 -m pytest e2e/ -v
```

Everything binds only `127.0.0.1`; no DNS, no network egress — safe for
fully offline CI.
