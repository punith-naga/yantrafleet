-- ============================================================
-- Yantrika 0010 — EXPIRING READ-ONLY SHARE LINKS (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql — REQUIRES yf_has_role/yf_rank !!
-- !!  (public.yf_has_role('manager', site) gates minting and  !!
-- !!  revocation). Applying this to a demo-open project is    !!
-- !!  pointless: there, anon can already read everything.     !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- A manager hands a robot vendor's support desk a URL that shows one
-- live fleet view, or one incident, and stops working on a date they
-- chose. The outside party never signs up, never gets an account, and
-- never gets a database role.
--
-- ------------------------------------------------------------
-- WHY A RESOLVED SHARE TOKEN IS STRICTLY READ-ONLY
-- ------------------------------------------------------------
-- A share token grants NO table privileges whatsoever. Unlike 0009's
-- demo token — which is wired into RLS policies so a sandbox visitor can
-- write — a share token is only ever an *argument* to one SECURITY
-- DEFINER function, `share_resolve(p_token)`. That function contains
-- nothing but SELECTs. There is no share-scoped policy on any table, so
-- there is no mechanism by which holding a token could authorise an
-- insert, an update, an ack, a command, a settings read, or a read of
-- any site other than the one the link was minted for.
--
-- The payload is allow-listed per `kind`, column by column, not
-- filtered after the fact:
--   * kind='fleet'    -> robot telemetry-ish state, alert rows, open
--                        incident headers, and counts, for ONE site.
--   * kind='incident' -> ONE incident, the robot it points at, and that
--                        robot's open maintenance findings.
-- Deliberately never in either payload:
--   * `commands` rows           — they carry requested_by / decided_by,
--                                 which are operator EMAIL ADDRESSES;
--   * anything from `user_roles`, `auth.users`, `certificates`,
--     `academy_progress`, `app_settings`, `demo_sessions`;
--   * `share_links` itself      — one token can never enumerate another;
--   * any row whose site_id differs from the link's site_id.
--
-- Expiry and revocation are checked on every resolve, not cached.
--
-- Run after 0009_demo_sandbox.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Table
-- ------------------------------------------------------------

create table if not exists public.share_links (
  token          text primary key,                 -- opaque bearer secret (64 hex)
  kind           text not null check (kind in ('fleet', 'incident')),
  target_id      text,                             -- incident id when kind='incident'
  site_id        text not null,
  created_by     uuid references auth.users (id) on delete set null,
  created_email  text,                             -- snapshot, for the manager's own list
  label          text,                             -- e.g. 'MiR support — ticket 41822'
  created_at     timestamptz not null default now(),
  expires_at     timestamptz not null,
  revoked_at     timestamptz,
  revoked_by     text,
  last_viewed_at timestamptz,
  view_count     int not null default 0,
  constraint share_links_target_shape check (
    (kind = 'incident' and target_id is not null) or
    (kind = 'fleet'    and target_id is null))
);

create index if not exists share_links_site_created
  on public.share_links (site_id, created_at desc);
create index if not exists share_links_live
  on public.share_links (expires_at) where revoked_at is null;

alter table public.share_links enable row level security;

-- No policies and no grants, exactly like 0008's app_settings: the table
-- is reachable only through the SECURITY DEFINER RPCs below. This is what
-- makes "one token can never enumerate another" true even from a signed-in
-- manager's browser devtools.
revoke all on public.share_links from anon, authenticated;


-- ------------------------------------------------------------
-- 2) RPCs
-- ------------------------------------------------------------

-- Mint. manager+ at the target site (0007 hierarchy; admins global).
create or replace function public.share_mint_link(
  p_kind      text,
  p_target_id text default null,
  p_site      text default 'BLR-DC1',
  p_ttl_hours int  default 168,
  p_label     text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row   public.share_links%rowtype;
  v_token text;
  v_ttl   int;
begin
  if auth.uid() is null then
    raise exception 'share_mint_link: not authenticated — sign in first';
  end if;
  if p_site is null or length(btrim(p_site)) = 0 then
    raise exception 'share_mint_link: p_site is required';
  end if;
  if not public.yf_has_role('manager', p_site) then
    raise exception 'share_mint_link: requires manager role (or higher) at site %', p_site;
  end if;
  if p_kind is null or p_kind not in ('fleet', 'incident') then
    raise exception 'share_mint_link: p_kind must be ''fleet'' or ''incident'' (got %)', p_kind;
  end if;
  if p_kind = 'incident' then
    if p_target_id is null or length(btrim(p_target_id)) = 0 then
      raise exception 'share_mint_link: p_target_id (incident id) is required for kind=''incident''';
    end if;
    if not exists (select 1 from public.incidents i
                    where i.id = p_target_id and i.site_id = p_site) then
      raise exception 'share_mint_link: incident % not found at site %', p_target_id, p_site;
    end if;
  elsif p_target_id is not null then
    raise exception 'share_mint_link: p_target_id must be null for kind=''fleet''';
  end if;

  v_ttl := least(greatest(coalesce(p_ttl_hours, 168), 1), 24 * 90);   -- 1h .. 90d
  v_token := replace(gen_random_uuid()::text, '-', '')
          || replace(gen_random_uuid()::text, '-', '');

  insert into public.share_links (token, kind, target_id, site_id, created_by,
                                  created_email, label, expires_at)
  values (v_token, p_kind, p_target_id, p_site, auth.uid(),
          auth.email(), nullif(btrim(coalesce(p_label, '')), ''),
          now() + make_interval(hours => v_ttl))
  returning * into v_row;

  return json_build_object(
    'token', v_row.token, 'kind', v_row.kind, 'target_id', v_row.target_id,
    'site_id', v_row.site_id, 'label', v_row.label,
    'created_at', v_row.created_at, 'expires_at', v_row.expires_at,
    'ttl_hours', v_ttl, 'revoked_at', null, 'view_count', 0);
end
$$;

-- Resolve. Callable by ANON — this is the whole point of a share link.
-- Returns ONLY what `kind` permits. Never raises for a bad token: the
-- outside party gets a clean "this link is no longer valid" page, and an
-- attacker learns nothing from the difference between "wrong" and
-- "expired" beyond the `status` string, which is intentional UX.
create or replace function public.share_resolve(p_token text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_link      public.share_links%rowtype;
  v_status    text;
  v_robots    json;
  v_alerts    json;
  v_incidents json;
  v_incident  json;
  v_robot     json;
  v_findings  json;
  v_counts    json;
  v_tlabel    text;
begin
  if p_token is null or p_token !~ '^[0-9a-f]{32,128}$' then
    return json_build_object('valid', false, 'status', 'not_found');
  end if;

  select * into v_link from public.share_links where token = p_token;
  if not found then
    return json_build_object('valid', false, 'status', 'not_found');
  end if;
  v_status := case when v_link.revoked_at is not null then 'revoked'
                   when v_link.expires_at <= now()    then 'expired'
                   else 'ok' end;
  if v_status <> 'ok' then
    return json_build_object('valid', false, 'status', v_status,
                             'expires_at', v_link.expires_at);
  end if;

  update public.share_links
     set view_count = view_count + 1, last_viewed_at = now()
   where token = p_token;

  if v_link.kind = 'fleet' then
    select coalesce(json_agg(row_to_json(r) order by r.id), '[]'::json) into v_robots
      from (select id, vendor, status, battery, pos, speed, task_kind,
                   health, motor_temp, tasks_done, fault_msg, updated_at
              from public.robots
             where site_id = v_link.site_id) r;

    select coalesce(json_agg(row_to_json(a) order by a.created_at desc), '[]'::json)
      into v_alerts
      from (select id, sev, msg, src, tlabel, ack, created_at
              from public.alerts
             where site_id = v_link.site_id
             order by created_at desc
             limit 50) a;

    select coalesce(json_agg(row_to_json(i) order by i.created_at desc), '[]'::json)
      into v_incidents
      from (select id, sev, title, src, tlabel, state, created_at
              from public.incidents
             where site_id = v_link.site_id and state = 'Open'
             order by created_at desc
             limit 50) i;

    select json_build_object(
             'robots_total', count(*),
             'robots_active', count(*) filter (where status = 'active'),
             'robots_idle', count(*) filter (where status in ('idle', 'paused')),
             'robots_charging', count(*) filter (where status = 'charging'),
             'robots_faulted', count(*) filter (where status in ('fault', 'estop', 'degraded')))
      into v_counts
      from public.robots where site_id = v_link.site_id;

    return json_build_object(
      'valid', true, 'status', 'ok', 'kind', 'fleet',
      'site_id', v_link.site_id, 'label', v_link.label,
      'expires_at', v_link.expires_at, 'generated_at', now(),
      'counts', v_counts, 'robots', v_robots,
      'alerts', v_alerts, 'open_incidents', v_incidents);
  end if;

  -- kind = 'incident'
  select row_to_json(i) into v_incident
    from (select id, sev, title, src, tlabel, state, impact, rca, fix, dur,
                 created_at
            from public.incidents
           where id = v_link.target_id and site_id = v_link.site_id) i;
  if v_incident is null then
    return json_build_object('valid', false, 'status', 'target_missing');
  end if;

  v_tlabel := v_incident ->> 'tlabel';

  select row_to_json(r) into v_robot
    from (select id, vendor, status, battery, pos, speed, task_kind,
                 health, motor_temp, tasks_done, fault_msg, updated_at
            from public.robots
           where site_id = v_link.site_id and id = v_tlabel) r;

  select coalesce(json_agg(row_to_json(f) order by f.created_at desc), '[]'::json)
    into v_findings
    from (select id, robot_id, component, finding, rul_days, confidence,
                 action, state, created_at
            from public.maintenance_findings
           where site_id = v_link.site_id
             and robot_id = v_tlabel
             and state = 'Open'
           order by created_at desc
           limit 20) f;

  return json_build_object(
    'valid', true, 'status', 'ok', 'kind', 'incident',
    'site_id', v_link.site_id, 'label', v_link.label,
    'expires_at', v_link.expires_at, 'generated_at', now(),
    'incident', v_incident, 'robot', v_robot,
    'maintenance_findings', v_findings);
end
$$;

-- Revoke. manager+ at the link's site, or the person who minted it.
create or replace function public.share_revoke(p_token text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_link public.share_links%rowtype;
begin
  if auth.uid() is null then
    raise exception 'share_revoke: not authenticated — sign in first';
  end if;
  select * into v_link from public.share_links where token = p_token for update;
  if not found then
    raise exception 'share_revoke: unknown share token';
  end if;
  if not (public.yf_has_role('manager', v_link.site_id)
          or v_link.created_by = auth.uid()) then
    raise exception 'share_revoke: requires manager role (or higher) at site %, or being the link''s creator', v_link.site_id;
  end if;
  if v_link.revoked_at is null then
    update public.share_links
       set revoked_at = now(), revoked_by = coalesce(auth.email(), auth.uid()::text)
     where token = p_token
     returning * into v_link;
  end if;
  return json_build_object('token', v_link.token, 'kind', v_link.kind,
                           'site_id', v_link.site_id,
                           'revoked_at', v_link.revoked_at,
                           'revoked_by', v_link.revoked_by);
end
$$;

-- The manager's own list. manager+ at p_site. Includes the token so the
-- URL can be re-copied — that is the manager's own credential to manage.
create or replace function public.share_list_links(
  p_site           text    default 'BLR-DC1',
  p_include_dead   boolean default false,
  p_limit          int     default 100)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if not public.yf_has_role('manager', p_site) then
    raise exception 'share_list_links: requires manager role (or higher) at site %', p_site;
  end if;
  select coalesce(json_agg(row_to_json(t) order by t.created_at desc), '[]'::json)
    into v_result
    from (select token, kind, target_id, site_id, label, created_email,
                 created_at, expires_at, revoked_at, revoked_by,
                 last_viewed_at, view_count,
                 (revoked_at is null and expires_at > now()) as live
            from public.share_links
           where site_id = p_site
             and (coalesce(p_include_dead, false)
                  or (revoked_at is null and expires_at > now()))
           order by created_at desc
           limit greatest(coalesce(p_limit, 100), 1)) t;
  return v_result;
end
$$;

revoke execute on function public.share_mint_link(text, text, text, int, text) from public, anon;
revoke execute on function public.share_revoke(text)                           from public, anon;
revoke execute on function public.share_list_links(text, boolean, int)         from public, anon;
revoke execute on function public.share_resolve(text)                          from public;

grant execute on function public.share_mint_link(text, text, text, int, text) to authenticated, service_role;
grant execute on function public.share_revoke(text)                           to authenticated, service_role;
grant execute on function public.share_list_links(text, boolean, int)         to authenticated, service_role;
grant execute on function public.share_resolve(text)                          to anon, authenticated, service_role;

select 'v0.10 SHARE LINKS: share_links table (no table grants) + share_mint_link/share_revoke/share_list_links (manager+) and share_resolve (anon, read-only, kind-scoped payload)' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the share-link objects.
-- Purely additive migration: nothing here replaced an existing policy or
-- grant, so there is nothing to restore, only something to drop.
-- ------------------------------------------------------------
-- drop function if exists public.share_list_links(text, boolean, int);
-- drop function if exists public.share_revoke(text);
-- drop function if exists public.share_resolve(text);
-- drop function if exists public.share_mint_link(text, text, text, int, text);
-- drop table if exists public.share_links;
--
-- select 'ROLLED BACK 0010: share links removed' as result;
-- ============================================================
