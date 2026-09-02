# YantraFleet — Test Report (v0.9.0)

All suites executed offline, 2026-09-02.

| Suite | Tests |
|---|---|
| core | 8 |
| sim | 68 |
| connector | 66 |
| detector | 60 |
| notifier | 64 |
| copilot | 65 |
| ops | 45 |
| e2e | 17 |
| console | 33 (24 browser via Playwright/Chromium + 9 static) |
| **Total** | **426** — 0 failed |

Also green: node scripts/check_console.mjs · bash deploy/aws/validate.sh (18 checks).

Known gaps: live Supabase RLS/0006, real webhook/Twilio delivery, LLM tiers
1-2 against real APIs, and an actual EC2 boot of deploy/aws are validated by
fakes/parsers/linters offline — first live runs are the remaining test.
QoS-0 instantActions are fire-once (offline robots miss them; re-approve).
