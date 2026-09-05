"""Session capture and replay: grade a vehicle without touching its broker.

WHY THIS EXISTS
---------------
The people who most need a conformance result are the ones who cannot hand
anyone access to the broker it would come from. An AGV vendor's broker sits
inside a customer's plant network; an integrator's sits behind a VPN and a
change-control process. "Point our tool at your fleet" is, for them, a
procurement conversation, not a five-minute evaluation.

So the tester separates the two halves of a run. COLLECTING evidence needs the
broker and takes a minute. GRADING it is pure arithmetic over that evidence and
needs nothing at all. A capture file is the seam: the vendor runs the collect
half on their own network, reads the file (it is plain JSON -- every byte in it
came off their own wire and they can check that), and sends it on. Anyone can
then grade it, re-grade it against a newer rule set, or diff two of them,
offline and forever.

FIDELITY
--------
A replay is not an approximation. The capture records exactly the messages the
collector consumed, in order, plus the probes the runner sent and what the
retain probe found -- which is the complete input to every check, since checks
are pure functions of an :class:`~yantraconform.observe.Observation`. Replaying
a capture therefore reproduces the original report check for check, including
the active-probe checks. :func:`replay` asserts nothing and invents nothing: if
a piece of evidence was not captured, the corresponding check reports ``skip``
exactly as it would have live.

Times are stored relative to the start of the run rather than as absolute
monotonic values, because monotonic clocks are meaningless across processes;
the few checks that compare "did this arrive after that probe" keep working
because both sides of the comparison are rebased identically.
"""
from __future__ import annotations

from typing import Any

from . import checks, spec
from .model import TOOL_NAME, TOOL_VERSION, Report, RobotReport
from .observe import Collector, Observation, ProbeEvent
from .session import Message

#: ``kind`` marker so a capture is never mistaken for a scored report (they are
#: both JSON with a ``schema_version``, and ``render`` vs ``replay`` need to
#: tell a user which one they handed over).
CAPTURE_KIND = "yantra-conform-capture"
CAPTURE_SCHEMA = "1.0"


def _robot_key(obs: Observation) -> str:
    return f"{obs.manufacturer}/{obs.serial}"


def build(collector: Collector, targets: list[Observation], *,
          options: dict[str, Any], broker: str, captured_at: str,
          warnings: list[str] | None = None) -> dict[str, Any]:
    """Serialize a finished run's raw evidence into a capture document."""
    messages = collector.consumed
    starts = [m.received_at for m in messages]
    for obs in targets:
        starts.extend(float(ev.data["sent_at"]) for ev in obs.probes
                      if isinstance(ev.data.get("sent_at"), (int, float)))
    t0 = min(starts) if starts else 0.0

    robots: list[dict[str, Any]] = []
    for obs in targets:
        probes = []
        for ev in obs.probes:
            data = dict(ev.data)
            if isinstance(data.get("sent_at"), (int, float)):
                data["sent_at"] = round(float(data["sent_at"]) - t0, 6)
            probes.append({"name": ev.name, "state_index": ev.state_index,
                           "data": data})
        robots.append({
            "manufacturer": obs.manufacturer,
            "serial": obs.serial,
            "interface": obs.interface,
            "major_version": obs.major,
            "active_probing": obs.active_probing,
            "probes": probes,
            "retain_probe": dict(obs.retain_probe),
            "retain_probed": obs.retain_probed,
            "wills_observable": obs.wills_observable,
            "wills": [list(w) for w in obs.wills],
            "offtopic": list(obs.offtopic),
        })

    return {
        "schema_version": CAPTURE_SCHEMA,
        "kind": CAPTURE_KIND,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "spec": f"VDA 5050 {spec.TARGET_VERSION}",
        "captured_at": captured_at,
        "broker": broker,
        "options": options,
        "warnings": list(warnings or []),
        "robots": robots,
        "messages": [
            {"t": round(m.received_at - t0, 6), "topic": m.topic,
             "payload": m.text(), "qos": m.qos, "retain": m.retain}
            for m in messages
        ],
    }


def is_capture(document: dict[str, Any]) -> bool:
    return document.get("kind") == CAPTURE_KIND


def replay(document: dict[str, Any], *, generated_at: str = "") -> Report:
    """Re-grade a capture document, producing the same report shape as a run.

    Raises :class:`ValueError` when handed something that is not a capture, so
    the CLI can say "that is a report, you want `render`" instead of emitting
    an empty result.
    """
    if not isinstance(document, dict) or not is_capture(document):
        raise ValueError(
            "not a yantra-conform capture file (no \"kind\": "
            f"\"{CAPTURE_KIND}\")")
    major = str(document.get("schema_version", "")).split(".")[0]
    if major != CAPTURE_SCHEMA.split(".")[0]:
        raise ValueError(
            f"capture schema {document.get('schema_version')!r} is not "
            f"readable by yantra-conform {TOOL_VERSION} (expects "
            f"{CAPTURE_SCHEMA})")

    options = document.get("options") or {}
    collector = Collector(
        interface=options.get("interface", spec.DEFAULT_INTERFACE),
        major=options.get("major_version", spec.DEFAULT_MAJOR_SEGMENT),
        manufacturer=options.get("manufacturer"),
        serial=options.get("serial"),
    )
    for entry in document.get("messages") or []:
        collector.feed(Message(
            topic=entry.get("topic") or "",
            payload=(entry.get("payload") or "").encode("utf-8"),
            qos=int(entry.get("qos") or 0),
            retain=bool(entry.get("retain")),
            received_at=float(entry.get("t") or 0.0),
        ))

    report = Report(
        broker=document.get("broker") or "",
        generated_at=generated_at or document.get("captured_at") or "",
        options=dict(options),
        warnings=list(document.get("warnings") or []),
    )
    report.warnings.append(
        "replayed from a capture recorded at "
        f"{document.get('captured_at') or 'an unrecorded time'}"
        + (f" against {document['broker']}" if document.get("broker") else "")
        + ": the vehicle was not contacted during this grading run.")

    for entry in document.get("robots") or []:
        obs = collector.get(entry["manufacturer"], entry["serial"])
        obs.interface = entry.get("interface", obs.interface)
        obs.major = entry.get("major_version", obs.major)
        obs.active_probing = bool(entry.get("active_probing", True))
        obs.retain_probe = dict(entry.get("retain_probe") or {})
        obs.retain_probed = bool(entry.get("retain_probed"))
        obs.wills_observable = bool(entry.get("wills_observable"))
        obs.wills = [tuple(w) for w in (entry.get("wills") or [])]
        obs.offtopic = list(entry.get("offtopic") or [])
        obs.probes = [
            ProbeEvent(name=p["name"], state_index=int(p["state_index"]),
                       data=dict(p.get("data") or {}))
            for p in (entry.get("probes") or [])
        ]

        robot = RobotReport(
            manufacturer=obs.manufacturer, serial=obs.serial,
            interface=obs.interface, major=obs.major,
            version_reported=obs.version_reported(),
            topics_seen=obs.topics_seen(),
            message_counts=obs.message_counts(),
        )
        if not obs.active_probing:
            robot.notes.append("active probing disabled: order, instantAction "
                               "and actionState checks were skipped")
        robot.notes.append("graded offline from a captured session")
        robot.checks = checks.run_checks(obs)
        report.robots.append(robot)
    return report


__all__ = ["CAPTURE_KIND", "CAPTURE_SCHEMA", "build", "is_capture", "replay"]
