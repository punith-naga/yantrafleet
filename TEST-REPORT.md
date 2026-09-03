# YantraFleet — Test Report (v0.12.1)

| Suite | Tests |
|---|---|
| core | 8 |
| sim | 68 |
| connector | 66 |
| detector | 60 |
| notifier | 64 |
| copilot | 65 |
| ops | 73 (incl. 5 version single-sourcing tests) |
| e2e | 35 (incl. 17 RBAC enforcement) |
| academy | 36 (browser; incl. lesson search + print-lesson) |
| console | 50 (browser; incl. login/signup/RBAC + empty-state card) |
| **Total** | **525** — 0 failed |

Also green: check_console.mjs · check_academy.mjs · academy/content/validate.py
· deploy/aws/validate.sh.
0007_rbac.sql additionally executed against real Postgres 16 (Supabase-
faithful harness): 11 behavioral RBAC tests + rollback + re-apply.

Note: `console/tests/test_console_rbac.py::test_expired_token_refreshes_once_and_retries`
is timing-sensitive under full-suite parallel load and can flake when run
alongside the other 49 browser tests; it passes reliably in isolation and
is not a functional regression.

Remaining live-only validation: real Supabase Auth (JWT issuance, own-row
user_roles read), 0006/0007 on the production project, EC2 boot, WebLLM
real download, LLM tiers 1-2, webhook/Twilio delivery.
