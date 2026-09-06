# Yantrika — Windows runbook (PowerShell)

Everything on this page is plain PowerShell from the repo root. No WSL, no
Docker, no venv activation — the venv's `python.exe` is called by full path
throughout, so it works the same in any shell.

**Prerequisites**

- Windows 10/11, PowerShell 5.1 or newer (7.x fine).
- Python **3.10 or newer** (3.11/3.12/3.13 all fine). The installer probes the
  `py` launcher and `python` on PATH and picks the first suitable one; if none
  exists it points you at <https://www.python.org/downloads/windows/> (check
  *"Add python.exe to PATH"* in that installer).
- A modern browser. **Playwright is not required** — it is only used by the
  console *test* suite, never at runtime.

---

## 1. Install

### Path A — the one-shot installer (recommended)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1        # install only
powershell -ExecutionPolicy Bypass -File install.ps1 -Run   # install, then start the demo
```

It creates `.venv\` in the repo root, installs the six packages editable plus
the copilot requirements, and verifies `python -m yantraops --help` imports
cleanly. It is idempotent — re-run it any time to refresh the install (an
existing `.venv` is reused). Set `INSTALL_VENV_DIR` to put the venv elsewhere.

### Path B — manual venv

If you prefer to see every step (or the installer is blocked by policy):

```powershell
py -3.12 -m venv .venv          # or -3.11 / -3.13; plain "py -3" also works if it's >= 3.10
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e core -e sim -e connector -e detector -e notifier -e ops
.venv\Scripts\python.exe -m pip install -r copilot\requirements.txt
```

Then check the environment:

```powershell
.venv\Scripts\python.exe -m yantraops doctor
```

`doctor` prints a PASS/WARN/FAIL line per prerequisite and ends with the exact
next command (either `up --loopback`, or the `pip install` line that fixes
whatever failed). A "Supabase unreachable" **WARN** on a blocked network is
normal and harmless.

---

## 2. The loopback demo (no cloud, no keys, no robots)

```powershell
.venv\Scripts\python.exe -m yantraops up --loopback
```

This starts, in one process tree: an in-memory fake PostgREST backend, the
10-robot simulator, the incident detector, the notifier (dry-run), the Sarathi
copilot API, and a static server for the console. It ends with a banner like:

```
✔ READY
  backend   http://127.0.0.1:53211
  sarathi   http://127.0.0.1:53214
  console   http://127.0.0.1:53217/index.html?supa=...&key=...
```

Your default browser opens the **console** URL automatically (suppress with
`--no-open`). If you open a URL by hand, use the one labeled `console` — the
`backend` URL is a REST API and just shows JSON.

- `Ctrl-C` shuts everything down cleanly (children stopped in reverse order).
- `.venv\Scripts\python.exe -m yantraops status` health-checks a running stack.
- `--duration 60` runs for a minute then exits — handy for smoke tests.

**First run:** Windows Defender Firewall may pop a prompt for `python.exe`.
Everything binds 127.0.0.1 only, so you can safely click *Cancel* — loopback
traffic is not affected either way. Allowing it on private networks is also
fine.

---

## 3. The `--mqtt` demo (real VDA 5050 wire)

Same demo, but robot data travels over an actual MQTT broker with real
VDA 5050 v2.1 topics, and `yantrabridge` bridges it into the backend —
approved operator commands go back out as VDA `instantActions`.

```powershell
.venv\Scripts\python.exe -m pip install -e ".\ops[mqtt]"    # embedded broker (amqtt) + client
.venv\Scripts\python.exe -m yantraops up --loopback --mqtt
```

(Quote `".\ops[mqtt]"` — square brackets are special to PowerShell.)

Against your own broker and real robots:

```powershell
.venv\Scripts\python.exe -m yantraops up --supabase --mqtt --broker your-host:1883 --no-sim
```

The embedded broker is demo-grade (no TLS/auth); use your own mosquitto for
anything beyond a laptop demo.

---

## 4. Real Supabase

1. Create a project at <https://supabase.com/dashboard> (free tier is enough).

2. **Get the Postgres connection string** (for migrations only): on the
   project page click **Connect** (or **Project Settings → Database**) and copy
   the **Direct connection** URI:

   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.<project-ref>.supabase.co:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]` with your **database password** (set at project
   creation; resettable under Project Settings → Database). This is *not* the
   anon API key. On IPv4-only networks use the **Session pooler** URI from the
   same Connect dialog instead — it works identically.

3. **Apply the schema** (quote the URL — passwords often contain `&` or `!`;
   URL-encode special characters, e.g. `@` → `%40`):

   ```powershell
   .venv\Scripts\python.exe -m yantraops migrate --db-url "postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres"
   ```

   The runner applies `supabase\0001…0005.sql` in order, each file in its own
   transaction, tracks them in `_yf_migrations`, and skips anything already
   applied — rerunning is always safe. `--dry-run` previews; no psycopg or
   port 5432 blocked? Paste the files into the dashboard **SQL Editor** in
   order instead (they are idempotent). See `supabase\README.md`.

4. **Get the runtime credentials**: Project Settings → API →
   `https://<project-ref>.supabase.co` and the **anon** key.

5. **Start the stack against it**:

   ```powershell
   .venv\Scripts\python.exe -m yantraops up --supabase --url https://<project-ref>.supabase.co --key <anon-key>
   ```

   (Or set `SUPABASE_URL` / `SUPABASE_KEY` in the environment.) `up --supabase`
   probes the schema first — if migrations are missing it exits with code 2 and
   prints the exact `migrate` command to run.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named yantraops` (often right after an apparently fine install) | The install ran under **Python 3.9 or older** — the packages require `>= 3.10`, so pip skips or refuses them; or you're invoking a different Python than the venv's. | Check `.venv\Scripts\python.exe --version`. If it's 3.9, delete `.venv\` and re-run `install.ps1` (it only picks 3.10+), or `py -3.12 -m venv .venv` manually. Always start the stack with `.venv\Scripts\python.exe -m yantraops ...`, not bare `python`/`py`. |
| Browser shows raw JSON like `[]` or `{"robots":...}` instead of a dashboard | You opened the **backend** URL from the banner instead of the **console** URL. | Use the line labeled `console` (it ends in `/index.html?supa=...`). The banner's `backend`/`sarathi` lines are APIs. |
| Windows Defender Firewall prompt on first start | Python binding its first listening socket. | Safe either way: everything is 127.0.0.1-only, so *Cancel* still works; *Allow on private networks* is fine too. |
| `playwright: command not found` / worrying about browsers | Playwright is a **test-only** dependency for `console\tests`. | Nothing to do at runtime. To run the browser suite: `pip install playwright pytest-playwright`, then `python -m playwright install chromium` (or point `PLAYWRIGHT_BROWSERS_PATH` at a preinstalled cache). |
| `[WinError 10048] only one usage of each socket address` / port already in use | A previous stack (or another app) is still holding a port — usually a stale run that wasn't Ctrl-C'd. | `.venv\Scripts\python.exe -m yantraops status` to see what's alive; close the old window or `taskkill /f /im python.exe` (careful: kills *all* Pythons). The stack itself uses ephemeral ports, so a clean restart normally just picks new ones; delete `%USERPROFILE%\.yantraops-state.json` if `status` reports a long-gone stack. |
| `install.ps1 cannot be loaded ... running scripts is disabled` | PowerShell execution policy. | Run it exactly as documented: `powershell -ExecutionPolicy Bypass -File install.ps1` (bypasses policy for this one invocation, changes nothing system-wide). |
| `pip install -e ops[mqtt]` fails oddly in PowerShell | Square brackets are wildcard characters in PowerShell. | Quote it: `pip install -e ".\ops[mqtt]"`. |

Still stuck? `.venv\Scripts\python.exe -m yantraops doctor` diagnoses most
environment problems and prints the fixing command; failing that, open an
issue with the doctor output attached.
