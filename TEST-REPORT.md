# Yantrika — Test Report (v0.12.1)

| Suite | Tests |
|---|---|
| core | 8 |
| sim | 89 (incl. 21 VDA5050 order/instantAction/connection conformance tests) |
| connector | 86 (incl. 20 VDA5050 connection-topic + order-publish tests) |
| detector | 60 |
| notifier | 89 (incl. 25 settings-sync/live-config tests) |
| copilot | 85 (incl. 20 settings-sync/live-config tests) |
| ops | 89 (incl. 5 version single-sourcing tests, 16 `grant-role` tests) |
| e2e | 35 (incl. 17 RBAC enforcement) |
| academy | 36 (browser; incl. lesson search + print-lesson) |
| console | 56 (browser; incl. login/signup/RBAC, empty-state card, admin Settings panel) |
| **Total** | **633** — 0 failed |

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
academy's `test_polish.py`: both hardcoded `"Yantrika v0.12.0"` as the
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

**VDA 5050 v2.1 conformance gaps closed.** An independent audit of `sim`
(yantrasim) and `connector` (yantrabridge) against the spec found the
fleet had no real inbound-order contract: a compliant master control
system could publish a valid `order` message and it would be silently
ignored. Fixed: inbound `order` topic support (orderId/orderUpdateId
adoption, stale-update rejection) plus a matching `publish_order()` on
the connector side; the `connection` topic (ONLINE/OFFLINE/
CONNECTIONBROKEN), previously never subscribed to at all; standard
instantActions (`cancelOrder`, `stateRequest`, `factsheetRequest`,
`initPosition`), previously unconditionally `FAILED`; order-driven task
`actionStates` now report a terminal `FINISHED` instead of silently
vanishing; and MQTT last-will/reconnect handling so
`CONNECTIONBROKEN` is actually reachable. One gap (auto-generating an
`order` from a dispatched mission) needs a `missions`-table schema
decision outside `sim`/`connector`'s scope and is documented as a
follow-up, not silently dropped.

**Admin settings panel — rotate operational keys without a redeploy.**
`GEMINI_API_KEY`, `SARATHI_TOKEN`, `WEBHOOK_URL`,
`YANTRA_WEBHOOK_SECRET`, and the four `TWILIO_*` values previously
required editing the deploy script's EDIT ME block and recreating/
rebooting the instance to change. New opt-in migration
`0008_app_settings.sql` adds an `app_settings` table with no direct
grants to anon/authenticated at all — access is only through two
`SECURITY DEFINER` RPCs (`admin_list_settings`/`admin_set_setting`)
gated by `yf_has_role('admin')`. `copilot` and `notifier` each poll it
in the background with a real fallback chain (live override -> existing
env var -> none), so a deployment that never touches the new table
behaves exactly as before. `console/index.html` gained an admin-only
Settings view. `SUPABASE_URL`/`SUPABASE_KEY`/`REPO_URL` deliberately
stay bootstrap-time-only values — there's no way to reach Supabase or
know which repo to run before those already exist.

**Public marketing site.** New `marketing/` directory: a static,
framework-free 7-page site (landing, about, features, security, docs
link-out, get-involved, changelog) with per-page SEO metadata (title/
description/canonical/Open Graph/JSON-LD), `sitemap.xml`, and
`robots.txt`. Every factual claim is sourced from this repo's own
README/docs/TEST-REPORT — no fabricated testimonials, customer logos,
or usage numbers. Two placeholders (the domain and the GitHub URL) need
a real value before going live; `marketing/README.md` has the exact
command.

Remaining live-only validation: real Supabase Auth (JWT issuance, own-row
user_roles read), 0006/0007/0008 on the production project, EC2 boot,
WebLLM real download, LLM tiers 1-2, webhook/Twilio delivery,
`grant-role` against a real Supabase project (offline-tested against a
fake backend only, same posture as `migrate`'s own test suite), and the
VDA5050 order/connection/instantAction fixes against a real (or
third-party) AGV rather than the offline fake-broker harness.
