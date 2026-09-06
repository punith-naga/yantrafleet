-- ============================================================
-- Yantrika 0014 — INBOUND OPS ACTIONS / WHATSAPP (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql AND 0009_demo_sandbox.sql —    !!
-- !!  REQUIRES public.yf_has_role()/yf_rank() (0007) and     !!
-- !!  public.yf_is_service_role() (0009).                    !!
-- !!  THE ACTION RPC IS service_role-ONLY. It is called by   !!
-- !!  the webhook receiver, never by a browser. Read         !!
-- !!  "AN INBOUND MESSAGE IS NOT A CREDENTIAL" before        !!
-- !!  wiring anything to it.                                 !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- An operator gets a WhatsApp alert at 2am and replies "ACK A-1042" from
-- a phone. That reply must do exactly what the same person could have
-- done in the console, and nothing more.
--
-- ------------------------------------------------------------
-- AN INBOUND MESSAGE IS NOT A CREDENTIAL
-- ------------------------------------------------------------
-- A phone number arriving on a webhook is an ASSERTION by the messaging
-- provider, not proof of identity, and it is trivially spoofable at the
-- edges of the SMS/WhatsApp world. So the design refuses to let it
-- ESCALATE anything:
--
--  1. The sender is only ever a LOOKUP KEY. `channel_identities` maps
--     (channel, external_id) -> a real platform user, uniquely. An
--     unmapped number performs NOTHING; it is logged with
--     result='unmapped' and the message is dropped.
--
--  2. THE SAME ROLE BAR AS THE CONSOLE. The mapped user's role is read
--     from `user_roles` via `yf_rank_at()` and checked with the identical
--     thresholds the console enforces: ack = operator+, assign =
--     operator+, approve/reject a command = manager+ and pending-only,
--     exactly like 0007's `decide_command()`. There is no inbound-only
--     shortcut, no "trusted number" bypass, no service-account fallback
--     that would perform the action as somebody more privileged.
--
--  3. THE MAPPING ITSELF IS ADMIN-ONLY. Only `yf_has_role('admin')` can
--     create or remove a mapping, so an attacker cannot self-enrol a
--     number. A mapping can be revoked without deleting its audit trail.
--
--  4. THE RPC IS NOT REACHABLE FROM A BROWSER. `inbound_perform()` has
--     EXECUTE revoked from public/anon/authenticated and granted only to
--     service_role, and additionally asserts `yf_is_service_role()`
--     internally. Otherwise any signed-in operator could pass an
--     arbitrary `p_external_id` and act as a colleague.
--
--  5. EVERYTHING IS RECORDED, INCLUDING THE REFUSALS. `inbound_actions`
--     logs every attempt with its result — ok / denied / not_found /
--     invalid / unmapped / error — so a spoofing campaign is visible in
--     the audit table rather than invisible in a dropped packet.
--
--  6. REPLAY-SAFE. A provider message id (when supplied) is unique; a
--     redelivered webhook returns the ORIGINAL outcome instead of
--     performing the action twice.
--
-- Run after 0013_maintenance_explain.sql. Idempotent. ROLLBACK at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Incident assignment (the target of the 'assign' action)
-- ------------------------------------------------------------

alter table public.incidents
  add column if not exists assignee     text;      -- display name or email
alter table public.incidents
  add column if not exists assignee_uid uuid references auth.users (id) on delete set null;
alter table public.incidents
  add column if not exists assigned_at  timestamptz;

create index if not exists incidents_site_assignee
  on public.incidents (site_id, assignee);


-- ------------------------------------------------------------
-- 2) Tables
-- ------------------------------------------------------------

create table if not exists public.channel_identities (
  id           uuid primary key default gen_random_uuid(),
  channel      text not null check (channel in ('whatsapp', 'sms', 'telegram', 'email', 'voice')),
  external_id  text not null,                 -- E.164 phone, chat id, address
  user_id      uuid not null references auth.users (id) on delete cascade,
  site_id      text not null default 'BLR-DC1',
  display_name text,
  verified_at  timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz not null default now(),
  created_by   text
);

-- One external identity maps to at most ONE platform user. Without this,
-- a spoofed number could be ambiguous and the resolver would have to
-- pick — so the database refuses to let the ambiguity exist.
create unique index if not exists channel_identities_unique
  on public.channel_identities (channel, external_id);
create index if not exists channel_identities_user
  on public.channel_identities (user_id);

create table if not exists public.inbound_actions (
  id           uuid primary key default gen_random_uuid(),
  channel      text not null,
  external_id  text not null,                 -- as received, unmodified
  user_id      uuid references auth.users (id) on delete set null,
  mapped_as    text,                          -- display name at the time
  site_id      text,
  action       text not null check (action in (
                 'ack_alert', 'assign_incident', 'approve_command',
                 'reject_command', 'status', 'unknown')),
  target_id    text,
  raw_text     text,
  result       text not null check (result in (
                 'ok', 'denied', 'not_found', 'invalid', 'unmapped', 'error')),
  detail       text,
  message_id   text,                          -- provider message id
  received_at  timestamptz not null default now(),
  processed_at timestamptz not null default now()
);

create unique index if not exists inbound_actions_message_id
  on public.inbound_actions (message_id) where message_id is not null;
create index if not exists inbound_actions_site_received
  on public.inbound_actions (site_id, received_at desc);
create index if not exists inbound_actions_external
  on public.inbound_actions (channel, external_id, received_at desc);

alter table public.channel_identities enable row level security;
alter table public.inbound_actions    enable row level security;

-- Both tables carry PII (phone numbers) and an authorisation mapping.
-- No table-level access for anyone: everything goes through the RPCs,
-- which mask the external_id for non-admins (0008's app_settings posture).
revoke all on public.channel_identities from anon, authenticated;
revoke all on public.inbound_actions    from anon, authenticated;


-- ------------------------------------------------------------
-- 3) Helpers
-- ------------------------------------------------------------

-- The role rank of an ARBITRARY user at a site — the by-user twin of
-- 0007's yf_has_role(), which can only ever answer for auth.uid().
-- Mirrors 0007 exactly: role at that site, or 4 when the user is admin
-- at ANY site (admin is global by design). 0 = no role, fails closed.
create or replace function public.yf_rank_at(p_user uuid, p_site text)
returns int
language sql stable security definer
set search_path = ''
as $$
  select greatest(
    coalesce((select public.yf_rank(ur.role)
                from public.user_roles ur
               where ur.user_id = p_user and ur.site_id = p_site), 0),
    case when exists (select 1 from public.user_roles ur
                       where ur.user_id = p_user and ur.role = 'admin')
         then 4 else 0 end)
$$;

-- Last 4 characters only — enough for an admin to recognise a colleague's
-- number in an audit list, never enough to redistribute it.
create or replace function public._yf_mask_identity(p_value text)
returns text
language sql immutable
set search_path = ''
as $$
  select case
    when p_value is null or length(p_value) = 0 then null
    when length(p_value) <= 4 then repeat('•', 6)
    else repeat('•', 6) || right(p_value, 4)
  end
$$;

revoke execute on function public.yf_rank_at(uuid, text)   from public, anon;
revoke execute on function public._yf_mask_identity(text)  from public, anon;
grant  execute on function public.yf_rank_at(uuid, text)   to authenticated, service_role;
grant  execute on function public._yf_mask_identity(text)  to authenticated, service_role;


-- ------------------------------------------------------------
-- 4) Mapping management (admin only)
-- ------------------------------------------------------------

create or replace function public.inbound_map_identity(
  p_channel      text,
  p_external_id  text,
  p_user_email   text,
  p_site         text default 'BLR-DC1',
  p_display_name text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_uid uuid;
  v_row public.channel_identities%rowtype;
begin
  if not public.yf_has_role('admin', p_site) then
    raise exception 'inbound_map_identity: requires admin role';
  end if;
  if p_channel is null or p_channel not in ('whatsapp', 'sms', 'telegram', 'email', 'voice') then
    raise exception 'inbound_map_identity: unknown channel "%"', p_channel;
  end if;
  if p_external_id is null or length(btrim(p_external_id)) = 0 then
    raise exception 'inbound_map_identity: p_external_id is required';
  end if;

  select id into v_uid from auth.users where lower(email) = lower(btrim(p_user_email));
  if v_uid is null then
    raise exception 'inbound_map_identity: no auth user with email % — they must sign in once first', p_user_email;
  end if;

  insert into public.channel_identities as ci (
      channel, external_id, user_id, site_id, display_name,
      verified_at, revoked_at, created_by)
  values (p_channel, btrim(p_external_id), v_uid, p_site,
          nullif(btrim(coalesce(p_display_name, '')), ''),
          now(), null, coalesce(auth.email(), auth.uid()::text))
  on conflict (channel, external_id) do update set
      user_id      = excluded.user_id,
      site_id      = excluded.site_id,
      display_name = coalesce(excluded.display_name, ci.display_name),
      verified_at  = now(),
      revoked_at   = null,
      created_by   = excluded.created_by
  returning * into v_row;

  return json_build_object(
    'id', v_row.id, 'channel', v_row.channel,
    'external_id', v_row.external_id, 'user_id', v_row.user_id,
    'site_id', v_row.site_id, 'display_name', v_row.display_name,
    'verified_at', v_row.verified_at, 'revoked_at', v_row.revoked_at,
    'created_at', v_row.created_at, 'created_by', v_row.created_by);
end
$$;

create or replace function public.inbound_unmap_identity(
  p_channel     text,
  p_external_id text)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_row public.channel_identities%rowtype;
begin
  select * into v_row from public.channel_identities
   where channel = p_channel and external_id = btrim(coalesce(p_external_id, ''));
  if not found then
    raise exception 'inbound_unmap_identity: no mapping for % on %', p_external_id, p_channel;
  end if;
  if not public.yf_has_role('admin', v_row.site_id) then
    raise exception 'inbound_unmap_identity: requires admin role';
  end if;
  -- Revoke, don't delete: the audit trail in inbound_actions must keep
  -- pointing at a mapping that once existed.
  update public.channel_identities
     set revoked_at = now()
   where id = v_row.id
   returning * into v_row;
  return json_build_object('id', v_row.id, 'channel', v_row.channel,
                           'external_id', v_row.external_id,
                           'revoked_at', v_row.revoked_at);
end
$$;

create or replace function public.inbound_list_identities(
  p_site text default null)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if not public.yf_has_role('admin', coalesce(p_site, 'BLR-DC1')) then
    raise exception 'inbound_list_identities: requires admin role';
  end if;
  select coalesce(json_agg(row_to_json(x) order by x.created_at desc), '[]'::json)
    into v_result
    from (select ci.id, ci.channel, ci.external_id, ci.user_id, ci.site_id,
                 ci.display_name, ci.verified_at, ci.revoked_at, ci.created_at,
                 public.yf_rank_at(ci.user_id, ci.site_id) as rank_at_site,
                 (ci.revoked_at is null) as active
            from public.channel_identities ci
           where p_site is null or ci.site_id = p_site
           order by ci.created_at desc) x;
  return v_result;
end
$$;


-- ------------------------------------------------------------
-- 5) The action RPC — service_role only
-- ------------------------------------------------------------

-- Resolve a sender WITHOUT performing anything. Deliberately does NOT
-- return the mapped user's email: the webhook receiver has no need for
-- it, and a leaky log line should not become a roster dump.
create or replace function public.inbound_resolve_sender(
  p_channel     text,
  p_external_id text)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_row public.channel_identities%rowtype;
begin
  if not public.yf_is_service_role() then
    raise exception 'inbound_resolve_sender: service_role only';
  end if;
  select * into v_row from public.channel_identities
   where channel = p_channel
     and external_id = btrim(coalesce(p_external_id, ''))
     and revoked_at is null;
  if not found then
    return json_build_object('mapped', false, 'channel', p_channel,
                             'user_id', null, 'site_id', null,
                             'display_name', null, 'rank', 0);
  end if;
  return json_build_object(
    'mapped', true, 'channel', v_row.channel,
    'user_id', v_row.user_id, 'site_id', v_row.site_id,
    'display_name', coalesce(v_row.display_name,
                             public._yf_mask_identity(v_row.external_id)),
    'rank', public.yf_rank_at(v_row.user_id, v_row.site_id));
end
$$;

-- The single entry point for every inbound channel.
--
-- Returns {ok, result, action, ...} and NEVER raises for an
-- authorisation failure: a webhook receiver must be able to send the
-- sender a polite "you're not allowed to do that" instead of 500ing, and
-- the refusal must land in the audit table either way.
create or replace function public.inbound_perform(
  p_channel     text,
  p_external_id text,
  p_action      text,
  p_target_id   text default null,
  p_note        text default null,
  p_raw_text    text default null,
  p_message_id  text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_id      public.channel_identities%rowtype;
  v_prior   public.inbound_actions%rowtype;
  v_site    text;
  v_rank    int  := 0;
  v_result  text := 'error';
  v_detail  text;
  v_action  text := coalesce(p_action, 'unknown');
  v_name    text;
  v_uid     uuid;
  v_log     uuid;
  v_alert   public.alerts%rowtype;
  v_inc     public.incidents%rowtype;
  v_cmd     public.commands%rowtype;
  v_payload json := null;
  v_n       int;
begin
  if not public.yf_is_service_role() then
    raise exception 'inbound_perform: service_role only — an inbound message must never be replayable from a browser session';
  end if;
  if v_action not in ('ack_alert', 'assign_incident', 'approve_command',
                      'reject_command', 'status') then
    v_action := 'unknown';
  end if;

  -- 6) Replay safety: a redelivered webhook returns the original outcome.
  if p_message_id is not null and length(btrim(p_message_id)) > 0 then
    select * into v_prior from public.inbound_actions
     where message_id = btrim(p_message_id);
    if found then
      return json_build_object(
        'ok', v_prior.result = 'ok', 'result', v_prior.result,
        'action', v_prior.action, 'target_id', v_prior.target_id,
        'site_id', v_prior.site_id, 'mapped_as', v_prior.mapped_as,
        -- Neither the caller's rank nor the action payload is persisted —
        -- only result/detail are — so a replay reports null for both rather
        -- than omitting the keys. The envelope's key set is identical for
        -- every outcome (see docs/FEATURE-CONTRACTS.md).
        'rank', null,
        'detail', v_prior.detail,
        'payload', null,
        'log_id', v_prior.id, 'replayed', true);
    end if;
  end if;

  -- 1) The sender is only a lookup key.
  select * into v_id from public.channel_identities
   where channel = p_channel
     and external_id = btrim(coalesce(p_external_id, ''))
     and revoked_at is null;

  if not found then
    v_result := 'unmapped';
    v_detail := 'sender is not mapped to a platform user';
  else
    v_uid  := v_id.user_id;
    v_site := v_id.site_id;
    v_name := coalesce(v_id.display_name,
                       public._yf_mask_identity(v_id.external_id));
    -- 2) The same role bar as the console.
    v_rank := public.yf_rank_at(v_uid, v_site);

    if v_action = 'unknown' then
      v_result := 'invalid';
      v_detail := format('unrecognised action "%s"', p_action);

    elsif v_rank < 1 then
      v_result := 'denied';
      v_detail := format('%s holds no role at site %s', v_name, v_site);

    elsif v_action = 'status' then
      select json_build_object(
               'site_id', v_site,
               'robots_total', (select count(*) from public.robots where site_id = v_site),
               'robots_active', (select count(*) from public.robots
                                  where site_id = v_site and status = 'active'),
               'robots_faulted', (select count(*) from public.robots
                                   where site_id = v_site
                                     and status in ('fault', 'estop', 'degraded')),
               'open_incidents', (select count(*) from public.incidents
                                   where site_id = v_site and state = 'Open'),
               'unacked_alerts', (select count(*) from public.alerts
                                   where site_id = v_site and not ack),
               'pending_commands', (select count(*) from public.commands
                                     where site_id = v_site and status = 'pending'))
        into v_payload;
      v_result := 'ok';
      v_detail := 'status snapshot';

    elsif v_action = 'ack_alert' then
      select * into v_alert from public.alerts
       where id = p_target_id and site_id = v_site for update;
      if not found then
        v_result := 'not_found';
        v_detail := format('no alert %s at site %s', p_target_id, v_site);
      else
        update public.alerts set ack = true where id = v_alert.id;
        v_result := 'ok';
        v_detail := format('alert %s acknowledged by %s', v_alert.id, v_name);
        v_payload := json_build_object('id', v_alert.id, 'sev', v_alert.sev,
                                       'msg', v_alert.msg, 'ack', true);
      end if;

    elsif v_action = 'assign_incident' then
      select * into v_inc from public.incidents
       where id = p_target_id and site_id = v_site for update;
      if not found then
        v_result := 'not_found';
        v_detail := format('no incident %s at site %s', p_target_id, v_site);
      else
        -- Self-assign is the normal case ("MINE"). Assigning SOMEBODY
        -- ELSE is a manager+ act, and the assignee is then recorded as a
        -- free-text name with no uid — inbound cannot bind a colleague's
        -- account to work they never accepted.
        if p_note is not null and length(btrim(p_note)) > 0 then
          if v_rank < 3 then
            v_result := 'denied';
            v_detail := format('%s may self-assign, but assigning another person requires manager role at site %s',
                               v_name, v_site);
          else
            update public.incidents
               set assignee = btrim(p_note), assignee_uid = null,
                   assigned_at = now()
             where id = v_inc.id;
            v_result := 'ok';
            v_detail := format('incident %s assigned to %s by %s',
                               v_inc.id, btrim(p_note), v_name);
          end if;
        else
          update public.incidents
             set assignee = v_name, assignee_uid = v_uid, assigned_at = now()
           where id = v_inc.id;
          v_result := 'ok';
          v_detail := format('incident %s self-assigned by %s', v_inc.id, v_name);
        end if;
        if v_result = 'ok' then
          v_payload := json_build_object('id', v_inc.id, 'title', v_inc.title,
                                         'sev', v_inc.sev, 'state', v_inc.state);
        end if;
      end if;

    elsif v_action in ('approve_command', 'reject_command') then
      -- Identical gate and state machine to 0007's decide_command().
      if v_rank < 3 then
        v_result := 'denied';
        v_detail := format('%s (rank %s) may not decide commands — manager role required at site %s',
                           v_name, v_rank, v_site);
      else
        select * into v_cmd from public.commands
         where id::text = p_target_id and site_id = v_site for update;
        if not found then
          v_result := 'not_found';
          v_detail := format('no command %s at site %s', p_target_id, v_site);
        elsif v_cmd.status <> 'pending' then
          v_result := 'invalid';
          v_detail := format('command %s is already ''%s'' — only pending commands can be decided',
                             v_cmd.id, v_cmd.status);
        else
          update public.commands
             set status     = case when v_action = 'approve_command'
                                   then 'approved' else 'rejected' end,
                 decided_by = p_channel || ':' || v_name,
                 decided_at = now(),
                 note       = coalesce(nullif(btrim(coalesce(p_note, '')), ''), note)
           where id = v_cmd.id
           returning * into v_cmd;
          v_result := 'ok';
          v_detail := format('command %s %s by %s via %s', v_cmd.id, v_cmd.status,
                             v_name, p_channel);
          v_payload := json_build_object('id', v_cmd.id, 'robot_id', v_cmd.robot_id,
                                         'cmd', v_cmd.cmd, 'status', v_cmd.status);
        end if;
      end if;
    end if;
  end if;

  -- 5) Everything is recorded, including the refusals.
  insert into public.inbound_actions (
      channel, external_id, user_id, mapped_as, site_id, action,
      target_id, raw_text, result, detail, message_id)
  values (coalesce(p_channel, 'unknown'), btrim(coalesce(p_external_id, '')),
          v_uid, v_name, v_site, v_action, p_target_id, p_raw_text,
          v_result, v_detail, nullif(btrim(coalesce(p_message_id, '')), ''))
  returning id into v_log;

  return json_build_object(
    'ok', v_result = 'ok', 'result', v_result, 'action', v_action,
    'target_id', p_target_id, 'site_id', v_site, 'mapped_as', v_name,
    'rank', v_rank, 'detail', v_detail, 'payload', v_payload,
    'log_id', v_log, 'replayed', false);
end
$$;

-- The audit trail. manager+ at the site; external ids are MASKED here —
-- a manager needs to see that a number acted, not the number itself.
-- Admins use inbound_list_identities() for the unmasked roster.
create or replace function public.inbound_recent_actions(
  p_site  text default 'BLR-DC1',
  p_limit int  default 50)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if not public.yf_has_role('manager', p_site) then
    raise exception 'inbound_recent_actions: requires manager role (or higher) at site %', p_site;
  end if;
  select coalesce(json_agg(row_to_json(x) order by x.received_at desc), '[]'::json)
    into v_result
    from (select ia.id, ia.channel,
                 public._yf_mask_identity(ia.external_id) as external_id_masked,
                 ia.mapped_as, ia.site_id, ia.action, ia.target_id,
                 ia.result, ia.detail, ia.received_at, ia.processed_at
            from public.inbound_actions ia
           where ia.site_id = p_site or ia.site_id is null
           order by ia.received_at desc
           limit greatest(coalesce(p_limit, 50), 1)) x;
  return v_result;
end
$$;

revoke execute on function public.inbound_map_identity(text, text, text, text, text) from public, anon;
revoke execute on function public.inbound_unmap_identity(text, text)                 from public, anon;
revoke execute on function public.inbound_list_identities(text)                      from public, anon;
revoke execute on function public.inbound_recent_actions(text, int)                  from public, anon;
-- 4) NOT reachable from a browser: no grant to authenticated, ever.
revoke execute on function public.inbound_resolve_sender(text, text)                             from public, anon, authenticated;
revoke execute on function public.inbound_perform(text, text, text, text, text, text, text)      from public, anon, authenticated;

grant execute on function public.inbound_map_identity(text, text, text, text, text) to authenticated, service_role;
grant execute on function public.inbound_unmap_identity(text, text)                 to authenticated, service_role;
grant execute on function public.inbound_list_identities(text)                      to authenticated, service_role;
grant execute on function public.inbound_recent_actions(text, int)                  to authenticated, service_role;
grant execute on function public.inbound_resolve_sender(text, text)                             to service_role;
grant execute on function public.inbound_perform(text, text, text, text, text, text, text)      to service_role;

select 'v0.14 INBOUND OPS: channel_identities + inbound_actions (no table grants); inbound_perform()/inbound_resolve_sender() are service_role-only and re-check the mapped user''s role with the console''s own thresholds; incidents.assignee added' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the inbound-ops objects.
-- The incidents.assignee columns are LEFT IN PLACE by default (real
-- ownership data). Uncomment the final ALTERs to drop them too.
-- ------------------------------------------------------------
-- drop function if exists public.inbound_recent_actions(text, int);
-- drop function if exists public.inbound_perform(text, text, text, text, text, text, text);
-- drop function if exists public.inbound_resolve_sender(text, text);
-- drop function if exists public.inbound_list_identities(text);
-- drop function if exists public.inbound_unmap_identity(text, text);
-- drop function if exists public.inbound_map_identity(text, text, text, text, text);
-- drop table    if exists public.inbound_actions;
-- drop table    if exists public.channel_identities;
-- drop function if exists public._yf_mask_identity(text);
-- drop function if exists public.yf_rank_at(uuid, text);
--
-- -- destructive, opt-in-within-the-rollback:
-- -- alter table public.incidents drop column if exists assignee;
-- -- alter table public.incidents drop column if exists assignee_uid;
-- -- alter table public.incidents drop column if exists assigned_at;
--
-- select 'ROLLED BACK 0014: inbound ops actions removed' as result;
-- ============================================================
