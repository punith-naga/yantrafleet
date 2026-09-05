-- ============================================================
-- YantraFleet 0017 — DEMO SANDBOX: ROBOT-SCOPED WRITES (SECURITY FIX).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN ONLY IF YOU APPLIED 0009_demo_sandbox.sql (OPT-IN) !!
-- !!  REQUIRES 0007_rbac.sql + 0009 — it REPLACES 0009's     !!
-- !!  `demo_sandbox_insert` / `demo_sandbox_update` policies !!
-- !!  on commands/robot_telemetry/maintenance_findings. On a !!
-- !!  project that never ran 0009 it SKIPS ITSELF with a     !!
-- !!  NOTICE — there are no anon demo policies to tighten.   !!
-- !!  SECURITY FIX — apply before exposing the sandbox.      !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- ------------------------------------------------------------
-- WHAT WAS WRONG
-- ------------------------------------------------------------
-- 0009 pinned every anon demo write to one row value: `site_id =
-- yf_demo_site()`. That is exactly right for the ROW's home, and it is
-- the whole of what those policies check. It says nothing about the
-- *other* columns of the row — and four of the seven demo-writable
-- tables carry a foreign-key-shaped `text` column that names a robot
-- and has no foreign key behind it (0002/0003/0004 predate 0005's
-- site_id, so nothing ever tied `robot_id` to a site):
--
--     commands.robot_id             text not null   -- no FK   [fixed here]
--     robot_telemetry.robot_id      text not null   -- no FK   [fixed here]
--     maintenance_findings.robot_id text not null   -- no FK   [fixed here]
--     missions.robots               jsonb array     -- no FK   [see §2]
--
-- So an anonymous sandbox visitor could POST
--
--     {"robot_id": "AMR-01", "cmd": "estop", "status": "approved",
--      "site_id": "DEMO-…"}
--
-- naming a REAL robot in a REAL site, with the human approval gate
-- already satisfied at INSERT time. 0009's column-level grants narrow
-- only UPDATE (`grant update (status, decided_by, …)`); INSERT is
-- table-wide, so `status`/`decided_by`/`decided_at`/`executed_at` were
-- all attacker-chosen on the way in.
--
-- The row stays inside the demo site, so no real row is written,
-- deleted or read. The blast radius is entirely DOWNSTREAM, in the
-- executors that hold the service key and therefore bypass RLS:
--   * `yantrasim.transports.supabase.poll_commands` polled
--     `commands?status=eq.approved` with NO site filter at all;
--   * `yantrabridge.CommandPublisher.poll` added one only when
--     `--site` was passed.
-- Either would have published a VDA 5050 `estop` instantAction to a
-- serial derived from attacker-supplied text. Both are fixed in the
-- same change; this migration is the half that does not depend on
-- anyone upgrading their executor.
--
-- ------------------------------------------------------------
-- WHAT THIS MIGRATION DOES
-- ------------------------------------------------------------
-- Adds, to the anon demo INSERT policies only:
--   1. `commands`: the row must name a robot that lives in the SAME
--      demo site, must be `pending`, and must carry no decision or
--      execution stamps. A sandbox can still demo the approval loop —
--      it PATCHes pending -> approved afterwards, which is what a human
--      does and what the column grants already allow.
--   2. `robot_telemetry`, `maintenance_findings`: same-site robot.
-- and the same same-site-robot predicate to `demo_sandbox_update` on
-- `commands`, so a forged row that predates this migration cannot be
-- walked forward either.
--
-- WHY `exists (select 1 from public.robots …)` IS SAFE HERE
--   * The subquery runs as the CALLER (anon), so 0009's
--     `demo_sandbox_read` policy on `robots` applies to it as well: a
--     demo token can only *see* robots in its own sandbox. The
--     explicit `r.site_id = <table>.site_id` term is therefore belt
--     and braces, not the only control — but it is kept so the policy
--     stays correct if a future migration ever widens anon's read.
--   * EVERY reference is table-qualified. `robots` has a `site_id`
--     column of its own, so an unqualified `site_id` inside the
--     subquery would bind to `r.site_id` and the predicate would
--     collapse to `r.site_id = r.site_id` — always true, silently
--     reopening the hole. Do not "simplify" the qualifiers away.
--   * `robots.id` is the primary key, so the probe is an index lookup.
--
-- WHAT THIS DELIBERATELY DOES NOT DO (see docs/SECURITY.md)
--   * `alerts.src` / `incidents.src` / `*.tlabel` stay free text. They
--     are display labels, not references — `src` is legitimately
--     'fleet' as well as a robot id, and 0009's own seed writes it that
--     way. No executor routes on them.
--   * `robots` / `alerts` / `incidents` INSERT is unchanged: their
--     `id`s are global text primary keys, and constraining them to a
--     `<site>-` prefix would break writers (yantrasim's alert ids are
--     content hashes, not site-prefixed). A demo visitor colliding with
--     a real primary key already gets nothing: `on conflict do update`
--     is refused by 0009's `demo_sandbox_update` USING clause, `do
--     nothing` silently skips, and a bare insert raises 23505. The
--     residual is a duplicate-key *oracle* (you can learn that some id
--     exists somewhere), documented rather than papered over.
--   * `commands` inserts by AUTHENTICATED users (0007's
--     `rbac_commands_insert`) are unchanged. The same unconstrained
--     `robot_id` exists there, but requiring the robot to already be in
--     `robots` would reject a legitimate command for a robot that has
--     not reported in yet. That path is closed in the executors
--     instead (both pollers now verify the site of every row they act
--     on). Noted in docs/SECURITY.md.
--
-- Run after 0016_public_status.sql. Idempotent. ROLLBACK at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) + 2) The policies, behind a 0009 guard
-- ------------------------------------------------------------
-- `yantraops migrate --include-opt-in` applies EVERY opt-in file, and a
-- deployment that hardened with 0007 but deliberately never enabled the
-- demo sandbox has no `yf_demo_site()` to reference. Creating these
-- policies there would fail the whole migrate run (verified: `ERROR:
-- function public.yf_is_demo_site(text) does not exist`), so the file
-- skips itself with a NOTICE instead. There is nothing to tighten on a
-- project with no anon demo policies.
do $mig$
begin
  if to_regprocedure('public.yf_demo_site()') is null then
    raise notice '0017: 0009_demo_sandbox.sql is not applied on this '
                 'database — there are no anon demo policies to tighten. '
                 'Skipping (this is not an error).';
    return;
  end if;

  -- 1) commands — the reported hole
  execute $ddl$ drop policy if exists demo_sandbox_insert on public.commands $ddl$;
  execute $ddl$
    create policy demo_sandbox_insert on public.commands
      for insert to anon
      with check (
        public.yf_is_demo_site(commands.site_id)
        and commands.site_id = public.yf_demo_site()
        -- the human approval gate cannot be pre-satisfied on the way in
        and commands.status = 'pending'
        and commands.decided_by is null
        and commands.decided_at is null
        and commands.executed_at is null
        -- ...and the command must target a robot of THIS sandbox
        and exists (select 1
                      from public.robots r
                     where r.id = commands.robot_id
                       and r.site_id = commands.site_id)
      ) $ddl$;

  -- The update path already could not change `robot_id` (0009 grants
  -- UPDATE on status/decided_by/decided_at/note/executed_at only), but a
  -- row forged before this migration must not be walkable to 'approved'
  -- either, and a future grant change must not silently reopen the hole.
  execute $ddl$ drop policy if exists demo_sandbox_update on public.commands $ddl$;
  execute $ddl$
    create policy demo_sandbox_update on public.commands
      for update to anon
      using (public.yf_is_demo_site(commands.site_id)
             and commands.site_id = public.yf_demo_site()
             and exists (select 1
                           from public.robots r
                          where r.id = commands.robot_id
                            and r.site_id = commands.site_id))
      with check (public.yf_is_demo_site(commands.site_id)
                  and commands.site_id = public.yf_demo_site()
                  and exists (select 1
                                from public.robots r
                               where r.id = commands.robot_id
                                 and r.site_id = commands.site_id)) $ddl$;

  -- 2) The same class of gap on the other robot-naming tables.
  -- robot_telemetry: forged samples for a real robot are what a
  -- site-blind detector would ingest (detector/yantradetect's
  -- fetch_telemetry has no site filter today — see docs/SECURITY.md).
  execute $ddl$ drop policy if exists demo_sandbox_insert on public.robot_telemetry $ddl$;
  execute $ddl$
    create policy demo_sandbox_insert on public.robot_telemetry
      for insert to anon
      with check (
        public.yf_is_demo_site(robot_telemetry.site_id)
        and robot_telemetry.site_id = public.yf_demo_site()
        and exists (select 1
                      from public.robots r
                     where r.id = robot_telemetry.robot_id
                       and r.site_id = robot_telemetry.site_id)
      ) $ddl$;

  -- maintenance_findings: a forged OPEN finding for a real robot is
  -- worse than noise — the detector seeds its dedup set from open
  -- findings, so one could SUPPRESS a genuine finding for that robot.
  execute $ddl$ drop policy if exists demo_sandbox_insert on public.maintenance_findings $ddl$;
  execute $ddl$
    create policy demo_sandbox_insert on public.maintenance_findings
      for insert to anon
      with check (
        public.yf_is_demo_site(maintenance_findings.site_id)
        and maintenance_findings.site_id = public.yf_demo_site()
        and exists (select 1
                      from public.robots r
                     where r.id = maintenance_findings.robot_id
                       and r.site_id = maintenance_findings.site_id)
      ) $ddl$;
end $mig$;

-- missions.robots (a jsonb array of robot ids) is the fourth member of
-- this family and is DELIBERATELY LEFT ALONE. The equivalent policy is
-- written out in the commented block below; do not enable it before
-- fixing the writer, because YantraFleet's own demo sandbox violates it:
--
--   ops/yantraops/sandbox.py builds its FleetSim first and renames the
--   robots afterwards (`robot.robot_id = self.robot_id(i)`), but
--   FleetSim.__init__ has already spawned missions whose crew list was
--   captured from the ORIGINAL ids. So a sandbox's `missions.robots`
--   genuinely reads ["AMR-01", "AMR-02", ...] — real fleet robot ids —
--   today. Turning the policy on breaks every sandbox tick.
--
-- The exposure is low: nothing dispatches from `missions.robots` (VDA
-- orders are published from an explicit caller-supplied robot id, see
-- yantrabridge.CommandPublisher.publish_order), 0012's throughput
-- rollups count missions by `site_id`, and the console renders the list
-- as a label. It is display noise, in the same category as `alerts.src`
-- — but it is noise that proves the label is untrusted. See
-- docs/SECURITY.md and the NEEDS note in the change report.
--
-- After the writer is fixed (rename the robots BEFORE spawning
-- missions, or re-key the crew list at snapshot time), enable:
--
-- drop policy if exists demo_sandbox_insert on public.missions;
-- create policy demo_sandbox_insert on public.missions
--   for insert to anon
--   with check (
--     public.yf_is_demo_site(missions.site_id)
--     and missions.site_id = public.yf_demo_site()
--     -- jsonb_typeof guards the cast, so `{"robots": {"a": 1}}` is a
--     -- clean policy refusal rather than a 22023 cast error
--     and jsonb_typeof(missions.robots) = 'array'
--     and not exists (
--           select 1
--             from jsonb_array_elements_text(missions.robots) as e(robot)
--            where not exists (select 1
--                                from public.robots r
--                               where r.id = e.robot
--                                 and r.site_id = missions.site_id))
--   );


-- ------------------------------------------------------------
-- 3) OPTIONAL HARDENING (commented — changes a documented grant)
-- ------------------------------------------------------------
-- MINOR FINDING, reported in docs/SECURITY.md: 0009 grants
-- `demo_reap_expired(int)` to `authenticated` with no role check, so
-- ANY signed-in user can purge every already-expired sandbox. The
-- damage is bounded (only sandboxes whose tokens already stopped
-- working are touched, and `_yf_demo_purge_sites` refuses non-DEMO
-- sites), so this is a nuisance-grade DoS on other visitors' expired
-- demos, not a data-loss path — which is why it is left as-is by
-- default: docs/FEATURE-CONTRACTS.md publishes that grant, and
-- `yantraops sandbox reap` uses it.
--
-- If your deployment does not need a non-admin reaper, run:
--
-- revoke execute on function public.demo_reap_expired(int) from authenticated;
-- -- `demo_mint_session`'s opportunistic `perform demo_reap_expired()`
-- -- keeps working: it is SECURITY DEFINER, so the EXECUTE privilege is
-- -- checked against its owner, not the anon caller.


select 'v0.17 DEMO SCOPE FIX: anon demo inserts on commands/robot_telemetry/maintenance_findings must name a robot in their OWN sandbox site; demo commands must be inserted pending with no decision/execution stamps (missions.robots deliberately unchanged - see the note above)' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — restore 0009's policies verbatim.
-- This re-opens the hole described above; the only reason to run it is
-- to get back to a known 0009 state before re-applying a corrected
-- 0017. Nothing else in 0009-0016 is touched by this migration.
-- ------------------------------------------------------------
-- do $$
-- declare t text;
-- begin
--   foreach t in array array[
--     'commands', 'robot_telemetry', 'maintenance_findings'
--   ] loop
--     execute format('drop policy if exists demo_sandbox_insert on public.%I', t);
--     execute format(
--       'create policy demo_sandbox_insert on public.%I
--          for insert to anon
--          with check (public.yf_is_demo_site(site_id)
--                      and site_id = public.yf_demo_site())', t);
--   end loop;
-- end $$;
--
-- drop policy if exists demo_sandbox_update on public.commands;
-- create policy demo_sandbox_update on public.commands
--   for update to anon
--   using (public.yf_is_demo_site(site_id)
--          and site_id = public.yf_demo_site())
--   with check (public.yf_is_demo_site(site_id)
--               and site_id = public.yf_demo_site());
--
-- select 'ROLLED BACK 0017: 0009 demo insert/update policies restored' as result;
-- ============================================================
