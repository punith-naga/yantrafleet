# YantraFleet — Test Report (v0.11.0)

| Suite | Tests |
|---|---|
| core | 8 |
| sim | 68 |
| connector | 66 |
| detector | 60 |
| notifier | 64 |
| copilot | 65 |
| ops | 65 (incl. 17 audit-security) |
| e2e | 35 (incl. 17 RBAC enforcement) |
| academy | 26 (browser; incl. accounts + WebLLM-fake tiers) |
| console | 41 (browser; incl. 8 login/RBAC) |
| **Total** | **498** — 0 failed |

Also green: check_console.mjs · check_academy.mjs · deploy/aws/validate.sh.
0007_rbac.sql additionally executed against real Postgres 16 (Supabase-
faithful harness): 11 behavioral RBAC tests + rollback + re-apply.

Remaining live-only validation: real Supabase Auth (JWT issuance, own-row
user_roles read), 0006/0007 on the production project, EC2 boot, WebLLM
real download, LLM tiers 1-2, webhook/Twilio delivery.
