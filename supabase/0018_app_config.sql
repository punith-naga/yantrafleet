-- ============================================================
-- Yantrika 0018 — ADMIN RUNTIME CONFIG PANEL (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql — REQUIRES yf_has_role/yf_rank !!
-- !!  (public.yf_has_role('admin') gates both RPCs below).   !!
-- !!  A vanilla 0001-0005 (demo-open) or 0006 (hardened,     !!
-- !!  no roles) project does not have yf_has_role yet — do   !!
-- !!  not run this migration until 0007 has been applied.    !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Sibling of 0008_app_settings.sql, split into its own table on purpose:
-- 0008 holds 8 CREDENTIALS (masked on read — a raw GET can never see them,
-- only the two RPCs, and even those only show the last 4 chars). This
-- migration holds operational TUNABLES — a site id, poll intervals, a
-- battery-alert percentage, an MQTT host/port/topic/user/pass — none of
-- which are secrets in the "must never be shown in full" sense. An admin
-- editing "battery threshold = 20" should see "20", not "****20", so
-- admin_list_config() below returns values UNMASKED. (CONNECTOR_MQTT_
-- PASSWORD is arguably credential-shaped, but the task that created this
-- table explicitly grouped it with the other connector tunables rather
-- than with 0008's secrets, so it lives here, unmasked, like its siblings.
-- If that changes later, move the one key into 0008's CHECK/RPC lists —
-- nothing else has to change.)
--
-- Lets an `admin` tune 16 operational knobs from the console UI, live,
-- with no deploy-script edit and no instance restart:
--   YANTRA_SITE_ID
--   SARATHI_LOW_BATTERY_THRESHOLD
--   DETECTOR_PENDING_POLLS, DETECTOR_CLEAR_POLLS, DETECTOR_REOPEN_WINDOW,
--     DETECTOR_STALE_POLLS, DETECTOR_WINDOW_HOURS, DETECTOR_INTERVAL
--   NOTIFIER_INTERVAL
--   SIM_INTERVAL
--   CONNECTOR_BATTERY_THRESHOLD, CONNECTOR_MQTT_HOST, CONNECTOR_MQTT_PORT,
--     CONNECTOR_MQTT_TOPIC, CONNECTOR_MQTT_USERNAME, CONNECTOR_MQTT_PASSWORD
-- SUPABASE_URL / SUPABASE_KEY and the Postgres URL `yantraops migrate`
-- itself uses stay bootstrap-only (env-var/flag-only forever) — nothing
-- can source its own database connection from a row inside that same
-- database, so they are deliberately NOT in this table (same principle
-- as 0008's SUPABASE_URL/SUPABASE_KEY/REPO_URL exclusion).
--
-- Design principle carried over from 0008: the hardcoded default (in the
-- consuming service's own source, documented per-key below) stays the
-- fallback forever. A row here OVERRIDES the default/CLI-flag while
-- present; deleting the row (or saving an empty value, which this
-- migration's RPC treats as a delete) reverts to whatever the service
-- would otherwise have used — a CLI flag, if one was explicitly passed,
-- else its own hardcoded default. A deployment that never touches the
-- panel behaves exactly as it does today.
--
-- Independent of 0006/0007's table hardening: this file is purely
-- additive (no existing policy is replaced), so its rollback block
-- (bottom) only needs to drop what it created.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Table: app_config
-- ------------------------------------------------------------

create table if not exists public.app_config (
  key        text primary key
             check (key in (
               'YANTRA_SITE_ID',
               'SARATHI_LOW_BATTERY_THRESHOLD',
               'DETECTOR_PENDING_POLLS','DETECTOR_CLEAR_POLLS',
               'DETECTOR_REOPEN_WINDOW','DETECTOR_STALE_POLLS',
               'DETECTOR_WINDOW_HOURS','DETECTOR_INTERVAL',
               'NOTIFIER_INTERVAL',
               'SIM_INTERVAL',
               'CONNECTOR_BATTERY_THRESHOLD','CONNECTOR_MQTT_HOST',
               'CONNECTOR_MQTT_PORT','CONNECTOR_MQTT_TOPIC',
               'CONNECTOR_MQTT_USERNAME','CONNECTOR_MQTT_PASSWORD'
             )),
  value      text not null,             -- always stored as text; typed/validated per-key in the RPC below
  updated_at timestamptz not null default now(),
  updated_by text                        -- auth.email() of the admin who last wrote it
);

alter table public.app_config enable row level security;

-- Same shape as 0008_app_settings: no select/insert/update/delete policy
-- for authenticated (not even admins) — the only way in or out for a
-- signed-in human is the two SECURITY DEFINER RPCs below. service_role
-- continues to read the table directly (bypasses RLS, no explicit grant
-- needed — matches every other table in this schema).
revoke all on public.app_config from anon, authenticated;


-- ------------------------------------------------------------
-- 2) Validation helper (numeric keys only reject non-numeric text)
-- ------------------------------------------------------------

-- Keys whose value must parse as a number; everything else in the
-- allowlist is a free-form string (site id, mqtt host/topic/user/pass).
-- Kept as a SQL function (not inlined) so admin_set_config below stays
-- readable, and so a future numeric key only needs to join this array.
create or replace function public._yf_config_is_numeric_key(p_key text)
returns boolean
language sql immutable
set search_path = ''
as $$
  select p_key in (
    'SARATHI_LOW_BATTERY_THRESHOLD',
    'DETECTOR_PENDING_POLLS','DETECTOR_CLEAR_POLLS',
    'DETECTOR_REOPEN_WINDOW','DETECTOR_STALE_POLLS',
    'DETECTOR_WINDOW_HOURS','DETECTOR_INTERVAL',
    'NOTIFIER_INTERVAL','SIM_INTERVAL',
    'CONNECTOR_BATTERY_THRESHOLD','CONNECTOR_MQTT_PORT'
  )
$$;

-- Left without an explicit revoke/grant, same as yf_rank in 0007 and
-- _yf_mask_setting in 0008 — a pure, argument-only predicate with no
-- access to secret/config state, so PUBLIC execute is harmless.


-- ------------------------------------------------------------
-- 3) RPCs (SECURITY DEFINER, admin-only) — values NOT masked
-- ------------------------------------------------------------

-- Always returns all 16 known keys (not just ones with a row), so the
-- console can render a fixed config form without a first-run special
-- case — same convention as admin_list_settings(). Unlike that RPC,
-- "masked_value" here is the value AS STORED: these are tunables, not
-- credentials.
create or replace function public.admin_list_config()
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_result json;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_list_config: requires admin role';
  end if;
  select coalesce(json_agg(row_to_json(t) order by t.key), '[]'::json)
    into v_result
    from (
      select k.key,
             (c.key is not null) as configured,
             c.value,
             c.updated_at, c.updated_by
        from unnest(array[
          'YANTRA_SITE_ID',
          'SARATHI_LOW_BATTERY_THRESHOLD',
          'DETECTOR_PENDING_POLLS','DETECTOR_CLEAR_POLLS',
          'DETECTOR_REOPEN_WINDOW','DETECTOR_STALE_POLLS',
          'DETECTOR_WINDOW_HOURS','DETECTOR_INTERVAL',
          'NOTIFIER_INTERVAL',
          'SIM_INTERVAL',
          'CONNECTOR_BATTERY_THRESHOLD','CONNECTOR_MQTT_HOST',
          'CONNECTOR_MQTT_PORT','CONNECTOR_MQTT_TOPIC',
          'CONNECTOR_MQTT_USERNAME','CONNECTOR_MQTT_PASSWORD']) as k(key)
        left join public.app_config c on c.key = k.key
    ) t;
  return v_result;
end
$$;

-- p_value null/empty => DELETE the row (explicit "revert to the
-- service's own CLI-flag/hardcoded default" action), same convention as
-- admin_set_setting(). Numeric keys (see _yf_config_is_numeric_key) must
-- parse as a Postgres numeric or the write is rejected with a clear
-- error instead of silently storing text a consumer can't parse either.
create or replace function public.admin_set_config(
  p_key   text,
  p_value text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.app_config%rowtype;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_set_config: requires admin role';
  end if;
  if p_key is null or p_key not in (
       'YANTRA_SITE_ID',
       'SARATHI_LOW_BATTERY_THRESHOLD',
       'DETECTOR_PENDING_POLLS','DETECTOR_CLEAR_POLLS',
       'DETECTOR_REOPEN_WINDOW','DETECTOR_STALE_POLLS',
       'DETECTOR_WINDOW_HOURS','DETECTOR_INTERVAL',
       'NOTIFIER_INTERVAL',
       'SIM_INTERVAL',
       'CONNECTOR_BATTERY_THRESHOLD','CONNECTOR_MQTT_HOST',
       'CONNECTOR_MQTT_PORT','CONNECTOR_MQTT_TOPIC',
       'CONNECTOR_MQTT_USERNAME','CONNECTOR_MQTT_PASSWORD') then
    raise exception 'admin_set_config: unknown config key "%"', p_key;
  end if;

  if p_value is null or length(trim(p_value)) = 0 then
    delete from public.app_config where key = p_key;
    return json_build_object('key', p_key, 'configured', false,
                              'value', null,
                              'updated_at', null, 'updated_by', null);
  end if;

  if public._yf_config_is_numeric_key(p_key) then
    begin
      perform p_value::numeric;
    exception when invalid_text_representation then
      raise exception 'admin_set_config: "%" must be numeric, got "%"',
        p_key, p_value;
    end;
    if p_key = 'CONNECTOR_MQTT_PORT'
       and (p_value::numeric < 1 or p_value::numeric > 65535
            or p_value::numeric != trunc(p_value::numeric)) then
      raise exception 'admin_set_config: CONNECTOR_MQTT_PORT must be an integer 1-65535, got "%"', p_value;
    end if;
  end if;

  insert into public.app_config (key, value, updated_at, updated_by)
  values (p_key, p_value, now(), coalesce(auth.email(), auth.uid()::text))
  on conflict (key) do update
    set value = excluded.value, updated_at = now(), updated_by = excluded.updated_by
  returning * into v_row;

  return json_build_object('key', v_row.key, 'configured', true,
                            'value', v_row.value,
                            'updated_at', v_row.updated_at, 'updated_by', v_row.updated_by);
end
$$;

revoke execute on function public.admin_list_config()         from public, anon;
revoke execute on function public.admin_set_config(text, text) from public, anon;
grant  execute on function public.admin_list_config()         to authenticated;
grant  execute on function public.admin_set_config(text, text) to authenticated;

select 'v0.18 APP CONFIG: app_config table added (no table-level grants); admin_list_config()/admin_set_config() RPCs gated by yf_has_role(''admin''), values unmasked' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop just the config panel objects.
-- Purely additive migration: nothing here replaced an existing policy,
-- so there is nothing to restore, only something to drop. Safe to run
-- independently of any 0006/0007/0008 rollback.
-- ------------------------------------------------------------
-- drop function if exists public.admin_set_config(text, text);
-- drop function if exists public.admin_list_config();
-- drop function if exists public._yf_config_is_numeric_key(text);
-- drop table if exists public.app_config;
-- ============================================================
