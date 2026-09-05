-- ============================================================
-- YantraFleet 0012 — UTILIZATION + COST ROLLUPS (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql AND 0009_demo_sandbox.sql —    !!
-- !!  REQUIRES public.yf_can_read_site() (0009) and          !!
-- !!  public.yf_has_role() (0007). Adds a column to          !!
-- !!  robot_telemetry and a column + trigger to missions.    !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- "How much of the day is this fleet actually working?" and the inputs a
-- "your idle time is worth X per month" calculation needs.
--
-- ------------------------------------------------------------
-- HOW TIME-IN-STATE IS COMPUTED (and why it is honest)
-- ------------------------------------------------------------
-- `robot_telemetry` is a downsampled sample stream (0003: ~1 sample per
-- robot per 6s of sim time), not an event log of state transitions. So a
-- sample at ts with status S is credited with the time until the NEXT
-- sample for that robot — CAPPED at `p_max_gap_seconds`.
--
-- The cap is the whole point. Without it, a writer that was offline from
-- 18:00 to 06:00 would silently book twelve hours of "idle" and make the
-- utilization number a lie. With it, an outage shows up as *missing*
-- coverage: `covered_seconds` falls below the wall-clock window, and
-- `coverage_pct` says so. Percentages are always computed against
-- covered time, never against wall clock, so a gappy day reports
-- "78% active over the 4h we have data for", not "13% active".
--
-- Buckets (canonical statuses from 0002):
--   active   <- active
--   idle     <- idle, paused
--   charging <- charging
--   faulted  <- fault, estop, degraded
-- The exact per-status seconds are ALSO returned (`by_status`), so no
-- information is lost by the bucketing and the UI can re-cut it.
--
-- ------------------------------------------------------------
-- MONEY IS NOT IN THIS FILE
-- ------------------------------------------------------------
-- No currency and no rate is hardcoded in any SQL here. `site_cost_settings`
-- stores an operator-supplied assumption whose columns are all NULLABLE
-- with NO default: an unconfigured fleet reports `configured: false` and
-- the UI shows "set your hourly cost" rather than a fabricated number.
-- The rollup returns SECONDS and ROBOT-HOURS; the multiplication into
-- money happens in the UI layer, where the operator can see the
-- assumption next to the result.
--
-- Run after 0011_replay.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Throughput plumbing
-- ------------------------------------------------------------

-- Cumulative task counter alongside each telemetry sample. NULLABLE and
-- unset by today's writers: when it is null the per-period task delta is
-- reported as null (unknown), never as zero. sim/connector should start
-- stamping it — see docs/FEATURE-CONTRACTS.md.
alter table public.robot_telemetry
  add column if not exists tasks_done int;

-- Missions had a `state` (Queued -> Running -> Done) but no completion
-- timestamp, so "how many missions finished in this window" was
-- unanswerable. Same trigger pattern as 0011's incidents.closed_at:
-- existing writers get replayable throughput with no code change.
alter table public.missions
  add column if not exists completed_at timestamptz;
alter table public.missions
  add column if not exists updated_at   timestamptz;

update public.missions
   set completed_at = created_at
 where state = 'Done' and completed_at is null;
update public.missions
   set updated_at = coalesce(completed_at, created_at)
 where updated_at is null;

create or replace function public.yf_missions_stamp()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  if new.state = 'Done' then
    if new.completed_at is null then
      new.completed_at := now();
    end if;
  else
    new.completed_at := null;
  end if;
  return new;
end
$$;

drop trigger if exists yf_missions_stamp_trg on public.missions;
create trigger yf_missions_stamp_trg
  before insert or update on public.missions
  for each row execute function public.yf_missions_stamp();

create index if not exists missions_site_completed
  on public.missions (site_id, completed_at);


-- ------------------------------------------------------------
-- 2) The cost assumption (operator-supplied, never defaulted)
-- ------------------------------------------------------------

create table if not exists public.site_cost_settings (
  site_id                text primary key,
  currency               text,        -- ISO 4217 label chosen by the operator
  robot_hour_cost        numeric check (robot_hour_cost >= 0),
  operator_hour_cost     numeric check (operator_hour_cost >= 0),
  target_utilization_pct numeric check (target_utilization_pct between 0 and 100),
  shift_hours_per_day    numeric check (shift_hours_per_day > 0 and shift_hours_per_day <= 24),
  notes                  text,
  updated_at             timestamptz not null default now(),
  updated_by             text
);

alter table public.site_cost_settings enable row level security;

-- Readable by anyone with a role at the site; written only through the
-- admin RPC below (same shape as 0007's academy tables).
revoke insert, update, delete on public.site_cost_settings from anon, authenticated;
revoke all on public.site_cost_settings from anon;
grant select on public.site_cost_settings to authenticated;

drop policy if exists cost_settings_read on public.site_cost_settings;
create policy cost_settings_read on public.site_cost_settings
  for select to authenticated
  using (public.yf_has_role('operator', site_id));


-- ------------------------------------------------------------
-- 3) RPCs
-- ------------------------------------------------------------

create or replace function public.get_cost_settings(p_site text default 'BLR-DC1')
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_row public.site_cost_settings%rowtype;
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'get_cost_settings: requires operator role (or higher) at site %', p_site;
  end if;
  select * into v_row from public.site_cost_settings where site_id = p_site;
  if not found then
    return json_build_object(
      'site_id', p_site, 'configured', false,
      'currency', null, 'robot_hour_cost', null, 'operator_hour_cost', null,
      'target_utilization_pct', null, 'shift_hours_per_day', null,
      'notes', null, 'updated_at', null, 'updated_by', null);
  end if;
  return json_build_object(
    'site_id', v_row.site_id,
    'configured', (v_row.robot_hour_cost is not null or v_row.operator_hour_cost is not null),
    'currency', v_row.currency,
    'robot_hour_cost', v_row.robot_hour_cost,
    'operator_hour_cost', v_row.operator_hour_cost,
    'target_utilization_pct', v_row.target_utilization_pct,
    'shift_hours_per_day', v_row.shift_hours_per_day,
    'notes', v_row.notes,
    'updated_at', v_row.updated_at, 'updated_by', v_row.updated_by);
end
$$;

create or replace function public.admin_set_cost_settings(
  p_site                   text,
  p_currency               text    default null,
  p_robot_hour_cost        numeric default null,
  p_operator_hour_cost     numeric default null,
  p_target_utilization_pct numeric default null,
  p_shift_hours_per_day    numeric default null,
  p_notes                  text    default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_row public.site_cost_settings%rowtype;
begin
  if p_site is null or length(btrim(p_site)) = 0 then
    raise exception 'admin_set_cost_settings: p_site is required';
  end if;
  if not public.yf_has_role('admin', p_site) then
    raise exception 'admin_set_cost_settings: requires admin role';
  end if;

  insert into public.site_cost_settings as s (
      site_id, currency, robot_hour_cost, operator_hour_cost,
      target_utilization_pct, shift_hours_per_day, notes,
      updated_at, updated_by)
  values (p_site, p_currency, p_robot_hour_cost, p_operator_hour_cost,
          p_target_utilization_pct, p_shift_hours_per_day, p_notes,
          now(), coalesce(auth.email(), auth.uid()::text))
  on conflict (site_id) do update set
      -- NULL means "leave as is"; clearing a value is a separate,
      -- explicit act (pass a negative-free 0, or delete the row in SQL).
      currency               = coalesce(excluded.currency, s.currency),
      robot_hour_cost        = coalesce(excluded.robot_hour_cost, s.robot_hour_cost),
      operator_hour_cost     = coalesce(excluded.operator_hour_cost, s.operator_hour_cost),
      target_utilization_pct = coalesce(excluded.target_utilization_pct, s.target_utilization_pct),
      shift_hours_per_day    = coalesce(excluded.shift_hours_per_day, s.shift_hours_per_day),
      notes                  = coalesce(excluded.notes, s.notes),
      updated_at             = now(),
      updated_by             = excluded.updated_by
  returning * into v_row;

  return json_build_object(
    'site_id', v_row.site_id, 'configured', true,
    'currency', v_row.currency,
    'robot_hour_cost', v_row.robot_hour_cost,
    'operator_hour_cost', v_row.operator_hour_cost,
    'target_utilization_pct', v_row.target_utilization_pct,
    'shift_hours_per_day', v_row.shift_hours_per_day,
    'notes', v_row.notes,
    'updated_at', v_row.updated_at, 'updated_by', v_row.updated_by);
end
$$;

-- The rollup. Everything the utilization view and the ROI widget need.
create or replace function public.utilization_rollup(
  p_site            text,
  p_from            timestamptz default null,
  p_to              timestamptz default null,
  p_max_gap_seconds int         default 60)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_to      timestamptz := coalesce(p_to, now());
  v_from    timestamptz := coalesce(p_from, v_to - interval '24 hours');
  v_gap     int         := least(greatest(coalesce(p_max_gap_seconds, 60), 1), 3600);
  v_window  numeric;
  v_robots  json;
  v_fleet   json;
  v_through json;
  v_cost    json;
  v_missions bigint;
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'utilization_rollup: requires operator role (or higher) at site %', p_site;
  end if;
  if v_from >= v_to then
    raise exception 'utilization_rollup: p_from (%) must be before p_to (%)', v_from, v_to;
  end if;
  v_window := extract(epoch from (v_to - v_from));

  with samples as (
    select t.robot_id, t.ts, t.status, t.tasks_done,
           lead(t.ts) over (partition by t.robot_id order by t.ts) as next_ts
      from public.robot_telemetry t
     where t.site_id = p_site and t.ts >= v_from and t.ts <= v_to
  ),
  spans as (
    select s.robot_id, s.ts, s.status, s.tasks_done,
           extract(epoch from least(coalesce(s.next_ts, v_to) - s.ts,
                                    make_interval(secs => v_gap)))::numeric as secs
      from samples s
  ),
  per_robot as (
    select sp.robot_id,
           sum(sp.secs) filter (where sp.status = 'active')                        as active_seconds,
           sum(sp.secs) filter (where sp.status in ('idle', 'paused'))             as idle_seconds,
           sum(sp.secs) filter (where sp.status = 'charging')                      as charging_seconds,
           sum(sp.secs) filter (where sp.status in ('fault', 'estop', 'degraded')) as faulted_seconds,
           sum(sp.secs) filter (where sp.status is null
                                   or sp.status not in ('active', 'idle', 'paused',
                                                        'charging', 'fault', 'estop',
                                                        'degraded'))               as other_seconds,
           sum(sp.secs)                                                            as covered_seconds,
           count(*)                                                                as samples,
           min(sp.ts)                                                              as first_ts,
           max(sp.ts)                                                              as last_ts,
           max(sp.tasks_done) - min(sp.tasks_done)                                 as tasks_done_delta
      from spans sp
     group by sp.robot_id
  ),
  by_status as (
    select sp.robot_id,
           coalesce(json_object_agg(sp.status, sp.status_seconds), '{}'::json) as by_status
      from (select robot_id, coalesce(status, 'unknown') as status,
                   round(sum(secs), 1) as status_seconds
              from spans group by robot_id, coalesce(status, 'unknown')) sp
     group by sp.robot_id
  )
  select coalesce(json_agg(row_to_json(x) order by x.robot_id), '[]'::json)
    into v_robots
    from (select p.robot_id,
                 round(coalesce(p.active_seconds, 0), 1)   as active_seconds,
                 round(coalesce(p.idle_seconds, 0), 1)     as idle_seconds,
                 round(coalesce(p.charging_seconds, 0), 1) as charging_seconds,
                 round(coalesce(p.faulted_seconds, 0), 1)  as faulted_seconds,
                 round(coalesce(p.other_seconds, 0), 1)    as other_seconds,
                 round(coalesce(p.covered_seconds, 0), 1)  as covered_seconds,
                 case when coalesce(p.covered_seconds, 0) > 0
                      then round(100 * coalesce(p.active_seconds, 0) / p.covered_seconds, 2)
                      end                                  as utilization_pct,
                 case when v_window > 0
                      then round(100 * coalesce(p.covered_seconds, 0) / v_window, 2)
                      end                                  as coverage_pct,
                 p.samples, p.first_ts, p.last_ts,
                 p.tasks_done_delta,
                 b.by_status
            from per_robot p
            left join by_status b on b.robot_id = p.robot_id) x;

  select json_build_object(
           'robot_count',      coalesce(count(*), 0),
           'active_seconds',   round(coalesce(sum((e ->> 'active_seconds')::numeric), 0), 1),
           'idle_seconds',     round(coalesce(sum((e ->> 'idle_seconds')::numeric), 0), 1),
           'charging_seconds', round(coalesce(sum((e ->> 'charging_seconds')::numeric), 0), 1),
           'faulted_seconds',  round(coalesce(sum((e ->> 'faulted_seconds')::numeric), 0), 1),
           'other_seconds',    round(coalesce(sum((e ->> 'other_seconds')::numeric), 0), 1),
           'covered_seconds',  round(coalesce(sum((e ->> 'covered_seconds')::numeric), 0), 1),
           'active_robot_hours',   round(coalesce(sum((e ->> 'active_seconds')::numeric), 0) / 3600, 3),
           'idle_robot_hours',     round(coalesce(sum((e ->> 'idle_seconds')::numeric), 0) / 3600, 3),
           'charging_robot_hours', round(coalesce(sum((e ->> 'charging_seconds')::numeric), 0) / 3600, 3),
           'faulted_robot_hours',  round(coalesce(sum((e ->> 'faulted_seconds')::numeric), 0) / 3600, 3),
           'covered_robot_hours',  round(coalesce(sum((e ->> 'covered_seconds')::numeric), 0) / 3600, 3),
           'utilization_pct',
             case when coalesce(sum((e ->> 'covered_seconds')::numeric), 0) > 0
                  then round(100 * sum((e ->> 'active_seconds')::numeric)
                                 / sum((e ->> 'covered_seconds')::numeric), 2) end,
           'coverage_pct',
             case when v_window > 0 and count(*) > 0
                  then round(100 * sum((e ->> 'covered_seconds')::numeric)
                                 / (v_window * count(*)), 2) end)
    into v_fleet
    from json_array_elements(v_robots) e;

  select count(*) into v_missions
    from public.missions
   where site_id = p_site
     and completed_at is not null
     and completed_at >= v_from and completed_at <= v_to;

  select json_build_object(
           'completed_missions', coalesce(v_missions, 0),
           'tasks_done_delta',
             (select sum((e ->> 'tasks_done_delta')::numeric)
                from json_array_elements(v_robots) e
               where e ->> 'tasks_done_delta' is not null),
           'tasks_done_total',
             (select coalesce(sum(r.tasks_done), 0) from public.robots r
               where r.site_id = p_site),
           'tasks_per_hour',
             case when v_window > 0
                  then round((coalesce(v_missions, 0) * 3600.0 / v_window)::numeric, 3) end)
    into v_through;

  v_cost := public.get_cost_settings(p_site);

  return json_build_object(
    'site_id', p_site,
    'from', v_from, 'to', v_to,
    'window_seconds', round(v_window, 1),
    'max_gap_seconds', v_gap,
    'generated_at', now(),
    'robots', v_robots,
    'fleet', v_fleet,
    'throughput', v_through,
    'cost', v_cost);
end
$$;

-- Day-by-day series for the trend chart. Same span/cap arithmetic, one
-- row per calendar day in the caller's chosen bucket.
create or replace function public.utilization_series(
  p_site            text,
  p_from            timestamptz default null,
  p_to              timestamptz default null,
  p_bucket          text        default 'day',
  p_max_gap_seconds int         default 60)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_to     timestamptz := coalesce(p_to, now());
  v_from   timestamptz := coalesce(p_from, v_to - interval '7 days');
  v_gap    int         := least(greatest(coalesce(p_max_gap_seconds, 60), 1), 3600);
  v_bucket text        := lower(coalesce(p_bucket, 'day'));
  v_rows   json;
begin
  if not public.yf_can_read_site(p_site) then
    raise exception 'utilization_series: requires operator role (or higher) at site %', p_site;
  end if;
  if v_bucket not in ('hour', 'day', 'week') then
    raise exception 'utilization_series: p_bucket must be ''hour'', ''day'' or ''week'' (got %)', p_bucket;
  end if;
  if v_from >= v_to then
    raise exception 'utilization_series: p_from (%) must be before p_to (%)', v_from, v_to;
  end if;

  with samples as (
    select t.robot_id, t.ts, t.status,
           lead(t.ts) over (partition by t.robot_id order by t.ts) as next_ts
      from public.robot_telemetry t
     where t.site_id = p_site and t.ts >= v_from and t.ts <= v_to
  ),
  spans as (
    select date_trunc(v_bucket, s.ts) as bucket, s.status,
           extract(epoch from least(coalesce(s.next_ts, v_to) - s.ts,
                                    make_interval(secs => v_gap)))::numeric as secs
      from samples s
  )
  select coalesce(json_agg(row_to_json(x) order by x.bucket), '[]'::json)
    into v_rows
    from (select sp.bucket,
                 round(coalesce(sum(sp.secs) filter (where sp.status = 'active'), 0), 1)                        as active_seconds,
                 round(coalesce(sum(sp.secs) filter (where sp.status in ('idle', 'paused')), 0), 1)             as idle_seconds,
                 round(coalesce(sum(sp.secs) filter (where sp.status = 'charging'), 0), 1)                      as charging_seconds,
                 round(coalesce(sum(sp.secs) filter (where sp.status in ('fault', 'estop', 'degraded')), 0), 1) as faulted_seconds,
                 round(sum(sp.secs), 1)                                                                          as covered_seconds,
                 case when sum(sp.secs) > 0
                      then round(100 * coalesce(sum(sp.secs) filter (where sp.status = 'active'), 0)
                                     / sum(sp.secs), 2) end                                                      as utilization_pct
            from spans sp
           group by sp.bucket) x;

  return json_build_object(
    'site_id', p_site, 'from', v_from, 'to', v_to,
    'bucket', v_bucket, 'max_gap_seconds', v_gap,
    'generated_at', now(), 'buckets', v_rows);
end
$$;

revoke execute on function public.get_cost_settings(text)                                              from public;
revoke execute on function public.utilization_rollup(text, timestamptz, timestamptz, int)              from public;
revoke execute on function public.utilization_series(text, timestamptz, timestamptz, text, int)        from public;
revoke execute on function public.admin_set_cost_settings(text, text, numeric, numeric, numeric, numeric, text) from public, anon;

grant execute on function public.get_cost_settings(text)                                   to anon, authenticated, service_role;
grant execute on function public.utilization_rollup(text, timestamptz, timestamptz, int)   to anon, authenticated, service_role;
grant execute on function public.utilization_series(text, timestamptz, timestamptz, text, int) to anon, authenticated, service_role;
grant execute on function public.admin_set_cost_settings(text, text, numeric, numeric, numeric, numeric, text) to authenticated, service_role;

select 'v0.12 UTILIZATION: robot_telemetry.tasks_done + missions.completed_at (trigger-stamped); site_cost_settings (operator-supplied, no defaults); utilization_rollup()/utilization_series()/get_cost_settings()/admin_set_cost_settings()' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the utilization objects.
-- The added columns are LEFT IN PLACE by default: missions.completed_at
-- is real history nothing else records. Uncomment the final ALTERs only
-- if you genuinely want it gone.
-- ------------------------------------------------------------
-- drop function if exists public.utilization_series(text, timestamptz, timestamptz, text, int);
-- drop function if exists public.utilization_rollup(text, timestamptz, timestamptz, int);
-- drop function if exists public.admin_set_cost_settings(text, text, numeric, numeric, numeric, numeric, text);
-- drop function if exists public.get_cost_settings(text);
-- drop policy   if exists cost_settings_read on public.site_cost_settings;
-- drop table    if exists public.site_cost_settings;
-- drop trigger  if exists yf_missions_stamp_trg on public.missions;
-- drop function if exists public.yf_missions_stamp();
-- drop index    if exists public.missions_site_completed;
--
-- -- destructive, opt-in-within-the-rollback:
-- -- alter table public.missions        drop column if exists completed_at;
-- -- alter table public.missions        drop column if exists updated_at;
-- -- alter table public.robot_telemetry drop column if exists tasks_done;
--
-- select 'ROLLED BACK 0012: utilization RPCs + cost settings removed' as result;
-- ============================================================
