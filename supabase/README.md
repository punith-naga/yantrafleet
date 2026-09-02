# supabase/ — schema migrations for YantraFleet

Ordered, idempotent SQL files (`0001_...` → `0005_...`). Apply them **in
filename order** to a fresh Supabase project and every YantraFleet component
(sim, detector, notifier, copilot, console) can talk to it.

```
0001_init.sql         robots, alerts, incidents, missions, fleet_meta
0002_commands.sql     operator command queue + canonical robot statuses
0003_telemetry.sql    telemetry history (replay/analytics) + purge function
0004_maintenance.sql  maintenance tracking
0005_sites.sql        multi-site support
```

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
3. Repeat for `0002` … `0005`, **in order**.

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
