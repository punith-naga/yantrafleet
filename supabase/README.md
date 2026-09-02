# supabase/ — schema migrations for YantraFleet

Ordered, idempotent SQL files. Apply them **in filename order** to a fresh
Supabase project and every YantraFleet component (sim, detector, notifier,
copilot, console) can talk to it.

## Migration order + modes

| File | Mode | Apply when |
|---|---|---|
| `0001_init.sql` | baseline (demo-open) | always — robots, alerts, incidents, missions, fleet_meta |
| `0002_commands.sql` | baseline | always — operator command queue + canonical robot statuses |
| `0003_telemetry.sql` | baseline | always — telemetry history + purge function |
| `0004_maintenance.sql` | baseline | always — predictive-maintenance findings |
| `0005_sites.sql` | baseline | always — `site_id` on every operational table |
| `0006_harden.sql` | **opt-in: hardened** | moving beyond demo — drops `demo_all`; authenticated = read-only, writers switch to the service_role key |
| `0007_rbac.sql` | **opt-in: RBAC** | production — replaces demo/0006 policies with role-based ones (`user_roles`, `yf_role()`/`yf_has_role()`, `decide_command()` RPC, academy tables). Can be applied straight after 0005; drops 0006's policies itself if present |

The three postures (demo / hardened / RBAC), the role capability matrix,
and what to reconfigure after each opt-in file are documented in
[`docs/SECURITY.md`](../docs/SECURITY.md). Both opt-in files start with a
loud warning header and end with a commented **ROLLBACK** block that
restores the previous posture.

Two caveats for the opt-in files:

* **`yantraops migrate` applies every `*.sql` file it finds**, 0006/0007
  included — running it against a project you want to keep demo-open will
  lock that project down. To stay demo-open, apply `0001`–`0005` by hand in
  the SQL editor (or point `--dir` at a copy of this directory without the
  opt-in files). If it happens by accident, run the ROLLBACK block at the
  bottom of the applied file(s).
* **`0007_rbac.sql` requires a Supabase project** — it references
  `auth.users`, `auth.uid()` and `auth.email()`, so it will not apply to a
  vanilla Postgres database (0001–0006 will).

`python -m yantraops migrate --print-order` prints the exact order (needs no
database and no extra packages).

## One-command apply (recommended)

```bash
pip install -e ops/          # brings in psycopg
python -m yantraops migrate --db-url "postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres"
```

The runner:

* applies each file **in its own transaction** — a failure never leaves a
  half-applied file;
* records applied files in `_yf_migrations(filename, applied_at, checksum)`
  and **skips** anything already applied, so rerunning is always safe;
* **warns** if a checked-in file changed after it was applied (checksum
  mismatch) and only reapplies it with `--force`;
* `--dry-run` connects and lists what *would* run, changing nothing;
* prints a summary like `done: 5 applied, 0 skipped.`

### Where to get the `--db-url` (Supabase dashboard)

1. Open your project at <https://supabase.com/dashboard>.
2. Click **Connect** at the top of the project page (or **Project Settings →
   Database**).
3. Copy the **Direct connection** URI — it looks like:

   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.<project-ref>.supabase.co:5432/postgres
   ```

4. Replace `[YOUR-PASSWORD]` with your **database password** (set when the
   project was created; resettable under Project Settings → Database). This
   is *not* the anon API key.
5. On IPv4-only networks the direct host may not resolve — use the **Session
   pooler** URI from the same Connect dialog instead (port 5432 via
   `aws-*.pooler.supabase.com`); the migrate command works identically.

Quote the URL in your shell (passwords often contain `&` or `!`). Special
characters in the password must be URL-encoded (`@` → `%40`, etc.).

Note the runtime components use different credentials: `SUPABASE_URL`
(`https://<project-ref>.supabase.co`) + the **anon key** from Project
Settings → API. Only `migrate` needs the Postgres URL.

## Manual fallback: the SQL editor

No psycopg, or outbound port 5432 blocked? Apply the files by hand:

1. Dashboard → **SQL Editor** → **New query**.
2. Paste the contents of `0001_init.sql`, click **Run**.
3. Repeat for `0002` … `0005`, **in order** — and `0006`/`0007` only if
   you are opting into hardened/RBAC mode (read their headers first).

Every file is idempotent (`create table if not exists`, guarded `alter`s),
so running one twice is harmless. The manual path does not populate
`_yf_migrations`; if you later switch to `yantraops migrate`, it will replay
the files — harmless for the same reason, or pre-seed the table if you care.

## After migrating

```bash
python -m yantraops up --supabase --url https://<project-ref>.supabase.co --key <anon-key>
```

`up --supabase` probes `rest/v1/robots` before starting anything and tells
you to run `migrate` (exit code 2) if the schema is missing.
