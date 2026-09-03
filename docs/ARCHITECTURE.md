# YantraFleet — What We Built, and How

*Comprehensive implementation reference — v0.12.1, 2026-09-03*

This document is the single place to understand the whole platform: every
component, why it exists, how it talks to the others, the security model,
how it gets deployed, how it's tested, and how it was actually built. It is
written for a technical reader (engineer, CTO, or a technical investor) who
wants the real picture, not the pitch deck.

For the pitch-deck version, see `docs/index.html`. For the day-to-day
command reference, see the root `README.md` and `docs/RUNBOOK-WINDOWS.md`.
For the security posture in isolation, see `docs/SECURITY.md` and
`docs/COMPLIANCE.md`.

---

## 1. What YantraFleet is

YantraFleet is an operations platform for **Physical AI** — specifically,
fleets of autonomous mobile robots (AMRs) in warehouses and industrial
sites. "Physical AI" is the umbrella the industry (NVIDIA, Amazon Robotics,
Physical Intelligence, Google DeepMind and others) uses for AI systems that
act in the physical world rather than just generate text or images: a
warehouse AMR, a manipulator arm, a humanoid. Every one of those systems
needs the same operational layer once you have more than a handful of them
running: a place to see what they're doing, a way to catch problems before
they become costly, a gate that keeps a human in the loop for anything
consequential, and a way for new operators to learn the system quickly.
YantraFleet is that layer.

Concretely, the platform is three things that share one backend and one
identity system:

1. **The fleet-ops core** — simulator/connector → Supabase → console,
   detector, notifier, predictive maintenance, human-approval command gate.
   This is the part a real deployment runs continuously.
2. **Sarathi** — an AI copilot, embedded in the console, that answers
   questions about the fleet ("why is AMR-07 down?", "what's pending
   approval?") with cited evidence pulled live from the same database,
   never from imagination.
3. **YantraFleet Academy** — a self-contained learning product, sharing the
   same login, that teaches two things: how to *operate* this specific
   platform (hands-on, against a live simulated fleet), and how Physical AI
   *actually works* as a field (perception, planning, robot learning,
   simulation, the 2026 industry landscape) — content built from
   web-verified 2026 industry facts, not platform marketing.

All three run from one repository, one `yantraops` CLI, and (for the
demo/local path) zero paid infrastructure — the loopback mode needs no
Supabase account, no API keys, and no internet access at all.

## 2. System architecture

### 2.1 Data flow

```
                          ┌─────────────────────────────────────────┐
   Real AMRs (any vendor) │              MQTT broker                │
   speaking VDA 5050 v2.1 │         (VDA 5050 v2.1 topics)          │
        ──────────────────▶  state / instantActions / actionStates  │
                          └───────────────┬───────────────────────┬─┘
                                          │                       │
                    ┌─────────────────────▼───┐      ┌────────────▼────────┐
                    │  yantrabridge (connector) │      │   yantrasim (sim)    │
                    │  VDA→row translation,     │      │  10-AMR deterministic │
                    │  alert dedup, batched sink │      │  simulator, same     │
                    │  + CommandPublisher        │      │  translation code    │
                    └─────────────┬──────────────┘      └───────────┬─────────┘
                                  │                                 │
                                  └────────────────┬────────────────┘
                                                    ▼
                                     ┌───────────────────────────┐
                                     │   Supabase (PostgREST)    │
                                     │  robots · alerts · incidents │
                                     │  commands · missions ·     │
                                     │  robot_telemetry ·         │
                                     │  maintenance_findings ·    │
                                     │  fleet_meta · user_roles · │
                                     │  academy_progress ·        │
                                     │  certificates              │
                                     └──┬───────┬─────────┬───────┘
                                        │       │         │
                       ┌────────────────┘       │         └───────────────┐
                       ▼                        ▼                         ▼
             ┌──────────────────┐    ┌────────────────────┐   ┌──────────────────────┐
             │  yantradetect     │    │  yantranotify        │   │  console / academy    │
             │  incidents +      │    │  console/webhook/     │   │  (single-file apps,   │
             │  predictive       │    │  WhatsApp fan-out,    │   │  no build step)       │
             │  maintenance      │    │  dedup + backoff      │   │  ↕ Sarathi /ask       │
             └──────────────────┘    └────────────────────┘   └──────────────────────┘
```

Every arrow above is a real, tested code path — not a diagram of an
aspiration. The simulator and the real-robot connector deliberately share
the same translation logic (`translate.py` in each package, kept in parity
and covered by parallel test suites), so the console, copilot, detector,
and notifier cannot tell a simulated fleet from a real one. That is the
core design decision that let this whole platform be built and proven
*before* a physical robot was ever involved: everything downstream of the
`robots` table is validated against the simulator today and will not need
to change when a real fleet is plugged in — only `yantrabridge`'s source
changes, from JSONL replay to a live MQTT feed from the robots' own
VDA 5050 stack.

### 2.2 Why VDA 5050

VDA 5050 is the real, published interoperability standard (maintained by
the VDA — the German Association of the Automotive Industry — and the VDMA)
for AMR fleets from *different vendors* to speak one protocol to a
fleet-management layer. Building on a real standard rather than a bespoke
schema means: (a) the platform is credible to anyone evaluating it against
industrial requirements, (b) a real AMR vendor's existing VDA 5050 stack
can plug into `yantrabridge` with translation work only, not a robot-side
integration project, and (c) the "instantActions" / "actionStates" pattern
gives the human-approval command gate (below) a standards-based mechanism
for closing the loop, rather than an invented one.

### 2.3 Why Supabase

Supabase was chosen deliberately for the reasons that matter to an
early-stage platform: it is Postgres (so the schema, RLS policies, and
SECURITY DEFINER functions are portable to any Postgres — no lock-in), it
exposes PostgREST (a REST API generated from the schema — every component
in this repo talks to the backend over plain HTTPS/JSON, no vendor SDK
lock-in either), it has a real free tier sufficient for a genuine pilot,
and it ships Auth (JWT issuance, refresh tokens) that the RBAC model in
Section 5 builds directly on. Row Level Security is not a Supabase feature
— it is Postgres's own security model — so the "hardened" and "RBAC" modes
described below would work identically against self-hosted Postgres.

## 3. Components, one by one

| Component | Package | Lines | What it does |
|---|---|---|---|
| `core/` | `yantracore` | ~110 | The contract every other component imports: the seven canonical robot statuses (`active · idle · charging · paused · estop · degraded · fault`), a `normalize()` for legacy spellings, the multi-site `site_id()` helper, and the single-sourced release `__version__`. |
| `sim/` | `yantrasim` | ~1,590 | A deterministic, seeded 10-AMR warehouse simulator: 3 simulated vendors, an 8×5 waypoint grid, battery/charging physics, a scripted localization-fault incident (used pedagogically in the Academy), and mission-aware task assignment. Emits real VDA 5050 v2.1 `state` messages and publishes them via Supabase, MQTT, or stdout. |
| `connector/` | `yantrabridge` | ~1,780 | The real-robot path: VDA 5050 → row translation (kept in lockstep with the simulator's), alert dedup with hysteresis, a batched PostgREST sink, sources for MQTT or recorded JSONL/MCAP files, and `CommandPublisher` — the piece that turns an approved command into a real VDA 5050 `instantAction` and watches for the robot's `actionState` acknowledgment. |
| `detector/` | `yantradetect` | ~1,020 | Two engines in one package. `IncidentEngine`: PagerDuty-style incident lifecycle — flap-guarded fault/estop detection, one open incident per robot, deterministic idempotent `INC-XXXX` ids, auto-resolve with duration and impact. `MaintenanceEngine`: reads `robot_telemetry` trend windows and raises `maintenance_findings` (motor-temp trend, battery-drain anomaly) with a heuristic remaining-useful-life estimate and confidence score — this is the "predictive maintenance" story. |
| `notifier/` | `yantranotify` | ~1,080 | Multi-channel alert fan-out for unacked critical/serious alerts and open incidents: console, webhook (Slack Block Kit / Discord embed shaped), WhatsApp via Twilio. Retry with backoff, a circuit breaker so a dead channel doesn't block the others, severity-grouped digests, HMAC-signed webhooks, multi-site filtering. |
| `copilot/` | `sarathi` | ~1,760 | The AI copilot, "Sarathi" (Sanskrit for charioteer/guide — the one who drives with you). A FastAPI service exposing `POST /ask`; see Section 6. Also exposes the same fleet tools over MCP (Model Context Protocol) for external agents. |
| `ops/` | `yantraops` | ~1,990 | The orchestrator CLI: `up` (loopback/Supabase/MQTT modes, auto-open browser, READY banner with timing), `migrate` (tracked, checksummed, OPT-IN-gated schema application), `doctor` (environment preflight), `status`, `audit-security` (live security-posture scan — see Section 5). |
| `console/` | — | 2,161 (single file) | The operations console: live fleet map, robot detail, alerts, incidents with recorded-telemetry replay, missions, a Pending Approvals card (the human-in-the-loop gate), an Audit view, an SLA/uptime analytics card, the Sarathi Copilot chat panel, a command palette (Ctrl/Cmd+K), a first-run guided tour, a printable shift report, and sign-in/sign-up. Deliberately zero build step — one HTML file, one `<script>` block, works from `file://` or any static host. |
| `academy/` | — | 1,776 (single file) | The learning platform — see Section 7. Same single-file, zero-build architecture as the console, same design language, shares the console's login session. |
| `supabase/` | — | 7 migrations | The schema, as ordered, idempotent, hand-reviewed SQL — see Section 4. |
| `deploy/aws/` | — | — | A console-UI-only (no CLI, no Terraform) EC2 deployment kit — see Section 8. |
| `docker/` | — | — | Container packaging for the whole demo stack (host networking). |
| `e2e/` | — | — | `fakerest.py`: an in-process fake PostgREST server used by nearly every test suite in the repo (including an RBAC-emulation mode) so the whole platform can be verified with zero network egress. |
| `docs/` | — | — | This document, the landing page (`index.html`), the Windows runbook, `SECURITY.md`, `COMPLIANCE.md`. |

Every Python package (`core`, `sim`, `connector`, `detector`, `notifier`,
`copilot`, `ops`) is installed editable (`pip install -e`) and has its own
offline pytest suite with no network dependency. `console` and `academy`
are plain files with Playwright-driven browser test suites.

## 4. Data model

One Postgres schema, applied as seven ordered migrations, backs every
component:

```
robots(id, vendor, status, battery, pos jsonb[x,y], speed, task_kind,
       health, motor_temp, tasks_done, fault_msg, site_id, updated_at)
alerts(id, sev, msg, src, tlabel, ack, site_id, created_at)
incidents(id, sev, title, src, tlabel, state, impact, rca, fix, dur,
          site_id, created_at)
commands(id, robot_id, cmd, status, requested_by, decided_by, note,
         site_id, requested_at, decided_at)
missions(id, name, robots jsonb, state, prog, eta, site_id, created_at)
robot_telemetry(id, robot_id, ts, battery, speed, motor_temp, status,
                pos, site_id)
maintenance_findings(id, robot_id, component, finding, rul_days,
                     confidence, action, state, site_id, created_at,
                     cleared_at)
fleet_meta(id=1, writer_id, sim_min, throughput, site_id, updated_at)

-- added by the OPT-IN 0007_rbac.sql migration:
user_roles(user_id, site_id, role)                 -- operator|engineer|manager|admin
academy_progress(user_id, lesson_id, status, score, updated_at)
certificates(id, user_id, track_id, issued_at, badge jsonb)
```

`robots.status` is constrained at the database level (a `CHECK` constraint)
to the seven canonical values from `yantracore` — this is what keeps the
simulator, the real-robot connector, the console, and the copilot from ever
disagreeing about what a robot's state means. Every table carries `site_id`
so one Supabase project can host multiple physical sites with row-level
isolation once RBAC is enabled.

The migrations are designed to be applied incrementally and safely:

- `0001_init.sql` – `0005_sites.sql`: the baseline schema above, plus
  permissive `demo_all` RLS policies (anyone with the publishable key can
  read/write) — this is what a brand-new demo project needs, nothing more.
- `0006_harden.sql` (**OPT-IN**): revokes the demo policies and installs
  authenticated-read / service-role-only-write policies. Writers (sim,
  detector, notifier, bridge) switch to the service-role key; console
  readers still need only Supabase Auth, not the service key.
- `0007_rbac.sql` (**OPT-IN**, requires 0006 first): adds `user_roles`,
  `academy_progress`, `certificates`, the `yf_role()`/`yf_has_role()`
  helper functions, and three `SECURITY DEFINER` RPCs — `decide_command`,
  `save_progress`, `issue_certificate` — that enforce the role hierarchy
  (`operator < engineer < manager < admin`) at the database layer, not just
  in application code.

"OPT-IN" is not a comment — it's an enforced property. `ops/yantraops/
migrate.py` scans each migration file for an "OPT-IN" marker in its header
and will **not** apply 0006 or 0007 unless the operator explicitly passes
`--include-opt-in`. This exists because of a real failure mode caught
during development: running the lockdown migrations against a brand-new
Supabase project, before any Auth user exists to hold the `admin` role,
would permanently lock the operator out of their own project. `migrate`
defaults to the safe path; going further is a deliberate, informed choice.

0007 is also the one migration in this repo that was validated beyond
review — it was executed against a real local Postgres 16 instance with a
Supabase-faithful test harness: all 14 policies and 11 behavioral tests
(role escalation attempts, cross-site access attempts, RPC authorization)
plus a full rollback-and-reapply cycle were actually run, not just read.

## 5. Security & RBAC model

The platform ships **three deployment modes**, and it is honest with the
operator about which one they're in — `yantraops audit-security` connects
to the live backend and classifies it automatically:

| Mode | Migrations applied | Who can write | Who can read | Intended use |
|---|---|---|---|---|
| `demo` | 0001–0005 | Anyone with the publishable key | Anyone with the publishable key | Local/loopback demos, first evaluation |
| `hardened` | + 0006 | Service-role key only (backend writers) | Any authenticated Supabase user | A shared pilot where writers are trusted infra and readers are known people |
| `rbac` | + 0007 | Service-role key + role-gated RPCs for human actions (approve/reject commands) | Role- and site-scoped per `user_roles` | Production-shaped: least privilege, auditable, multi-site |

`audit-security` also has a fourth diagnosis, `no-schema` — added after a
real bug: the original classifier only distinguished modes by HTTP 401/403
(RLS denial) vs. 200 (success), and misread a brand-new, unmigrated project
(which returns 404 — the tables don't exist yet) as `rbac` mode, which was
actively misleading. It now correctly detects "you haven't run the
migrations yet" and says so, with the exact remediation command.

Authentication is real Supabase Auth (JWT-based), not a mock: `/auth/v1/
signup` and `/auth/v1/token` (password and refresh_token grants) are used
directly by both the console and the academy, each maintaining its own
`YFAuth` module — sign-in, create-account, one automatic refresh-and-retry
on a 401, and role-aware UI (an operator sees the same dashboard a manager
does, but the approve/reject actions on the Pending Approvals card only
render for manager-and-above roles, and the underlying `decide_command` RPC
enforces that same rule server-side regardless of what the UI shows).

Beyond RLS and RBAC, the security work covers the pieces a real deployment
actually needs: an optional bearer-token gate on the Sarathi API
(`SARATHI_TOKEN`, constant-time compared), optional HMAC-SHA256 signed
webhooks (`YANTRA_WEBHOOK_SECRET`) so a receiver can verify a notification
really came from this platform, and a documented key-class matrix (which
component needs the publishable key vs. the service-role key, and why).
The full model — including the responsible defaults and what's explicitly
deferred as future work (e.g., production certificate signing) — is in
`docs/SECURITY.md`; the compliance mapping (SOC 2 CC-series, ISO 27001
Annex A, EU AI Act Article 14 human-oversight requirements, VDA 5050
conformance statement, ISA-18.2/EEMUA alarm-rate budgets, NIST CSF) is in
`docs/COMPLIANCE.md`, including an explicit gaps-to-close checklist rather
than an unqualified compliance claim.

## 6. The human-in-the-loop command gate

This is the mechanism that makes the platform trustworthy for anything
beyond passive monitoring, so it's worth calling out on its own. A robot
action initiated from the console — pause, send-to-charger, e-stop — does
not execute directly. It is written as a `pending` row in `commands`. It
only becomes real once a human with sufficient role approves it (or a
manager rejects it, with a note, also recorded). From there:

- **Simulated fleet**: `yantrasim` polls approved commands, applies them to
  the simulated world, and marks the row `executed`/`failed`.
- **Real fleet**: `yantrabridge`'s `CommandPublisher` polls approved
  commands, publishes them as genuine VDA 5050 `instantActions` over MQTT,
  and — critically — does **not** invent a completion status. It watches
  for the robot's own `actionState` (`FINISHED`/`FAILED`) coming back on
  the state topic and closes the loop from that, the same signal a real
  fleet-management system would use.

Every decision is fully audited: who requested it, who decided it, when,
and the outcome — visible on the console's Audit view. This pattern — an
explicit approval step with a database-enforced role check on the deciding
side (`decide_command` RPC under RBAC mode) — is what lets the EU AI Act
Article 14 mapping in `docs/COMPLIANCE.md` claim "human oversight by
design" rather than "human oversight if someone remembers to add it."

## 7. The AI systems

### 7.1 Sarathi — the fleet copilot

Sarathi answers questions about the live fleet with cited evidence, and it
is built to degrade honestly rather than fail silently. Three tiers, tried
in order:

1. **`grounded`** — an LLM (via `litellm`, so any of several providers
   work through `GEMINI_API_KEY`/`OPENAI_API_KEY`/`SARATHI_MODEL`) with
   function-calling access to a `Toolbox` of six fleet tools
   (`get_fleet_summary`, `query_robots`, `query_alerts`, `query_incidents`,
   `query_commands`, `query_telemetry`), each call recorded with a
   citation. A **grounding guard** then extracts any numbers the LLM's
   answer states and cross-checks them against the actual tool-result
   payloads, tagging the answer `verified`, `unverified`, or `computed` —
   this is the mechanism that stops the model from confidently stating a
   battery percentage it didn't actually look up.
2. **`llm_only`** — same LLM, without a live grounding cross-check (used
   when tool access isn't available in context).
3. **`offline`** — a zero-LLM-key fallback: a keyword intent classifier
   over the same live data, producing template answers. This is what runs
   in the default loopback demo, with no API key required at all, and it's
   also the mode the platform degrades to if the LLM tiers fail — the
   copilot never just returns an error to the operator.

Sarathi is also exposed over MCP (`copilot/sarathi/mcp_server.py`), so an
external agent platform (including the user's other Agentic AI platform,
Vegaduta) can query fleet state as a tool — this is the integration seam
between YantraFleet and the broader multi-agent ecosystem it's meant to sit
inside.

### 7.2 Academy tutor — the same pattern, in the browser

The Academy's lesson tutor uses the identical three-tier degradation
philosophy, but tier 1 runs **on-device**: WebLLM (`@mlc-ai/web-llm`,
pinned to `0.2.79`) loads a small open-weight model (default
Qwen2.5-1.5B-Instruct, with a Llama-3.2 1B/3B ladder available) directly in
the learner's browser via WebGPU — no server round-trip, no API key, no
data leaving the device. It falls back to `sarathi` (server-side, grounded
in the same fleet data a lesson is teaching against) and finally to a
canned keyword-match tutor if neither is available. The three backends
share one `window.YFTutor` interface (`status/init/ask/grade`), and a test
seam (`window.__YF_WEBLLM_FACTORY`) lets the whole flow be exercised in
Playwright without ever downloading a real model in CI.

### 7.3 Why in-browser LLM inference at all

This was a deliberate industry-alignment choice, not a gimmick: running the
tutor's model in the learner's browser means the Academy has zero marginal
inference cost per learner and zero dependency on an LLM API key to teach
its core content — which matters for a learning product aimed at operators
and students, not just funded pilots. It also doubles as a live,
hands-on demonstration of on-device AI inference, which is directly
relevant to the "Physical AI Foundations" curriculum described next (edge
inference is exactly what runs onboard a real robot).

## 8. YantraFleet Academy — curriculum

The Academy is content-driven: `academy/content/pack-physical-ai.json`
defines tracks, lessons, quizzes, practicals, tutor grounding context, and
a checkride — all data, so the curriculum grows without touching the
application code. As of v0.12, the pack (v1.1.0) has **4 tracks, 19
lessons**:

- **Operator**, **Fleet Admin**, **Integrator** — product-usage tracks:
  hands-on lessons taught *against the running simulated fleet* (open the
  console, find a robot, approve a command, read an incident) rather than
  as abstract reading — the platform teaches itself.
- **Physical AI Foundations** (new in v0.12, 7 lessons) — general
  industry knowledge, not product usage: the sense→plan→act loop,
  sensing/perception/SLAM, planning and control, robot learning and
  vision-language-action (VLA) foundation models, simulation-first
  development methodology, the 2026 industry landscape, and safety/
  standards/human oversight.

Every platform-usage fact in the curriculum is sourced directly from this
repository's own code and behavior — nothing is aspirational. Every
industry-knowledge fact was web-verified during construction against 2026
sources: NVIDIA's GR00T open-weight foundation model and three-computer
robotics stack, Physical Intelligence's Apache-licensed openpi/π0 models,
Google's Gemini Robotics 1.5 / Robotics-ER 1.5, Amazon Robotics passing its
one-millionth deployed robot alongside the DeepFleet coordination model,
and the IFR's ranking of India as the world's #6 robotics market — the
research context the user is personally operating in.

The Academy ends in a **checkride**: a practical assessment gated behind
lesson completion, producing an Open Badges 3.0-shaped certificate
(exported as JSON) recorded server-side under RBAC mode via the
`issue_certificate` RPC — documented explicitly as demo-grade (unsigned)
rather than overclaiming a production credentialing system.

## 9. Testing philosophy and coverage

The guiding rule for this whole build was: **every suite that exists must
stay green before anything is committed.** No feature was merged on the
strength of "it should work" — every batch of work ended with a full
verification sweep across every suite that could have been affected,
including suites owned by a different agent in the same batch.

As of v0.12.1, the full suite is:

| Suite | Tests | Notes |
|---|---|---|
| `core` | 8 | status contract, site helper, version |
| `sim` | 68 | simulator physics, translation, commands, telemetry history |
| `connector` | 66 | VDA→row translation, dedup, sinks, sources, CommandPublisher |
| `detector` | 60 | incident lifecycle + predictive maintenance |
| `notifier` | 64 | multi-channel fan-out, retry/backoff, circuit breaker, HMAC |
| `copilot` | 65 | Toolbox, offline/LLM tiers, grounding guard, MCP |
| `ops` | 73 | orchestrator, migrate (incl. OPT-IN gating), doctor, audit-security, version |
| `e2e` | 35 | full offline pipeline over real localhost HTTP, incl. 17 RBAC-enforcement tests |
| `academy` | 36 | browser (Playwright): lessons, tutor backends, checkride, auth |
| `console` | 50 | browser (Playwright): map, approvals, audit, SLA, copilot panel, auth |
| **Total** | **525** | 0 failures |

Plus static/structural checks that run on every verification pass:
`scripts/check_console.mjs` and `academy/tests/check_academy.mjs` (enforce
the "exactly one `<script>` block, must parse" invariant that keeps both
apps buildless), `academy/content/validate.py` (schema-validates the
curriculum JSON), and `deploy/aws/validate.sh` (18 offline checks against
the cloud-init script, systemd units, and nginx config).

The backbone that makes all of this possible offline is `e2e/fakerest.py`
— an in-process fake PostgREST server (including an RBAC-emulation mode
with real JWT-shaped tokens) that nearly every suite in the repo runs
against over real localhost HTTP. This means the entire platform — sim
through console through RBAC enforcement — is verified with **zero network
egress**, which is also exactly why the loopback demo can run with no
internet access at all: it's the same code path.

0007_rbac.sql is the one piece validated beyond the offline harness: it
was applied to and tested against a real local Postgres 16 instance
(Section 4).

## 10. Deployment paths

### 10.1 Local / loopback (zero config)

`yantraops up --loopback` starts an embedded in-memory backend, the
simulator, detector, notifier, and Sarathi, prints a `✔ READY in X.Xs`
line, and auto-opens the console in the default browser. No Supabase
account, no API key, no network access required. This is the fastest path
to seeing the whole platform, and it's also exactly what the automated test
suites exercise.

### 10.2 Real Supabase backend

`yantraops migrate --db-url <...>` applies the tracked, checksummed
baseline migrations (0001–0005; 0006/0007 require the explicit
`--include-opt-in` flag). `yantraops up --supabase` then runs the same
stack against the real project. `yantraops doctor` preflights the
environment and `yantraops audit-security` verifies the resulting security
posture before going further.

### 10.3 AWS EC2, console-only

Built specifically for an operator who provisions infrastructure through
the **AWS Console UI** — no AWS CLI, no Terraform/CDK, matching how the
user actually works. `deploy/aws/` ships:

- `user-data.sh` — cloud-init for a stock Ubuntu 24.04 instance: installs
  git/python3.12/venv/nginx, clones the repo, runs the installer, writes
  `/etc/yantrafleet.env` from clearly marked EDIT-ME placeholders
  (Supabase URL/key, site id, optional LLM key, Sarathi token).
- `systemd/*.service` units for the simulator (optional), detector,
  notifier, and Sarathi (bound to `127.0.0.1:8001`), each
  `Restart=always`.
- `nginx/yantrafleet.conf` — serves the console at `/`, reverse-proxies
  `/ask` and `/health` to Sarathi, sets security headers, and redirects
  `/` to the console with the Supabase params templated in at install time
  (so the operator never has to hand-construct a URL).
- `README.md` — the literal console-UI walkthrough: instance type
  (t3.small, 20 GB), security-group rules, pasting the user-data with its
  two edits marked, opening the public IP, pushing the repo to a private
  GitHub repo first, updating via `git pull` + `systemctl restart`, a cost
  note (~$15–18/month), and a hardening checklist (HTTPS via certbot,
  restricting port 80, key rotation).
- `validate.sh` — 18 offline checks (syntax, `nginx -t`, systemd unit
  sanity) that run in CI/dev without ever touching AWS.

### 10.4 Docker

`docker build -f docker/Dockerfile` / `docker compose` packages the whole
demo stack with host networking (the supported path on Linux; Docker
Desktop needs its host-networking option on macOS/Windows).

## 11. How this was actually built

Two things made the pace of this build possible, and both are worth
recording honestly rather than glossing over:

**Parallel agent development with strict ownership boundaries.** Nearly
every version bump in this repo (v0.9 through v0.12.1) was built by
dispatching multiple coding agents in parallel, each given an explicit,
disjoint set of files/directories it was allowed to touch (e.g., one agent
owned `deploy/aws/**` and a small `ops/` wiring change; another
simultaneously owned `supabase/0006_harden.sql` + `copilot/**` +
`docs/SECURITY.md`; a third owned `console/index.html` alone) plus a
shared context block describing the current repo state and the invariants
that must not break. This is what let, for example, the AWS deploy kit,
the security hardening migration, the console's enterprise views, and the
documentation site all get built in one pass rather than four sequential
ones. After every parallel batch, a single verification pass ran *all*
suites (not just the touched ones) and fixed any cross-agent regressions
before anything was committed — the discipline that kept 525 tests green
throughout rather than accumulating debt.

**A real, if compressed, version history.** The repository has 16 tagged
releases, from `v0.1.0` (2026-08-26, baseline schema + simulator) through
`v0.12.1` (2026-09-03, this release) — a genuine incremental build, not a
single generated drop:

| Tag | What it added |
|---|---|
| v0.1.0–v0.2.0 | Baseline schema, simulator, VDA 5050 translation, canonical statuses, the command-approval gate |
| v0.3.0–v0.4.0 | Telemetry history, incident detector, real-recording replay, offline e2e harness |
| v0.5.0 | Missions, predictive maintenance, notifier, MCP server |
| v0.6.0–v0.6.2 | Startup UX (log-level control, auto-open browser, wrong-URL redirect), copilot grounding guard |
| v0.7.0–v0.8.0 | Console command palette / tour / shift report, MQTT transport + real broker |
| v0.9.0 | AWS deploy kit, MQTT command loop closing on real `actionStates`, security hardening (0006), console audit/SLA views, docs site |
| v0.10.0 | YantraFleet Academy: learn-by-operating, live-verified checkride certification, WebLLM on-device tutor |
| v0.11.0–v0.11.1 | Real Supabase Auth + enforced RBAC (0007, Postgres-verified), `audit-security`, compliance mappings, no-schema detection + OPT-IN migration gating |
| v0.12.0 | Account signup, console↔academy cross-discovery, the Physical AI Foundations curriculum track |
| v0.12.1 | Version single-sourcing, WebLLM version pin, academy search/print, console empty-state handling |

## 12. Honest status — what's real, what's still ahead

**Fully implemented and test-verified today:** the entire data path
(simulated or replayed-real VDA 5050 → Supabase → every consumer), the
human-approval command gate over both transports, automatic incident
detection and predictive maintenance, multi-channel notification, the
3-tier degrading AI copilot, real Supabase Auth with enforced,
database-level RBAC, the full Academy with on-device LLM tutoring and
certification, the AWS console-UI deployment kit, and 525 automated tests
across 10 suites plus static/content validators.

**Explicitly deferred / documented as not-yet-production** (see
`docs/SECURITY.md` and `docs/COMPLIANCE.md` for the full lists rather than
a summary that could go stale): production-grade signed certificates
(today's are demo-grade Open Badges-*shaped* JSON, unsigned), LLM tiers 1–2
and WhatsApp/webhook delivery verified only with live credentials the
platform doesn't ship with, HTTPS termination on the AWS path (documented
as a certbot step the operator runs, not yet automated), and a real
physical robot integration (the connector is proven against recorded and
simulated VDA 5050 traffic; the remaining gap to a physical fleet is
purely "point it at a robot's real MQTT broker," not new code).

**Live loose end at time of writing:** the user's own Supabase project
still needs the baseline migrations applied before any of the Supabase-
backed features can be demonstrated against it — a combined single-paste
SQL file was prepared for this. Once applied, `yantraops audit-security`
against that project should read `mode: demo` with all schema checks
passing, which is the signal that it's ready for the next step (optionally
`--include-opt-in` for hardened/RBAC mode).

---

*This document is maintained alongside the codebase — when a component's
behavior changes materially, this file should change with it. It is not
auto-generated; treat drift here as a documentation bug, not an expected
gap between "what we said" and "what we built."*
