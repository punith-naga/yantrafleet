-- ============================================================
-- Yantrika 0011 — TIME-TRAVEL REPLAY (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql AND 0009_demo_sandbox.sql —    !!
-- !!  REQUIRES public.yf_can_read_site() (defined in 0009),  !!
-- !!  which in turn REQUIRES yf_has_role() from 0007.        !!
-- !!  Adds columns + a trigger to public.incidents; read the !!
-- !!  "WHAT WAS ALREADY QUERYABLE" note before applying.     !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Scrub the fleet backwards in time: "every robot's pose plus the open
-- incidents as of 14:32 last Tuesday, for site BLR-DC1", in one call.
--
-- ------------------------------------------------------------
-- WHAT WAS ALREADY QUERYABLE, AND WHAT WAS MISSING
-- ------------------------------------------------------------
-- Already there (0003 + 0005): `robot_telemetry(robot_id, ts, battery,
-- speed, motor_temp, status, pos, site_id)` with indexes on
-- (robot_id, ts desc) and (site_id, ts desc). Poses at time T were
-- therefore *derivable*, but only with a per-robot DISTINCT ON that the
-- existing indexes do not support well — hence the new composite index
-- below.
--
-- MISSING, and added here: incidents recorded `created_at` and a `state`
-- but NOTHING said WHEN an incident stopped being open. `dur` is a
-- minutes integer written by the detector on resolve, which is neither
-- reliable nor present for incidents closed by hand. Without a close
-- timestamp, "open as of T" is unanswerable — you can only ever ask
-- "open NOW". So this migration adds `incidents.closed_at` (+
-- `incidents.updated_at`) and a BEFORE trigger that stamps it on the
-- Open -> not-Open transition. The trigger means EVERY existing writer
-- (detector, console PATCH, service_role scripts) starts producing
-- replayable history with zero code changes; nobody has to remember to
-- set the column.
--
-- Existing rows are backfilled from `created_at + dur minutes` where the
-- detector left a plausible duration, else from `created_at`. Backfilled
-- values are approximate by construction — that is recorded in the data
-- itself via `incidents.closed_at_estimated`.
--
-- Run after 0010_share_links.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Close timestamps on incidents
-- ------------------------------------------------------------

alter table public.incidents
  add column if not exists closed_at           timestamptz;
alter table public.incidents
  add column if not exists closed_at_estimated boolean not null default false;
alter table public.incidents
  add column if not exists updated_at          timestamptz;

-- One-shot backfill for rows that closed before this migration existed.
update public.incidents
   set closed_at = created_at
                 + make_interval(mins => greatest(coalesce(dur, 0), 0)),
       closed_at_estimated = true
 where state <> 'Open' and closed_at is null;

update public.incidents
   set updated_at = coalesce(closed_at, created_at)
 where updated_at is null;

create or replace function public.yf_incidents_stamp()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  if new.state = 'Open' then
    -- Re-opened: it is open again, so it has no close time.
    new.closed_at := null;
    new.closed_at_estimated := false;
  elsif new.closed_at is null then
    new.closed_at := now();
    new.closed_at_estimated := false;
  end if;
  return new;
end
$$;

drop trigger if exists yf_incidents_stamp_trg on public.incidents;
create trigger yf_incidents_stamp_trg
  before insert or update on public.incidents
  for each row execute function public.yf_incidents_stamp();

-- Replay's hot paths.
create index if not exists incidents_site_closed
  on public.incidents (site_id, closed_at);
create index if not exists robot_telemetry_site_robot_ts
  on public.robot_telemetry (site_id, robot_id, ts desc);


-- ------------------------------------------------------------
-- 2) Replay RPCs
-- ------------------------------------------------------------

-- Everything the scrubber needs to size itself: the oldest and newest
-- moment this site has any recoverable state for.
create or replace function public.replay_time_range(p_site text default 'BLR-DC1')
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_tel_from  timestamptz;
  v_tel_to    timestamptz;
  v_samples   bigint;
  v_robots    bigint;
  v_inc_from  timestamptz;
  v_inc_to    timestamptz;
  v_incidents bigint;
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'replay_time_range: requires operator role (or higher) at site %', p_site;
  end if;

  select min(ts), max(ts), count(*), count(distinct robot_id)
    into v_tel_from, v_tel_to, v_samples, v_robots
    from public.robot_telemetry where site_id = p_site;

  select min(created_at), max(greatest(created_at, coalesce(closed_at, created_at))),
         count(*)
    into v_inc_from, v_inc_to, v_incidents
    from public.incidents where site_id = p_site;

  return json_build_object(
    'site_id', p_site,
    'from', least(v_tel_from, v_inc_from),
    'to',   greatest(v_tel_to, v_inc_to),
    'telemetry_from', v_tel_from,
    'telemetry_to',   v_tel_to,
    'incidents_from', v_inc_from,
    'incidents_to',   v_inc_to,
    'sample_count',   coalesce(v_samples, 0),
    'robot_count',    coalesce(v_robots, 0),
    'incident_count', coalesce(v_incidents, 0),
    'now', now(),
    'has_history', (v_samples > 0 or v_incidents > 0));
end
$$;

-- The single call the scrubber makes on every tick.
--
-- p_max_age_seconds: how stale a telemetry sample may be and still count
-- as "where the robot was at T". Beyond it the robot is reported with
-- `stale = true` (its last known pose, honestly labelled) instead of
-- being silently dropped or silently teleported.
--
-- CAVEAT, stated in the contract because the UI must not pretend
-- otherwise: `alerts` carries no ack timestamp (0001), so `ack` in the
-- alerts array is the CURRENT value, not its value at T. Alert
-- membership is point-in-time correct; the ack flag is not. Commands
-- ARE point-in-time correct: `decided_at` lets us report a command that
-- was still pending at T as pending.
create or replace function public.replay_state_at(
  p_site            text,
  p_at              timestamptz,
  p_max_age_seconds int default 120,
  p_limit           int default 200)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_at        timestamptz := coalesce(p_at, now());
  v_max_age   int         := greatest(coalesce(p_max_age_seconds, 120), 1);
  v_limit     int         := least(greatest(coalesce(p_limit, 200), 1), 1000);
  v_robots    json;
  v_incidents json;
  v_alerts    json;
  v_commands  json;
  v_counts    json;
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'replay_state_at: requires operator role (or higher) at site %', p_site;
  end if;

  with last_sample as (
    select distinct on (t.robot_id)
           t.robot_id, t.ts, t.pos, t.battery, t.speed, t.motor_temp, t.status
      from public.robot_telemetry t
     where t.site_id = p_site and t.ts <= v_at
     order by t.robot_id, t.ts desc
  )
  select coalesce(json_agg(row_to_json(x) order by x.robot_id), '[]'::json)
    into v_robots
    from (select s.robot_id, s.ts, s.pos, s.battery, s.speed,
                 s.motor_temp, s.status,
                 (v_at - s.ts) > make_interval(secs => v_max_age) as stale,
                 floor(extract(epoch from (v_at - s.ts)))::int as age_seconds,
                 r.vendor
            from last_sample s
            left join public.robots r
              on r.id = s.robot_id and r.site_id = p_site
           limit v_limit) x;

  select coalesce(json_agg(row_to_json(x) order by x.created_at desc), '[]'::json)
    into v_incidents
    from (select i.id, i.sev, i.title, i.src, i.tlabel, i.created_at,
                 i.closed_at, i.closed_at_estimated, i.impact
            from public.incidents i
           where i.site_id = p_site
             and i.created_at <= v_at
             and (i.closed_at is null or i.closed_at > v_at)
           order by i.created_at desc
           limit v_limit) x;

  select coalesce(json_agg(row_to_json(x) order by x.created_at desc), '[]'::json)
    into v_alerts
    from (select a.id, a.sev, a.msg, a.src, a.tlabel, a.created_at,
                 a.ack as ack_now
            from public.alerts a
           where a.site_id = p_site and a.created_at <= v_at
           order by a.created_at desc
           limit least(v_limit, 100)) x;

  select coalesce(json_agg(row_to_json(x) order by x.created_at desc), '[]'::json)
    into v_commands
    from (select c.id, c.robot_id, c.cmd, c.requested_by, c.created_at,
                 case when c.decided_at is null or c.decided_at > v_at
                      then 'pending' else c.status end as status_at,
                 c.status as status_now
            from public.commands c
           where c.site_id = p_site and c.created_at <= v_at
           order by c.created_at desc
           limit least(v_limit, 100)) x;

  select json_build_object(
           'robots', json_array_length(v_robots),
           'robots_stale', (select count(*) from json_array_elements(v_robots) e
                             where (e ->> 'stale')::boolean),
           'open_incidents', json_array_length(v_incidents),
           'alerts', json_array_length(v_alerts),
           'commands', json_array_length(v_commands))
    into v_counts;

  return json_build_object(
    'site_id', p_site,
    'at', v_at,
    'max_age_seconds', v_max_age,
    'generated_at', now(),
    'counts', v_counts,
    'robots', v_robots,
    'open_incidents', v_incidents,
    'alerts', v_alerts,
    'commands', v_commands);
end
$$;

-- One robot's track between two moments — the "follow this robot" overlay
-- the scrubber draws on top of replay_state_at.
create or replace function public.replay_robot_track(
  p_site   text,
  p_robot  text,
  p_from   timestamptz,
  p_to     timestamptz,
  p_limit  int default 2000)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_points json;
  v_limit  int := least(greatest(coalesce(p_limit, 2000), 1), 20000);
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'replay_robot_track: requires operator role (or higher) at site %', p_site;
  end if;
  if p_robot is null or length(btrim(p_robot)) = 0 then
    raise exception 'replay_robot_track: p_robot is required';
  end if;

  select coalesce(json_agg(row_to_json(x) order by x.ts), '[]'::json)
    into v_points
    from (select t.ts, t.pos, t.battery, t.speed, t.motor_temp, t.status
            from public.robot_telemetry t
           where t.site_id = p_site
             and t.robot_id = p_robot
             and t.ts >= coalesce(p_from, '-infinity'::timestamptz)
             and t.ts <= coalesce(p_to, now())
           order by t.ts
           limit v_limit) x;

  return json_build_object(
    'site_id', p_site, 'robot_id', p_robot,
    'from', p_from, 'to', coalesce(p_to, now()),
    'point_count', json_array_length(v_points),
    'truncated', (json_array_length(v_points) >= v_limit),
    'points', v_points);
end
$$;

revoke execute on function public.replay_time_range(text)                                from public;
revoke execute on function public.replay_state_at(text, timestamptz, int, int)           from public;
revoke execute on function public.replay_robot_track(text, text, timestamptz, timestamptz, int) from public;

-- anon is granted execute because a DEMO sandbox visitor must be able to
-- scrub their own sandbox: yf_can_read_site() resolves their demo token
-- and refuses every other site. Without a live demo token, an anon call
-- raises — the grant alone authorises nothing.
grant execute on function public.replay_time_range(text)                                to anon, authenticated, service_role;
grant execute on function public.replay_state_at(text, timestamptz, int, int)           to anon, authenticated, service_role;
grant execute on function public.replay_robot_track(text, text, timestamptz, timestamptz, int) to anon, authenticated, service_role;

select 'v0.11 REPLAY: incidents.closed_at/updated_at + stamping trigger; replay_state_at()/replay_time_range()/replay_robot_track(); index robot_telemetry(site_id, robot_id, ts desc)' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the replay objects.
-- The incidents columns are LEFT IN PLACE by default: dropping them
-- destroys real close-time history that nothing else records. Uncomment
-- the final two ALTERs only if you genuinely want that history gone.
-- ------------------------------------------------------------
-- drop function if exists public.replay_robot_track(text, text, timestamptz, timestamptz, int);
-- drop function if exists public.replay_state_at(text, timestamptz, int, int);
-- drop function if exists public.replay_time_range(text);
-- drop trigger  if exists yf_incidents_stamp_trg on public.incidents;
-- drop function if exists public.yf_incidents_stamp();
-- drop index if exists public.incidents_site_closed;
-- drop index if exists public.robot_telemetry_site_robot_ts;
--
-- -- destructive, opt-in-within-the-rollback:
-- -- alter table public.incidents drop column if exists closed_at;
-- -- alter table public.incidents drop column if exists closed_at_estimated;
-- -- alter table public.incidents drop column if exists updated_at;
--
-- select 'ROLLED BACK 0011: replay RPCs removed (incidents.closed_at kept)' as result;
-- ============================================================
