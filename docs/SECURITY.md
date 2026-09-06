# Yantrika security: demo, hardened, and RBAC modes

Yantrika ships demo-open on purpose — one shared Supabase project,
one client-safe key, zero setup. This page explains what that means,
and the two opt-in lockdown levels for real deployments.

## The three modes

| | Demo (default) | Hardened (`0006_harden.sql`) | RBAC (`0007_rbac.sql`) |
|---|---|---|---|
| RLS posture | `demo_all`: **anyone** with the anon key can read *and write* every table | `demo_all` dropped; `authenticated` is **read-only** (everything); anon: nothing | 0006 policies replaced by role-based ones: reads scoped to the user's **site + role**, acks/requests/decisions gated per role (matrix below); anon: nothing |
| Login required? | no | yes (Supabase Auth) — or Option B read-only anon | yes — Supabase Auth session **and** a `user_roles` row (no role = sees nothing) |
| Writers (sim, detector, connector, ops) | anon key | **service_role** key (bypasses RLS) | **service_role** key (bypasses RLS) |
| Command approval | console writes `commands.status` directly | no client path (service_role only) | `decide_command()` RPC, manager+ only |
| Good for | demos, evaluation, throwaway data | single-team read-only dashboards over real data | real operations with per-person accountability |

Modes are applied by running the corresponding migration; each file
carries a commented **ROLLBACK** block that restores the previous
posture. 0007 can be applied directly after 0005 (it drops the 0006
policies itself if they exist). See
[`supabase/README.md`](../supabase/README.md) for ordering — `yantraops
migrate` applies `0001`-`0005` by default and skips 0006/0007 unless you
pass `--include-opt-in`, specifically so a plain `migrate` run can never
lock a demo project down by accident. That same README's "Going to
production" section has the exact commands (`migrate --include-opt-in`
then `grant-role`) for moving a real deployment to RBAC.

## Key classes — know the difference

- **Publishable / anon key** (`sb_publishable_...` / legacy `anon` JWT):
  designed to be embedded in browsers and shipped in client code. It is
  only as dangerous as your RLS policies allow — in demo mode that is
  "fully open", in hardened/RBAC mode "nothing at all". In RBAC mode
  the console still ships this key: it is what `supabase-js` uses to
  *reach* the API, but every request also carries the signed-in user's
  JWT, and RLS decides from the JWT.
- **service_role key** (`sb_secret_...` / legacy `service_role` JWT):
  bypasses Row Level Security entirely. It is a server-side admin
  credential. **Never ship the service_role key to a browser, embed it
  in the console, commit it to git, or put it in client-side code.**
  Servers and cron jobs only.
- **User JWT** (RBAC mode): issued by Supabase Auth when a person signs
  in; sent as `Authorization: Bearer <access_token>` alongside the anon
  `apikey`. It maps to the Postgres `authenticated` role and carries
  the user id (`auth.uid()`) and email (`auth.email()`) the policies
  and RPCs check.

## Which key does each component use?

| Component | Demo | Hardened (0006) | RBAC (0007) |
|---|---|---|---|
| sim / detector / connector / ops (writers) | anon | service_role | service_role |
| copilot (`sarathi`), notifier (readers) | anon | service_role | service_role (they act for the fleet, not a person) |
| console (browser) | anon, no login | anon key + Auth session — or Option B read-only anon | anon key + Auth session; capabilities from `user_roles` |
| academy (browser) | localStorage only, no key | localStorage only | anon key + Auth session; `save_progress()` / `issue_certificate()` RPCs |
| `yantraops migrate` | Postgres URL (database password) — all modes | same | same |

## The RBAC model (`0007_rbac.sql`)

### Roles and capabilities

Roles live in `public.user_roles (user_id, role, site_id)` — one role
per user per site, default site `BLR-DC1`. The hierarchy is
`operator < engineer < manager < admin`; every role includes the
capabilities below it. An `admin` role at **any** site is global: it
grants read access to all sites and role management everywhere.

| Capability | operator | engineer | manager | admin |
|---|---|---|---|---|
| View fleet data (robots, alerts, incidents, missions, telemetry, findings, commands) at own site | yes | yes | yes | yes (all sites) |
| Acknowledge alerts (`alerts.ack`) | yes | yes | yes | yes |
| Request commands (insert `pending` rows as themselves) | yes | yes | yes | yes |
| Connector management / telemetry export workflows | — | yes | yes | yes |
| Approve/reject commands (`decide_command()`) | — | — | yes | yes |
| Manage `user_roles` | — | — | — | yes |

Notes on enforcement level:

- The engineer tier (connectors / telemetry export) is enforced in the
  **application layer**: those tools run server-side with the
  service_role key and check `public.yf_role()` /
  `public.yf_has_role('engineer')` before acting. At the database
  level an engineer's read surface equals an operator's.
- Everything else in the matrix is enforced **in the database** by RLS
  policies and SECURITY DEFINER RPCs — a hand-crafted PostgREST
  request cannot exceed it.

### Helper functions

- `public.yf_role(p_site default 'BLR-DC1') -> text` — the calling
  user's role at a site; `null` for anon or role-less users.
- `public.yf_has_role(p_min_role, p_site default 'BLR-DC1') -> boolean`
  — hierarchy-aware gate; also true when the user is an admin
  anywhere. Fails closed (anon, no role, unknown role name = false).

Both are `SECURITY DEFINER`, `STABLE`, with a pinned empty
`search_path`; every RLS policy is expressed through them.

### Write paths, precisely

- **Fleet data writes** (robots, alerts, incidents, missions,
  fleet_meta, telemetry, findings): service_role only. No
  insert/update policy exists for `authenticated` — service_role
  bypasses RLS, so none is needed.
- **Alert acks**: `rbac_alerts_ack` allows operator+ to UPDATE alert
  rows at their site. **Caveat: Postgres RLS is row-level, not
  column-level** — the policy alone would let an operator PATCH *any*
  column of those rows. 0007 therefore also revokes the table-wide
  UPDATE privilege from `authenticated` and grants UPDATE on the
  `ack` column only, so "ack rides this policy" is exactly and only
  what the console's ack toggle can do.
- **Command requests**: `rbac_commands_insert` lets operator+ INSERT
  rows that are `status = 'pending'`, at their own site, with
  `requested_by = auth.email()` (no spoofing another requester) and
  no `decided_by`. RBAC mode assumes email-based Auth accounts.
- **Command decisions**: there is **no UPDATE path** on `commands` for
  any human role (no policy, and the UPDATE privilege is revoked).
  Approve/reject goes through the `decide_command(p_id, p_decision,
  p_note)` RPC — SECURITY DEFINER, executable by `authenticated`
  only. It verifies the caller is manager+ at the command's site,
  the row is still `pending`, and the decision is `approved` or
  `rejected`; it stamps `decided_by = auth.email()` and
  `decided_at = now()` and returns the row as JSON. Execution
  (`executed` / `failed`) remains the service-key executor's job.
- **Academy**: `academy_progress` and `certificates` accept no direct
  client writes (privileges revoked). The self-service RPCs
  `save_progress(p_pack, p_data)` and `issue_certificate(p_track,
  p_score, p_code)` insert/update rows for `auth.uid()` only. Users
  read their own rows; admins can audit all.

### Seeding roles

Only admins can write `user_roles` through the API — so the **first**
admin has a chicken-and-egg problem: nobody has an admin role yet to
grant one. Bootstrap it with the direct Postgres connection (same
`--db-url` as `migrate`):

```bash
python -m yantraops grant-role --db-url "<postgres-url>" \
    --email ops-lead@example.com --role admin
```

`grant-role` requires an existing `auth.users` row for that email (sign
up or sign in once first), gives a clear error naming the exact fix if
0007 hasn't been applied yet or the email hasn't signed up, and is
idempotent — running it again just updates the role. It works for any
role, not only admin (`--role operator|engineer|manager|admin`,
`--site <site_id>`), so it's also a fine way to grant roles from a
script/CI job rather than the dashboard.

Equivalent by hand, if you'd rather use the SQL editor (see the
commented helper at the bottom of `0007_rbac.sql`):

```sql
insert into public.user_roles (user_id, role, site_id)
select id, 'admin', 'BLR-DC1' from auth.users
 where email = 'ops-lead@example.com'
on conflict (user_id, site_id) do update set role = excluded.role;
```

After the first admin exists, admins manage everyone else's roles from
any signed-in client (`POST /rest/v1/user_roles`) or by running
`grant-role` again.

### JWT flow (how clients sign in)

Supabase Auth issues the JWTs; the console/academy use `supabase-js`,
which wraps all of this — the raw endpoints are listed for curl-level
debugging:

1. **Password grant**: `POST {SUPABASE_URL}/auth/v1/token?grant_type=password`
   with `{"email": "...", "password": "..."}` and the anon key as
   `apikey`. Returns `access_token` (short-lived JWT) +
   `refresh_token`. `supabase-js`: `auth.signInWithPassword(...)`.
2. **Refresh**: `POST /auth/v1/token?grant_type=refresh_token` with
   `{"refresh_token": "..."}`. `supabase-js` auto-refreshes before
   expiry.
3. **Requests**: every PostgREST/RPC call sends `apikey: <anon key>`
   and `Authorization: Bearer <access_token>`. Postgres sees role
   `authenticated`, `auth.uid()`, `auth.email()`.
4. **Google OAuth** (optional): enable the Google provider under
   Authentication → Providers in the Supabase dashboard, add your
   OAuth client id/secret and redirect URL, then
   `auth.signInWithOAuth({ provider: 'google' })`. Same JWT comes out;
   RLS neither knows nor cares how the user signed in. See
   [Supabase's Google login guide](https://supabase.com/docs/guides/auth/social-login/auth-google).

Sign-ups default to open in Supabase — for a private fleet, disable
public sign-ups (Authentication → Providers → Email) or leave them on
and rely on the fact that a fresh user has no `user_roles` row and can
see nothing until an admin grants a role.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `SUPABASE_URL` | all services | Supabase project URL |
| `SUPABASE_KEY` | all services | Demo mode: the anon key (default is baked in). Hardened/RBAC modes: **writers must switch this to the service_role key** — with `demo_all` gone, anon writes are refused |
| `SARATHI_TOKEN` | copilot (`sarathi`) | Optional. When set, `POST /ask` requires `Authorization: Bearer <token>` and answers 401 otherwise (constant-time compare). `GET /health` stays open but reports `auth_required: true`. Unset = open (demo behaviour) |
| `YANTRA_WEBHOOK_SECRET` | notifier | Optional. When set, every webhook POST carries `X-Yantra-Signature: sha256=<hex>` — an HMAC-SHA256 of the exact request body — so receivers can verify the sender. Unset = no header |

## Mode 1 → 2: the hardening migration (`0006_harden.sql`)

Run [`supabase/0006_harden.sql`](../supabase/0006_harden.sql) in the
Supabase SQL editor (or via `psql`) **only when moving beyond demo**.
It is idempotent. For every table (`robots`, `alerts`, `incidents`,
`missions`, `fleet_meta`, `robot_telemetry`, `maintenance_findings`,
`commands`) it:

1. drops the `demo_all` policy;
2. creates `hardened_read_authenticated` — `for select to
   authenticated using (true)`.

No write policy is created because none is needed: writers use the
service_role key, which bypasses RLS. No `anon` policy is created at
all, so the publishable key loses both read and write access.

Immediately after running it:

1. switch every **writer's** `SUPABASE_KEY` to the service_role key
   (sim, detector, connector, ops — and copilot/notifier if you keep
   them on a plain key rather than an Auth session);
2. set `SARATHI_TOKEN` and `YANTRA_WEBHOOK_SECRET`;
3. give console users a way in: Supabase Auth (any signed-in user can
   read everything in this mode), or **Option B** below if you are not
   ready for console auth.

**Option B — read-only anon variant:** keep the console loginless but
harmless by granting anon **read-only** access. Run this after (or
instead of the strict form of) 0006:

```sql
do $$
declare t text;
begin
  foreach t in array array[
    'robots', 'alerts', 'incidents', 'missions', 'fleet_meta',
    'robot_telemetry', 'maintenance_findings', 'commands'
  ] loop
    execute format('drop policy if exists hardened_read_anon on public.%I', t);
    execute format(
      'create policy hardened_read_anon on public.%I
         for select to anon using (true)', t);
  end loop;
end $$;
```

Writes still require service_role either way. Option B makes all
fleet data readable by anyone holding the publishable key — fine for
dashboards on trusted networks, not fine for sensitive data. Remove it
with `drop policy if exists hardened_read_anon on public.<table>;` per
table. (0007 drops it automatically if you later move to RBAC.)

## Mode 2 → 3 (or 1 → 3): the RBAC migration (`0007_rbac.sql`)

Run [`supabase/0007_rbac.sql`](../supabase/0007_rbac.sql) after 0005
(0006 is optional — 0007 supersedes and removes its policies). Then:

1. writers on the service_role key, exactly as for 0006;
2. seed the first admin (above), who then assigns everyone's roles;
3. make sure console users sign in — a signed-in user with no role
   sees an empty fleet, which is the fail-safe, not a bug.

Rolling back: 0007 ends with a commented ROLLBACK block that drops the
RBAC policies, restores `demo_all`, and re-grants the revoked table
privileges; a further commented block tears down the RBAC tables and
functions entirely (destroying role assignments and academy data).

## Running the demo sandbox (`0009_demo_sandbox.sql`) on your own project

`0009` is the one migration that hands **anonymous internet visitors write
access** to your database. Its threat model (in the file's header) is
sound about *where a row can land*: every anon policy is
`site_id = public.yf_demo_site()`, so a visitor's writes are pinned to one
throwaway `DEMO-*` site and cannot touch, move or read a real one. If you
self-host the "try it" button, read this section as well — it covers the
class of problem that pinning the row's **site** does not address.

### The gap 0017 closes: a row can name something it does not own

A sandbox row lands in the sandbox's site, but four of the seven
demo-writable tables carry a column that *names a robot* and has no
foreign key behind it (0002/0003/0004 predate 0005's `site_id`):

| Column | Consumed by | Was |
|---|---|---|
| `commands.robot_id` | approved-command executors (yantrasim, yantrabridge) | **critical** |
| `robot_telemetry.robot_id` | detector, replay, utilization | poisoning |
| `maintenance_findings.robot_id` | detector dedup, copilot | suppression |
| `missions.robots[]` | console display only | noise |

Before `0017_demo_command_scope.sql`, an anonymous visitor holding only
the publishable key and a freshly minted demo token could POST

```json
{"robot_id": "AMR-01", "cmd": "estop", "status": "approved",
 "requested_by": "demo-visitor", "site_id": "DEMO-…"}
```

— a **real** robot in a **real** site, with the human approval gate
already satisfied, because 0009's insert policy checked `site_id` and
nothing else, and its column-level grants narrow only UPDATE. The row
stayed inside the demo site, so no real row was written or read. The
exposure was downstream, in the executors that hold the service key and
therefore bypass RLS: they polled `commands?status=eq.approved` and would
have published a VDA 5050 `estop` instantAction to a serial derived from
attacker-supplied text.

Three independent layers now stand between a forged row and a robot:

1. **`0017_demo_command_scope.sql`** — a demo insert into `commands`,
   `robot_telemetry` or `maintenance_findings` must name a robot that
   lives in the **same sandbox site**, and a demo `commands` row must be
   inserted `pending` with no `decided_by` / `decided_at` /
   `executed_at`. (The sandbox still demos the approval loop: it PATCHes
   its own pending row to `approved`, which is what a human does.)
2. **Both approved-command pollers are site-scoped and no longer
   opt-in.** `yantrasim`'s `SupabaseTransport` polls only its own site
   (`site=` / `YANTRA_SITE_ID` / `BLR-DC1` — the same resolution that
   stamps every row it writes). `yantrabridge`'s `CommandPublisher`
   **requires** a site: it raises at construction if neither `--site` nor
   `YANTRA_SITE_ID` is set, rather than guessing. Both re-check every
   returned row's `site_id` client-side, and the bridge's robot lookup is
   site-scoped too. `--site '*'` is a deliberate, logged opt-out.
3. **Site hygiene**: run the sandbox on a project that has no real fleet
   in it if you possibly can. Layers 1 and 2 assume the demo and the real
   fleet share a database; not sharing one removes the question.

**If you ran 0009 before 0017**, rows forged under the old policy are
still in the table (0017 stops them being created and stops them being
walked forward, but does not delete them). Audit and clean up with:

```sql
-- demo rows that name a robot outside their own site
select c.id, c.site_id, c.robot_id, c.status
  from public.commands c
 where c.site_id like 'DEMO-%'
   and not exists (select 1 from public.robots r
                    where r.id = c.robot_id and r.site_id = c.site_id);
-- ...same shape for robot_telemetry and maintenance_findings.
-- delete them, or just purge every sandbox:
-- select public.demo_reap_expired(0);
```

### What 0017 deliberately leaves open

- **`missions.robots`** is unchanged. The equivalent policy is written
  out (commented) in 0017, but enabling it today breaks the sandbox
  itself: `ops/yantraops/sandbox.py` builds its `FleetSim` and renames
  the robots *afterwards*, while `FleetSim.__init__` has already captured
  mission crews from the original ids — so a sandbox's `missions.robots`
  genuinely reads `["AMR-01", …]`. Fix the writer, then enable the
  policy. Nothing dispatches from that column.
- **`alerts.src` / `incidents.src` / `*.tlabel`** stay free text. They
  are display labels, not references (`src` is legitimately `'fleet'`),
  and no executor routes on them. A sandbox visitor can therefore put any
  string, including a real robot's id, in an alert *inside their own
  sandbox*. If you run the notifier without `--site`, that string will
  show up in your alert feed.
- **A duplicate-key oracle.** `robots.id`, `alerts.id`, `incidents.id`,
  `missions.id` and `maintenance_findings.id` are global text primary
  keys. A demo visitor cannot hijack a real row through them —
  `on conflict do update` is refused by the demo UPDATE policy's USING
  clause, `do nothing` silently skips, and a bare insert raises `23505` —
  but the *difference* between those outcomes tells them whether an id
  exists somewhere in the database.
- **Authenticated command requests are not robot-scoped.** 0007's
  `rbac_commands_insert` checks the role and the row's site but, like
  0009, not `robot_id`: an operator at site A can queue a command naming
  a robot that lives at site B, stamped with site A. Requiring the robot
  to already exist would reject a legitimate command for a robot that has
  not reported in yet, so the database policy is unchanged and the
  executors' site check (layer 2) is what closes it.
- **`demo_reap_expired(p_grace_minutes int)` is granted to
  `authenticated` with no role check.** Any signed-in user — including a
  brand-new sign-up with no `user_roles` row — can call it and purge
  every **already-expired** sandbox's rows. It cannot touch a live
  sandbox (`expires_at < now()`), cannot touch a non-demo site
  (`_yf_demo_purge_sites` raises), and cannot enumerate anything: it
  returns counts. So this is a nuisance-grade denial of service against
  other visitors' dead demos, not a data-loss or disclosure path — which
  is why the published grant is left as-is. If you do not need a
  non-admin reaper, `revoke execute on function
  public.demo_reap_expired(int) from authenticated;` (0017 carries the
  same line, commented). `demo_mint_session`'s opportunistic reap keeps
  working: it is SECURITY DEFINER, so the privilege is checked against
  its owner.
- **Row-count abuse and the bearer-token model** are unchanged from
  0009's own "RESIDUAL RISKS" note: read it too.

### Turning the sandbox off

```sql
select public.admin_set_demo_limits(p_enabled => false);  -- admin only
select public.demo_reap_expired(0);                        -- purge what is left
```

or run 0009's commented ROLLBACK block to remove the anon grants and
policies entirely.

## EC2 (or any Linux host) notes

- Keep secrets out of shell history and unit files. Put them in
  `/etc/yantrafleet.env`:

  ```
  SUPABASE_URL=https://<project>.supabase.co
  SUPABASE_KEY=<service_role key — server side only>
  SARATHI_TOKEN=<long random string, e.g. openssl rand -hex 32>
  YANTRA_WEBHOOK_SECRET=<long random string>
  ```

- Lock the file down: `sudo chown root:root /etc/yantrafleet.env &&
  sudo chmod 600 /etc/yantrafleet.env`.
- Load it via systemd (`EnvironmentFile=/etc/yantrafleet.env`) or
  `set -a; . /etc/yantrafleet.env; set +a` in service start scripts.
- Rotate the service_role key from the Supabase dashboard if it ever
  leaks; rotating `SARATHI_TOKEN` / `YANTRA_WEBHOOK_SECRET` is just
  editing the file and restarting the services (and updating callers /
  webhook receivers).
