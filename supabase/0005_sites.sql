-- ============================================================
-- Yantrika v0.5.x migration — multi-site groundwork.
-- Run after 0004_maintenance.sql. Idempotent.
--
-- Every operational table gains a ``site_id`` column. The default
-- 'BLR-DC1' (the original — and so far only — deployment) means:
--   * existing rows are backfilled to BLR-DC1 in place,
--   * legacy writers that do not stamp site_id yet keep working
--     (the column default fills it in),
--   * new writers stamp it explicitly via yantracore.site_id()
--     (env YANTRA_SITE_ID, default 'BLR-DC1').
-- fleet_meta stays single-site for now (one heartbeat row, id=1);
-- it will become (site_id, id) keyed when a second site actually
-- writes.
-- ============================================================

alter table public.robots
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.alerts
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.incidents
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.missions
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.robot_telemetry
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.maintenance_findings
  add column if not exists site_id text not null default 'BLR-DC1';
alter table public.commands
  add column if not exists site_id text not null default 'BLR-DC1';

-- Composite indexes: every hot read path filters by site first, then
-- orders/windows by its natural time column.
create index if not exists robots_site_updated
  on public.robots (site_id, updated_at desc);
create index if not exists alerts_site_created
  on public.alerts (site_id, created_at desc);
create index if not exists incidents_site_created
  on public.incidents (site_id, created_at desc);
create index if not exists missions_site_created
  on public.missions (site_id, created_at desc);
create index if not exists robot_telemetry_site_ts
  on public.robot_telemetry (site_id, ts desc);
create index if not exists maintenance_findings_site_created
  on public.maintenance_findings (site_id, created_at desc);
create index if not exists commands_site_created
  on public.commands (site_id, created_at desc);

-- ------------------------------------------------------------
-- FUTURE per-site RLS sketch (not enabled yet — demo_all still
-- rules). When sites get real tenancy:
--
--   1. Issue per-site JWTs carrying a ``site_id`` claim
--      (auth.jwt() ->> 'site_id').
--   2. Replace demo_all with per-table policies of the shape:
--
--      -- drop policy if exists demo_all on public.robots;
--      -- create policy site_read on public.robots
--      --   for select to authenticated
--      --   using (site_id = (auth.jwt() ->> 'site_id'));
--      -- create policy site_write on public.robots
--      --   for insert to authenticated
--      --   with check (site_id = (auth.jwt() ->> 'site_id'));
--      -- create policy site_update on public.robots
--      --   for update to authenticated
--      --   using (site_id = (auth.jwt() ->> 'site_id'))
--      --   with check (site_id = (auth.jwt() ->> 'site_id'));
--
--   3. A fleet-wide operator role keeps cross-site reads via a
--      dedicated claim (e.g. site_id = '*' bypass in the USING
--      clause) — the notifier's --all-sites mode maps onto it.
--   4. Anon (console demo) access then narrows to a single
--      published site or goes away entirely.
-- ------------------------------------------------------------

select 'v0.5.x ready: site_id on robots/alerts/incidents/missions/robot_telemetry/maintenance_findings/commands' as result;
