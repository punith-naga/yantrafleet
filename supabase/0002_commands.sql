-- ============================================================
-- Yantrika v0.2 migration — command queue with human approval
-- + canonical robot-status vocabulary.
-- Run AFTER fleetmind_schema.sql (0001). Idempotent.
-- ============================================================

-- 1) Canonical status vocabulary (see core/yantracore/status.py)
update public.robots set status = case status
  when 'working'     then 'active'
  when 'moving'      then 'active'
  when 'to_charger'  then 'active'
  when 'driving'     then 'active'
  when 'safety_stop' then 'estop'
  when 'estopped'    then 'estop'
  else status end
where status not in ('active','idle','charging','paused','estop','degraded','fault');

update public.robots set status='idle'
where status not in ('active','idle','charging','paused','estop','degraded','fault');

alter table public.robots drop constraint if exists robots_status_canon;
alter table public.robots add constraint robots_status_canon
  check (status in ('active','idle','charging','paused','estop','degraded','fault'));

-- 2) Operator command queue (human-in-the-loop gate)
create table if not exists public.commands (
  id uuid primary key default gen_random_uuid(),
  robot_id text not null,
  cmd text not null check (cmd in ('pause','resume','charge','estop')),
  params jsonb not null default '{}',
  status text not null default 'pending'
    check (status in ('pending','approved','rejected','executed','failed')),
  requested_by text not null default 'console',
  decided_by text,
  note text,
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  executed_at timestamptz
);

create index if not exists commands_status_idx on public.commands (status, created_at);

alter table public.commands enable row level security;
drop policy if exists demo_all on public.commands;
create policy demo_all on public.commands
  for all to anon, authenticated using (true) with check (true);

select 'v0.2 ready: commands table + canonical status constraint' as result;
