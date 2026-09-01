-- ============================================================
-- YantraFleet v0.4 migration — predictive maintenance findings.
-- Run after 0003_telemetry.sql. Idempotent.
-- Written by detector/yantradetect (python -m yantradetect --maintenance):
-- deterministic text ids (MF-XXXX) make POST ... on_conflict=id retries
-- idempotent; findings are Cleared (never deleted) when the trend abates.
-- ============================================================

create table if not exists public.maintenance_findings (
  id text primary key,                 -- deterministic MF-XXXX from robot:component:created_at
  robot_id text not null,
  component text not null check (component in ('drive motor', 'battery', 'drivetrain')),
  finding text not null,               -- human-readable trend description
  rul_days numeric,                    -- heuristic remaining-useful-life estimate
  confidence numeric check (confidence >= 0 and confidence <= 1),
  action text,                         -- recommended maintenance action
  state text not null default 'Open' check (state in ('Open', 'Cleared')),
  created_at timestamptz not null default now(),
  cleared_at timestamptz
);

-- Detector dedup seed (state=eq.Open) and console per-robot views.
create index if not exists maintenance_findings_robot_component
  on public.maintenance_findings (robot_id, component, state);
create index if not exists maintenance_findings_created
  on public.maintenance_findings (created_at desc);

alter table public.maintenance_findings enable row level security;

-- Demo posture (matches the rest of the schema): anon can read/write.
drop policy if exists demo_all on public.maintenance_findings;
create policy demo_all on public.maintenance_findings
  for all to anon, authenticated using (true) with check (true);

-- ------------------------------------------------------------
-- HARDENED policy block — the promised lockdown path.
-- To lock down: run the drop below, then the create statements,
-- and switch the detector to an authenticated (service/JWT) key.
-- Anon keeps read-only access for the console; only authenticated
-- roles may insert/update findings. Deletes stay disallowed for all.
-- ------------------------------------------------------------
-- drop policy if exists demo_all on public.maintenance_findings;
--
-- drop policy if exists maint_read_all on public.maintenance_findings;
-- create policy maint_read_all on public.maintenance_findings
--   for select to anon, authenticated using (true);
--
-- drop policy if exists maint_write_auth on public.maintenance_findings;
-- create policy maint_write_auth on public.maintenance_findings
--   for insert to authenticated with check (true);
--
-- drop policy if exists maint_update_auth on public.maintenance_findings;
-- create policy maint_update_auth on public.maintenance_findings
--   for update to authenticated using (true) with check (true);

select 'v0.4 ready: maintenance_findings' as result;
