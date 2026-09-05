"""The conformance check battery.

Every check is a pure function of one :class:`~yantraconform.observe.Observation`
-- the evidence collected from (and the probes sent to) a single vehicle. No
check touches the network, a clock, or global state, so each one can be unit
tested against three lines of synthetic evidence.

A check declares its identity ONCE, in :data:`CATALOGUE`, so
``yantra-conform checks`` can print the full rule set (id, category, severity,
spec reference, expected behaviour, remediation) without a broker anywhere in
sight -- which is what a web layer renders as the "what we test" page.

DOUBLE-COUNTING POLICY
----------------------
A single defect must cost a vehicle once, not fifteen times. So when the
evidence a family of checks depends on is entirely absent, one check FAILS for
the absence and the rest report ``skip``. Example: an AGV that never publishes
``state`` fails ``state.published`` (critical) and skips the other eighteen
state checks, rather than failing all nineteen and scoring a misleadingly
catastrophic 0 on things that were never observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import spec
from .model import CheckResult
from .observe import Observation, Received

MAX_EXAMPLES = 3


@dataclass(frozen=True)
class CheckSpec:
    """Static identity of one check (no results, no evidence)."""

    id: str
    title: str
    category: str
    severity: str
    spec_ref: str
    expected: str
    remediation: str
    #: the evaluator; set by the @check decorator
    fn: Callable[[Observation], "CheckResult | None"] | None = field(
        default=None, compare=False, repr=False)

    def result(self, status: str, observed: str, *,
               remediation: str | None = None,
               detail: dict[str, Any] | None = None) -> CheckResult:
        return CheckResult(
            id=self.id, title=self.title, category=self.category,
            severity=self.severity, spec_ref=self.spec_ref, status=status,
            expected=self.expected, observed=observed,
            remediation="" if status == "pass" else (
                self.remediation if remediation is None else remediation),
            detail=detail or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "category": self.category,
                "severity": self.severity, "spec_ref": self.spec_ref,
                "expected": self.expected, "remediation": self.remediation}


CATALOGUE: list[CheckSpec] = []


def check(id: str, title: str, category: str, severity: str, spec_ref: str,
          expected: str, remediation: str):
    """Register one check function in :data:`CATALOGUE`."""

    def deco(fn: Callable[[CheckSpec, Observation], CheckResult]):
        cs = CheckSpec(id=id, title=title, category=category,
                       severity=severity, spec_ref=spec_ref,
                       expected=expected, remediation=remediation)
        bound = CheckSpec(**{**cs.__dict__, "fn": lambda obs: fn(cs, obs)})
        CATALOGUE.append(bound)
        return fn

    return deco


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _summarize(problems: list[str], total: int, offenders: int) -> str:
    head = "; ".join(problems[:MAX_EXAMPLES])
    if len(problems) > MAX_EXAMPLES:
        head += f"; (+{len(problems) - MAX_EXAMPLES} more)"
    return f"{offenders}/{total} message(s) affected: {head}"


def _payloads(recs: list[Received]) -> list[dict[str, Any]]:
    return [r.payload for r in recs if r.payload is not None]


def _order_states(obs: Observation, probe_name: str,
                  order_id: str) -> list[dict[str, Any]]:
    """States published after ``probe_name`` that are about ``order_id``."""
    ev = obs.probe(probe_name)
    if ev is None:
        return []
    return [r.payload for r in obs.states_after(ev)
            if r.payload and r.payload.get("orderId") == order_id]


def _int_or_none(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def _order_before(obs: Observation, ev) -> tuple[str | None, int | None]:
    """The (orderId, orderUpdateId) the AGV last reported BEFORE probe ``ev``.

    This is the only honest baseline for the order checks. The tester knows
    what it dispatched, but a vehicle that is also its own master control (or
    that was re-dispatched by a real one mid-run) can legitimately have moved
    on to a different order, and grading against what the tester ASSUMED is
    in progress produces confident, wrong findings.
    """
    for rec in reversed(obs.states[:ev.state_index]):
        p = rec.payload or {}
        order_id = p.get("orderId")
        if isinstance(order_id, str) and order_id:
            return order_id, _int_or_none(p.get("orderUpdateId"))
    return None, None


def _moved_on(obs: Observation, ev, order_id: str) -> str | None:
    """The other orderId the AGV had adopted before ``ev``, if it had."""
    before_id, _ = _order_before(obs, ev)
    if before_id and before_id != order_id:
        return before_id
    return None


def _switched_away(obs: Observation, ev, order_id: str) -> str | None:
    """Another orderId the AGV adopted DURING the response window, if any.

    A vehicle that re-plans (or that is also its own master control, as this
    repo's simulator is) can move off the tester's order between the probe and
    the reply. The order in progress is then no longer the one being updated,
    so the update result is unevaluable -- which is reported as such rather
    than as a failure the vendor cannot act on.
    """
    for rec in obs.states_after(ev):
        other = (rec.payload or {}).get("orderId")
        if isinstance(other, str) and other and other != order_id:
            return other
    return None


def _terminal_status(obs: Observation, action_id: str) -> str | None:
    """Last reported actionStatus for ``action_id`` if it is terminal."""
    history = obs.action_states().get(action_id) or []
    if not history:
        return None
    last = str(history[-1][1].get("actionStatus") or "").upper()
    return last if last in spec.TERMINAL_ACTION_STATUSES else None


def _instant_probe_result(cs: CheckSpec, obs: Observation, probe_name: str,
                          *, want: str = "FINISHED",
                          failed_is: str = "warn") -> CheckResult:
    """Shared shape for 'did the AGV acknowledge this instantAction?'."""
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe(probe_name)
    if ev is None:
        return cs.result("skip", "probe was not sent")
    action_id = str(ev.data.get("action_id") or "")
    history = obs.action_states().get(action_id) or []
    if not history:
        return cs.result(
            "fail",
            f"no actionStates entry for actionId {action_id!r} in the "
            f"{len(obs.states_after(ev))} state message(s) that followed",
            detail={"action_id": action_id})
    statuses = [str(a.get("actionStatus") or "").upper() for _, a in history]
    last = statuses[-1]
    detail = {"action_id": action_id, "statuses": statuses,
              "resultDescription": history[-1][1].get("resultDescription")}
    if last == want:
        return cs.result("pass", f"reported {' -> '.join(statuses)}", detail=detail)
    if last in spec.TERMINAL_ACTION_STATUSES:
        return cs.result(
            failed_is,
            f"reported terminal {last} rather than {want}"
            + (f" ({history[-1][1].get('resultDescription')})"
               if history[-1][1].get("resultDescription") else ""),
            detail=detail)
    return cs.result("fail", f"never reached a terminal status (last {last})",
                     detail=detail)


# ---------------------------------------------------------------------------
# connection topic
# ---------------------------------------------------------------------------

@check("connection.published", "Connection topic is published", "connection",
       "critical", "VDA 5050 2.1 §5.3",
       "The AGV publishes a connection message on "
       "<interface>/<major>/<manufacturer>/<serial>/connection.",
       "Publish an ONLINE connection message on connect, OFFLINE on clean "
       "shutdown, and register CONNECTIONBROKEN as the MQTT last-will.")
def _connection_published(cs, obs):
    if obs.connections:
        return cs.result("pass", f"{len(obs.connections)} connection message(s) seen")
    return cs.result("fail", "no connection message observed on this vehicle's "
                             "connection topic")


@check("connection.state_enum", "connectionState uses the defined vocabulary",
       "connection", "critical", "VDA 5050 2.1 §5.3",
       "connectionState is exactly one of ONLINE, OFFLINE, CONNECTIONBROKEN.",
       "Emit the literal uppercase enum values; a fleet manager keys liveness "
       "off this field and cannot guess at custom strings.")
def _connection_state_enum(cs, obs):
    if not obs.connections:
        return cs.result("skip", "no connection message observed")
    bad = [str(p.get("connectionState"))
           for p in _payloads(obs.connections)
           if p.get("connectionState") not in spec.CONNECTION_STATES]
    if bad:
        return cs.result("fail",
                         f"{len(bad)} message(s) carried a value outside the "
                         f"enum, e.g. {bad[0]!r}",
                         detail={"unexpected": sorted(set(bad))[:MAX_EXAMPLES]})
    seen = sorted({str(p.get("connectionState")) for p in _payloads(obs.connections)})
    return cs.result("pass", f"observed {', '.join(seen)}", detail={"seen": seen})


@check("connection.online", "ONLINE is announced", "connection", "major",
       "VDA 5050 2.1 §5.3",
       "The AGV announces ONLINE once its MQTT session is up.",
       "Publish connectionState ONLINE immediately after CONNACK, and again "
       "after every reconnect, so a fleet manager that subscribed late still "
       "learns the vehicle is alive.")
def _connection_online(cs, obs):
    if not obs.connections:
        return cs.result("skip", "no connection message observed")
    states = [p.get("connectionState") for p in _payloads(obs.connections)]
    if "ONLINE" in states:
        return cs.result("pass", "ONLINE observed")
    return cs.result("fail", f"connection states seen: {sorted(set(map(str, states)))}")


@check("connection.retained", "Connection message is retained", "connection",
       "major", "VDA 5050 2.1 §5.3",
       "The connection topic is published with the MQTT retain flag so a "
       "late-joining fleet manager immediately learns vehicle liveness.",
       "Set retain=1 (and QoS 1) when publishing the connection topic. "
       "Without it, a fleet manager that restarts sees no vehicles at all "
       "until each one happens to reconnect.")
def _connection_retained(cs, obs):
    if not obs.retain_probed:
        return cs.result("skip", "retain probe did not run")
    got = obs.retain_probe.get("connection")
    if got is None:
        return cs.result("skip", "retain probe did not cover the connection topic")
    if got:
        return cs.result("pass", "a retained copy was delivered on re-subscribe")
    return cs.result("fail", "re-subscribing to the connection topic delivered "
                             "no retained message")


@check("connection.identity", "Connection header identity matches the topic",
       "connection", "major", "VDA 5050 2.1 §5.2, §5.4",
       "manufacturer and serialNumber in the payload equal the corresponding "
       "topic segments.",
       "Derive the header identity from the same configuration that builds the "
       "topic path; a mismatch makes messages unroutable for any fleet manager "
       "that keys off the topic (which the spec says is authoritative).")
def _connection_identity(cs, obs):
    return _identity_check(cs, obs, obs.connections, "connection")


@check("connection.lastwill", "CONNECTIONBROKEN is registered as a last-will",
       "connection", "minor", "VDA 5050 2.1 §5.3",
       "The AGV registers an MQTT last-will publishing CONNECTIONBROKEN on its "
       "own connection topic, so an unexpected drop is visible immediately.",
       "Call will_set(<prefix>/connection, {...\"connectionState\": "
       "\"CONNECTIONBROKEN\"}, qos=1, retain=True) BEFORE connecting. Without "
       "it a crashed vehicle stays ONLINE on the wire until the fleet manager's "
       "own staleness timer fires.")
def _connection_lastwill(cs, obs):
    if not obs.wills_observable:
        return cs.result(
            "skip",
            "a last-will is registered inside the broker and is not visible to "
            "another MQTT client; verify it by killing the vehicle's network "
            "and watching its connection topic")
    prefix = obs.topic_prefix + "/connection"
    for topic, payload in obs.wills:
        if topic == prefix:
            if "CONNECTIONBROKEN" in payload:
                return cs.result("pass", "last-will publishes CONNECTIONBROKEN")
            return cs.result("fail", "a last-will is registered on the connection "
                                     "topic but does not carry CONNECTIONBROKEN",
                             detail={"payload": payload[:200]})
    return cs.result("fail", "no last-will registered on this vehicle's "
                             "connection topic")


# ---------------------------------------------------------------------------
# state topic
# ---------------------------------------------------------------------------

@check("state.published", "State topic is published", "state", "critical",
       "VDA 5050 2.1 §6.4",
       "The AGV publishes state messages continuously (at least on every "
       "significant change, and at its declared minStateInterval).",
       "Publish the state topic on change AND on a heartbeat interval. A fleet "
       "manager has no other way to know where the vehicle is.")
def _state_published(cs, obs):
    if obs.states:
        return cs.result("pass", f"{len(obs.states)} state message(s) observed")
    return cs.result("fail", "no state message observed during the run")


@check("state.required_fields", "State carries every required field", "state",
       "critical", "VDA 5050 2.1 §6.4",
       "Every state message contains the required fields: "
       + ", ".join(sorted(spec.STATE_REQUIRED_FIELDS)) + ".",
       "Populate every required field on every publish, including the empty "
       "cases (errors: [], nodeStates: [], actionStates: []). Omitting a field "
       "is not the same as sending an empty array, and consumers written to "
       "the spec will KeyError on it.")
def _state_required_fields(cs, obs):
    if not obs.states:
        return cs.result("skip", "no state message observed")
    problems, offenders = [], 0
    for p in _payloads(obs.states):
        probs = spec.field_problems(p, spec.STATE_REQUIRED_FIELDS)
        if probs:
            offenders += 1
            problems.extend(probs)
    if problems:
        return cs.result("fail", _summarize(sorted(set(problems)), len(obs.states),
                                            offenders),
                         detail={"problems": sorted(set(problems))})
    return cs.result("pass", f"all {len(obs.states)} state message(s) complete")


@check("state.field_types", "State sub-objects are correctly typed", "state",
       "major", "VDA 5050 2.1 §6.4",
       "batteryState, safetyState, agvPosition, actionStates[] and errors[] "
       "contain their required members with the spec's types.",
       "Check the JSON types, not just presence: booleans must be JSON true/"
       "false (not \"true\"), numbers must be numbers (not strings), and "
       "batteryCharge is a percentage, not a fraction.")
def _state_field_types(cs, obs):
    if not obs.states:
        return cs.result("skip", "no state message observed")
    problems, offenders = [], 0
    for p in _payloads(obs.states):
        probs: list[str] = []
        for name, req in (("batteryState", spec.BATTERY_STATE_REQUIRED),
                          ("safetyState", spec.SAFETY_STATE_REQUIRED)):
            block = p.get(name)
            if isinstance(block, dict):
                probs.extend(spec.field_problems(block, req, where=name))
        pos = p.get("agvPosition")
        if isinstance(pos, dict):
            probs.extend(spec.field_problems(pos, spec.AGV_POSITION_REQUIRED,
                                             where="agvPosition"))
        for i, a in enumerate(p.get("actionStates") or []):
            if not isinstance(a, dict):
                probs.append(f"actionStates[{i}] is not an object")
                continue
            probs.extend(spec.field_problems(a, spec.ACTION_STATE_REQUIRED,
                                             where=f"actionStates[{i}]"))
        for i, e in enumerate(p.get("errors") or []):
            if not isinstance(e, dict):
                probs.append(f"errors[{i}] is not an object")
                continue
            probs.extend(spec.field_problems(e, spec.ERROR_REQUIRED,
                                             where=f"errors[{i}]"))
        if probs:
            offenders += 1
            problems.extend(probs)
    if problems:
        return cs.result("fail", _summarize(sorted(set(problems)), len(obs.states),
                                            offenders),
                         detail={"problems": sorted(set(problems))})
    return cs.result("pass", "every sub-object matched the spec's types")


@check("state.recommended_fields", "State carries the practically-required "
       "optional fields", "state", "minor", "VDA 5050 2.1 §6.4",
       "agvPosition, velocity, paused and newBaseRequest are present. The spec "
       "marks them optional; every fleet manager needs them.",
       "Add the missing blocks. Without agvPosition there is no map view; "
       "without newBaseRequest the fleet manager cannot know when to extend "
       "the vehicle's base.")
def _state_recommended(cs, obs):
    if not obs.states:
        return cs.result("skip", "no state message observed")
    missing = set()
    for p in _payloads(obs.states):
        for name in spec.STATE_RECOMMENDED_FIELDS:
            if name not in p or p[name] is None:
                missing.add(name)
    if missing:
        return cs.result("warn", f"never observed: {', '.join(sorted(missing))}",
                         detail={"missing": sorted(missing)})
    return cs.result("pass", "all present")


@check("state.header", "State carries a well-formed header", "state", "major",
       "VDA 5050 2.1 §5.4",
       "headerId (integer), timestamp (string), version, manufacturer and "
       "serialNumber are present on every message.",
       "Emit the full five-field header on every topic, including connection "
       "and factsheet. headerId must be a JSON integer, not a string.")
def _state_header(cs, obs):
    return _header_check(cs, obs, obs.states, "state")


@check("state.header_id_monotonic", "headerId increases monotonically", "state",
       "major", "VDA 5050 2.1 §5.4",
       "headerId increases by at least 1 with every message published on the "
       "same topic, so a consumer can detect loss and reordering.",
       "Keep one counter PER TOPIC and increment it on every publish. Sharing "
       "a counter across topics, resetting on reconnect, or reusing an id makes "
       "message-loss detection impossible downstream.")
def _state_header_monotonic(cs, obs):
    if len(obs.states) < 2:
        return cs.result("skip", f"only {len(obs.states)} state message(s) "
                                 "observed; need at least 2")
    ids = [p.get("headerId") for p in _payloads(obs.states)]
    problems = []
    last = None
    for i, hid in enumerate(ids):
        if isinstance(hid, bool) or not isinstance(hid, int):
            problems.append(f"state[{i}].headerId is not an integer ({hid!r})")
            continue
        if last is not None and hid <= last:
            problems.append(f"state[{i}].headerId={hid} did not increase "
                            f"(previous {last})")
        last = hid
    if problems:
        return cs.result("fail", _summarize(problems, len(ids), len(problems)),
                         detail={"header_ids": ids[:20]})
    return cs.result("pass", f"strictly increasing across {len(ids)} messages "
                             f"({ids[0]} -> {ids[-1]})")


@check("state.timestamp_format", "timestamp is ISO-8601 UTC", "state", "major",
       "VDA 5050 2.1 §5.4",
       "timestamp is ISO-8601 in UTC with a trailing 'Z', e.g. "
       "2026-08-26T10:15:32.512Z.",
       "Format with UTC and a literal 'Z' suffix. Local time, a naive "
       "timestamp, a '+00:00' offset or an epoch number all break consumers "
       "that parse to the spec.")
def _state_timestamp_format(cs, obs):
    if not obs.states:
        return cs.result("skip", "no state message observed")
    problems, offenders = [], 0
    for i, p in enumerate(_payloads(obs.states)):
        problem = spec.timestamp_problem(p.get("timestamp"))
        if problem:
            offenders += 1
            problems.append(f"{p.get('timestamp')!r}: {problem}")
    if problems:
        return cs.result("fail", _summarize(sorted(set(problems)),
                                            len(obs.states), offenders))
    return cs.result("pass", f"all {len(obs.states)} timestamps conformant "
                             f"(e.g. {_payloads(obs.states)[0].get('timestamp')})")


@check("state.timestamp_monotonic", "timestamps do not go backwards", "state",
       "minor", "VDA 5050 2.1 §5.4",
       "Successive state messages carry non-decreasing timestamps.",
       "Stamp the message at publish time from a monotonic UTC source. A "
       "backwards jump makes any time-series view of the fleet unusable.")
def _state_timestamp_monotonic(cs, obs):
    ts = [spec.parse_timestamp(p.get("timestamp")) for p in _payloads(obs.states)]
    ts = [t for t in ts if t is not None]
    if len(ts) < 2:
        return cs.result("skip", "fewer than 2 parseable timestamps")
    backwards = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    if backwards:
        return cs.result("fail", f"{backwards} backwards step(s) across "
                                 f"{len(ts)} timestamps")
    return cs.result("pass", f"non-decreasing across {len(ts)} timestamps")


@check("state.identity", "State header identity matches the topic", "state",
       "critical", "VDA 5050 2.1 §5.2, §5.4",
       "manufacturer and serialNumber in the payload equal the corresponding "
       "topic segments.",
       "The topic path is authoritative. Build both from one source of truth, "
       "or a fleet manager subscribing per-vehicle will attribute your data to "
       "the wrong robot.")
def _state_identity(cs, obs):
    return _identity_check(cs, obs, obs.states, "state")


@check("state.serial_charset", "serialNumber uses the permitted charset",
       "state", "minor", "VDA 5050 2.1 §5.2",
       "serialNumber contains only characters legal in an MQTT topic segment "
       "(A-Z a-z 0-9 _ . : -).",
       "Sanitize the vehicle id before it becomes a topic segment; '/' and '+' "
       "and '#' silently change the topic's meaning rather than erroring.")
def _state_serial_charset(cs, obs):
    if not spec.is_legal_serial(obs.serial):
        return cs.result("fail", f"topic serial {obs.serial!r} contains "
                                 "characters outside the permitted charset")
    bad = sorted({str(p.get("serialNumber")) for p in _payloads(obs.states)
                  if not spec.is_legal_serial(str(p.get("serialNumber") or ""))})
    if bad:
        return cs.result("fail", f"payload serialNumber(s) outside the charset: "
                                 f"{bad[:MAX_EXAMPLES]}")
    return cs.result("pass", f"{obs.serial!r} is a legal topic segment")


@check("state.version_field", "version is a valid, in-scope protocol version",
       "state", "major", "VDA 5050 2.1 §5.4",
       "version is <major>.<minor>.<patch> and its major matches the "
       "majorVersion topic segment.",
       "Report the protocol version you actually implement, and keep the "
       "'v<major>' topic segment in step with it. A mismatch means a fleet "
       "manager subscribed to v2 topics is being handed v1 or v3 payloads.")
def _state_version(cs, obs):
    if not obs.states:
        return cs.result("skip", "no state message observed")
    versions = sorted({str(p.get("version")) for p in _payloads(obs.states)})
    bad = [v for v in versions if spec.parse_version(v) is None]
    if bad:
        return cs.result("fail", f"unparseable version field(s): {bad}")
    seg_major = obs.major[1:] if spec.is_major_segment(obs.major) else None
    mismatched = [v for v in versions
                  if seg_major is not None and str(spec.parse_version(v)[0]) != seg_major]
    if mismatched:
        return cs.result("fail", f"version {mismatched} does not match the "
                                 f"'{obs.major}' topic segment")
    return cs.result("pass", f"version {', '.join(versions)} on '{obs.major}' topics")


@check("state.not_retained", "State is NOT retained", "state", "minor",
       "VDA 5050 2.1 §5.3",
       "The state topic is published without the retain flag: it is a live "
       "value, not a configuration fact.",
       "Publish state with retain=0. A retained state message hands every "
       "reconnecting consumer a stale pose, which reads as a robot that has "
       "teleported or frozen.")
def _state_not_retained(cs, obs):
    if not obs.retain_probed:
        return cs.result("skip", "retain probe did not run")
    got = obs.retain_probe.get("state")
    if got is None:
        return cs.result("skip", "retain probe did not cover the state topic")
    if got:
        return cs.result("fail", "re-subscribing delivered a RETAINED state "
                                 "message; state must not be retained")
    return cs.result("pass", "no retained copy on the broker")


@check("state.agv_position", "agvPosition is well-formed", "state", "minor",
       "VDA 5050 2.1 §6.4",
       "agvPosition carries x, y, theta (normalized to [-pi, pi]), mapId and "
       "positionInitialized.",
       "Normalize theta into [-pi, pi] before publishing, and report "
       "positionInitialized honestly -- a fleet manager uses it to decide "
       "whether the pose can be trusted at all.")
def _state_agv_position(cs, obs):
    positions = [p.get("agvPosition") for p in _payloads(obs.states)
                 if isinstance(p.get("agvPosition"), dict)]
    if not positions:
        return cs.result("skip", "no agvPosition observed")
    problems = set()
    for pos in positions:
        problems.update(spec.field_problems(pos, spec.AGV_POSITION_REQUIRED))
        if "theta" in pos and not spec.theta_in_range(pos.get("theta")):
            problems.add(f"theta={pos.get('theta')!r} is outside [-pi, pi]")
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(positions),
                                            len(problems)))
    return cs.result("pass", f"{len(positions)} position(s) well-formed")


@check("state.battery_range", "batteryState is plausible", "state", "minor",
       "VDA 5050 2.1 §6.4",
       "batteryCharge is a percentage in 0..100 and charging is a boolean.",
       "Report batteryCharge as a percentage, not a 0..1 fraction and not a "
       "voltage. Operators read this number directly.")
def _state_battery(cs, obs):
    batteries = [p.get("batteryState") for p in _payloads(obs.states)
                 if isinstance(p.get("batteryState"), dict)]
    if not batteries:
        return cs.result("skip", "no batteryState observed")
    problems = set()
    for b in batteries:
        problems.update(spec.field_problems(b, spec.BATTERY_STATE_REQUIRED))
        charge = b.get("batteryCharge")
        if isinstance(charge, (int, float)) and not isinstance(charge, bool):
            if not 0.0 <= float(charge) <= 100.0:
                problems.add(f"batteryCharge={charge} is outside 0..100")
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(batteries),
                                            len(problems)))
    charges = [float(b["batteryCharge"]) for b in batteries
               if isinstance(b.get("batteryCharge"), (int, float))
               and not isinstance(b.get("batteryCharge"), bool)]
    # A 0..1 fraction is numerically "in range" but means something else
    # entirely: 0.84 read as a percentage is a vehicle about to strand. It
    # cannot be proven from the wire (a genuinely dying battery reports 0.8%),
    # so it is a warning with the evidence attached, never a hard failure.
    if len(charges) >= 3 and 0.0 < max(charges) <= 1.0:
        return cs.result(
            "warn",
            f"every batteryCharge observed was between 0 and 1 (max "
            f"{max(charges)}), which looks like a 0..1 fraction reported where "
            "the spec expects a 0..100 percentage",
            remediation="Multiply by 100 if you are publishing a fraction. If "
                        "the pack really is under 1% charged, ignore this "
                        "warning -- and send the vehicle to a charger.",
            detail={"observed_charges": charges[:10]})
    return cs.result("pass", f"{len(batteries)} reading(s) in range")


@check("state.errors_wellformed", "errors[] entries are well-formed", "state",
       "major", "VDA 5050 2.1 §6.4",
       "Every errors[] entry has an errorType and an errorLevel of WARNING or "
       "FATAL.",
       "Populate errorType (a stable machine key) and errorLevel on every "
       "error, and add errorDescription/errorHint -- that hint is what an "
       "operator on the floor actually reads.")
def _state_errors(cs, obs):
    errors = [e for p in _payloads(obs.states) for e in (p.get("errors") or [])]
    if not errors:
        return cs.result("pass", "no errors reported during the run "
                                 "(vacuously conformant)")
    problems = set()
    for e in errors:
        if not isinstance(e, dict):
            problems.add("an errors[] entry is not an object")
            continue
        problems.update(spec.field_problems(e, spec.ERROR_REQUIRED))
        level = str(e.get("errorLevel") or "").upper()
        if level and level not in spec.ERROR_LEVELS:
            problems.add(f"errorLevel {level!r} is outside "
                         f"{list(spec.ERROR_LEVELS)}")
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(errors),
                                            len(problems)))
    kinds = sorted({str(e.get("errorType")) for e in errors})
    return cs.result("pass", f"{len(errors)} error(s), all well-formed "
                             f"({', '.join(kinds[:MAX_EXAMPLES])})")


@check("state.operating_mode", "operatingMode uses the defined vocabulary",
       "state", "minor", "VDA 5050 2.1 §6.4",
       "operatingMode is one of " + ", ".join(spec.OPERATING_MODES) + ".",
       "Map your vehicle's internal mode onto the spec's five values; a fleet "
       "manager gates operator commands on this field.")
def _state_operating_mode(cs, obs):
    modes = {str(p.get("operatingMode")) for p in _payloads(obs.states)
             if p.get("operatingMode") is not None}
    if not modes:
        return cs.result("skip", "operatingMode never observed")
    bad = sorted(m for m in modes if m not in spec.OPERATING_MODES)
    if bad:
        return cs.result("fail", f"values outside the enum: {bad}")
    return cs.result("pass", f"observed {', '.join(sorted(modes))}")


@check("state.safety_state", "safetyState is well-formed", "state", "major",
       "VDA 5050 2.1 §6.4",
       "safetyState.eStop is one of " + ", ".join(spec.ESTOP_VALUES)
       + " and fieldViolation is a boolean.",
       "Report eStop with the spec's vocabulary rather than a boolean or a "
       "vendor string; safety state is the one field a fleet manager must "
       "never have to guess at.")
def _state_safety(cs, obs):
    blocks = [p.get("safetyState") for p in _payloads(obs.states)
              if isinstance(p.get("safetyState"), dict)]
    if not blocks:
        return cs.result("skip", "no safetyState observed")
    problems = set()
    for b in blocks:
        problems.update(spec.field_problems(b, spec.SAFETY_STATE_REQUIRED))
        estop = b.get("eStop")
        if isinstance(estop, str) and estop.upper() not in spec.ESTOP_VALUES:
            problems.add(f"eStop {estop!r} is outside {list(spec.ESTOP_VALUES)}")
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(blocks),
                                            len(problems)))
    return cs.result("pass", f"{len(blocks)} safetyState block(s) conformant")


@check("state.sequence_ids", "nodeStates/edgeStates sequenceIds are numbered "
       "correctly", "state", "minor", "VDA 5050 2.1 §6.6",
       "sequenceIds ascend within each list, with nodes on even and edges on "
       "odd values.",
       "Number the order's nodes and edges alternately from the base "
       "(node 0, edge 1, node 2, ...) and echo those same ids in state; the "
       "fleet manager matches its order graph to your progress by them.")
def _state_sequence_ids(cs, obs):
    payloads = [p for p in _payloads(obs.states)
                if p.get("nodeStates") or p.get("edgeStates")]
    if not payloads:
        return cs.result("skip", "no non-empty nodeStates/edgeStates observed")
    problems = set()
    for p in payloads:
        problems.update(spec.sequence_ids_wellformed(
            p.get("nodeStates") or [], p.get("edgeStates") or []))
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(payloads),
                                            len(problems)))
    return cs.result("pass", f"{len(payloads)} message(s) with a horizon, all "
                             "correctly numbered")


# ---------------------------------------------------------------------------
# order handling
# ---------------------------------------------------------------------------

@check("order.accept_new", "A new order is accepted", "order", "critical",
       "VDA 5050 2.1 §6.5",
       "After a valid order is published, the AGV adopts it and echoes its "
       "orderId in state.",
       "Subscribe to <prefix>/order and adopt a new orderId unconditionally "
       "(it always supersedes the current order). An AGV that ignores the "
       "order topic cannot be dispatched by any fleet manager.")
def _order_accept_new(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_new")
    if ev is None:
        return cs.result("skip", "no order probe was sent (no usable nodeId "
                                 "was discovered from the AGV's own state)")
    order_id = str(ev.data.get("order_id"))
    matched = _order_states(obs, "order_new", order_id)
    if matched:
        return cs.result("pass", f"orderId {order_id!r} echoed in "
                                 f"{len(matched)} state message(s)")
    seen = sorted({str(r.payload.get("orderId")) for r in obs.states_after(ev)
                   if r.payload})
    return cs.result("fail", f"orderId {order_id!r} never appeared in the "
                             f"{len(obs.states_after(ev))} following state "
                             f"message(s); saw {seen[:MAX_EXAMPLES]}",
                     detail={"order_id": order_id, "order_ids_seen": seen})


@check("order.accept_update", "An order update is accepted", "order", "major",
       "VDA 5050 2.1 §6.5",
       "A higher orderUpdateId for the current orderId is adopted and echoed.",
       "Accept an update whose orderUpdateId is strictly greater than the one "
       "in progress, and echo the new value in state. Without this a fleet "
       "manager cannot extend a vehicle's base while it drives.")
def _order_accept_update(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_update")
    if ev is None:
        return cs.result("skip", "no order-update probe was sent")
    order_id = str(ev.data.get("order_id"))
    want = int(ev.data.get("order_update_id") or 0)
    matched = _order_states(obs, "order_update", order_id)
    if not matched:
        other = _moved_on(obs, ev, order_id)
        if other:
            return cs.result(
                "skip",
                f"the AGV had already adopted a different orderId ({other!r}) "
                f"before the update was published, so the update never applied "
                "to an order in progress")
        return cs.result("fail", f"orderId {order_id!r} was not reported at all "
                                 "after the update")
    got = [p.get("orderUpdateId") for p in matched]
    if want in got:
        return cs.result("pass", f"orderUpdateId {want} echoed")
    switched = _switched_away(obs, ev, order_id)
    if switched:
        return cs.result(
            "skip",
            f"the AGV moved on to orderId {switched!r} during the response "
            f"window, so orderId {order_id!r} was no longer the order in "
            "progress and the update could not be evaluated",
            detail={"order_id": order_id, "switched_to": switched,
                    "seen": got[:20]})
    return cs.result("fail", f"orderUpdateId {want} never echoed; saw {got[:5]}",
                     detail={"order_id": order_id, "seen": got[:20]})


@check("order.reject_stale", "A stale orderUpdateId does not rewind the AGV",
       "order", "critical", "VDA 5050 2.1 §6.5",
       "An orderUpdateId lower than or equal to the one in progress is "
       "rejected; the AGV must not adopt it.",
       "Compare orderUpdateId against the value currently in progress and "
       "discard anything not strictly greater. MQTT redelivery and a "
       "master-control restart both replay old orders; an AGV that accepts "
       "them will drive a route it already finished.")
def _order_reject_stale(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_stale")
    if ev is None:
        return cs.result("skip", "no stale-update probe was sent")
    order_id = str(ev.data.get("order_id"))
    stale = int(ev.data.get("order_update_id") or 0)
    # The baseline is what the AGV ITSELF reported as the order IN PROGRESS
    # immediately before the replay -- never what the tester assumed. Replaying
    # an orderId the vehicle is not currently running is a NEW order, which
    # §6.5 says must be accepted, so grading it as a rewind would be wrong.
    before_id, live = _order_before(obs, ev)
    if before_id != order_id or live is None:
        moved = (f" (it was running {before_id!r})"
                 if before_id and before_id != order_id else "")
        return cs.result(
            "skip",
            f"the AGV was not running orderId {order_id!r} when the stale "
            f"update was replayed{moved}, so the replay was a new order rather "
            "than a stale update and staleness could not be exercised")
    if live <= stale:
        return cs.result(
            "skip",
            f"the AGV never advanced past orderUpdateId {live}, so replaying "
            f"{stale} was not actually a rewind")
    matched = _order_states(obs, "order_stale", order_id)
    if not matched:
        return cs.result("skip", f"the AGV stopped reporting orderId "
                                 f"{order_id!r} before the stale update could "
                                 "be evaluated")
    rewound = [p.get("orderUpdateId") for p in matched
               if isinstance(p.get("orderUpdateId"), int)
               and not isinstance(p.get("orderUpdateId"), bool)
               and p["orderUpdateId"] < live]
    if rewound:
        return cs.result("fail", f"orderUpdateId fell back to {rewound[0]} after "
                                 f"a stale update {stale} was replayed "
                                 f"(was {live})",
                         detail={"order_id": order_id, "stale": stale,
                                 "current": live, "observed": rewound[:5]})
    return cs.result("pass", f"held at orderUpdateId {live} after replaying "
                             f"update {stale}")


@check("order.reject_stale_reports_error", "A rejected order update is reported "
       "as an error", "order", "minor", "VDA 5050 2.1 §6.5",
       "Rejecting an order update raises an errors[] entry of type "
       "orderUpdateError so master control learns the update was dropped.",
       "Add an errors[] entry with errorType 'orderUpdateError' (errorLevel "
       "WARNING) when discarding a stale update. Silently ignoring it leaves "
       "master control believing the vehicle took an order it never did.")
def _order_stale_error(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_stale")
    if ev is None:
        return cs.result("skip", "no stale-update probe was sent")
    # Double-counting policy: if the replay was never actually stale (see
    # order.reject_stale), there was nothing to report an error about.
    order_id = str(ev.data.get("order_id"))
    stale = int(ev.data.get("order_update_id") or 0)
    before_id, live = _order_before(obs, ev)
    if before_id != order_id or live is None or live <= stale:
        return cs.result("skip", "the replayed update was not stale from the "
                                 "AGV's point of view (see order.reject_stale)")
    after = obs.states_after(ev)
    types = {str(e.get("errorType")) for r in after if r.payload
             for e in (r.payload.get("errors") or []) if isinstance(e, dict)}
    hit = [t for t in types if "orderupdate" in t.lower()]
    if hit:
        return cs.result("pass", f"reported {hit[0]}")
    return cs.result("fail", "no orderUpdateError raised after the stale update "
                             f"(errors seen: {sorted(types) or 'none'})",
                     detail={"error_types": sorted(types)})


@check("order.reject_invalid", "An unexecutable order is not adopted", "order",
       "major", "VDA 5050 2.1 §6.5",
       "An order referencing nodes the AGV cannot reach is rejected: its "
       "orderId must not become the vehicle's active order.",
       "Validate every node/edge id against your map before adopting an order, "
       "reject the whole order if any released node is unreachable, and report "
       "a validationError. Adopting it and then stalling looks identical to a "
       "hung vehicle from master control.")
def _order_reject_invalid(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_invalid")
    if ev is None:
        return cs.result("skip", "no invalid-order probe was sent")
    order_id = str(ev.data.get("order_id"))
    matched = _order_states(obs, "order_invalid", order_id)
    after = obs.states_after(ev)
    types = sorted({str(e.get("errorType")) for r in after if r.payload
                    for e in (r.payload.get("errors") or []) if isinstance(e, dict)})
    if matched:
        return cs.result("fail", f"the AGV adopted orderId {order_id!r}, which "
                                 "referenced a node id that does not exist",
                         detail={"order_id": order_id, "error_types": types})
    reported = [t for t in types if "valid" in t.lower() or "order" in t.lower()]
    if reported:
        return cs.result("pass", f"rejected, and reported {reported[0]}")
    return cs.result("pass", "rejected (no validationError raised, which the "
                             "spec recommends but does not require here)",
                     detail={"error_types": types})


@check("order.echo", "orderId/orderUpdateId are echoed with the right types",
       "order", "major", "VDA 5050 2.1 §6.4, §6.5",
       "state.orderId is the accepted order's id as a string and "
       "state.orderUpdateId is that order's update id as an integer.",
       "Echo the order identity verbatim. Coercing orderUpdateId to a string, "
       "or re-generating your own orderId, breaks every correlation the fleet "
       "manager does between what it dispatched and what is happening.")
def _order_echo(cs, obs):
    if not obs.active_probing:
        return cs.result("skip", "active probing disabled (--passive)")
    ev = obs.probe("order_new")
    if ev is None:
        return cs.result("skip", "no order probe was sent")
    order_id = str(ev.data.get("order_id"))
    matched = _order_states(obs, "order_new", order_id)
    if not matched:
        return cs.result("skip", "the order was never echoed (see "
                                 "order.accept_new)")
    problems = set()
    for p in matched:
        if not isinstance(p.get("orderId"), str):
            problems.add(f"orderId is {type(p.get('orderId')).__name__}, "
                         "expected string")
        ouid = p.get("orderUpdateId")
        if isinstance(ouid, bool) or not isinstance(ouid, int):
            problems.add(f"orderUpdateId is {type(ouid).__name__}, "
                         "expected integer")
    if problems:
        return cs.result("fail", "; ".join(sorted(problems)))
    return cs.result("pass", f"echoed {order_id!r} with an integer "
                             "orderUpdateId")


# ---------------------------------------------------------------------------
# actionStates lifecycle
# ---------------------------------------------------------------------------

@check("actions.status_enum", "actionStatus uses the defined vocabulary",
       "actions", "major", "VDA 5050 2.1 §6.9",
       "Every actionStatus is one of " + ", ".join(spec.ACTION_STATUSES) + ".",
       "Map your internal action lifecycle onto the spec's six statuses. A "
       "custom status is invisible to a compliant fleet manager, which will "
       "treat the action as never having finished.")
def _actions_status_enum(cs, obs):
    history = obs.action_states()
    if not history:
        return cs.result("skip", "no actionStates observed")
    bad = sorted({str(a.get("actionStatus")) for hs in history.values()
                  for _, a in hs
                  if str(a.get("actionStatus") or "").upper()
                  not in spec.ACTION_STATUSES})
    if bad:
        return cs.result("fail", f"statuses outside the enum: {bad[:MAX_EXAMPLES]}")
    seen = sorted({str(a.get("actionStatus")).upper() for hs in history.values()
                   for _, a in hs})
    return cs.result("pass", f"observed {', '.join(seen)}")


@check("actions.required_fields", "actionStates entries carry actionId, "
       "actionType and actionStatus", "actions", "major",
       "VDA 5050 2.1 §6.9",
       "Every actionStates entry has an actionId (string), an actionStatus, "
       "and an actionType identifying what is running.",
       "Include the actionId master control sent you, verbatim -- it is the "
       "only handle the fleet manager has to correlate the action it "
       "dispatched with the progress you are reporting.")
def _actions_required_fields(cs, obs):
    entries = [a for p in _payloads(obs.states)
               for a in (p.get("actionStates") or []) if isinstance(a, dict)]
    if not entries:
        return cs.result("skip", "no actionStates observed")
    problems = set()
    for a in entries:
        problems.update(spec.field_problems(a, spec.ACTION_STATE_REQUIRED))
        if not a.get("actionType"):
            problems.add("actionType missing (expected string)")
    if problems:
        return cs.result("fail", _summarize(sorted(problems), len(entries),
                                            len(problems)))
    return cs.result("pass", f"{len(entries)} actionStates entr(ies) complete")


@check("actions.terminal_reported", "Actions report a terminal status before "
       "disappearing", "actions", "major", "VDA 5050 2.1 §6.9",
       "An action that stops being reported must have reached FINISHED or "
       "FAILED first; actions never silently vanish from actionStates.",
       "Keep a finished action in actionStates with its terminal status until "
       "the next order arrives (or at minimum repeat it for a few state "
       "messages). Dropping it at completion means a fleet manager that lost "
       "one QoS-0 state message waits forever for an action that is done.")
def _actions_terminal(cs, obs):
    history = obs.action_states()
    if not history:
        return cs.result("skip", "no actionStates observed")
    last_index = len(obs.states) - 1
    vanished = []
    for action_id, hs in history.items():
        final_idx, final = hs[-1]
        status = str(final.get("actionStatus") or "").upper()
        if final_idx < last_index and status not in spec.TERMINAL_ACTION_STATUSES:
            vanished.append((action_id, status))
    if vanished:
        names = ", ".join(f"{aid} (last {st or 'unset'})"
                          for aid, st in vanished[:MAX_EXAMPLES])
        return cs.result("fail", f"{len(vanished)} action(s) disappeared without "
                                 f"a terminal status: {names}",
                         detail={"vanished": [a for a, _ in vanished]})
    still_running = [aid for aid, hs in history.items()
                     if str(hs[-1][1].get("actionStatus") or "").upper()
                     not in spec.TERMINAL_ACTION_STATUSES]
    note = (f"; {len(still_running)} still in flight at the end of the run"
            if still_running else "")
    return cs.result("pass", f"{len(history)} action(s) tracked{note}")


@check("actions.unique_ids", "actionId appears at most once per state message",
       "actions", "minor", "VDA 5050 2.1 §6.9",
       "Within one state message, each actionId appears in actionStates "
       "exactly once: the array is the current status of each action, not a "
       "log of status changes.",
       "Keep actionStates as a map keyed by actionId and REPLACE an entry when "
       "its status changes rather than appending a second one. Two entries for "
       "one actionId leave the fleet manager reading whichever it happens to "
       "see last, which is how an action appears to finish and then start "
       "running again.")
def _actions_unique_ids(cs, obs):
    entries = [p.get("actionStates") for p in _payloads(obs.states)
               if isinstance(p.get("actionStates"), list)]
    if not any(entries):
        return cs.result("skip", "no actionStates observed")
    offenders, examples = 0, set()
    for arr in entries:
        seen: set[str] = set()
        dupes = set()
        for a in arr:
            if not isinstance(a, dict):
                continue
            aid = a.get("actionId")
            if not isinstance(aid, str):
                continue
            if aid in seen:
                dupes.add(aid)
            seen.add(aid)
        if dupes:
            offenders += 1
            examples.update(dupes)
    if offenders:
        return cs.result(
            "fail",
            f"{offenders}/{len(entries)} state message(s) repeated an actionId: "
            f"{sorted(examples)[:MAX_EXAMPLES]}",
            detail={"duplicate_action_ids": sorted(examples)})
    return cs.result("pass", f"no repeated actionId across {len(entries)} "
                             "state message(s)")


@check("actions.no_regression", "Actions do not leave a terminal status",
       "actions", "minor", "VDA 5050 2.1 §6.9",
       "Once an action reports FINISHED or FAILED it never reverts to a "
       "non-terminal status.",
       "Treat FINISHED/FAILED as absorbing. Re-running an actionId after it "
       "terminated (rather than issuing a new one) makes the fleet manager's "
       "completion accounting wrong in both directions.")
def _actions_no_regression(cs, obs):
    history = obs.action_states()
    if not history:
        return cs.result("skip", "no actionStates observed")
    bad = []
    for action_id, hs in history.items():
        terminal_seen = False
        for _, a in hs:
            status = str(a.get("actionStatus") or "").upper()
            if terminal_seen and status not in spec.TERMINAL_ACTION_STATUSES:
                bad.append(f"{action_id} went back to {status}")
                break
            terminal_seen = terminal_seen or status in spec.TERMINAL_ACTION_STATUSES
    if bad:
        return cs.result("fail", "; ".join(bad[:MAX_EXAMPLES]))
    return cs.result("pass", "no terminal action regressed")


# ---------------------------------------------------------------------------
# instantActions
# ---------------------------------------------------------------------------

@check("instant.stateRequest", "stateRequest is answered", "instant", "major",
       "VDA 5050 2.1 §6.10",
       "A stateRequest instantAction is acknowledged in actionStates and "
       "reaches FINISHED.",
       "Subscribe to <prefix>/instantActions, publish a fresh state message "
       "immediately, and report the action FINISHED. Master control uses this "
       "to resynchronise after its own restart.")
def _instant_state_request(cs, obs):
    return _instant_probe_result(cs, obs, "instant_stateRequest")


@check("instant.factsheetRequest", "factsheetRequest is answered", "instant",
       "major", "VDA 5050 2.1 §6.10, §7",
       "A factsheetRequest instantAction is acknowledged and a factsheet "
       "message is published.",
       "Answer factsheetRequest by publishing the factsheet topic (QoS 1, "
       "retained) and reporting the action FINISHED. Without it a fleet "
       "manager cannot discover the vehicle's capabilities or limits.")
def _instant_factsheet_request(cs, obs):
    base = _instant_probe_result(cs, obs, "instant_factsheetRequest")
    if base.status in ("skip",):
        return base
    ev = obs.probe("instant_factsheetRequest")
    published = [r for r in obs.factsheets
                 if r.message.received_at >= (ev.data.get("sent_at") or 0)]
    if base.status == "pass" and not published:
        return cs.result("warn", "the action reported FINISHED but no factsheet "
                                 "message was published on the factsheet topic",
                         detail=base.detail)
    if base.status != "pass" and published:
        return cs.result("warn", "a factsheet was published but the action was "
                                 f"not acknowledged ({base.observed})",
                         detail=base.detail)
    if base.status == "pass":
        return cs.result("pass", f"acknowledged and {len(published)} factsheet "
                                 "message(s) published", detail=base.detail)
    return base


@check("instant.cancelOrder", "cancelOrder is honoured", "instant", "critical",
       "VDA 5050 2.1 §6.10.2",
       "A cancelOrder instantAction is acknowledged and reaches FINISHED once "
       "the vehicle has stopped executing the order.",
       "Implement cancelOrder: stop, drop the remaining nodes/edges, set every "
       "outstanding order action to FAILED, then report the cancelOrder action "
       "FINISHED. This is the fleet manager's only clean abort path.")
def _instant_cancel_order(cs, obs):
    return _instant_probe_result(cs, obs, "instant_cancelOrder")


@check("instant.initPosition", "initPosition is acknowledged", "instant",
       "major", "VDA 5050 2.1 §6.10",
       "An initPosition instantAction reaches a terminal status (FINISHED, or "
       "FAILED with a reason if the vehicle cannot accept it right now).",
       "Accept initPosition when stationary and set the pose from its "
       "actionParameters; when you cannot, still report FAILED with a "
       "resultDescription. Silence is the one unacceptable answer.")
def _instant_init_position(cs, obs):
    return _instant_probe_result(cs, obs, "instant_initPosition")


@check("instant.unknown_rejected", "An unsupported actionType is reported "
       "FAILED", "instant", "major", "VDA 5050 2.1 §6.9, §6.10",
       "An instantAction the vehicle does not implement is reported FAILED, "
       "not ignored and not FINISHED.",
       "Report FAILED with a resultDescription naming the unsupported "
       "actionType. Silently dropping it leaves master control waiting on an "
       "action that will never complete; reporting FINISHED is worse -- it "
       "claims work that never happened.")
def _instant_unknown(cs, obs):
    return _instant_probe_result(cs, obs, "instant_unknown", want="FAILED",
                                 failed_is="fail")


# ---------------------------------------------------------------------------
# factsheet
# ---------------------------------------------------------------------------

@check("factsheet.published", "Factsheet topic is published", "factsheet",
       "major", "VDA 5050 2.1 §7",
       "The AGV publishes a factsheet, either retained at startup or in "
       "response to factsheetRequest.",
       "Publish the factsheet topic with QoS 1 and retain=1 at startup. It is "
       "how a fleet manager learns your vehicle's speed limits, footprint, "
       "supported actions and protocol limits without a manual.")
def _factsheet_published(cs, obs):
    if obs.factsheets:
        return cs.result("pass", f"{len(obs.factsheets)} factsheet message(s) seen")
    return cs.result("fail", "no factsheet observed, including after an explicit "
                             "factsheetRequest")


@check("factsheet.required_blocks", "Factsheet contains every required block",
       "factsheet", "major", "VDA 5050 2.1 §7",
       "The factsheet contains " + ", ".join(spec.FACTSHEET_REQUIRED_BLOCKS) + ".",
       "Fill in every required block. agvGeometry and loadSpecification may be "
       "structurally empty if they genuinely do not apply, but the keys must "
       "be present or a spec-compliant reader cannot parse the document.")
def _factsheet_blocks(cs, obs):
    if not obs.factsheets:
        return cs.result("skip", "no factsheet observed")
    latest = _payloads(obs.factsheets)[-1]
    missing = [b for b in spec.FACTSHEET_REQUIRED_BLOCKS if b not in latest]
    if missing:
        return cs.result("fail", f"missing block(s): {', '.join(missing)}",
                         detail={"missing": missing,
                                 "present": sorted(latest.keys())})
    return cs.result("pass", "all six required blocks present")


@check("factsheet.type_specification", "typeSpecification is complete and uses "
       "the defined vocabularies", "factsheet", "minor", "VDA 5050 2.1 §7.1",
       "typeSpecification carries seriesName, agvKinematic, agvClass, "
       "maxLoadMass, localizationTypes and navigationTypes, with values drawn "
       "from the spec's enumerations.",
       "Use the spec's enum values (agvKinematic DIFF/OMNI/THREEWHEEL, "
       "agvClass FORKLIFT/CONVEYOR/TUGGER/CARRIER). A fleet manager sizes "
       "loads and plans routes off these.")
def _factsheet_type_spec(cs, obs):
    if not obs.factsheets:
        return cs.result("skip", "no factsheet observed")
    block = _payloads(obs.factsheets)[-1].get("typeSpecification")
    if not isinstance(block, dict):
        return cs.result("fail", "typeSpecification missing or not an object")
    problems = spec.field_problems(block, spec.FACTSHEET_TYPE_SPEC_REQUIRED)
    if block.get("agvKinematic") not in spec.AGV_KINEMATICS and "agvKinematic" in block:
        problems.append(f"agvKinematic {block.get('agvKinematic')!r} outside "
                        f"{list(spec.AGV_KINEMATICS)}")
    if block.get("agvClass") not in spec.AGV_CLASSES and "agvClass" in block:
        problems.append(f"agvClass {block.get('agvClass')!r} outside "
                        f"{list(spec.AGV_CLASSES)}")
    for name, allowed in (("localizationTypes", spec.LOCALIZATION_TYPES),
                          ("navigationTypes", spec.NAVIGATION_TYPES)):
        values = block.get(name)
        if isinstance(values, list):
            bad = [v for v in values if v not in allowed]
            if bad:
                problems.append(f"{name} contains {bad} outside {list(allowed)}")
    if problems:
        return cs.result("fail", "; ".join(problems[:MAX_EXAMPLES]),
                         detail={"problems": problems})
    return cs.result("pass", f"{block.get('seriesName')} / "
                             f"{block.get('agvKinematic')} / {block.get('agvClass')}")


@check("factsheet.physical_parameters", "physicalParameters is complete",
       "factsheet", "minor", "VDA 5050 2.1 §7.2",
       "physicalParameters carries speedMin, speedMax, accelerationMax, "
       "decelerationMax, heightMax, width and length as numbers.",
       "Publish real numbers from the vehicle datasheet. A fleet manager uses "
       "footprint and speed to plan traffic; placeholder zeros produce "
       "collisions on paper and stalls in practice.")
def _factsheet_physical(cs, obs):
    if not obs.factsheets:
        return cs.result("skip", "no factsheet observed")
    block = _payloads(obs.factsheets)[-1].get("physicalParameters")
    if not isinstance(block, dict):
        return cs.result("fail", "physicalParameters missing or not an object")
    problems = spec.field_problems(block, spec.FACTSHEET_PHYSICAL_REQUIRED)
    if problems:
        return cs.result("fail", "; ".join(problems[:MAX_EXAMPLES]),
                         detail={"problems": problems})
    return cs.result("pass", f"speedMax {block.get('speedMax')} m/s, footprint "
                             f"{block.get('length')}x{block.get('width')} m")


@check("factsheet.header", "Factsheet carries a well-formed header",
       "factsheet", "minor", "VDA 5050 2.1 §5.4, §7",
       "The factsheet carries the standard five-field header and its identity "
       "matches the topic.",
       "The factsheet is a normal VDA message: give it the same header every "
       "other topic gets.")
def _factsheet_header(cs, obs):
    header = _header_check(cs, obs, obs.factsheets, "factsheet")
    if header.status != "pass":
        return header
    return _identity_check(cs, obs, obs.factsheets, "factsheet")


@check("factsheet.retained", "Factsheet is retained", "factsheet", "minor",
       "VDA 5050 2.1 §5.3, §7",
       "The factsheet is published with the retain flag: it is static "
       "capability data a late subscriber must be able to read without asking.",
       "Publish the factsheet with retain=1. Otherwise every fleet-manager "
       "restart has to issue a factsheetRequest to every vehicle before it can "
       "do anything.")
def _factsheet_retained(cs, obs):
    if not obs.retain_probed:
        return cs.result("skip", "retain probe did not run")
    got = obs.retain_probe.get("factsheet")
    if got is None:
        return cs.result("skip", "retain probe did not cover the factsheet topic")
    if got:
        return cs.result("pass", "a retained copy was delivered on re-subscribe")
    if not obs.factsheets:
        return cs.result("skip", "no factsheet observed at all "
                                 "(see factsheet.published)")
    return cs.result("fail", "the factsheet was published but not retained")


# ---------------------------------------------------------------------------
# visualization (optional topic)
# ---------------------------------------------------------------------------

@check("visualization.wellformed", "visualization messages are usable",
       "visualization", "minor", "VDA 5050 2.1 §5.3",
       "If the optional visualization topic is used, its messages carry the "
       "standard header plus agvPosition and/or velocity.",
       "Either publish visualization with a header and a position/velocity "
       "payload at a high rate, or do not publish the topic at all. A "
       "header-less or empty visualization message is worse than none.")
def _visualization(cs, obs):
    if not obs.visualizations:
        return cs.result("skip", "optional visualization topic not used")
    problems = set()
    for p in _payloads(obs.visualizations):
        problems.update(spec.field_problems(p, spec.HEADER_FIELDS))
        if not any(isinstance(p.get(b), dict)
                   for b in spec.VISUALIZATION_PAYLOAD_BLOCKS):
            problems.add("carries neither agvPosition nor velocity")
    if problems:
        return cs.result("fail", _summarize(sorted(problems),
                                            len(obs.visualizations),
                                            len(problems)))
    return cs.result("pass", f"{len(obs.visualizations)} visualization "
                             "message(s), all usable")


# ---------------------------------------------------------------------------
# protocol hygiene
# ---------------------------------------------------------------------------

@check("protocol.topic_scheme", "Topics follow the five-segment scheme",
       "protocol", "critical", "VDA 5050 2.1 §5.2",
       "Every topic is interfaceName/majorVersion/manufacturer/serialNumber/"
       "topic, with a known sub-topic name.",
       "Publish on exactly the five-segment path. Extra segments, a missing "
       "version level, or a renamed sub-topic mean a compliant fleet manager's "
       "subscriptions simply never match.")
def _protocol_topic_scheme(cs, obs):
    seen = obs.topics_seen()
    if not seen:
        return cs.result("skip", "no messages observed for this vehicle")
    bad = [t for t in seen
           if (parts := spec.parse_topic(t)) is None
           or parts.subtopic not in spec.SUBTOPICS]
    if bad:
        return cs.result("fail", f"non-conformant topic(s): {bad[:MAX_EXAMPLES]}",
                         detail={"bad_topics": bad})
    return cs.result("pass", f"{len(seen)} topic(s), all conformant")


@check("protocol.major_version_segment", "The majorVersion topic segment is "
       "'v<major>'", "protocol", "major", "VDA 5050 2.1 §5.2",
       "The second topic segment is the literal 'v' followed by the protocol "
       "major version, e.g. 'v2'.",
       "Use 'v2' (not '2', not '2.1.0', not 'v2.1'). This segment is how a "
       "fleet manager subscribes to one protocol generation at a time.")
def _protocol_major_segment(cs, obs):
    if not spec.is_major_segment(obs.major):
        return cs.result("fail", f"segment is {obs.major!r}, expected 'v<major>' "
                                 "(e.g. 'v2')")
    return cs.result("pass", f"'{obs.major}'")


@check("protocol.version_consistency", "One protocol version across all topics",
       "protocol", "minor", "VDA 5050 2.1 §5.4",
       "state, connection and factsheet all report the same version string.",
       "Single-source the version constant. Different versions on different "
       "topics means one of the publishers was updated and the others were "
       "not -- which is exactly the bug this check exists to catch.")
def _protocol_version_consistency(cs, obs):
    versions: dict[str, set[str]] = {}
    for label, recs in (("state", obs.states), ("connection", obs.connections),
                        ("factsheet", obs.factsheets),
                        ("visualization", obs.visualizations)):
        for p in _payloads(recs):
            if isinstance(p.get("version"), str):
                versions.setdefault(label, set()).add(p["version"])
    distinct = {v for vs in versions.values() for v in vs}
    if not distinct:
        return cs.result("skip", "no version field observed")
    if len(distinct) > 1:
        detail = {k: sorted(v) for k, v in versions.items()}
        return cs.result("fail", f"conflicting versions: {detail}", detail=detail)
    return cs.result("pass", f"{distinct.pop()} everywhere")


@check("protocol.json_valid", "Every payload is a JSON object", "protocol",
       "critical", "VDA 5050 2.1 §5.4",
       "Every message payload parses as a JSON object.",
       "Publish UTF-8 JSON objects. A truncated, empty or non-object payload "
       "is dropped by any strict consumer, which then reports the vehicle as "
       "silent rather than as broken.")
def _protocol_json_valid(cs, obs):
    total = len(obs.all_received)
    if not total:
        return cs.result("skip", "no messages observed for this vehicle")
    if obs.malformed:
        examples = [f"{r.message.topic}: {r.parse_error}"
                    for r in obs.malformed[:MAX_EXAMPLES]]
        return cs.result("fail", f"{len(obs.malformed)}/{total} payload(s) "
                                 f"unparseable: {'; '.join(examples)}",
                         detail={"malformed": len(obs.malformed)})
    return cs.result("pass", f"{total}/{total} payloads parsed")


@check("protocol.supported_version", "Protocol version is in this tester's "
       "scope", "protocol", "info", "VDA 5050 2.1",
       "The vehicle reports a 2.x protocol version, which is what this rule "
       "set grades.",
       "This tester encodes VDA 5050 2.1. A vehicle on another major version "
       "is not failed -- the result is simply out of scope, and the checks "
       "above should be read with that in mind.")
def _protocol_supported_version(cs, obs):
    version = obs.version_reported()
    if version is None:
        return cs.result("skip", "no version field observed")
    parsed = spec.parse_version(version)
    if parsed is None:
        return cs.result("warn", f"version {version!r} is not <major>.<minor>."
                                 "<patch>")
    if str(parsed[0]) not in spec.SUPPORTED_MAJORS:
        return cs.result("warn", f"version {version} is outside this rule set's "
                                 f"scope (VDA 5050 {spec.TARGET_VERSION})")
    return cs.result("pass", f"version {version} (graded against "
                             f"{spec.TARGET_VERSION})")


# ---------------------------------------------------------------------------
# shared sub-checks used by more than one topic
# ---------------------------------------------------------------------------

def _header_check(cs: CheckSpec, obs: Observation, recs: list[Received],
                  label: str) -> CheckResult:
    if not recs:
        return cs.result("skip", f"no {label} message observed")
    problems, offenders = [], 0
    for p in _payloads(recs):
        probs = spec.field_problems(p, spec.HEADER_FIELDS)
        if probs:
            offenders += 1
            problems.extend(probs)
    if problems:
        return cs.result("fail", _summarize(sorted(set(problems)), len(recs),
                                            offenders),
                         detail={"problems": sorted(set(problems))})
    return cs.result("pass", f"all {len(recs)} {label} header(s) complete")


def _identity_check(cs: CheckSpec, obs: Observation, recs: list[Received],
                    label: str) -> CheckResult:
    if not recs:
        return cs.result("skip", f"no {label} message observed")
    problems = set()
    for rec in recs:
        p = rec.payload or {}
        t = rec.topic
        if t is None:
            continue
        if p.get("manufacturer") != t.manufacturer:
            problems.add(f"manufacturer {p.get('manufacturer')!r} != topic "
                         f"segment {t.manufacturer!r}")
        if p.get("serialNumber") != t.serial:
            problems.add(f"serialNumber {p.get('serialNumber')!r} != topic "
                         f"segment {t.serial!r}")
    if problems:
        return cs.result("fail", "; ".join(sorted(problems)[:MAX_EXAMPLES]),
                         detail={"problems": sorted(problems)})
    return cs.result("pass", f"{obs.manufacturer}/{obs.serial} consistent across "
                             f"{len(recs)} {label} message(s)")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run_checks(obs: Observation) -> list[CheckResult]:
    """Evaluate the whole catalogue against one vehicle's evidence."""
    results: list[CheckResult] = []
    for cs in CATALOGUE:
        assert cs.fn is not None
        result = cs.fn(obs)
        if result is not None:
            results.append(result)
    return results


def catalogue() -> list[dict[str, Any]]:
    """The static rule set, for ``yantra-conform checks`` and a web layer."""
    return [cs.to_dict() for cs in CATALOGUE]
