"""VDA 5050 state -> Supabase table rows (pure functions, no I/O).

Status derivation follows real fleet-manager practice (the dashboard
mapping in the VDA spec): FATAL errors dominate, then safety state, then
charging/driving/paused, with a RUNNING actionState shown as "working".
The connection topic is network-level only, so status never depends on it.

Columns written match the existing tables:
  robots(id, vendor, status, battery, pos jsonb [x,y], speed, task_kind,
         health, motor_temp, tasks_done, fault_msg, site_id, updated_at)
  alerts(id, sev, msg, src, tlabel, ack, site_id, created_at)
  fleet_meta(id=1, writer_id, sim_min, throughput, updated_at)
"""
from __future__ import annotations

from typing import Any, Mapping

from yantracore import site_id

from .sim import Event


def derive_status(state: Mapping[str, Any]) -> str:
    """Canonical dashboard status from a VDA state message alone.

    Returns only values from the shared vocabulary (yantracore.CANONICAL):
    fault | estop | paused | degraded | charging | active | idle.
    """
    errors = state.get("errors", [])
    if any(e.get("errorLevel") == "FATAL" for e in errors):
        return "fault"
    safety = state.get("safetyState", {})
    if safety.get("eStop", "NONE") != "NONE" or safety.get("fieldViolation"):
        return "estop"
    if state.get("paused"):
        return "paused"
    if errors:  # WARNING-level only
        return "degraded"
    if state.get("batteryState", {}).get("charging"):
        return "charging"
    if any(a.get("actionStatus") == "RUNNING" for a in state.get("actionStates", [])):
        return "active"
    if state.get("driving"):
        return "active"
    return "idle"


def robot_row(state: Mapping[str, Any], extras: Mapping[str, Any]) -> dict[str, Any]:
    """One upsert-ready row for the ``robots`` table.

    ``state`` is the VDA message (wire truth for battery/pos/errors);
    ``extras`` carries the non-VDA telemetry the table wants (health,
    motor_temp, tasks_done, task_kind) plus the raw fleet robot id —
    the DB keeps ``AMR-07`` even though the VDA serial is ``AMR_07``.
    """
    pos = state.get("agvPosition", {})
    vel = state.get("velocity", {})
    speed = round((vel.get("vx", 0.0) ** 2 + vel.get("vy", 0.0) ** 2) ** 0.5, 2)
    errors = state.get("errors", [])
    fault_msg = errors[0].get("errorDescription") if errors else None
    return {
        "id": extras["robot_id"],
        "vendor": state["manufacturer"],
        "status": derive_status(state),
        "battery": state["batteryState"]["batteryCharge"],
        "pos": [pos.get("x", 0.0), pos.get("y", 0.0)],
        "speed": speed,
        "task_kind": extras.get("task_kind"),
        "health": extras["health"],
        "motor_temp": extras["motor_temp"],
        "tasks_done": extras["tasks_done"],
        "fault_msg": fault_msg,
        "site_id": site_id(),
        "updated_at": state["timestamp"],
    }


def alert_row(event: Event, timestamp: str) -> dict[str, Any]:
    """One insert-ready row for the ``alerts`` table.

    The id is deterministic (robot + kind + tick) so retried batches are
    idempotent under ``resolution=ignore-duplicates``.
    """
    # tlabel: short human time label, e.g. "10:15:32"
    tlabel = timestamp[11:19] if len(timestamp) >= 19 else timestamp
    return {
        "id": f"al-{event.robot_id}-{event.kind}-{event.tick}",
        "sev": event.sev,
        "msg": event.msg,
        "src": event.robot_id,
        "tlabel": tlabel,
        "ack": False,
        "site_id": site_id(),
        "created_at": timestamp,
    }


def fleet_meta_row(writer_id: str, sim_time_s: float, throughput_per_h: float,
                   timestamp: str) -> dict[str, Any]:
    """Singleton heartbeat row for the ``fleet_meta`` table (id=1)."""
    return {
        "id": 1,
        "writer_id": writer_id,
        "sim_min": round(sim_time_s / 60.0, 1),
        "throughput": throughput_per_h,
        "updated_at": timestamp,
    }


def telemetry_row(state, extras, timestamp: str):
    """One history sample for ``robot_telemetry`` (v0.3).

    Derived from the same VDA state as robot_row so history and live rows
    can never disagree.
    """
    row = robot_row(state, extras)
    return {
        "robot_id": row["id"],
        "ts": timestamp,
        "battery": row["battery"],
        "speed": row["speed"],
        "motor_temp": row["motor_temp"],
        "status": row["status"],
        "pos": row["pos"],
        "site_id": row["site_id"],
    }
