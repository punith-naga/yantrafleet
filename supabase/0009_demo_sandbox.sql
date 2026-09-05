-- ============================================================
-- YantraFleet 0009 — EPHEMERAL DEMO SANDBOX (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql — REQUIRES yf_has_role/yf_rank !!
-- !!  and the 0007 policy posture (anon has NO access).      !!
-- !!  Applying this to a demo-open (0001-0005) project is a  !!
-- !!  NO-OP for security: demo_all already gives anon every  !!
-- !!  row of every site, so there is nothing to isolate. The !!
-- !!  isolation promise below only holds once 0007 has run.  !!
-- !!  THIS MIGRATION GIVES ANONYMOUS INTERNET VISITORS WRITE !!
-- !!  ACCESS. Read the THREAT MODEL block before applying.   !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- A visitor clicks "try it" on yantrika.ai, gets a throwaway
-- ``site_id`` and a live sandbox with no signup, and the whole thing
-- self-destructs about an hour later.
--
-- ------------------------------------------------------------
-- THREAT MODEL — why the blast radius is exactly one ephemeral site
-- ------------------------------------------------------------
-- The attacker is assumed to be a competent, hostile anonymous visitor
-- who holds the publishable (anon) key, can mint unlimited demo
-- sessions, can read this file, and will send hand-crafted PostgREST
-- requests rather than using our console. Five independent controls
-- have to fail before real fleet data is touched:
--
--  1. IDENTITY IS A BEARER SECRET, NOT A ROLE. A demo session is a
--     64-hex-char token (two v4 UUIDs of entropy) presented in the
--     ``x-yf-demo-token`` request header. It grants no database role;
--     it is only an input to `yf_demo_site()`.
--
--  2. EVERY DEMO POLICY IS PINNED TO ONE ROW VALUE. The anon policies
--     below are all of the shape
--         site_id = public.yf_demo_site()
--     which resolves to a SINGLE site_id or NULL. NULL compares as
--     NULL (never true), so a missing / expired / reaped / bogus token
--     sees nothing and writes nothing. Fail-closed by construction —
--     there is no "no filter" branch anywhere.
--
--  3. WRITES CANNOT ESCAPE THE SITE. Every insert/update policy repeats
--     the same predicate in WITH CHECK, so a demo visitor cannot POST a
--     row carrying ``site_id: 'BLR-DC1'`` (the check rejects it) and
--     cannot PATCH a row from site A into site B.
--
--  4. THE SITE NAMESPACE IS DISJOINT. `yf_is_demo_site()` additionally
--     requires the ``DEMO-`` prefix on both sides of every policy, and
--     minted site ids are random (`DEMO-<12 hex>`). A real site would
--     have to be named ``DEMO-…`` *and* have a matching row in
--     `demo_sessions` before a token could ever address it.
--
--  5. THE PRIVILEGE SURFACE IS ENUMERATED, NOT INHERITED. anon is
--     granted SELECT/INSERT/UPDATE on exactly seven site-scoped fleet
--     tables, with column-level UPDATE grants on `alerts` and
--     `commands` that mirror 0007's operator posture. anon gets:
--       * NO DELETE anywhere (only the SECURITY DEFINER reaper deletes),
--       * NO access to `fleet_meta` (it has no site_id — nothing to
--         scope it by, so it is simply off-limits),
--       * NO access to `app_settings` (0008), `user_roles`,
--         `academy_progress`, `certificates`, `demo_sessions`, or any
--         table a later migration adds — grants here are explicit and
--         additive only.
--
-- RESIDUAL RISKS, stated plainly rather than hidden:
--   * Row-count abuse. A visitor can insert rows into their own sandbox
--     until the reaper runs. Mitigated by `demo_limits.max_live_sessions`
--     (a global cap on concurrent sandboxes, checked at mint time) and by
--     the ~1h TTL. If you expose this on a hot public site, put a real
--     rate limiter in front of `demo_mint_session` too.
--   * Reaping is not automatic. `demo_reap_expired()` must be scheduled
--     (pg_cron, an edge function, or `yantraops` on a timer). Until it
--     runs, expired sandboxes keep their rows — but their tokens already
--     stopped working (control 2 checks `expires_at` on every call).
--   * The token is a bearer credential: whoever holds it holds that one
--     sandbox. That is the intended, and only, consequence of leaking it.
--
-- ALSO DEFINED HERE (used by 0011-0016): `public.yf_can_read_site(text)`,
-- the single site-read gate — "operator+ at this site, OR this is my own
-- live demo sandbox" — and `public.yf_is_service_role()`.
--
-- Run after 0008_app_settings.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Tables
-- ------------------------------------------------------------

create table if not exists public.demo_sessions (
  token       text primary key,                    -- opaque bearer secret (64 hex)
  site_id     text not null unique,                -- 'DEMO-<12 hex>'
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null,
  claimed     boolean not null default false,      -- has the visitor opened it yet
  claimed_at  timestamptz,
  reaped_at   timestamptz,                         -- set when its rows were purged
  origin      text                                 -- optional coarse marker (e.g. 'marketing')
);

create index if not exists demo_sessions_expires
  on public.demo_sessions (expires_at) where reaped_at is null;

-- Operator-tunable knobs for the sandbox (single row, id = 1).
create table if not exists public.demo_limits (
  id                int primary key default 1 check (id = 1),
  enabled           boolean not null default true,
  ttl_minutes       int not null default 60  check (ttl_minutes between 5 and 1440),
  max_live_sessions int not null default 25  check (max_live_sessions between 0 and 10000),
  max_seed_robots   int not null default 12  check (max_seed_robots between 0 and 50),
  updated_at        timestamptz not null default now(),
  updated_by        text
);
insert into public.demo_limits (id) values (1) on conflict (id) do nothing;

alter table public.demo_sessions enable row level security;
alter table public.demo_limits   enable row level security;

-- No policies, no grants: both tables are reachable ONLY through the
-- SECURITY DEFINER RPCs below (same posture as 0008's app_settings).
-- A visitor must never be able to list other visitors' tokens.
revoke all on public.demo_sessions from anon, authenticated;
revoke all on public.demo_limits   from anon, authenticated;


-- ------------------------------------------------------------
-- 2) Helpers
-- ------------------------------------------------------------

-- Namespace guard: demo site ids live in a disjoint prefix.
create or replace function public.yf_is_demo_site(p_site text)
returns boolean
language sql immutable
set search_path = ''
as $$
  select p_site is not null and p_site like 'DEMO-%'
$$;

-- The demo token presented on THIS request, from the ``x-yf-demo-token``
-- header. NULL outside a PostgREST request, or when the header is absent
-- or blank. Never raises — a malformed header context must not turn into
-- a 500 on every fleet read.
create or replace function public.yf_demo_token()
returns text
language plpgsql stable
set search_path = ''
as $$
declare
  v_headers text;
  v_token   text;
begin
  begin
    v_headers := current_setting('request.headers', true);
  exception when others then
    return null;
  end;
  if v_headers is null or length(v_headers) = 0 then
    return null;
  end if;
  begin
    v_token := (v_headers::json ->> 'x-yf-demo-token');
  exception when others then
    return null;
  end;
  v_token := nullif(btrim(coalesce(v_token, '')), '');
  -- Shape guard: tokens are hex only. Cheap, and keeps junk out of the
  -- index probe below.
  if v_token is null or v_token !~ '^[0-9a-f]{32,128}$' then
    return null;
  end if;
  return v_token;
end
$$;

-- The one site_id this request's demo token may touch, or NULL.
-- SECURITY DEFINER: demo_sessions has no grants for anon at all, but the
-- policies below (which run as anon) must be able to resolve the token.
-- Leaks nothing — you only ever learn the site behind a token you hold.
create or replace function public.yf_demo_site()
returns text
language sql stable security definer
set search_path = ''
as $$
  select ds.site_id
    from public.demo_sessions ds
   where ds.token = public.yf_demo_token()
     and ds.reaped_at is null
     and ds.expires_at > now()
$$;

-- The site-read gate reused by 0011-0016: operator+ at this site
-- (0007's hierarchy, admins global), OR this request's own live sandbox.
create or replace function public.yf_can_read_site(p_site text)
returns boolean
language plpgsql stable security definer
set search_path = ''
as $$
begin
  if p_site is null then
    return false;
  end if;
  if public.yf_is_demo_site(p_site) and p_site = public.yf_demo_site() then
    return true;
  end if;
  return public.yf_has_role('operator', p_site);
end
$$;

-- True when the caller is the service_role key, or a direct database
-- session (psql / ops tooling) with no PostgREST request context at all.
-- Used by 0014's inbound webhook RPCs as belt-and-braces behind their
-- EXECUTE grants.
create or replace function public.yf_is_service_role()
returns boolean
language plpgsql stable
set search_path = ''
as $$
declare
  v_claims text;
  v_role   text;
begin
  begin
    v_claims := current_setting('request.jwt.claims', true);
  exception when others then
    return true;   -- no request context => direct DB session
  end;
  if v_claims is null or length(v_claims) = 0 then
    return true;   -- direct DB session (psql, migrations, ops tooling)
  end if;
  begin
    v_role := (v_claims::json ->> 'role');
  exception when others then
    return false;
  end;
  return coalesce(v_role, '') = 'service_role';
end
$$;

revoke execute on function public.yf_demo_site()          from public;
revoke execute on function public.yf_can_read_site(text)  from public;
revoke execute on function public.yf_is_service_role()    from public;
grant  execute on function public.yf_is_demo_site(text)   to anon, authenticated, service_role;
grant  execute on function public.yf_demo_token()         to anon, authenticated, service_role;
grant  execute on function public.yf_demo_site()          to anon, authenticated, service_role;
grant  execute on function public.yf_can_read_site(text)  to anon, authenticated, service_role;
grant  execute on function public.yf_is_service_role()    to authenticated, service_role;


-- ------------------------------------------------------------
-- 3) THE ANON SANDBOX POLICIES — controls 2/3/4 of the threat model.
-- ------------------------------------------------------------

do $$
declare t text;
begin
  foreach t in array array[
    'robots', 'alerts', 'incidents', 'missions',
    'robot_telemetry', 'maintenance_findings', 'commands'
  ] loop
    execute format('alter table public.%I enable row level security', t);

    execute format('drop policy if exists demo_sandbox_read on public.%I', t);
    execute format(
      'create policy demo_sandbox_read on public.%I
         for select to anon
         using (public.yf_is_demo_site(site_id)
                and site_id = public.yf_demo_site())', t);

    execute format('drop policy if exists demo_sandbox_insert on public.%I', t);
    execute format(
      'create policy demo_sandbox_insert on public.%I
         for insert to anon
         with check (public.yf_is_demo_site(site_id)
                     and site_id = public.yf_demo_site())', t);

    execute format('drop policy if exists demo_sandbox_update on public.%I', t);
    execute format(
      'create policy demo_sandbox_update on public.%I
         for update to anon
         using (public.yf_is_demo_site(site_id)
                and site_id = public.yf_demo_site())
         with check (public.yf_is_demo_site(site_id)
                     and site_id = public.yf_demo_site())', t);
  end loop;
end $$;

-- Control 5: the enumerated privilege surface. Note there is deliberately
-- no `for delete` policy and no DELETE grant on any table — a sandbox is
-- torn down by the reaper, never by its visitor.
grant select, insert, update on
  public.robots, public.incidents, public.missions,
  public.robot_telemetry, public.maintenance_findings to anon;

-- alerts / commands mirror 0007's operator posture: RLS is row-level, so
-- the column grants are what stop a demo visitor from rewriting an alert's
-- text or back-dating a command decision inside their own sandbox. Same
-- belt-and-braces pattern as 0007's `grant update (ack) ... to authenticated`.
revoke update on public.alerts   from anon;
revoke update on public.commands from anon;
grant  select, insert on public.alerts   to anon;
grant  select, insert on public.commands to anon;
grant  update (ack) on public.alerts to anon;
grant  update (status, decided_by, decided_at, note, executed_at)
  on public.commands to anon;

-- No DELETE, for anyone, anywhere. RLS alone already blocks it (there is
-- no `for delete` policy above), but Supabase's project defaults hand anon
-- a table-wide DELETE *privilege* on every table in `public`, and this
-- migration is the one that hands anon a working RLS path. Two independent
-- controls beat one: the privilege goes away too.
do $$
declare t text;
begin
  foreach t in array array[
    'robots', 'alerts', 'incidents', 'missions',
    'robot_telemetry', 'maintenance_findings', 'commands'
  ] loop
    execute format('revoke delete, truncate, references, trigger on public.%I from anon', t);
  end loop;
end $$;

-- fleet_meta has no site_id (0005) — nothing to scope a demo token by, so
-- it stays entirely off-limits to anon. Stated explicitly so a future
-- reader does not "fix" the omission.
revoke all on public.fleet_meta from anon;


-- ------------------------------------------------------------
-- 4) RPCs
-- ------------------------------------------------------------

-- Internal: purge every row scoped to the given sites. The ONLY delete
-- path in this migration. Refuses outright to touch a non-demo site — a
-- caller bug must not become data loss on a real fleet.
create or replace function public._yf_demo_purge_sites(p_sites text[])
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  t        text;
  v_site   text;
  v_counts jsonb := '{}'::jsonb;
  v_n      bigint;
  v_total  bigint := 0;
begin
  if p_sites is null or array_length(p_sites, 1) is null then
    return json_build_object('sites', '[]'::json, 'deleted', '{}'::json, 'total', 0);
  end if;
  foreach v_site in array p_sites loop
    if not public.yf_is_demo_site(v_site) then
      raise exception '_yf_demo_purge_sites: refusing to purge non-demo site "%"', v_site;
    end if;
  end loop;

  foreach t in array array[
    'robot_telemetry', 'maintenance_findings', 'commands',
    'alerts', 'incidents', 'missions', 'robots'
  ] loop
    execute format('delete from public.%I where site_id = any($1)', t) using p_sites;
    get diagnostics v_n = row_count;
    v_counts := v_counts || jsonb_build_object(t, v_n);
    v_total := v_total + v_n;
  end loop;

  return json_build_object(
    'sites', to_json(p_sites),
    'deleted', v_counts::json,
    'total', v_total);
end
$$;

-- Mint a sandbox. Callable by ANON — this is the "try it" button.
create or replace function public.demo_mint_session(
  p_ttl_minutes int default null,
  p_seed_robots int default 6,
  p_origin      text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_lim    public.demo_limits%rowtype;
  v_ttl    int;
  v_seed   int;
  v_live   int;
  v_token  text;
  v_site   text;
  v_row    public.demo_sessions%rowtype;
  i        int;
  v_status text;
begin
  select * into v_lim from public.demo_limits where id = 1;
  if not found then
    raise exception 'demo_mint_session: demo_limits row missing — re-run 0009';
  end if;
  if not v_lim.enabled then
    raise exception 'demo_mint_session: the demo sandbox is disabled on this deployment';
  end if;

  -- Opportunistic reap keeps the live-session cap honest without a cron.
  perform public.demo_reap_expired();

  select count(*) into v_live
    from public.demo_sessions
   where reaped_at is null and expires_at > now();
  if v_live >= v_lim.max_live_sessions then
    raise exception 'demo_mint_session: too many live demo sandboxes (% of %) — try again in a few minutes',
      v_live, v_lim.max_live_sessions;
  end if;

  v_ttl  := least(greatest(coalesce(p_ttl_minutes, v_lim.ttl_minutes), 5), v_lim.ttl_minutes);
  v_seed := least(greatest(coalesce(p_seed_robots, 0), 0), v_lim.max_seed_robots);

  -- 2 x v4 UUID of entropy, hex only (matches yf_demo_token's shape guard).
  v_token := replace(gen_random_uuid()::text, '-', '')
          || replace(gen_random_uuid()::text, '-', '');
  v_site  := 'DEMO-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 12));

  insert into public.demo_sessions (token, site_id, expires_at, origin)
  values (v_token, v_site, now() + make_interval(mins => v_ttl),
          nullif(btrim(coalesce(p_origin, '')), ''))
  returning * into v_row;

  -- Seed a small starter fleet so the sandbox is interesting the instant
  -- it loads, with or without a simulator attached to it.
  for i in 1 .. v_seed loop
    v_status := (array['active','active','idle','charging','active','idle'])[1 + (i - 1) % 6];
    insert into public.robots (id, vendor, status, battery, pos, speed,
                               task_kind, health, motor_temp, tasks_done,
                               site_id, updated_at)
    values (v_site || '-R' || lpad(i::text, 2, '0'),
            (array['MiR','OTTO','Geek+','Locus'])[1 + (i - 1) % 4],
            v_status,
            round(45 + (random() * 50)::numeric, 1),
            jsonb_build_array(round((random() * 40)::numeric, 2),
                              round((random() * 24)::numeric, 2)),
            case when v_status = 'active' then round((0.4 + random())::numeric, 2) else 0 end,
            case when v_status = 'active' then 'transport' else null end,
            round(88 + (random() * 11)::numeric, 1),
            round(38 + (random() * 22)::numeric, 1),
            (random() * 40)::int,
            v_site, now());
  end loop;

  if v_seed > 0 then
    insert into public.missions (id, name, robots, state, prog, eta, site_id)
    values (v_site || '-M01', 'Inbound pallet sweep',
            jsonb_build_array(v_site || '-R01'), 'Running', 42, '12m', v_site);
    insert into public.alerts (id, sev, msg, src, tlabel, ack, site_id)
    values (v_site || '-A01', 'warn',
            'Charge-bay contention: 2 robots queued for bay 3',
            'fleet', v_site || '-R03', false, v_site);
  end if;

  return json_build_object(
    'token',       v_row.token,
    'site_id',     v_row.site_id,
    'created_at',  v_row.created_at,
    'expires_at',  v_row.expires_at,
    'ttl_minutes', v_ttl,
    'claimed',     v_row.claimed,
    'seeded_robots', v_seed);
end
$$;

-- Mark a sandbox as opened by its visitor. Returns the same shape as
-- demo_session_info. Idempotent (claiming twice is not an error).
create or replace function public.demo_claim_session(p_token text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.demo_sessions%rowtype;
begin
  select * into v_row from public.demo_sessions
   where token = p_token and reaped_at is null for update;
  if not found then
    raise exception 'demo_claim_session: unknown or already-reaped demo token';
  end if;
  if v_row.expires_at <= now() then
    raise exception 'demo_claim_session: this demo sandbox expired at %', v_row.expires_at;
  end if;
  if not v_row.claimed then
    update public.demo_sessions
       set claimed = true, claimed_at = now()
     where token = p_token
     returning * into v_row;
  end if;
  return json_build_object(
    'token', v_row.token, 'site_id', v_row.site_id,
    'created_at', v_row.created_at, 'expires_at', v_row.expires_at,
    'claimed', v_row.claimed, 'claimed_at', v_row.claimed_at,
    'live', true,
    'seconds_remaining', greatest(0, floor(extract(epoch from (v_row.expires_at - now())))::int));
end
$$;

-- Non-mutating status probe for the scrubber/countdown in the UI. Never
-- raises for an unknown token: an expired sandbox is a normal, expected
-- state that the UI renders as "your demo ended", not an error toast.
create or replace function public.demo_session_info(p_token text)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_row public.demo_sessions%rowtype;
begin
  select * into v_row from public.demo_sessions where token = p_token;
  if not found then
    -- Same key set as the found branch, so the UI never has to
    -- special-case a missing field (see docs/FEATURE-CONTRACTS.md).
    return json_build_object('live', false, 'reason', 'unknown',
                             'token', null, 'site_id', null,
                             'created_at', null, 'expires_at', null,
                             'claimed', false, 'claimed_at', null,
                             'seconds_remaining', 0);
  end if;
  return json_build_object(
    'live', (v_row.reaped_at is null and v_row.expires_at > now()),
    'reason', case when v_row.reaped_at is not null then 'reaped'
                   when v_row.expires_at <= now() then 'expired'
                   else 'ok' end,
    'token', v_row.token, 'site_id', v_row.site_id,
    'created_at', v_row.created_at, 'expires_at', v_row.expires_at,
    'claimed', v_row.claimed, 'claimed_at', v_row.claimed_at,
    'seconds_remaining', greatest(0, floor(extract(epoch from (v_row.expires_at - now())))::int));
end
$$;

-- A visitor ending their own sandbox early ("I'm done"). Purges exactly
-- the site behind the presented token and nothing else.
create or replace function public.demo_end_session(p_token text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row    public.demo_sessions%rowtype;
  v_purged json;
begin
  select * into v_row from public.demo_sessions
   where token = p_token for update;
  if not found then
    raise exception 'demo_end_session: unknown demo token';
  end if;
  if v_row.reaped_at is not null then
    return json_build_object('site_id', v_row.site_id, 'already_reaped', true,
                             'deleted', '{}'::json, 'total', 0);
  end if;
  v_purged := public._yf_demo_purge_sites(array[v_row.site_id]);
  update public.demo_sessions
     set reaped_at = now(), expires_at = least(expires_at, now())
   where token = p_token;
  return json_build_object('site_id', v_row.site_id, 'already_reaped', false,
                           'deleted', v_purged -> 'deleted',
                           'total', v_purged -> 'total');
end
$$;

-- The reaper. Schedule it (pg_cron / edge function / ops timer) or let
-- demo_mint_session's opportunistic call carry it.
create or replace function public.demo_reap_expired(
  p_grace_minutes int default 0)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_sites  text[];
  v_purged json;
begin
  select coalesce(array_agg(site_id), array[]::text[]) into v_sites
    from public.demo_sessions
   where reaped_at is null
     and expires_at < now() - make_interval(mins => greatest(coalesce(p_grace_minutes, 0), 0));

  if array_length(v_sites, 1) is null then
    return json_build_object('sessions_reaped', 0, 'sites', '[]'::json,
                             'deleted', '{}'::json, 'total', 0);
  end if;

  v_purged := public._yf_demo_purge_sites(v_sites);
  update public.demo_sessions
     set reaped_at = now()
   where site_id = any(v_sites) and reaped_at is null;

  return json_build_object(
    'sessions_reaped', array_length(v_sites, 1),
    'sites', to_json(v_sites),
    'deleted', v_purged -> 'deleted',
    'total', v_purged -> 'total');
end
$$;

-- Admin visibility + knobs.
create or replace function public.admin_list_demo_sessions(p_limit int default 100)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_list_demo_sessions: requires admin role';
  end if;
  -- Tokens are deliberately NOT returned: an admin listing sandboxes has
  -- no reason to be handed live bearer credentials for them.
  select coalesce(json_agg(row_to_json(t) order by t.created_at desc), '[]'::json)
    into v_result
    from (select site_id, created_at, expires_at, claimed, claimed_at,
                 reaped_at, origin,
                 (reaped_at is null and expires_at > now()) as live
            from public.demo_sessions
           order by created_at desc
           limit greatest(coalesce(p_limit, 100), 1)) t;
  return v_result;
end
$$;

create or replace function public.admin_set_demo_limits(
  p_enabled           boolean default null,
  p_ttl_minutes       int     default null,
  p_max_live_sessions int     default null,
  p_max_seed_robots   int     default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_row public.demo_limits%rowtype;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_set_demo_limits: requires admin role';
  end if;
  update public.demo_limits
     set enabled           = coalesce(p_enabled, enabled),
         ttl_minutes       = coalesce(p_ttl_minutes, ttl_minutes),
         max_live_sessions = coalesce(p_max_live_sessions, max_live_sessions),
         max_seed_robots   = coalesce(p_max_seed_robots, max_seed_robots),
         updated_at        = now(),
         updated_by        = coalesce(auth.email(), auth.uid()::text)
   where id = 1
   returning * into v_row;
  return row_to_json(v_row);
end
$$;

create or replace function public.demo_limits_public()
returns json
language sql stable security definer
set search_path = ''
as $$
  -- Enough for the marketing page to render/hide the "try it" button.
  select json_build_object('enabled', l.enabled, 'ttl_minutes', l.ttl_minutes)
    from public.demo_limits l where l.id = 1
$$;

-- Grants. Only the four visitor-facing calls reach anon.
revoke execute on function public._yf_demo_purge_sites(text[])                from public, anon, authenticated;
revoke execute on function public.demo_mint_session(int, int, text)           from public;
revoke execute on function public.demo_claim_session(text)                    from public;
revoke execute on function public.demo_session_info(text)                     from public;
revoke execute on function public.demo_end_session(text)                      from public;
revoke execute on function public.demo_reap_expired(int)                      from public, anon;
revoke execute on function public.demo_limits_public()                        from public;
revoke execute on function public.admin_list_demo_sessions(int)               from public, anon;
revoke execute on function public.admin_set_demo_limits(boolean, int, int, int) from public, anon;

grant execute on function public.demo_mint_session(int, int, text) to anon, authenticated, service_role;
grant execute on function public.demo_claim_session(text)          to anon, authenticated, service_role;
grant execute on function public.demo_session_info(text)           to anon, authenticated, service_role;
grant execute on function public.demo_end_session(text)            to anon, authenticated, service_role;
grant execute on function public.demo_limits_public()              to anon, authenticated, service_role;
grant execute on function public.demo_reap_expired(int)            to authenticated, service_role;
grant execute on function public.admin_list_demo_sessions(int)     to authenticated, service_role;
grant execute on function public.admin_set_demo_limits(boolean, int, int, int) to authenticated, service_role;

select 'v0.9 DEMO SANDBOX: demo_sessions/demo_limits + mint/claim/info/end/reap RPCs; anon writes pinned to one DEMO-* site by policy AND column grants; yf_can_read_site()/yf_is_service_role() helpers added for 0011-0016' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — remove the sandbox entirely.
-- Purges every demo site's rows first, so no orphaned DEMO-* data is
-- left behind, then drops the anon policies, the anon grants, the RPCs
-- and the tables. Nothing in 0001-0008 is modified by this migration, so
-- there is nothing to restore — only something to drop.
-- ------------------------------------------------------------
-- select public._yf_demo_purge_sites(
--          (select coalesce(array_agg(site_id), array[]::text[])
--             from public.demo_sessions));
--
-- do $$
-- declare t text;
-- begin
--   foreach t in array array[
--     'robots', 'alerts', 'incidents', 'missions',
--     'robot_telemetry', 'maintenance_findings', 'commands'
--   ] loop
--     execute format('drop policy if exists demo_sandbox_read   on public.%I', t);
--     execute format('drop policy if exists demo_sandbox_insert on public.%I', t);
--     execute format('drop policy if exists demo_sandbox_update on public.%I', t);
--     execute format('revoke all on public.%I from anon', t);
--   end loop;
-- end $$;
--
-- drop function if exists public.admin_set_demo_limits(boolean, int, int, int);
-- drop function if exists public.admin_list_demo_sessions(int);
-- drop function if exists public.demo_limits_public();
-- drop function if exists public.demo_reap_expired(int);
-- drop function if exists public.demo_end_session(text);
-- drop function if exists public.demo_session_info(text);
-- drop function if exists public.demo_claim_session(text);
-- drop function if exists public.demo_mint_session(int, int, text);
-- drop function if exists public._yf_demo_purge_sites(text[]);
-- -- yf_can_read_site / yf_is_service_role are used by 0011-0016: drop
-- -- them only if you are rolling those back too.
-- -- drop function if exists public.yf_is_service_role();
-- -- drop function if exists public.yf_can_read_site(text);
-- drop function if exists public.yf_demo_site();
-- drop function if exists public.yf_demo_token();
-- drop function if exists public.yf_is_demo_site(text);
-- drop table if exists public.demo_limits;
-- drop table if exists public.demo_sessions;
--
-- select 'ROLLED BACK 0009: demo sandbox removed, anon has no access again' as result;
-- ============================================================
