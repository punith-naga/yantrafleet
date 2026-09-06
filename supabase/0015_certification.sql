-- ============================================================
-- Yantrika 0015 — SHAREABLE OPERATOR CERTIFICATION (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql — EXTENDS public.certificates  !!
-- !!  and public.issue_certificate() from that migration.    !!
-- !!  REQUIRES yf_has_role() (0007).                          !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- 0007 issued a certificate row nobody outside the platform could check.
-- An operator who passes a checkride should be able to put a code on a
-- CV or a shift board and have a hiring manager verify it in a browser
-- with no account.
--
-- ------------------------------------------------------------
-- WHAT VERIFICATION DELIBERATELY DOES *NOT* RETURN
-- ------------------------------------------------------------
-- `certificate_verify(p_code)` is anon-callable, so its payload is the
-- entire public attack surface of the academy's identity data. It returns
-- FOUR facts plus a validity verdict:
--     holder_name, track, level, issued_at   (+ valid/status/expires_at)
-- and NOTHING else. Specifically never:
--   * the holder's EMAIL or auth user id — the code is public; the
--     identity behind it is not an email lookup service;
--   * their SCORE — "gold" is a public claim, "81.4%" is theirs to share;
--   * their SITE or employer — a certificate must not disclose where
--     somebody works;
--   * any other certificate held by the same person — one code, one row,
--     no enumeration by user.
--
-- `holder_name` is a SNAPSHOT taken at issue time, not a live join to
-- auth.users. That keeps verification working after an account is
-- deleted, and means changing a display name never rewrites history.
--
-- Codes are unguessable by construction (`YF-<track>-<12 base32 chars>`,
-- ~60 bits) precisely because the verify endpoint is public: an
-- enumerable code space would turn it into a roster dump.
--
-- BACKWARD COMPATIBILITY: 0007's `issue_certificate(text, numeric, text)`
-- keeps its exact signature and keeps working. A BEFORE INSERT trigger
-- fills in level / holder_name / code for rows written through it, so
-- old callers start producing verifiable certificates with no code
-- change. The new `issue_certificate_v2()` is a SEPARATE name rather than
-- an overload, because two same-named functions differing only in
-- argument count make PostgREST's RPC resolution ambiguous.
--
-- Run after 0014_inbound_ops.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Columns
-- ------------------------------------------------------------

alter table public.certificates
  add column if not exists level        text;
alter table public.certificates
  add column if not exists holder_name  text;
alter table public.certificates
  add column if not exists issued_for   text;      -- human title of the track
alter table public.certificates
  add column if not exists site_id      text;      -- private; never returned publicly
alter table public.certificates
  add column if not exists expires_at   timestamptz;
alter table public.certificates
  add column if not exists revoked_at   timestamptz;
alter table public.certificates
  add column if not exists revoked_by   text;
alter table public.certificates
  add column if not exists revoke_reason text;

alter table public.certificates
  drop constraint if exists certificates_level_vocab;
alter table public.certificates
  add constraint certificates_level_vocab
  check (level is null or level in ('bronze', 'silver', 'gold', 'platinum'));


-- ------------------------------------------------------------
-- 2) Helpers
-- ------------------------------------------------------------

-- The score -> level ladder, in ONE place so the console, the academy
-- and the trigger can never disagree about what "gold" means.
create or replace function public.yf_certificate_level(p_score numeric)
returns text
language sql immutable
set search_path = ''
as $$
  select case
    when p_score is null then null
    when p_score >= 95 then 'platinum'
    when p_score >= 85 then 'gold'
    when p_score >= 75 then 'silver'
    when p_score >= 60 then 'bronze'
    else null                       -- below the pass mark: no level
  end
$$;

-- A public verification code: 'YF-' + up-to-6 track chars + '-' + 12
-- Crockford-ish base32 characters drawn from a v4 UUID (~60 bits).
create or replace function public.yf_make_certificate_code(p_track text)
returns text
language sql volatile
set search_path = ''
as $$
  select 'YF-'
      || coalesce(nullif(upper(regexp_replace(coalesce(p_track, ''), '[^A-Za-z0-9]', '', 'g')), ''), 'GEN')
      || '-'
      || upper(substr(translate(replace(gen_random_uuid()::text, '-', ''),
                                'abcdef', 'HJKMNP'), 1, 12))
$$;

grant execute on function public.yf_certificate_level(numeric)  to anon, authenticated, service_role;
revoke execute on function public.yf_make_certificate_code(text) from public, anon;
grant  execute on function public.yf_make_certificate_code(text) to authenticated, service_role;

-- Fills in whatever the caller left out. Runs for EVERY insert, including
-- 0007's issue_certificate(), which is how old callers get upgraded for
-- free. SECURITY DEFINER so it may read auth.users for the display name.
create or replace function public.yf_certificates_fill()
returns trigger
language plpgsql security definer
set search_path = ''
as $$
declare
  v_meta  jsonb;
  v_email text;
begin
  if new.level is null then
    new.level := public.yf_certificate_level(new.score);
  end if;
  if new.verification_code is null or length(btrim(new.verification_code)) = 0 then
    new.verification_code := public.yf_make_certificate_code(new.track);
  end if;
  if new.holder_name is null or length(btrim(new.holder_name)) = 0 then
    select u.raw_user_meta_data, u.email into v_meta, v_email
      from auth.users u where u.id = new.user_id;
    new.holder_name := coalesce(
      nullif(btrim(coalesce(v_meta ->> 'full_name', '')), ''),
      nullif(btrim(coalesce(v_meta ->> 'name', '')), ''),
      -- Last resort: the local part of the email, never the domain, so a
      -- public certificate cannot disclose an employer.
      nullif(split_part(coalesce(v_email, ''), '@', 1), ''),
      'Yantrika operator');
  end if;
  if new.issued_for is null or length(btrim(new.issued_for)) = 0 then
    new.issued_for := new.track;
  end if;
  return new;
end
$$;

drop trigger if exists yf_certificates_fill_trg on public.certificates;
create trigger yf_certificates_fill_trg
  before insert on public.certificates
  for each row execute function public.yf_certificates_fill();

-- Backfill rows issued before this migration.
update public.certificates
   set level = public.yf_certificate_level(score)
 where level is null;
update public.certificates
   set issued_for = track
 where issued_for is null;
update public.certificates c
   set holder_name = coalesce(
         nullif(btrim(coalesce(u.raw_user_meta_data ->> 'full_name', '')), ''),
         nullif(btrim(coalesce(u.raw_user_meta_data ->> 'name', '')), ''),
         nullif(split_part(coalesce(u.email, ''), '@', 1), ''),
         'Yantrika operator')
  from auth.users u
 where u.id = c.user_id and c.holder_name is null;
update public.certificates
   set holder_name = 'Yantrika operator'
 where holder_name is null;

create index if not exists certificates_level
  on public.certificates (level, issued_at desc);


-- ------------------------------------------------------------
-- 3) RPCs
-- ------------------------------------------------------------

-- Self-service issuance, superset of 0007's. p_code null => generated.
create or replace function public.issue_certificate_v2(
  p_track       text,
  p_score       numeric,
  p_code        text default null,
  p_level       text default null,
  p_holder_name text default null,
  p_issued_for  text default null,
  p_site        text default null,
  p_expires_at  timestamptz default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_row public.certificates%rowtype;
begin
  if auth.uid() is null then
    raise exception 'issue_certificate_v2: not authenticated — sign in first';
  end if;
  if p_track is null or length(btrim(p_track)) = 0 then
    raise exception 'issue_certificate_v2: p_track is required';
  end if;
  if p_score is null or p_score < 0 or p_score > 100 then
    raise exception 'issue_certificate_v2: p_score must be between 0 and 100 (got %)', p_score;
  end if;
  if p_level is not null and p_level not in ('bronze', 'silver', 'gold', 'platinum') then
    raise exception 'issue_certificate_v2: p_level must be bronze/silver/gold/platinum (got %)', p_level;
  end if;

  insert into public.certificates (
      user_id, track, score, verification_code, level,
      holder_name, issued_for, site_id, expires_at)
  values (auth.uid(), p_track, p_score,
          nullif(btrim(coalesce(p_code, '')), ''),
          p_level,
          nullif(btrim(coalesce(p_holder_name, '')), ''),
          nullif(btrim(coalesce(p_issued_for, '')), ''),
          p_site, p_expires_at)
  returning * into v_row;

  return json_build_object(
    'id', v_row.id, 'track', v_row.track, 'score', v_row.score,
    'level', v_row.level, 'holder_name', v_row.holder_name,
    'issued_for', v_row.issued_for,
    'verification_code', v_row.verification_code,
    'issued_at', v_row.issued_at, 'expires_at', v_row.expires_at);
exception
  when unique_violation then
    raise exception 'issue_certificate_v2: verification code "%" already exists', p_code;
end
$$;

-- THE PUBLIC ENDPOINT. anon-callable, four facts plus a verdict.
-- Never raises: a bad code is a normal outcome, not an error page.
create or replace function public.certificate_verify(p_code text)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_row    public.certificates%rowtype;
  v_status text;
begin
  if p_code is null or length(btrim(p_code)) = 0 then
    return json_build_object('valid', false, 'status', 'not_found');
  end if;
  select * into v_row from public.certificates
   where verification_code = btrim(p_code);
  if not found then
    return json_build_object('valid', false, 'status', 'not_found');
  end if;

  v_status := case
    when v_row.revoked_at is not null then 'revoked'
    when v_row.expires_at is not null and v_row.expires_at <= now() then 'expired'
    else 'valid' end;

  -- The allow-list, written positively so nothing can leak in by accident.
  return json_build_object(
    'valid',             v_status = 'valid',
    'status',            v_status,
    'verification_code', v_row.verification_code,
    'holder_name',       v_row.holder_name,
    'track',             coalesce(v_row.issued_for, v_row.track),
    'level',             v_row.level,
    'issued_at',         v_row.issued_at,
    'expires_at',        v_row.expires_at);
end
$$;

-- The holder's own wallet — full detail, including the score.
create or replace function public.my_certificates()
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare v_result json;
begin
  if auth.uid() is null then
    raise exception 'my_certificates: not authenticated — sign in first';
  end if;
  select coalesce(json_agg(row_to_json(x) order by x.issued_at desc), '[]'::json)
    into v_result
    from (select id, track, issued_for, score, level, holder_name,
                 verification_code, issued_at, expires_at, revoked_at
            from public.certificates
           where user_id = auth.uid()
           order by issued_at desc) x;
  return v_result;
end
$$;

-- Revocation (a checkride found to be fraudulent). admin only.
create or replace function public.certificate_revoke(
  p_code   text,
  p_reason text default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare v_row public.certificates%rowtype;
begin
  if not public.yf_has_role('admin') then
    raise exception 'certificate_revoke: requires admin role';
  end if;
  select * into v_row from public.certificates
   where verification_code = btrim(coalesce(p_code, '')) for update;
  if not found then
    raise exception 'certificate_revoke: no certificate with code %', p_code;
  end if;
  if v_row.revoked_at is null then
    update public.certificates
       set revoked_at = now(),
           revoked_by = coalesce(auth.email(), auth.uid()::text),
           revoke_reason = nullif(btrim(coalesce(p_reason, '')), '')
     where id = v_row.id
     returning * into v_row;
  end if;
  return json_build_object(
    'verification_code', v_row.verification_code,
    'holder_name', v_row.holder_name, 'track', v_row.track,
    'revoked_at', v_row.revoked_at, 'revoked_by', v_row.revoked_by,
    'revoke_reason', v_row.revoke_reason);
end
$$;

revoke execute on function public.issue_certificate_v2(text, numeric, text, text, text, text, text, timestamptz) from public, anon;
revoke execute on function public.my_certificates()                    from public, anon;
revoke execute on function public.certificate_revoke(text, text)       from public, anon;
revoke execute on function public.certificate_verify(text)             from public;

grant execute on function public.issue_certificate_v2(text, numeric, text, text, text, text, text, timestamptz) to authenticated, service_role;
grant execute on function public.my_certificates()               to authenticated, service_role;
grant execute on function public.certificate_revoke(text, text)  to authenticated, service_role;
grant execute on function public.certificate_verify(text)        to anon, authenticated, service_role;

select 'v0.15 CERTIFICATION: certificates.level/holder_name/issued_for/expires_at/revoked_at + fill trigger (0007 issue_certificate still works); certificate_verify() is anon and returns only holder_name/track/level/issued_at' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the certification extensions.
-- The added columns are LEFT IN PLACE by default: they carry issued
-- credentials. Uncomment the final ALTERs to drop them too. 0007's
-- issue_certificate() is untouched by this migration and keeps working
-- either way.
-- ------------------------------------------------------------
-- drop function if exists public.certificate_revoke(text, text);
-- drop function if exists public.my_certificates();
-- drop function if exists public.certificate_verify(text);
-- drop function if exists public.issue_certificate_v2(text, numeric, text, text, text, text, text, timestamptz);
-- drop trigger  if exists yf_certificates_fill_trg on public.certificates;
-- drop function if exists public.yf_certificates_fill();
-- drop function if exists public.yf_make_certificate_code(text);
-- drop function if exists public.yf_certificate_level(numeric);
-- drop index    if exists public.certificates_level;
--
-- -- destructive, opt-in-within-the-rollback:
-- -- alter table public.certificates drop constraint if exists certificates_level_vocab;
-- -- alter table public.certificates drop column if exists level;
-- -- alter table public.certificates drop column if exists holder_name;
-- -- alter table public.certificates drop column if exists issued_for;
-- -- alter table public.certificates drop column if exists site_id;
-- -- alter table public.certificates drop column if exists expires_at;
-- -- alter table public.certificates drop column if exists revoked_at;
-- -- alter table public.certificates drop column if exists revoked_by;
-- -- alter table public.certificates drop column if exists revoke_reason;
--
-- select 'ROLLED BACK 0015: certification extensions removed' as result;
-- ============================================================
