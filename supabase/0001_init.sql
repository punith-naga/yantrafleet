-- ============================================================
-- Yantrika base schema (v0.1) — robots, alerts, incidents,
-- missions, fleet_meta. Run FIRST, then 0002, then 0003.
-- Idempotent. DEMO-OPEN policies: lock down before real data.
-- (Checked in at v0.4 so the incidents contract is enforceable —
-- previously this DDL lived only in the Supabase project.)
-- ============================================================

create table if not exists public.robots (
  id text primary key,
  vendor text not null default '',
  status text not null default 'idle',
  battery numeric not null default 100,
  pos jsonb not null default '[0,0]',
  speed numeric not null default 0,
  task_kind text,
  health numeric not null default 95,
  motor_temp numeric not null default 45,
  tasks_done int not null default 0,
  fault_msg text,
  updated_at timestamptz not null default now()
);

create table if not exists public.alerts (
  id text primary key,
  sev text not null,
  msg text not null,
  src text not null default 'fleet',
  tlabel text not null default '',
  ack boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists public.incidents (
  id text primary key,
  sev text not null,
  title text not null,
  src text not null,
  tlabel text not null default '',
  state text not null default 'Open',
  impact text default '',
  rca text default '',
  fix text default '',
  dur int default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.missions (
  id text primary key,
  name text not null,
  robots jsonb not null default '[]',
  state text not null default 'Queued',
  prog int not null default 0,
  eta text default '—',
  created_at timestamptz not null default now()
);

create table if not exists public.fleet_meta (
  id int primary key,
  writer_id text,
  sim_min numeric default 0,
  throughput int default 0,
  updated_at timestamptz not null default now()
);

alter table public.robots     enable row level security;
alter table public.alerts     enable row level security;
alter table public.incidents  enable row level security;
alter table public.missions   enable row level security;
alter table public.fleet_meta enable row level security;

do $$
declare t text;
begin
  foreach t in array array['robots','alerts','incidents','missions','fleet_meta'] loop
    execute format('drop policy if exists demo_all on public.%I', t);
    execute format('create policy demo_all on public.%I for all to anon, authenticated using (true) with check (true)', t);
  end loop;
end $$;

insert into public.fleet_meta (id) values (1) on conflict (id) do nothing;

select 'Yantrika base schema ready' as result;
