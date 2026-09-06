-- ============================================================
-- Yantrika 0007 — ROLE-BASED ACCESS CONTROL (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN ONLY WHEN MOVING BEYOND DEMO.                     !!
-- !!  Like 0006, this migration REPLACES the demo-open      !!
-- !!  `demo_all` policies (and the 0006 policies, if you    !!
-- !!  applied them) with role-based ones. After it runs:    !!
-- !!    * WRITERS (sim, detector, connector, ops) MUST      !!
-- !!      use the service_role key — no anon writes.        !!
-- !!    * The console requires a Supabase Auth session AND  !!
-- !!      a row in public.user_roles (see the commented     !!
-- !!      seeding helper at the bottom) — a signed-in user  !!
-- !!      with no role sees NOTHING.                        !!
-- !!    * anon (publishable) key: no access at all.         !!
-- !!  Do NOT run this against the shared demo project.      !!
-- !!  NOTE: `yantraops migrate` applies every *.sql file it !!
-- !!  finds — including this one. See supabase/README.md    !!
-- !!  ("Migration order + modes") before running migrate    !!
-- !!  against a project you want to keep demo-open.         !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Run after 0005_sites.sql (0006 is optional — 0007 supersedes its
-- policies and drops them if present). Idempotent — safe to re-run.
-- Requires a Supabase project: references auth.users / auth.uid() /
-- auth.email(), so it will NOT apply to a vanilla Postgres database.
--
-- Role hierarchy (each role includes everything below it):
--   operator < engineer < manager < admin
--   operator : view fleet data, ack alerts, request commands (pending)
--   engineer : + connector / telemetry-export workflows (app-enforced)
--   manager  : + approve/reject commands via decide_command()
--   admin    : + manage user_roles; reads span ALL sites (an admin
--              role at any site is treated as global)
--
-- A full ROLLBACK block (restore demo_all) is at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) New tables: user_roles, academy_progress, certificates
-- ------------------------------------------------------------

create table if not exists public.user_roles (
  user_id    uuid not null references auth.users (id) on delete cascade,
  role       text not null check (role in ('operator','engineer','manager','admin')),
  site_id    text not null default 'BLR-DC1',
  granted_at timestamptz not null default now(),
  primary key (user_id, site_id)          -- one role per user per site
);

-- Server-side academy progress (upgrade path from the academy's
-- localStorage `yfa_progress_v1`; written only via save_progress()).
create table if not exists public.academy_progress (
  user_id    uuid not null references auth.users (id) on delete cascade,
  pack_id    text not null,               -- e.g. 'physical-ai-101'
  data       jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key (user_id, pack_id)
);

-- Issued checkride certificates (written only via issue_certificate()).
create table if not exists public.certificates (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users (id) on delete cascade,
  track             text not null,
  score             numeric not null check (score >= 0 and score <= 100),
  verification_code text not null,
  issued_at         timestamptz not null default now()
);

-- Verification codes must be unique to be lookup-able.
create unique index if not exists certificates_verification_code_key
  on public.certificates (verification_code);
create index if not exists certificates_user_issued
  on public.certificates (user_id, issued_at desc);

alter table public.user_roles       enable row level security;
alter table public.academy_progress enable row level security;
alter table public.certificates     enable row level security;

-- Academy tables are written ONLY through the SECURITY DEFINER RPCs
-- below (which run as the table owner and bypass both RLS and these
-- grants), so strip direct write privileges as defense in depth.
revoke insert, update, delete on public.academy_progress from anon, authenticated;
revoke insert, update, delete on public.certificates     from anon, authenticated;
revoke all on public.user_roles       from anon;
revoke all on public.academy_progress from anon;
revoke all on public.certificates     from anon;
grant select on public.academy_progress to authenticated;
grant select on public.certificates     to authenticated;
grant select, insert, update, delete on public.user_roles to authenticated;  -- RLS gates to admins


-- ------------------------------------------------------------
-- 2) Helper functions
-- ------------------------------------------------------------

-- Numeric rank for the hierarchy. NULL for unknown/absent roles, so
-- comparisons against a bad input fail CLOSED (null -> not true).
create or replace function public.yf_rank(p_role text)
returns int
language sql immutable
as $$
  select case p_role
    when 'operator' then 1
    when 'engineer' then 2
    when 'manager'  then 3
    when 'admin'    then 4
    else null
  end
$$;

-- The calling user's role at a site. NULL when there is no JWT (anon)
-- or no user_roles row. SECURITY DEFINER so it can read user_roles
-- regardless of the caller's RLS view of that table; STABLE so the
-- planner caches it per statement.
create or replace function public.yf_role(p_site text default 'BLR-DC1')
returns text
language sql stable security definer
set search_path = ''
as $$
  select ur.role
    from public.user_roles ur
   where ur.user_id = auth.uid()
     and ur.site_id = p_site
$$;

-- True when the calling user's role at p_site is at least p_min_role
-- (operator < engineer < manager < admin), OR the user holds 'admin'
-- at ANY site (admin is global by design — the role matrix's
-- "admin sees all sites"). Fails closed: anon, no role row, or an
-- unknown p_min_role all yield false.
create or replace function public.yf_has_role(p_min_role text, p_site text default 'BLR-DC1')
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select coalesce(
           (select public.yf_rank(ur.role) >= public.yf_rank(p_min_role)
              from public.user_roles ur
             where ur.user_id = auth.uid()
               and ur.site_id = p_site),
           false)
      or exists (select 1
                   from public.user_roles ur
                  where ur.user_id = auth.uid()
                    and ur.role = 'admin')
$$;

revoke execute on function public.yf_role(text)            from public, anon;
revoke execute on function public.yf_has_role(text, text)  from public, anon;
grant  execute on function public.yf_role(text)            to authenticated, service_role;
grant  execute on function public.yf_has_role(text, text)  to authenticated, service_role;

-- user_roles policies (must come after the helpers they use).
-- Everyone can see their own roles; admins manage all roles.
drop policy if exists rbac_user_roles_self_read on public.user_roles;
create policy rbac_user_roles_self_read on public.user_roles
  for select to authenticated
  using (user_id = auth.uid());

drop policy if exists rbac_user_roles_admin_all on public.user_roles;
create policy rbac_user_roles_admin_all on public.user_roles
  for all to authenticated
  using (public.yf_has_role('admin', site_id))
  with check (public.yf_has_role('admin', site_id));

-- Academy tables: users see their own rows; admins see everything.
drop policy if exists rbac_academy_progress_read on public.academy_progress;
create policy rbac_academy_progress_read on public.academy_progress
  for select to authenticated
  using (user_id = auth.uid() or public.yf_has_role('admin'));

drop policy if exists rbac_certificates_read on public.certificates;
create policy rbac_certificates_read on public.certificates
  for select to authenticated
  using (user_id = auth.uid() or public.yf_has_role('admin'));


-- ------------------------------------------------------------
-- 3) RPCs — the only non-service write paths besides alert acks
--    and pending-command inserts.
-- ------------------------------------------------------------

-- Managers approve/reject pending commands. SECURITY DEFINER: the
-- commands table deliberately has NO update policy for authenticated
-- (and its UPDATE privilege is revoked below), so this function is
-- the only way a human decides a command.
create or replace function public.decide_command(
  p_id       uuid,
  p_decision text,
  p_note     text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.commands%rowtype;
begin
  if auth.uid() is null then
    raise exception 'decide_command: not authenticated — sign in first';
  end if;
  if p_decision is null or p_decision not in ('approved', 'rejected') then
    raise exception 'decide_command: invalid decision "%" — expected ''approved'' or ''rejected''', p_decision;
  end if;

  select * into v_row from public.commands where id = p_id for update;
  if not found then
    raise exception 'decide_command: command % not found', p_id;
  end if;
  if not public.yf_has_role('manager', v_row.site_id) then
    raise exception 'decide_command: requires manager role (or higher) at site %', v_row.site_id;
  end if;
  if v_row.status <> 'pending' then
    raise exception 'decide_command: command % is already ''%'' — only pending commands can be decided', p_id, v_row.status;
  end if;

  update public.commands
     set status     = p_decision,
         decided_by = coalesce(auth.email(), auth.uid()::text),
         decided_at = now(),
         note       = coalesce(p_note, note)
   where id = p_id
   returning * into v_row;

  return row_to_json(v_row);
end
$$;

-- Self-service upsert of the calling user's academy progress.
create or replace function public.save_progress(
  p_pack text,
  p_data jsonb)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.academy_progress%rowtype;
begin
  if auth.uid() is null then
    raise exception 'save_progress: not authenticated — sign in first';
  end if;
  if p_pack is null or length(trim(p_pack)) = 0 then
    raise exception 'save_progress: p_pack (content pack id) is required';
  end if;

  insert into public.academy_progress (user_id, pack_id, data, updated_at)
  values (auth.uid(), p_pack, coalesce(p_data, '{}'::jsonb), now())
  on conflict (user_id, pack_id)
    do update set data = excluded.data, updated_at = now()
  returning * into v_row;

  return row_to_json(v_row);
end
$$;

-- Self-service certificate issuance for the calling user.
create or replace function public.issue_certificate(
  p_track text,
  p_score numeric,
  p_code  text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.certificates%rowtype;
begin
  if auth.uid() is null then
    raise exception 'issue_certificate: not authenticated — sign in first';
  end if;
  if p_track is null or length(trim(p_track)) = 0 then
    raise exception 'issue_certificate: p_track is required';
  end if;
  if p_score is null or p_score < 0 or p_score > 100 then
    raise exception 'issue_certificate: p_score must be between 0 and 100 (got %)', p_score;
  end if;
  if p_code is null or length(trim(p_code)) = 0 then
    raise exception 'issue_certificate: p_code (verification code) is required';
  end if;

  insert into public.certificates (user_id, track, score, verification_code)
  values (auth.uid(), p_track, p_score, p_code)
  returning * into v_row;

  return row_to_json(v_row);
exception
  when unique_violation then
    raise exception 'issue_certificate: verification code "%" already exists', p_code;
end
$$;

-- Functions are executable by PUBLIC by default — lock them to
-- signed-in users (service_role kept for ops tooling).
revoke execute on function public.decide_command(uuid, text, text)      from public, anon;
revoke execute on function public.save_progress(text, jsonb)            from public, anon;
revoke execute on function public.issue_certificate(text, numeric, text) from public, anon;
grant  execute on function public.decide_command(uuid, text, text)       to authenticated, service_role;
grant  execute on function public.save_progress(text, jsonb)             to authenticated, service_role;
grant  execute on function public.issue_certificate(text, numeric, text) to authenticated, service_role;


-- ------------------------------------------------------------
-- 4) THE POLICY SWAP — replaces demo_all (0001-0004) and the 0006
--    policies (hardened_read_authenticated / hardened_read_anon)
--    on every fleet data table.
--
--    Reads : any role at the row's site (admins: every site).
--    Writes: robots/alerts/incidents/missions/fleet_meta/
--            robot_telemetry/maintenance_findings get NO insert or
--            update policy — writers use the service_role key, which
--            BYPASSES RLS, so none is needed or wanted.
--    Except: alerts.ack updates (operator+) and pending-command
--            inserts (operator+) below.
-- ------------------------------------------------------------

do $$
declare t text;
begin
  foreach t in array array[
    'robots', 'alerts', 'incidents', 'missions',
    'robot_telemetry', 'maintenance_findings', 'commands'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists demo_all on public.%I', t);
    execute format('drop policy if exists hardened_read_authenticated on public.%I', t);
    execute format('drop policy if exists hardened_read_anon on public.%I', t);
    execute format('drop policy if exists rbac_read on public.%I', t);
    execute format(
      'create policy rbac_read on public.%I
         for select to authenticated
         using (public.yf_has_role(''operator'', site_id))', t);
  end loop;
end $$;

-- fleet_meta has no site_id (still single-site, one heartbeat row —
-- see 0005): readable by anyone holding a role at the original site
-- (admins pass via the global-admin clause).
alter table public.fleet_meta enable row level security;
drop policy if exists demo_all on public.fleet_meta;
drop policy if exists hardened_read_authenticated on public.fleet_meta;
drop policy if exists hardened_read_anon on public.fleet_meta;
drop policy if exists rbac_read on public.fleet_meta;
create policy rbac_read on public.fleet_meta
  for select to authenticated
  using (public.yf_has_role('operator', 'BLR-DC1'));

-- Alerts: operator+ may acknowledge. CAVEAT — Postgres RLS is
-- ROW-level, not COLUMN-level: this policy alone would let an
-- operator PATCH any column of an alert row at their site, not just
-- `ack`. The column-level GRANT below closes that gap: authenticated
-- loses the table-wide UPDATE privilege and keeps UPDATE on the
-- `ack` column only. Every console "ack" update rides this policy
-- pair; all other alert writes come from the service_role key.
drop policy if exists rbac_alerts_ack on public.alerts;
create policy rbac_alerts_ack on public.alerts
  for update to authenticated
  using (public.yf_has_role('operator', site_id))
  with check (public.yf_has_role('operator', site_id));

revoke update on public.alerts from anon, authenticated;
grant  update (ack) on public.alerts to authenticated;

-- Commands: operator+ may REQUEST (insert a pending row as
-- themselves). Nobody but service_role may UPDATE a command row
-- directly — approval/rejection must go through decide_command()
-- (SECURITY DEFINER above), which enforces the manager gate and the
-- pending-only transition. Belt and braces: the UPDATE privilege is
-- revoked too, so even a future stray policy could not reopen it.
drop policy if exists rbac_commands_insert on public.commands;
create policy rbac_commands_insert on public.commands
  for insert to authenticated
  with check (
    public.yf_has_role('operator', site_id)
    and status = 'pending'
    and requested_by = auth.email()   -- RBAC mode assumes email-based accounts
    and decided_by is null
  );

revoke update on public.commands from anon, authenticated;
grant  insert on public.commands to authenticated;

select 'v0.7 RBAC: demo_all/0006 policies replaced; reads=role at site (admin global); writes=service_role; ack=operator+; request=operator+; decide=decide_command(manager+)' as result;


-- ------------------------------------------------------------
-- 5) SEEDING ROLES (run in the SQL editor or via psql — the very
--    first admin must be seeded this way, because only admins can
--    write user_roles through the API; service_role bypasses RLS).
--
-- -- Make an existing Auth user the admin of BLR-DC1:
-- insert into public.user_roles (user_id, role, site_id)
-- select id, 'admin', 'BLR-DC1'
--   from auth.users
--  where email = 'ops-lead@example.com'
-- on conflict (user_id, site_id) do update set role = excluded.role;
--
-- -- Grant an operator at a specific site:
-- insert into public.user_roles (user_id, role, site_id)
-- select id, 'operator', 'BLR-DC1'
--   from auth.users
--  where email = 'operator1@example.com'
-- on conflict (user_id, site_id) do update set role = excluded.role;
--
-- After that, admins can manage roles from any authenticated client
-- (the rbac_user_roles_admin_all policy), e.g. via PostgREST:
--   POST /rest/v1/user_roles  {"user_id": "...", "role": "manager"}
-- ------------------------------------------------------------


-- ============================================================
-- ROLLBACK (back to demo-open mode)
-- ------------------------------------------------------------
-- Uncomment and run this whole block to drop the RBAC policies and
-- restore the demo_all policies exactly as 0001-0004 created them,
-- including the table-wide UPDATE grants this migration revoked.
-- Idempotent. The RBAC tables/functions are left in place (harmless
-- in demo mode); a full teardown is included, commented, below it.
--
-- do $$
-- declare t text;
-- begin
--   foreach t in array array[
--     'robots', 'alerts', 'incidents', 'missions', 'fleet_meta',
--     'robot_telemetry', 'maintenance_findings', 'commands'
--   ] loop
--     execute format('drop policy if exists rbac_read on public.%I', t);
--     execute format('drop policy if exists rbac_alerts_ack on public.%I', t);
--     execute format('drop policy if exists rbac_commands_insert on public.%I', t);
--     execute format('drop policy if exists hardened_read_authenticated on public.%I', t);
--     execute format('drop policy if exists hardened_read_anon on public.%I', t);
--     execute format('drop policy if exists demo_all on public.%I', t);
--     execute format(
--       'create policy demo_all on public.%I
--          for all to anon, authenticated using (true) with check (true)', t);
--   end loop;
-- end $$;
--
-- grant update on public.alerts   to anon, authenticated;
-- grant update on public.commands to anon, authenticated;
--
-- select 'ROLLED BACK to demo-open: demo_all restored, RBAC policies dropped' as result;
--
-- -- FULL teardown of the RBAC objects (only if you never want them
-- -- back — destroys role assignments and academy data!):
-- -- drop function if exists public.decide_command(uuid, text, text);
-- -- drop function if exists public.save_progress(text, jsonb);
-- -- drop function if exists public.issue_certificate(text, numeric, text);
-- -- drop function if exists public.yf_has_role(text, text);
-- -- drop function if exists public.yf_role(text);
-- -- drop function if exists public.yf_rank(text);
-- -- drop table if exists public.certificates;
-- -- drop table if exists public.academy_progress;
-- -- drop table if exists public.user_roles;
-- ============================================================
