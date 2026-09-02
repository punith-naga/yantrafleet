# YantraFleet — Test Report (v0.8.0)

All suites executed offline in the build container, 2026-08-26.

| Suite | Tests | Notes |
|---|---|---|
| core | 8 | canonical statuses + site_id helper |
| sim | 59 | VDA translation, commands, telemetry, missions |
| connector | 51 | untouched, no regression |
| detector | 60 | incidents + maintenance engines |
| notifier | 59 | dedup/digest + per-site filtering |
| copilot | 57 | incl. fake-LLM tier tests + grounding guard |
| e2e | 16 | full-stack loopback vs fake PostgREST |
| ops | 43 | orchestrator loopback smoke + subprocess run |
| console (browser) | 27 | 10 Playwright/Chromium + 9 static |
| **Total** | **380** | 0 failed |

`node scripts/check_console.mjs` — clean.

Known gaps (unchanged in kind): live Supabase RLS/CHECK behavior, real
webhook/Twilio delivery, and LLM tiers 1-2 against real APIs are only
exercised via fakes — first live run validates them. fleet_meta remains
single-site (rekey needed before a second site writes; noted in 0005).
Connector does not yet stamp site_id (covered by the DB column default).
