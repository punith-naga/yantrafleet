-- ============================================================
-- Yantrika v0.3 migration — telemetry history for replay/analytics.
-- Run after 0002_commands.sql. Idempotent.
-- Free-tier discipline: writers downsample (default: 1 sample/robot per
-- ~6s sim time) and a purge function keeps 72h of raw history.
-- ============================================================

create table if not exists public.robot_telemetry (
  id bigint generated always as identity primary key,
  robot_id text not null,
  ts timestamptz not null default now(),
  battery numeric,
  speed numeric,
  motor_temp numeric,
  status text,
  pos jsonb
);

create index if not exists robot_telemetry_robot_ts
  on public.robot_telemetry (robot_id, ts desc);

alter table public.robot_telemetry enable row level security;
drop policy if exists demo_all on public.robot_telemetry;
create policy demo_all on public.robot_telemetry
  for all to anon, authenticated using (true) with check (true);

-- Retention: call manually or via pg_cron/scheduled edge function later.
create or replace function public.purge_old_telemetry(keep interval default '72 hours')
returns bigint language sql as $$
  with gone as (
    delete from public.robot_telemetry where ts < now() - keep returning 1)
  select count(*) from gone;
$$;

select 'v0.3 ready: robot_telemetry + purge_old_telemetry()' as result;
