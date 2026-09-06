-- ============================================================
-- Yantrika 0006 — PRODUCTION HARDENING (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN ONLY WHEN MOVING BEYOND DEMO.                     !!
-- !!  This drops the demo-open `demo_all` policies. After   !!
-- !!  it runs:                                              !!
-- !!    * WRITERS (sim, detector, connector, ops) MUST      !!
-- !!      switch SUPABASE_KEY to the service_role key.      !!
-- !!    * The console then requires Supabase Auth           !!
-- !!      (see docs/SECURITY.md — including an Option B     !!
-- !!      read-only anon alternative if you are not ready   !!
-- !!      for console auth yet).                            !!
-- !!  Do NOT run this against the shared demo project.      !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Run after 0005_sites.sql. Idempotent — safe to re-run.
--
-- What it does, per table (robots, alerts, incidents, missions,
-- fleet_meta, robot_telemetry, maintenance_findings, commands):
--
--   1. drop policy if exists demo_all      -- the for-all/anon/authenticated
--                                          -- using(true) demo policy
--   2. create a READ-ONLY policy for the `authenticated` role
--      (select using (true)).
--
-- No write policy is created — and none is needed: writers use the
-- service_role key, and service_role BYPASSES RLS entirely
-- (Supabase's service_role has `bypassrls`). Deliberately NO
-- policies are created for `anon`: after this migration the anon
-- (publishable) key can neither read nor write.
-- ============================================================

do $$
declare t text;
begin
  foreach t in array array[
    'robots', 'alerts', 'incidents', 'missions', 'fleet_meta',
    'robot_telemetry', 'maintenance_findings', 'commands'
  ] loop
    -- Belt and braces: RLS is already enabled by 0001-0004, but a
    -- hardening migration should not depend on that.
    execute format('alter table public.%I enable row level security', t);

    -- 1. Remove the demo-open policy.
    execute format('drop policy if exists demo_all on public.%I', t);

    -- 2. Authenticated users read everything; nobody writes via RLS.
    --    (Writers use service_role, which bypasses RLS — no write
    --    policy required or wanted.)
    execute format('drop policy if exists hardened_read_authenticated on public.%I', t);
    execute format(
      'create policy hardened_read_authenticated on public.%I
         for select to authenticated using (true)', t);
  end loop;
end $$;

select 'v0.6 HARDENED: demo_all dropped; authenticated=read-only; writes via service_role (bypasses RLS); anon has no access' as result;

-- ============================================================
-- ROLLBACK (back to demo-open mode)
-- ------------------------------------------------------------
-- Uncomment and run this whole block to restore the demo_all
-- policies exactly as 0001-0004 created them. Idempotent.
--
-- do $$
-- declare t text;
-- begin
--   foreach t in array array[
--     'robots', 'alerts', 'incidents', 'missions', 'fleet_meta',
--     'robot_telemetry', 'maintenance_findings', 'commands'
--   ] loop
--     execute format('drop policy if exists hardened_read_authenticated on public.%I', t);
--     execute format('drop policy if exists demo_all on public.%I', t);
--     execute format(
--       'create policy demo_all on public.%I
--          for all to anon, authenticated using (true) with check (true)', t);
--   end loop;
-- end $$;
--
-- select 'ROLLED BACK to demo-open: demo_all restored on all tables' as result;
-- ============================================================
