"""Tier 3 — offline template answers. No LLM anywhere.

Intent is classified with keywords/regex, the relevant tools are executed,
and the answer text is rendered from tool data only. Every number in the
answer is therefore grounded by construction (Quantum-Lab-style offline
tier). Works with zero API keys; only needs the data backend — and when
that is down too, it degrades to an honest "no data" answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .tools import Toolbox, ToolResult
from .transport import TransportError

# Robot ids look like "R-003" / "AGV-001" / "bot_7" — letters, sep, digits.
_ROBOT_ID_RE = re.compile(r"\b([A-Za-z]{1,6}[-_]\d{1,5})\b")

# "last 15 minutes" / "past 5 min" — captures the window size.
_LAST_MIN_RE = re.compile(r"\b(?:last|past)\s+(\d{1,4})\s*min(?:ute)?s?\b", re.I)


@dataclass
class OfflineAnswer:
    """Rendered tier-3 answer plus the tool log that grounds it."""

    answer: str
    intent: str
    tool_log: list[ToolResult]

    @property
    def evidence(self) -> list[dict[str, str]]:
        return [{"label": t.label(), "ref": t.source_id} for t in self.tool_log]


def _num(v: Any) -> str:
    """Render a numeric value the same way it appears in JSON data."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def classify_intent(question: str) -> tuple[str, dict[str, Any]]:
    """Keyword/regex intent classification. Returns (intent, slots)."""
    q = question.lower()
    m = _ROBOT_ID_RE.search(question)
    m_min = _LAST_MIN_RE.search(question)
    if m_min or any(w in q for w in ("history", "trend", "over time", "telemetry")):
        slots: dict[str, Any] = {"robot_id": m.group(1) if m else None}
        if m_min:
            slots["minutes"] = int(m_min.group(1))
        return "history", slots
    if m:
        return "robot_detail", {"robot_id": m.group(1)}
    if "incident" in q:
        return "incidents", {}
    if any(w in q for w in ("approval", "approvals", "approve", "pending command", "command queue", "waiting for")):
        return "approvals", {}
    if "alert" in q or "alarm" in q:
        sev = None
        for word in ("critical", "warn", "info"):
            if word in q:
                sev = word
                break
        return "alerts", {"sev": sev}
    if any(w in q for w in ("fault", "broken", "down", "error")):
        return "faults", {}
    if any(w in q for w in ("throughput", "productivity", "tasks per")):
        return "throughput", {}
    if "charging" in q and any(w in q for w in ("how many", "which", "who", "count", "list")):
        return "charging", {}
    if any(w in q for w in ("battery", "charge", "power")):
        return "low_battery", {}
    # Default: overall fleet summary ("status", "overview", "how is", ...)
    return "fleet_summary", {}


class OfflineEngine:
    """Deterministic tier-3 answerer over a Toolbox."""

    def __init__(self, toolbox_factory: Any, low_battery_threshold: float = 20.0) -> None:
        # toolbox_factory: () -> Toolbox with a fresh (empty) log per request
        self._factory = toolbox_factory
        self._low = low_battery_threshold

    def answer(self, question: str) -> OfflineAnswer:
        intent, slots = classify_intent(question)
        box: Toolbox = self._factory()
        try:
            text = self._render(intent, slots, box)
        except TransportError:
            return OfflineAnswer(
                answer=(
                    "Live fleet data is currently unavailable (data backend "
                    "unreachable), so I can't provide numbers right now. "
                    "Please retry shortly or check the fleet console."
                ),
                intent=intent,
                tool_log=[],
            )
        return OfflineAnswer(answer=text, intent=intent, tool_log=box.log)

    # -- per-intent templates ---------------------------------------------

    def _render(self, intent: str, slots: dict[str, Any], box: Toolbox) -> str:
        if intent == "history":
            return self._history(box, slots.get("robot_id"), slots.get("minutes"))
        if intent == "robot_detail":
            return self._robot_detail(box, slots["robot_id"])
        if intent == "incidents":
            return self._incidents(box)
        if intent == "approvals":
            return self._approvals(box)
        if intent == "alerts":
            return self._alerts(box, slots.get("sev"))
        if intent == "faults":
            return self._faults(box)
        if intent == "throughput":
            return self._throughput(box)
        if intent == "charging":
            return self._charging(box)
        if intent == "low_battery":
            return self._low_battery(box)
        return self._fleet_summary(box)

    def _fleet_summary(self, box: Toolbox) -> str:
        d = box.get_fleet_summary().data
        parts = ", ".join(f"{n} {s}" for s, n in sorted(d["by_status"].items()))
        lines = [f"Fleet summary: {d['robots_total']} robots ({parts})."]
        if d["battery_avg"] is not None:
            lines.append(f"Average battery {_num(d['battery_avg'])}%.")
        if d["lowest_battery"]:
            lb = d["lowest_battery"]
            lines.append(
                f"Lowest battery: {lb['id']} at {_num(lb['battery'])}% ({lb['status']})."
            )
        if d["faulted_ids"]:
            lines.append(f"Faulted: {', '.join(d['faulted_ids'])}.")
        lines.append(f"{d['unacked_alerts']} unacknowledged alert(s).")
        if d["throughput"] is not None:
            lines.append(
                f"Throughput {_num(d['throughput'])} tasks/hr at sim minute "
                f"{_num(d['sim_min'])}."
            )
        return " ".join(lines)

    def _low_battery(self, box: Toolbox) -> str:
        r = box.query_robots(max_battery=self._low, order="battery.asc")
        rows = r.data["rows"]
        if not rows:
            return (
                f"No robots are below {_num(self._low)}% battery right now."
            )
        listing = ", ".join(
            f"{x['id']} at {_num(x['battery'])}% ({x['status']})" for x in rows
        )
        return (
            f"{r.data['count']} robot(s) below {_num(self._low)}% battery: {listing}."
        )

    def _charging(self, box: Toolbox) -> str:
        r = box.query_robots(status="charging")
        rows = r.data["rows"]
        if not rows:
            return "No robots are charging right now."
        listing = ", ".join(f"{x['id']} ({_num(x['battery'])}%)" for x in rows)
        return f"{r.data['count']} robot(s) charging: {listing}."

    def _faults(self, box: Toolbox) -> str:
        r = box.query_robots(status="fault")
        rows = r.data["rows"]
        if not rows:
            return "No robots are currently in a fault state."
        listing = "; ".join(
            f"{x['id']} — {x.get('fault_msg') or 'no fault message'} "
            f"(battery {_num(x['battery'])}%)"
            for x in rows
        )
        return f"{r.data['count']} robot(s) in fault: {listing}."

    def _robot_detail(self, box: Toolbox, robot_id: str) -> str:
        r = box.query_robots(robot_id=robot_id)
        rows = r.data["rows"]
        if not rows:
            return f"No robot with id {robot_id} was found in the fleet data."
        x = rows[0]
        bits = [f"{x['id']}: {x.get('status')}"]
        if x.get("battery") is not None:
            bits.append(f"battery {_num(x['battery'])}%")
        if x.get("task_kind"):
            bits.append(f"task {x['task_kind']}")
        if x.get("health") is not None:
            bits.append(f"health {_num(x['health'])}")
        if x.get("motor_temp") is not None:
            bits.append(f"motor {_num(x['motor_temp'])}C")
        if x.get("tasks_done") is not None:
            bits.append(f"{_num(x['tasks_done'])} tasks done")
        text = ", ".join(bits) + "."
        if x.get("fault_msg"):
            text += f" Fault: {x['fault_msg']}."
        return text

    def _history(
        self, box: Toolbox, robot_id: str | None, minutes: int | None
    ) -> str:
        if not robot_id:
            # No specific robot named — fall back to the fleet-wide snapshot.
            return self._fleet_summary(box)
        r = box.query_telemetry(robot_id=robot_id, minutes=minutes or 30)
        rows = r.data["rows"]
        window = r.data["window_minutes"]
        if not rows:
            return (
                f"No telemetry samples for {robot_id} in the last "
                f"{_num(window)} minutes."
            )
        s = r.data["stats"]
        newest, oldest = rows[0], rows[-1]  # rows are newest-first
        lines = [
            f"Telemetry for {robot_id} (last {_num(window)} min, "
            f"{_num(s['samples'])} samples):"
        ]
        if s["battery_min"] is not None:
            lines.append(
                f"Battery went {_num(oldest['battery'])}% -> "
                f"{_num(newest['battery'])}% "
                f"(min {_num(s['battery_min'])}%, max {_num(s['battery_max'])}%, "
                f"avg {_num(s['battery_avg'])}%)."
            )
        if s["speed_avg"] is not None:
            lines.append(f"Average speed {_num(s['speed_avg'])} m/s.")
        if s["motor_temp_max"] is not None:
            lines.append(f"Max motor temp {_num(s['motor_temp_max'])}C.")
        lines.append(
            f"{_num(s['status_changes'])} status change(s); "
            f"latest status: {s['last_status']}."
        )
        return " ".join(lines)

    def _alerts(self, box: Toolbox, sev: str | None) -> str:
        r = box.query_alerts(sev=sev, ack=False)
        rows = r.data["rows"]
        label = f"{sev} " if sev else ""
        if not rows:
            return f"No unacknowledged {label}alerts right now."
        listing = "; ".join(
            f"[{x.get('sev')}] {x.get('msg')} (src {x.get('src')}, {x.get('tlabel')})"
            for x in rows[:5]
        )
        return f"{r.data['count']} unacknowledged {label}alert(s): {listing}."

    def _approvals(self, box: Toolbox) -> str:
        res = box.query_commands(limit=20)
        rows = res.data["rows"]
        pending = [r for r in rows if r.get("status") == "pending"]
        if not pending:
            recent = rows[:3]
            tail = "; ".join(
                f"{r.get('cmd')} {r.get('robot_id')} -> {r.get('status')}"
                for r in recent) or "no commands recorded"
            return (f"No commands are waiting for approval. "
                    f"Recent activity: {tail}.")
        lines = ", ".join(
            f"{r.get('cmd')} {r.get('robot_id')} (from {r.get('requested_by') or 'console'})"
            for r in pending)
        return (f"{len(pending)} command(s) awaiting approval: {lines}. "
                f"Approve or reject them on the Overview page; approved "
                f"commands are executed by the live feed and audited.")

    def _incidents(self, box: Toolbox) -> str:
        r = box.query_incidents(state="open")
        rows = r.data["rows"]
        if not rows:
            return "No open incidents."
        listing = "; ".join(
            f"{x.get('id')} [{x.get('sev')}] {x.get('title')} — impact: {x.get('impact')}"
            for x in rows[:5]
        )
        return f"{r.data['count']} open incident(s): {listing}."

    def _throughput(self, box: Toolbox) -> str:
        d = box.get_fleet_summary().data
        if d["throughput"] is None:
            return "Throughput data is not available right now."
        return (
            f"Current throughput is {_num(d['throughput'])} tasks/hr "
            f"(sim minute {_num(d['sim_min'])})."
        )
