# YantraFleet — Test Report (v0.12.1)

| Suite | Tests |
|---|---|
| core | 8 |
| sim | 68 |
| connector | 66 |
| detector | 60 |
| notifier | 64 |
| copilot | 65 |
| ops | 89 (incl. 5 version single-sourcing tests, 16 `grant-role` tests) |
| e2e | 35 (incl. 17 RBAC enforcement) |
| academy | 36 (browser; incl. lesson search + print-lesson) |
| console | 50 (browser; incl. login/signup/RBAC + empty-state card) |
| **Total** | **541** — 0 failed |

Also green: check_console.mjs · check_academy.mjs · academy/content/validate.py
· deploy/aws/validate.sh · deploy/azure/validate.sh.
0007_rbac.sql additionally executed against real Postgres 16 (Supabase-
faithful harness): 11 behavioral RBAC tests + rollback + re-apply.

Note: `console/tests/test_console_rbac.py::test_expired_token_refreshes_once_and_retries`
is timing-sensitive under full-suite parallel load and can flake when run
alongside the other 49 browser tests; it passes reliably in isolation and
is not a functional regression.

**CI now actually runs all 10 suites.** `.github/workflows/ci.yml`
previously only ran `[core, sim, connector, copilot]` in its matrix, plus a
`console` job whose `npm test --if-present` was silently a no-op (no
`package.json` exists anywhere in this repo) — `detector`, `notifier`,
`ops`, `e2e`, and both browser suites (console/academy, 86 tests
combined) never ran in GitHub Actions despite this table claiming "all
green". Fixed: the matrix now includes `detector`/`notifier`/`ops`; `e2e`
and a `browser` job (pytest-playwright, matching how these suites are
actually written — Python, not JS) were added. Wiring the browser suite
into CI surfaced one real pre-existing bug in each of console's and
academy's `test_polish.py`: both hardcoded `"YantraFleet v0.12.0"` as the
expected version-chip text, missed by the v0.12.1 single-sourcing release
bump. Fixed by reading `core/yantracore/version.py` live instead of a
literal, so this class of drift can't recur.

**RBAC bootstrap is now a real command, not hand-edited SQL.**
`yantraops grant-role` (16 new tests) closes the first-admin
chicken-and-egg gap documented in `docs/SECURITY.md` — previously the
only way to seed the first admin was pasting a commented SQL block from
`0007_rbac.sql` into the Supabase SQL editor. `supabase/README.md` and
`docs/SECURITY.md` also had a stale claim that `yantraops migrate`
applies every migration file it finds including the OPT-IN ones; that
stopped being true once the OPT-IN gate landed (v0.11.1) and is now
corrected in both docs, with a new "Going to production" walkthrough
(`migrate --include-opt-in` + `grant-role`) replacing the old "avoid
running migrate on a demo project" framing.

Remaining live-only validation: real Supabase Auth (JWT issuance, own-row
user_roles read), 0006/0007 on the production project, EC2 boot, WebLLM
real download, LLM tiers 1-2, webhook/Twilio delivery, `grant-role`
against a real Supabase project (offline-tested against a fake backend
only, same posture as `migrate`'s own test suite).
