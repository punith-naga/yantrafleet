-- ============================================================
-- YantraFleet 0008 — ADMIN SETTINGS PANEL (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql — REQUIRES yf_has_role/yf_rank !!
-- !!  (public.yf_has_role('admin') gates both RPCs below).   !!
-- !!  A vanilla 0001-0005 (demo-open) or 0006 (hardened,     !!
-- !!  no roles) project does not have yf_has_role yet — do   !!
-- !!  not run this migration until 0007 has been applied.    !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Lets an `admin` rotate 8 operational credentials/settings from the
-- console UI, live, with no deploy-script edit and no instance restart:
--   GEMINI_API_KEY, SARATHI_TOKEN, WEBHOOK_URL, YANTRA_WEBHOOK_SECRET,
--   TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO
-- SUPABASE_URL / SUPABASE_KEY / REPO_URL stay bootstrap-only (env-var-only
-- forever) — the app can't reach Supabase or know its own identity before
-- those exist, so they are deliberately NOT in this table.
--
-- Design principle: the env var stays the fallback default forever. A row
-- here OVERRIDES the env var while present; deleting the row (or saving an
-- empty value, which this migration's RPC treats as a delete) reverts to
-- the env var. A deployment that never touches the panel behaves exactly
-- as it does today.
--
-- Independent of 0006/0007's table hardening: this file is purely
-- additive (no existing policy is replaced), so its rollback block
-- (bottom) only needs to drop what it created.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Table: app_settings
-- ------------------------------------------------------------

create table if not exists public.app_settings (
  key        text primary key
             check (key in (
               'GEMINI_API_KEY','SARATHI_TOKEN',
               'WEBHOOK_URL','YANTRA_WEBHOOK_SECRET',
               'TWILIO_SID','TWILIO_TOKEN','TWILIO_FROM','TWILIO_TO'
             )),
  value      text not null,             -- plaintext at rest (see README/SECURITY.md rationale)
  updated_at timestamptz not null default now(),
  updated_by text                        -- auth.email() of the admin who last wrote it
);

alter table public.app_settings enable row level security;

-- Deliberately NO select/insert/update/delete policy for authenticated,
-- not even for admins. Unlike academy_progress/certificates (0007 grants
-- `select` and gates it with an RLS policy), app_settings grants NOTHING
-- at the table level. The only way in or out for a signed-in human is
-- the two SECURITY DEFINER RPCs below — this is what makes the RPCs'
-- masking meaningful: a raw `GET /rest/v1/app_settings` cannot bypass it,
-- even from an admin's own browser devtools. service_role continues to
-- read the table directly (bypasses RLS entirely, same as every other
-- table in this schema — no explicit grant needed, matching the existing
-- convention: 0007 never grants service_role table privileges either).
revoke all on public.app_settings from anon, authenticated;


-- ------------------------------------------------------------
-- 2) Masking helper
-- ------------------------------------------------------------

-- Shows only the last 4 characters (e.g. ****ab12) — enough for an admin
-- to confirm *which* key is loaded / that a rotation took effect, never
-- enough to reconstruct the secret.
create or replace function public._yf_mask_setting(p_value text)
returns text
language sql immutable
set search_path = ''
as $$
  select case
    when p_value is null or length(p_value) = 0 then null
    when length(p_value) <= 4 then repeat('•', 8)
    else repeat('•', 8) || right(p_value, 4)
  end
$$;

-- Left without an explicit revoke/grant, same as yf_rank in 0007 — it's a
-- pure, argument-only string transform with no access to secret state, so
-- PUBLIC execute is harmless.


-- ------------------------------------------------------------
-- 3) RPCs (SECURITY DEFINER, admin-only)
-- ------------------------------------------------------------

-- Always returns all 8 known keys (not just ones with a row), so the
-- console can render a fixed settings form without a first-run special
-- case. Called with yf_has_role's default site param, which is fine here
-- because its "admin at any site is global" clause fires regardless of
-- the site argument.
create or replace function public.admin_list_settings()
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_result json;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_list_settings: requires admin role';
  end if;
  select coalesce(json_agg(row_to_json(t) order by t.key), '[]'::json)
    into v_result
    from (
      select k.key,
             (s.key is not null)              as configured,
             public._yf_mask_setting(s.value) as masked_value,
             s.updated_at, s.updated_by
        from unnest(array[
          'GEMINI_API_KEY','SARATHI_TOKEN','WEBHOOK_URL',
          'YANTRA_WEBHOOK_SECRET','TWILIO_SID','TWILIO_TOKEN',
          'TWILIO_FROM','TWILIO_TO']) as k(key)
        left join public.app_settings s on s.key = k.key
    ) t;
  return v_result;
end
$$;

-- p_value null/empty => DELETE the row (explicit "revert to the
-- deploy-time env var" action) rather than storing an empty string.
create or replace function public.admin_set_setting(
  p_key   text,
  p_value text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.app_settings%rowtype;
begin
  if not public.yf_has_role('admin') then
    raise exception 'admin_set_setting: requires admin role';
  end if;
  if p_key is null or p_key not in (
       'GEMINI_API_KEY','SARATHI_TOKEN','WEBHOOK_URL',
       'YANTRA_WEBHOOK_SECRET','TWILIO_SID','TWILIO_TOKEN',
       'TWILIO_FROM','TWILIO_TO') then
    raise exception 'admin_set_setting: unknown setting key "%"', p_key;
  end if;

  if p_value is null or length(trim(p_value)) = 0 then
    delete from public.app_settings where key = p_key;
    return json_build_object('key', p_key, 'configured', false,
                              'masked_value', null,
                              'updated_at', null, 'updated_by', null);
  end if;

  insert into public.app_settings (key, value, updated_at, updated_by)
  values (p_key, p_value, now(), coalesce(auth.email(), auth.uid()::text))
  on conflict (key) do update
    set value = excluded.value, updated_at = now(), updated_by = excluded.updated_by
  returning * into v_row;

  return json_build_object('key', v_row.key, 'configured', true,
                            'masked_value', public._yf_mask_setting(v_row.value),
                            'updated_at', v_row.updated_at, 'updated_by', v_row.updated_by);
end
$$;

revoke execute on function public.admin_list_settings()         from public, anon;
revoke execute on function public.admin_set_setting(text, text) from public, anon;
grant  execute on function public.admin_list_settings()         to authenticated;
grant  execute on function public.admin_set_setting(text, text) to authenticated;

select 'v0.8 APP SETTINGS: app_settings table added (no table-level grants); admin_list_settings()/admin_set_setting() RPCs gated by yf_has_role(''admin'')' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop just the settings panel objects.
-- Purely additive migration: nothing here replaced an existing policy,
-- so there is nothing to restore, only something to drop. Safe to run
-- independently of any 0006/0007 rollback.
-- ------------------------------------------------------------
-- drop function if exists public.admin_set_setting(text, text);
-- drop function if exists public.admin_list_settings();
-- drop function if exists public._yf_mask_setting(text);
-- drop table if exists public.app_settings;
-- ============================================================
