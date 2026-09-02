# YantraFleet compliance mapping

> **Linking note:** this page belongs next to
> [`docs/SECURITY.md`](SECURITY.md) and should be linked from its intro
> ("see docs/COMPLIANCE.md for the framework mapping"). That edit is not
> made here because SECURITY.md is being revised in a parallel change —
> add the link there when it lands.

**Scope, stated plainly.** YantraFleet is fleet *monitoring and
operations* tooling: it observes robots over VDA 5050, stores state in
Postgres/Supabase, raises alarms, and routes human-approved commands
back. It is **not** a safety controller, not a robot's safety system,
and not itself a certified product. Nothing below claims a
certification. Each section maps concrete platform features (with file
references) to the control they *support*, and the final checklist
lists what is missing before you can honestly claim more.

Every backend claim in this page is verifiable against a live
deployment with:

```
python -m yantraops audit-security          # RLS mode, key class, secrets, schema
```

---

## SOC 2 (Trust Services Criteria)

**SOC 2 is an audit of your organization, not of this software.** An
auditor examines *your* policies, processes and evidence over time. The
platform can only supply supporting technical controls; the table maps
which ones exist and where.

| TSC | Platform feature | Where |
|---|---|---|
| CC6.1 / CC6.3 — logical access, least privilege | RBAC mode: `operator < engineer < manager < admin` per site, enforced by Postgres RLS + `SECURITY DEFINER` RPCs; anon key gets nothing; a signed-in user with no role sees nothing (fail-closed) | `supabase/0007_rbac.sql`, `docs/SECURITY.md` |
| CC6.1 — credential classes | Publishable vs service_role key separation; writers server-side only; `audit-security` FAILs if a service key is configured client-side | `docs/SECURITY.md`, `ops/yantraops/audit.py` |
| CC6.6 — endpoint auth | Copilot `/ask` bearer auth (`SARATHI_TOKEN`, constant-time compare); webhook HMAC-SHA256 signing (`YANTRA_WEBHOOK_SECRET`, `X-Yantra-Signature`) | `copilot/sarathi/app.py`, `notifier/yantranotify/channels.py` |
| CC6.7 — encryption in transit | Supabase REST/Auth is HTTPS; `audit-security` WARNs on plain-http non-localhost URLs. **Gap:** MQTT is plaintext by default — put TLS on the broker for production (see checklist) | `ops/yantraops/audit.py` |
| CC7.2 — monitoring & audit trail | Full command audit trail: every operator command is a `commands` row with `requested_by`, `decided_by`, decision timestamps and terminal `executed`/`failed` status; RBAC prevents spoofing `requested_by` and stamps `decided_by = auth.email()` server-side; console Audit view renders the history | `supabase/0002_commands.sql`, `supabase/0007_rbac.sql`, `console/index.html` |
| CC7.2 — anomaly detection | Detector turns telemetry into incidents; alerts stream with dedup and severity | `detector/`, `connector/yantrabridge/translate.py` |
| CC8.1 — change management | All schema changes are versioned, checksummed SQL migrations applied by `yantraops migrate` (tracked apply-once, `--force` required to reapply changed files); code changes flow through git + CI (test suites per component) | `ops/yantraops/migrate.py`, `.github/workflows/` |
| A1.2 — availability monitoring | `yantraops status` / `doctor`; notifier delivery retries (3 attempts, exponential backoff) with a per-channel circuit breaker | `ops/yantraops/status.py`, `notifier/yantranotify/reliability.py` |

What the platform does **not** give you for SOC 2: HR/vendor policies,
risk assessments, access reviews, evidence collection, log retention
guarantees. Those are organizational.

## ISO/IEC 27001:2022 — Annex A touchpoints

Same caveat: ISO 27001 certifies an ISMS (your organization), not this
codebase. Touchpoints the platform supports:

| Annex A control | Platform feature |
|---|---|
| A.5.15 / A.5.18 Access control & rights | RBAC roles per user per site (`user_roles`), admin-managed |
| A.8.2 Privileged access | service_role confined to server-side writers; first admin seeded explicitly |
| A.8.5 Secure authentication | Supabase Auth (password, optional Google OAuth) issuing short-lived JWTs with refresh |
| A.8.15 Logging | Command audit trail; alert/incident history; `robot_telemetry` time series |
| A.8.16 Monitoring activities | Detector + notifier + console alarm KPIs |
| A.8.24 Use of cryptography | HTTPS transport; HMAC-signed webhooks; hashed alert dedup ids |
| A.8.32 Change management | Migration runner with checksums; git + CI |
| A.5.14 Information transfer | Signed webhook payloads; HTTPS-only guidance enforced by `audit-security` WARN |

Not covered by the platform: A.5 policy suite, A.6 people controls,
A.7 physical controls, supplier management, BC/DR (A.5.29/5.30).

## EU AI Act

Position: YantraFleet is **operations/monitoring tooling with human
oversight built in** — it surfaces state and recommendations; it does
not autonomously control robots.

- **Article 14 (human oversight) alignment by design:** robots are
  never commanded automatically. A command is a `pending` row requested
  by a named person, approved or rejected by a manager+ via
  `decide_command()` (RBAC mode), and only then published by the
  connector as a VDA instantAction — with the full who/when trail
  retained. The copilot answers questions; it does not act.
- **The platform is not the robot's safety system.** E-stop, field
  violation and safety-rated functions live in the robot/AGV per
  ISO 3691-4 / vendor certification; YantraFleet only *reports*
  `safetyState` (eStop, fieldViolation) from VDA 5050 messages. Do not
  wire an emergency stop path through this stack.
- Risk-class determination for a concrete deployment (e.g. whether
  your use makes any AI component a regulated system) is the
  deployer's obligation — the copilot's LLM answering is assistive
  tooling; it makes no automated decisions about people or machines.

## VDA 5050 conformance statement

Implemented: **VDA 5050 v2.1, `state` and `instantActions` topics
only**, over MQTT topics `uagv/v2/<manufacturer>/<serialNumber>/state`
and `.../instantActions` (`connector/yantrabridge/`).

`state` fields consumed (`translate.py`, `commands.handle_state`):

- `serialNumber` (required — row key), `manufacturer`, `timestamp`
- `errors[]`: `errorType`, `errorLevel` (v2.1 `WARNING`/`FATAL`; v3.0
  `CRITICAL`/`URGENT` tolerated), `errorDescription`, `errorHint`
- `safetyState`: `eStop`, `fieldViolation`
- `batteryState`: `batteryCharge`, `charging`
- `agvPosition`: `x`, `y`
- `velocity`: `vx`, `vy`
- `driving`, `paused`, `orderId`
- `actionStates[]`: `actionId`, `actionStatus`, `actionType`,
  `resultDescription`
- Vendor extensions (non-VDA, passed through when present):
  `motorTemp`/`motorTemperature`, `tasksDone`/`tasksCompleted`

`instantActions` messages published (`commands.py`): `headerId`
(per-topic counter), `timestamp`, `version` (`"2.1.0"`),
`manufacturer`, `serialNumber`, and one action per operator command —
`actionType` `yantra.pause|resume|charge|estop`, `actionId` = the
command row id, `blockingType: "HARD"`, empty `actionParameters`.
Command closure comes from the robot's next `state` message
(`actionStates[]` `FINISHED`→`executed`, `FAILED`→`failed`).

**Not implemented** (do not claim full VDA 5050 conformance): `order`,
`factsheet`, `visualization` and `connection` topics; order graphs,
node/edge traversal, zone sets. The `yantra.*` actionTypes are a
vendor-specific action set, which VDA 5050 permits but a target robot
must be adapted to understand. instantActions are published QoS 0 —
delivery is confirmed only by the actionState echo.

## ISA-18.2 / EEMUA 191 — alarm management already implemented

- **Alarm rate budget:** the console tracks alarms/hour against the
  ISA-18.2-derived operator budget of ≤ 12/hr and shows it on the
  header chip and stat tiles, coloring over-budget states
  (`console/index.html`).
- **Acknowledgement workflow:** every alert carries `ack`; operators
  ack from the console; in RBAC mode acks are the *only* client write
  on alerts — the UPDATE privilege is revoked table-wide and re-granted
  on the `ack` column alone (`supabase/0007_rbac.sql`).
- **Chattering/fleeting alarm suppression:** the connector's
  `AlertDeduper` emits one alert per condition transition
  (inactive→active), with battery-threshold hysteresis (+5 %) so
  values hovering at the threshold do not re-alarm
  (`connector/yantrabridge/translate.py`).
- **Flood handling:** the console flags > 5 unacked alerts as a flood
  condition; the shift report includes alarm rate vs budget.
- **Notification reliability:** delivery retries with backoff and a
  per-channel circuit breaker (5 failures → 5 min pause) prevent a
  dead channel from silently dropping alarms
  (`notifier/yantranotify/reliability.py`).

Not implemented from ISA-18.2's full lifecycle: master alarm database,
rationalization records per alarm, shelving with audit, out-of-service
management, periodic performance reporting.

## NIST CSF 2.0 quick map

| Function | Platform support |
|---|---|
| Identify | `yantraops doctor` (environment inventory), migration state tracking |
| Protect | RLS modes (demo → hardened → RBAC), key-class separation, bearer/HMAC auth, HTTPS |
| Detect | Incident detector, alarm stream + budget, `yantraops audit-security` posture probe |
| Respond | Ack workflow, human-approved commands, notifier escalation channels |
| Recover | Idempotent migrations with rollback blocks in 0006/0007; disposable compute (data lives in Supabase; the EC2 box is rebuildable from `user-data.sh`) |

## Gaps to close before claiming X

Before telling a customer or auditor "we are SOC 2 / ISO 27001 /
production-hardened", close these — none are provided by the platform
today:

- [ ] **Penetration test** of a deployed instance (console, PostgREST
      surface, copilot API, MQTT broker) by an external party.
- [ ] **SSO / SAML / SCIM** — Supabase Auth here supports password +
      Google OAuth; enterprise IdP federation and automated
      de-provisioning are not wired up.
- [ ] **Log retention policy** — Postgres keeps rows until you delete
      them; define and enforce retention/archival for `commands`,
      `alerts`, `robot_telemetry` (and Supabase platform logs), and
      document it.
- [ ] **DPA** with Supabase (and your LLM provider if the copilot uses
      one) covering data residency and subprocessors; confirm project
      region.
- [ ] **MQTT TLS + broker authentication** — the VDA wire defaults to
      plaintext, unauthenticated MQTT; production needs TLS and
      per-client credentials/ACLs on the broker.
- [ ] **Secrets management** — `/etc/yantrafleet.env` (chmod 600) is
      the floor; prefer a secrets manager and rotate the service key on
      a schedule.
- [ ] **Backup / disaster recovery drill** — Supabase PITR/backups
      enabled, restore actually tested, RTO/RPO written down.
- [ ] **Vulnerability management** — dependency scanning + patch
      cadence for the Python services and the EC2 AMI.
- [ ] **Incident response runbook** — who is paged on breach or fleet
      outage, and how evidence is preserved (the alarm pipeline pages
      on robot incidents, not on security incidents).
- [ ] Apply `0006`/`0007` and re-run `python -m yantraops
      audit-security` until it exits 0 — a demo-open backend
      (`anon-write` FAIL) invalidates most of the access-control claims
      above.
