-- ============================================================
-- Yantrika 0016 — PUBLIC FLEET STATUS PAGE (OPT-IN, OFF BY DEFAULT).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql AND 0012_utilization.sql —     !!
-- !!  REQUIRES yf_has_role() (0007) and missions.completed_at !!
-- !!  (0012). Applying this migration publishes NOTHING: the !!
-- !!  per-site flag defaults to FALSE and an admin must turn  !!
-- !!  each site on deliberately, one at a time.               !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- The status.yantrika.ai-style page a customer links their own customers
-- to: is the fleet up, how many robots are running, is anything on fire.
--
-- ------------------------------------------------------------
-- THE PAYLOAD IS COUNTS AND PERCENTAGES. FULL STOP.
-- ------------------------------------------------------------
-- `public_fleet_status()` is callable with the anon key by anyone on the
-- internet, so its return value is an explicit allow-list of AGGREGATES.
-- It returns no row from any table — every field is a count, a
-- percentage, or a timestamp. Never in the payload:
--   * robot ids, names or vendors      — `robots_online: 11`, not who;
--   * incident titles, ids, or text    — `active_incidents: 2`, and a
--     per-severity count, with no way to learn what happened;
--   * alert messages                   — only an unacked count;
--   * user identities of any kind      — no assignee, no acknowledger,
--     no requested_by, no email;
--   * poses, telemetry samples, or anything replayable;
--   * anything about any OTHER site.
-- `display_name` is the one free-text field, and it is chosen by the
-- admin who publishes the page — it is intended to be public.
--
-- ------------------------------------------------------------
-- WHY A DISABLED SITE AND A NONEXISTENT SITE LOOK IDENTICAL
-- ------------------------------------------------------------
-- Both return exactly {site_id, enabled: false, status: 'not_published'}.
-- If a disabled site returned a different shape from an unknown one, the
-- endpoint would become a site-name oracle: an attacker could enumerate a
-- customer's site ids by probing. So "off" and "never existed" are
-- indistinguishable from outside, and the caller cannot tell whether a
-- fleet exists at all until its owner chooses to publish it.
--
-- Run after 0015_certification.sql. Idempotent. ROLLBACK at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) The opt-in flag, off by default
-- ------------------------------------------------------------

create table if not exists public.site_status_pages (
  site_id         text primary key,
  enabled         boolean not null default false,   -- OFF unless an admin says otherwise
  display_name    text,                             -- public label, admin-chosen
  blurb           text,                             -- one public sentence, admin-chosen
  show_throughput boolean not null default true,
  show_incidents  boolean not null default true,
  online_grace_seconds int not null default 300
                  check (online_grace_seconds between 30 and 86400),
  updated_at      timestamptz not null default now(),
  updated_by      text
);

alter table public.site_status_pages enable row level security;

-- Like 0008's app_settings: no table-level access for anyone. The flag is
-- read only by the SECURITY DEFINER RPC below, so a raw
-- GET /rest/v1/site_status_pages can never enumerate which sites exist.
revoke all on public.site_status_pages from anon, authenticated;


-- ------------------------------------------------------------
-- 2) The public RPC
-- ------------------------------------------------------------

create or replace function public.public_fleet_status(p_site text)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_cfg      public.site_status_pages%rowtype;
  v_total    bigint := 0;
  v_online   bigint := 0;
  v_active   bigint := 0;
  v_charging bigint := 0;
  v_faulted  bigint := 0;
  v_incidents bigint := 0;
  v_by_sev   json := '{}'::json;
  v_alerts   bigint := 0;
  v_through  bigint := 0;
  v_uptime   numeric;
  v_covered  numeric;
  v_up       numeric;
  v_last     timestamptz;
  v_cutoff   timestamptz;
begin
  if p_site is null or length(btrim(p_site)) = 0 then
    return json_build_object('site_id', null, 'enabled', false,
                             'status', 'not_published');
  end if;

  select * into v_cfg from public.site_status_pages where site_id = btrim(p_site);
  if not found or not v_cfg.enabled then
    -- Identical for "off" and "never existed" — see the header note.
    return json_build_object('site_id', btrim(p_site), 'enabled', false,
                             'status', 'not_published');
  end if;

  v_cutoff := now() - make_interval(secs => v_cfg.online_grace_seconds);

  select count(*),
         count(*) filter (where r.updated_at > v_cutoff),
         count(*) filter (where r.status = 'active'),
         count(*) filter (where r.status = 'charging'),
         count(*) filter (where r.status in ('fault', 'estop', 'degraded')),
         max(r.updated_at)
    into v_total, v_online, v_active, v_charging, v_faulted, v_last
    from public.robots r
   where r.site_id = v_cfg.site_id;

  if v_cfg.show_incidents then
    select count(*) into v_incidents
      from public.incidents i
     where i.site_id = v_cfg.site_id and i.state = 'Open';
    -- Severity COUNTS only. No ids, no titles, no timestamps per incident.
    select coalesce(json_object_agg(t.sev, t.n), '{}'::json) into v_by_sev
      from (select i.sev, count(*) as n
              from public.incidents i
             where i.site_id = v_cfg.site_id and i.state = 'Open'
             group by i.sev) t;
    select count(*) into v_alerts
      from public.alerts a
     where a.site_id = v_cfg.site_id and not a.ack;
  end if;

  if v_cfg.show_throughput then
    select count(*) into v_through
      from public.missions m
     where m.site_id = v_cfg.site_id
       and m.completed_at is not null
       and m.completed_at >= date_trunc('day', now());
  end if;

  -- Uptime over the last 24h, from the same gap-capped span arithmetic
  -- 0012 uses, so the public number and the internal one agree.
  with samples as (
    select t.robot_id, t.ts, t.status,
           lead(t.ts) over (partition by t.robot_id order by t.ts) as next_ts
      from public.robot_telemetry t
     where t.site_id = v_cfg.site_id and t.ts >= now() - interval '24 hours'
  ),
  spans as (
    select s.status,
           extract(epoch from least(coalesce(s.next_ts, now()) - s.ts,
                                    interval '60 seconds'))::numeric as secs
      from samples s
  )
  select coalesce(sum(secs), 0),
         coalesce(sum(secs) filter (
           where status is null or status not in ('fault', 'estop', 'degraded')), 0)
    into v_covered, v_up
    from spans;

  if v_covered > 0 then
    v_uptime := round(100 * v_up / v_covered, 2);
  end if;

  return json_build_object(
    'site_id',            v_cfg.site_id,
    'enabled',            true,
    'status',             'published',
    'display_name',       coalesce(v_cfg.display_name, v_cfg.site_id),
    'blurb',              v_cfg.blurb,
    'generated_at',       now(),
    'robots_total',       v_total,
    'robots_online',      v_online,
    'robots_online_pct',  case when v_total > 0
                               then round(100.0 * v_online / v_total, 1) end,
    'robots_active',      v_active,
    'robots_charging',    v_charging,
    'robots_faulted',     v_faulted,
    'uptime_pct_24h',     v_uptime,
    'uptime_covered_seconds', round(coalesce(v_covered, 0), 0),
    'active_incidents',   case when v_cfg.show_incidents then v_incidents end,
    'active_incidents_by_sev', case when v_cfg.show_incidents then v_by_sev end,
    'unacked_alerts',     case when v_cfg.show_incidents then v_alerts end,
    'throughput_today',   case when v_cfg.show_throughput then v_through end,
    'last_robot_update',  v_last);
end
$$;


-- ------------------------------------------------------------
-- 3) Admin controls
-- ------------------------------------------------------------

create or replace function public.admin_set_status_page(
  p_site                 text,
  p_enabled              boolean default null,
  p_display_name         text    default null,
  p_blurb                text    default null,
  p_show_throughput      boolean default null,
  p_show_incidents       boolean default null,
  p_online_grace_seconds int     default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_row public.site_status_pages%rowtype;
begin
  if p_site is null or length(btrim(p_site)) = 0 then
    raise exception 'admin_set_status_page: p_site is required';
  end if;
  if not public.yf_has_role('admin', btrim(p_site)) then
    raise exception 'admin_set_status_page: requires admin role';
  end if;

  insert into public.site_status_pages as sp (
      site_id, enabled, display_name, blurb, show_throughput,
      show_incidents, online_grace_seconds, updated_at, updated_by)
  values (btrim(p_site), coalesce(p_enabled, false), p_display_name, p_blurb,
          coalesce(p_show_throughput, true), coalesce(p_show_incidents, true),
          coalesce(p_online_grace_seconds, 300),
          now(), coalesce(auth.email(), auth.uid()::text))
  on conflict (site_id) do update set
      enabled              = coalesce(excluded.enabled, sp.enabled),
      display_name         = coalesce(p_display_name, sp.display_name),
      blurb                = coalesce(p_blurb, sp.blurb),
      show_throughput      = coalesce(p_show_throughput, sp.show_throughput),
      show_incidents       = coalesce(p_show_incidents, sp.show_incidents),
      online_grace_seconds = coalesce(p_online_grace_seconds, sp.online_grace_seconds),
      updated_at           = now(),
      updated_by           = excluded.updated_by
  returning * into v_row;

  return row_to_json(v_row);
end
$$;

create or replace function public.admin_list_status_pages()
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_list_status_pages: requires admin role';
  end if;
  select coalesce(json_agg(row_to_json(x) order by x.site_id), '[]'::json)
    into v_result
    from (select site_id, enabled, display_name, blurb, show_throughput,
                 show_incidents, online_grace_seconds, updated_at, updated_by
            from public.site_status_pages) x;
  return v_result;
end
$$;

revoke execute on function public.public_fleet_status(text) from public;
revoke execute on function public.admin_set_status_page(text, boolean, text, text, boolean, boolean, int) from public, anon;
revoke execute on function public.admin_list_status_pages() from public, anon;

grant execute on function public.public_fleet_status(text) to anon, authenticated, service_role;
grant execute on function public.admin_set_status_page(text, boolean, text, text, boolean, boolean, int) to authenticated, service_role;
grant execute on function public.admin_list_status_pages() to authenticated, service_role;

select 'v0.16 PUBLIC STATUS: site_status_pages (enabled=false by default, no table grants); public_fleet_status() is anon and returns counts/percentages only; disabled and unknown sites are indistinguishable' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — unpublish and drop everything.
-- The first statement takes every page offline immediately, which is the
-- part you actually want in a hurry; the drops can follow at leisure.
-- ------------------------------------------------------------
-- update public.site_status_pages set enabled = false, updated_at = now();
--
-- drop function if exists public.admin_list_status_pages();
-- drop function if exists public.admin_set_status_page(text, boolean, text, text, boolean, boolean, int);
-- drop function if exists public.public_fleet_status(text);
-- drop table    if exists public.site_status_pages;
--
-- select 'ROLLED BACK 0016: public status pages removed' as result;
-- ============================================================
