# Changelog

## v0.2.0 — 2026-08-26
- **Canonical status vocabulary** (`core/yantracore`): active | idle | charging |
  paused | estop | degraded | fault — enforced by a DB CHECK constraint
  (`supabase/0002_commands.sql`); sim and connector both emit it, the console
  displays estop/degraded distinctly.
- **Human-in-the-loop command gate**: console queues pause/resume/charge/estop
  as `pending` rows in the new `commands` table; an operator approves/rejects
  in the Overview "Pending approvals" card; the simulator polls approved
  commands, executes them in the world (robots actually pause/hold/route to
  charger/e-stop), and marks them executed/failed with a reason. Full audit
  trail preserved.
- **Writer precedence**: a live `yantrasim` feed always outranks console tabs
  for fleet_meta leadership; console tabs fall back to viewer mode.
- Project hygiene: git repo, MIT license, GitHub Actions CI (4 python suites +
  console syntax check), CONTRIBUTING, this changelog.

## v0.1.0 — 2026-08-26
- Initial four components built via multi-agent workflow: yantrasim (VDA 5050
  v2.1 simulator), yantrabridge (VDA→Supabase connector), sarathi (3-tier
  copilot service), console (single-file ops console). 102 offline tests.
