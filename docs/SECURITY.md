# YantraFleet security: demo mode vs hardened mode

YantraFleet ships demo-open on purpose — one shared Supabase project,
one client-safe key, zero setup. This page explains what that means,
and how to harden a real deployment.

## The two modes

| | Demo (default) | Hardened (after `0006_harden.sql`) |
|---|---|---|
| RLS policy | `demo_all`: **anyone** with the anon key can read *and write* every table | `demo_all` dropped; `authenticated` role is **read-only**; anon has **no access** |
| Writers (sim, detector, connector, ops) | anon (publishable) key | **service_role** key (bypasses RLS) |
| Readers (copilot, notifier) | anon key | service_role key, or a Supabase Auth session (authenticated) |
| Console (browser) | anon key, no login | requires Supabase Auth — or Option B below |
| Good for | demos, evaluation, throwaway data | anything with real data |

## Key classes — know the difference

- **Publishable / anon key** (`sb_publishable_...` / legacy `anon` JWT):
  designed to be embedded in browsers and shipped in client code. It is
  only as dangerous as your RLS policies allow — in demo mode that is
  "fully open", in hardened mode "nothing at all".
- **service_role key** (`sb_secret_...` / legacy `service_role` JWT):
  bypasses Row Level Security entirely. It is a server-side admin
  credential. **Never ship the service_role key to a browser, embed it
  in the console, commit it to git, or put it in client-side code.**
  Servers and cron jobs only.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `SUPABASE_URL` | all services | Supabase project URL |
| `SUPABASE_KEY` | all services | Demo mode: the anon key (default is baked in). Hardened mode: **writers must switch this to the service_role key** — with `demo_all` gone, anon writes are refused |
| `SARATHI_TOKEN` | copilot (`sarathi`) | Optional. When set, `POST /ask` requires `Authorization: Bearer <token>` and answers 401 otherwise (constant-time compare). `GET /health` stays open but reports `auth_required: true`. Unset = open (demo behaviour) |
| `YANTRA_WEBHOOK_SECRET` | notifier | Optional. When set, every webhook POST carries `X-Yantra-Signature: sha256=<hex>` — an HMAC-SHA256 of the exact request body — so receivers can verify the sender. Unset = no header |

## Applying the hardening migration

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
3. decide what to do about the console (next section).

### Rolling back

`0006_harden.sql` ends with a clearly-marked, commented-out **ROLLBACK**
block. Uncomment and run it to restore `demo_all` on every table and
return to demo-open mode. (Re-running migrations 0001–0004 achieves the
same for their tables.)

## Console auth status

The browser console currently authenticates with the embedded anon key
only — it has no login flow yet. In hardened mode the anon key can read
nothing, so the console goes dark unless you pick one of:

- **Option A — Supabase Auth (planned/preferred):** add a login flow to
  the console; a signed-in user holds the `authenticated` role and the
  `hardened_read_authenticated` policies grant read access. Until the
  console grows that flow, use Option B.
- **Option B — read-only anon variant:** keep the console loginless but
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

  Writes still require service_role either way. Note Option B makes all
  fleet data readable by anyone holding the publishable key — fine for
  dashboards on trusted networks, not fine for sensitive data. Remove it
  with `drop policy if exists hardened_read_anon on public.<table>;`
  per table (or rerun the loop with only the drop line).

The console's command-approval writes (`commands` table) always need a
server-side path in hardened mode — anon/authenticated have no write
policy by design.

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
