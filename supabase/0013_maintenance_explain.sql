-- ============================================================
-- Yantrika 0013 — EXPLAINABLE PREDICTIVE MAINTENANCE (OPT-IN).
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- !!  RUN AFTER 0007_rbac.sql AND 0009_demo_sandbox.sql —    !!
-- !!  REQUIRES public.yf_has_role() (0007) and               !!
-- !!  public.yf_can_read_site() (0009). Extends              !!
-- !!  public.maintenance_findings from 0004_maintenance.sql. !!
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- 0004 gave a finding a human sentence ("motor temp trending up") and a
-- confidence number. A technician being asked to pull a robot off the
-- floor deserves to see WHY, in a form a UI can render as a bar chart and
-- a future model can train on — and deserves a way to say "you were
-- wrong" that the system actually keeps.
--
-- ------------------------------------------------------------
-- WHY `factors` IS STRUCTURED, NOT PROSE
-- ------------------------------------------------------------
-- `maintenance_findings.finding` is already prose and stays exactly as it
-- is. The new `factors` column is a JSON ARRAY of contributing factors,
-- each an object:
--
--   {"factor":       "motor_temp_slope",   -- stable machine key
--    "label":        "Motor temperature trend",
--    "weight":       0.42,                 -- 0..1, contribution to the score
--    "value":        3.8,                  -- the measured quantity
--    "unit":         "degC/h",
--    "direction":    "up",                 -- up | down | flat
--    "threshold":    2.0,                  -- what it was compared against
--    "window":       "24h",
--    "detail":       "rose 3.8 degC/h over 41 samples"}
--
-- Only `factor` and `weight` are required by the CHECK below; everything
-- else is optional so the detector can enrich over time without a
-- migration. `factor` being a STABLE KEY (not a sentence) is what makes
-- `maintenance_accuracy()` able to answer "which signals actually predict
-- failures on this fleet" — the feedback loop's whole payoff.
--
-- Weights are NOT required to sum to 1. A detector that emits raw
-- contributions should say so via `model_version`; the UI normalises for
-- display. Forcing a sum here would push detectors to fake it.
--
-- ------------------------------------------------------------
-- FEEDBACK IS APPEND-INTENT, NOT A MUTATION OF THE PREDICTION
-- ------------------------------------------------------------
-- A technician's verdict goes in a SEPARATE table. The original finding
-- is never edited: what the model said at the time is evidence, and
-- overwriting it would destroy the only record of how good the model
-- actually was. One verdict per (finding, technician), upsertable so a
-- correction is possible without losing the audit of who said what.
--
-- Run after 0012_utilization.sql. Idempotent. ROLLBACK block at the bottom.
-- ============================================================


-- ------------------------------------------------------------
-- 1) Explainability columns on maintenance_findings
-- ------------------------------------------------------------

alter table public.maintenance_findings
  add column if not exists factors       jsonb not null default '[]'::jsonb;
alter table public.maintenance_findings
  add column if not exists model_version text;
alter table public.maintenance_findings
  add column if not exists score         numeric;
alter table public.maintenance_findings
  add column if not exists severity      text;

alter table public.maintenance_findings
  drop constraint if exists maintenance_findings_factors_array;
alter table public.maintenance_findings
  add constraint maintenance_findings_factors_array
  check (jsonb_typeof(factors) = 'array');

alter table public.maintenance_findings
  drop constraint if exists maintenance_findings_severity_vocab;
alter table public.maintenance_findings
  add constraint maintenance_findings_severity_vocab
  check (severity is null or severity in ('info', 'warn', 'serious', 'critical'));

-- Validator the detector can call before writing, and that
-- `maintenance_explain` uses to flag malformed rows instead of crashing.
-- Returns null when the array is well-formed, else the first problem.
create or replace function public.yf_factors_problem(p_factors jsonb)
returns text
language plpgsql immutable
set search_path = ''
as $$
declare
  v_el jsonb;
  v_i  int := 0;
begin
  if p_factors is null then
    return null;                       -- absent is fine; '[]' is the default
  end if;
  if jsonb_typeof(p_factors) <> 'array' then
    return 'factors must be a JSON array';
  end if;
  for v_el in select value from jsonb_array_elements(p_factors) loop
    v_i := v_i + 1;
    if jsonb_typeof(v_el) <> 'object' then
      return format('factor %s is not an object', v_i);
    end if;
    if coalesce(btrim(v_el ->> 'factor'), '') = '' then
      return format('factor %s is missing "factor" (a stable machine key)', v_i);
    end if;
    if v_el ->> 'weight' is null then
      return format('factor "%s" is missing "weight"', v_el ->> 'factor');
    end if;
    begin
      perform (v_el ->> 'weight')::numeric;
    exception when others then
      return format('factor "%s" has a non-numeric weight', v_el ->> 'factor');
    end;
  end loop;
  return null;
end
$$;

grant execute on function public.yf_factors_problem(jsonb) to anon, authenticated, service_role;


-- ------------------------------------------------------------
-- 2) Feedback table
-- ------------------------------------------------------------

create table if not exists public.maintenance_feedback (
  id               uuid primary key default gen_random_uuid(),
  finding_id       text not null references public.maintenance_findings (id) on delete cascade,
  site_id          text not null default 'BLR-DC1',
  verdict          text not null check (verdict in ('correct', 'incorrect', 'unclear')),
  actual_fault     text,                 -- what was really wrong, if anything
  action_taken     text,
  parts_replaced   text,
  downtime_minutes numeric check (downtime_minutes is null or downtime_minutes >= 0),
  note             text,
  submitted_by     text,                 -- auth.email() snapshot
  submitted_by_uid uuid references auth.users (id) on delete set null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- One standing verdict per technician per finding (upsert target).
create unique index if not exists maintenance_feedback_one_per_user
  on public.maintenance_feedback (finding_id, submitted_by_uid);
create index if not exists maintenance_feedback_site_created
  on public.maintenance_feedback (site_id, created_at desc);
create index if not exists maintenance_feedback_finding
  on public.maintenance_feedback (finding_id);

alter table public.maintenance_feedback enable row level security;

-- Read: anyone with a role at the site. Write: only via the RPC below
-- (same posture as 0007's academy tables).
revoke insert, update, delete on public.maintenance_feedback from anon, authenticated;
revoke all on public.maintenance_feedback from anon;
grant select on public.maintenance_feedback to authenticated;

drop policy if exists maintenance_feedback_read on public.maintenance_feedback;
create policy maintenance_feedback_read on public.maintenance_feedback
  for select to authenticated
  using (public.yf_has_role('operator', site_id));


-- ------------------------------------------------------------
-- 3) RPCs
-- ------------------------------------------------------------

-- The "why did this fire?" panel: the finding, its factors (weights
-- normalised for display alongside the raw ones), and what technicians
-- have said about it.
create or replace function public.maintenance_explain(p_finding_id text)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_f        public.maintenance_findings%rowtype;
  v_total    numeric;
  v_factors  json;
  v_feedback json;
  v_problem  text;
begin
  if p_finding_id is null or length(btrim(p_finding_id)) = 0 then
    raise exception 'maintenance_explain: p_finding_id is required';
  end if;
  select * into v_f from public.maintenance_findings where id = p_finding_id;
  if not found then
    raise exception 'maintenance_explain: finding % not found', p_finding_id;
  end if;
  if not public.yf_can_read_site(v_f.site_id) then
    raise exception 'maintenance_explain: requires operator role (or higher) at site %', v_f.site_id;
  end if;

  v_problem := public.yf_factors_problem(v_f.factors);

  select nullif(sum(abs((e ->> 'weight')::numeric)), 0) into v_total
    from jsonb_array_elements(coalesce(v_f.factors, '[]'::jsonb)) e
   where v_problem is null;

  select coalesce(json_agg(row_to_json(x)
                    order by x.weight desc nulls last), '[]'::json)
    into v_factors
    from (select e ->> 'factor'    as factor,
                 coalesce(e ->> 'label', e ->> 'factor') as label,
                 (e ->> 'weight')::numeric as weight,
                 case when v_total is not null
                      then round(100 * abs((e ->> 'weight')::numeric) / v_total, 1)
                      end as weight_pct,
                 e ->> 'value'     as value,
                 e ->> 'unit'      as unit,
                 e ->> 'direction' as direction,
                 e ->> 'threshold' as threshold,
                 e ->> 'window'    as "window",
                 e ->> 'detail'    as detail
            from jsonb_array_elements(coalesce(v_f.factors, '[]'::jsonb)) e
           where v_problem is null) x;

  select coalesce(json_agg(row_to_json(x) order by x.created_at desc), '[]'::json)
    into v_feedback
    from (select verdict, actual_fault, action_taken, parts_replaced,
                 downtime_minutes, note, submitted_by, created_at, updated_at
            from public.maintenance_feedback
           where finding_id = p_finding_id
           order by created_at desc) x;

  return json_build_object(
    'id', v_f.id, 'robot_id', v_f.robot_id, 'site_id', v_f.site_id,
    'component', v_f.component, 'finding', v_f.finding,
    'rul_days', v_f.rul_days, 'confidence', v_f.confidence,
    'score', v_f.score, 'severity', v_f.severity,
    'action', v_f.action, 'state', v_f.state,
    'model_version', v_f.model_version,
    'created_at', v_f.created_at, 'cleared_at', v_f.cleared_at,
    'factors', v_factors,
    'factors_problem', v_problem,
    'feedback', v_feedback,
    'feedback_count', json_array_length(v_feedback));
end
$$;

-- A technician marks a prediction correct or wrong. engineer+ at the
-- finding's site — the same bar the console applies to maintenance
-- workflows, so an inbound channel or a curious operator cannot poison
-- the training signal.
create or replace function public.maintenance_submit_feedback(
  p_finding_id       text,
  p_verdict          text,
  p_actual_fault     text    default null,
  p_action_taken     text    default null,
  p_parts_replaced   text    default null,
  p_downtime_minutes numeric default null,
  p_note             text    default null)
returns json
language plpgsql security definer
set search_path = ''
as $$
declare
  v_f   public.maintenance_findings%rowtype;
  v_row public.maintenance_feedback%rowtype;
begin
  if auth.uid() is null then
    raise exception 'maintenance_submit_feedback: not authenticated — sign in first';
  end if;
  if p_verdict is null or p_verdict not in ('correct', 'incorrect', 'unclear') then
    raise exception 'maintenance_submit_feedback: p_verdict must be ''correct'', ''incorrect'' or ''unclear'' (got %)', p_verdict;
  end if;
  select * into v_f from public.maintenance_findings where id = p_finding_id;
  if not found then
    raise exception 'maintenance_submit_feedback: finding % not found', p_finding_id;
  end if;
  if not public.yf_has_role('engineer', v_f.site_id) then
    raise exception 'maintenance_submit_feedback: requires engineer role (or higher) at site %', v_f.site_id;
  end if;
  if p_downtime_minutes is not null and p_downtime_minutes < 0 then
    raise exception 'maintenance_submit_feedback: p_downtime_minutes must be >= 0';
  end if;

  insert into public.maintenance_feedback as mf (
      finding_id, site_id, verdict, actual_fault, action_taken,
      parts_replaced, downtime_minutes, note, submitted_by, submitted_by_uid)
  values (p_finding_id, v_f.site_id, p_verdict, p_actual_fault, p_action_taken,
          p_parts_replaced, p_downtime_minutes, p_note,
          coalesce(auth.email(), auth.uid()::text), auth.uid())
  on conflict (finding_id, submitted_by_uid) do update set
      verdict          = excluded.verdict,
      actual_fault     = excluded.actual_fault,
      action_taken     = excluded.action_taken,
      parts_replaced   = excluded.parts_replaced,
      downtime_minutes = excluded.downtime_minutes,
      note             = excluded.note,
      updated_at       = now()
  returning * into v_row;

  return json_build_object(
    'id', v_row.id, 'finding_id', v_row.finding_id, 'site_id', v_row.site_id,
    'verdict', v_row.verdict, 'actual_fault', v_row.actual_fault,
    'action_taken', v_row.action_taken, 'parts_replaced', v_row.parts_replaced,
    'downtime_minutes', v_row.downtime_minutes, 'note', v_row.note,
    'submitted_by', v_row.submitted_by,
    'created_at', v_row.created_at, 'updated_at', v_row.updated_at);
end
$$;

-- The payoff: how good have this fleet's predictions actually been, and
-- WHICH FACTORS carried the ones that were right. `by_factor` is the
-- tuning input — a factor whose mean weight is high on incorrect findings
-- and low on correct ones is a signal to down-weight.
create or replace function public.maintenance_accuracy(
  p_site text        default 'BLR-DC1',
  p_from timestamptz default null,
  p_to   timestamptz default null)
returns json
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_to        timestamptz := coalesce(p_to, now());
  v_from      timestamptz := coalesce(p_from, v_to - interval '90 days');
  v_overall   json;
  v_component json;
  v_factor    json;
begin
  if not public.yf_has_role('engineer', p_site) then
    raise exception 'maintenance_accuracy: requires engineer role (or higher) at site %', p_site;
  end if;

  with scored as (
    select f.id, f.component, f.model_version, f.confidence, f.factors,
           fb.verdict
      from public.maintenance_findings f
      join public.maintenance_feedback fb on fb.finding_id = f.id
     where f.site_id = p_site
       and f.created_at >= v_from and f.created_at <= v_to
  )
  select json_build_object(
           'total',     count(*),
           'correct',   count(*) filter (where verdict = 'correct'),
           'incorrect', count(*) filter (where verdict = 'incorrect'),
           'unclear',   count(*) filter (where verdict = 'unclear'),
           'precision', case when count(*) filter (where verdict in ('correct', 'incorrect')) > 0
                             then round(100.0 * count(*) filter (where verdict = 'correct')
                                      / count(*) filter (where verdict in ('correct', 'incorrect')), 2)
                        end,
           'mean_confidence_correct',
             round(avg(confidence) filter (where verdict = 'correct'), 4),
           'mean_confidence_incorrect',
             round(avg(confidence) filter (where verdict = 'incorrect'), 4))
    into v_overall
    from scored;

  with scored as (
    select f.component, fb.verdict
      from public.maintenance_findings f
      join public.maintenance_feedback fb on fb.finding_id = f.id
     where f.site_id = p_site
       and f.created_at >= v_from and f.created_at <= v_to
  )
  select coalesce(json_agg(row_to_json(x) order by x.total desc), '[]'::json)
    into v_component
    from (select component,
                 count(*)                                     as total,
                 count(*) filter (where verdict = 'correct')   as correct,
                 count(*) filter (where verdict = 'incorrect') as incorrect,
                 count(*) filter (where verdict = 'unclear')   as unclear,
                 case when count(*) filter (where verdict in ('correct', 'incorrect')) > 0
                      then round(100.0 * count(*) filter (where verdict = 'correct')
                               / count(*) filter (where verdict in ('correct', 'incorrect')), 2)
                 end                                          as "precision"
            from scored group by component) x;

  with exploded as (
    select e ->> 'factor' as factor,
           (e ->> 'weight')::numeric as weight,
           fb.verdict
      from public.maintenance_findings f
      join public.maintenance_feedback fb on fb.finding_id = f.id
      cross join lateral jsonb_array_elements(coalesce(f.factors, '[]'::jsonb)) e
     where f.site_id = p_site
       and f.created_at >= v_from and f.created_at <= v_to
       and jsonb_typeof(f.factors) = 'array'
       and coalesce(btrim(e ->> 'factor'), '') <> ''
       and (e ->> 'weight') ~ '^-?[0-9]+(\.[0-9]+)?$'
  )
  select coalesce(json_agg(row_to_json(x) order by x.appearances desc), '[]'::json)
    into v_factor
    from (select factor,
                 count(*)                                     as appearances,
                 count(*) filter (where verdict = 'correct')   as on_correct,
                 count(*) filter (where verdict = 'incorrect') as on_incorrect,
                 round(avg(weight) filter (where verdict = 'correct'), 4)   as mean_weight_correct,
                 round(avg(weight) filter (where verdict = 'incorrect'), 4) as mean_weight_incorrect,
                 case when count(*) filter (where verdict in ('correct', 'incorrect')) > 0
                      then round(100.0 * count(*) filter (where verdict = 'correct')
                               / count(*) filter (where verdict in ('correct', 'incorrect')), 2)
                 end                                          as "precision"
            from exploded group by factor) x;

  return json_build_object(
    'site_id', p_site, 'from', v_from, 'to', v_to,
    'generated_at', now(),
    'overall', v_overall,
    'by_component', v_component,
    'by_factor', v_factor);
end
$$;

revoke execute on function public.maintenance_explain(text)                                                   from public, anon;
revoke execute on function public.maintenance_submit_feedback(text, text, text, text, text, numeric, text)     from public, anon;
revoke execute on function public.maintenance_accuracy(text, timestamptz, timestamptz)                        from public, anon;

-- maintenance_explain is anon-executable so a DEMO sandbox visitor can see
-- the explainability panel on their own sandbox; yf_can_read_site() refuses
-- every real site to an anon caller.
grant execute on function public.maintenance_explain(text)                                               to anon, authenticated, service_role;
grant execute on function public.maintenance_submit_feedback(text, text, text, text, text, numeric, text) to authenticated, service_role;
grant execute on function public.maintenance_accuracy(text, timestamptz, timestamptz)                    to authenticated, service_role;

select 'v0.13 EXPLAINABLE MAINTENANCE: maintenance_findings.factors/model_version/score/severity + maintenance_feedback; maintenance_explain()/maintenance_submit_feedback(engineer+)/maintenance_accuracy(engineer+)' as result;


-- ============================================================
-- ROLLBACK (uncomment to run) — drop the explainability objects.
-- The added columns on maintenance_findings are LEFT IN PLACE by default:
-- they hold real model evidence. Uncomment the final ALTERs to drop them.
-- ------------------------------------------------------------
-- drop function if exists public.maintenance_accuracy(text, timestamptz, timestamptz);
-- drop function if exists public.maintenance_submit_feedback(text, text, text, text, text, numeric, text);
-- drop function if exists public.maintenance_explain(text);
-- drop policy   if exists maintenance_feedback_read on public.maintenance_feedback;
-- drop table    if exists public.maintenance_feedback;
-- drop function if exists public.yf_factors_problem(jsonb);
--
-- -- destructive, opt-in-within-the-rollback:
-- -- alter table public.maintenance_findings drop constraint if exists maintenance_findings_factors_array;
-- -- alter table public.maintenance_findings drop constraint if exists maintenance_findings_severity_vocab;
-- -- alter table public.maintenance_findings drop column if exists factors;
-- -- alter table public.maintenance_findings drop column if exists model_version;
-- -- alter table public.maintenance_findings drop column if exists score;
-- -- alter table public.maintenance_findings drop column if exists severity;
--
-- select 'ROLLED BACK 0013: maintenance explainability + feedback removed' as result;
-- ============================================================
