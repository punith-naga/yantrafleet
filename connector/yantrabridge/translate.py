"""Pure translation layer: VDA 5050 v2.1 ``state`` message -> Supabase rows.

Nothing in this module performs I/O. All functions take plain dicts (parsed
JSON) and return plain dicts shaped for the target Supabase tables, so the
whole layer is unit-testable offline.

Target tables (columns actually written are a subset — PostgREST upserts only
touch the columns present in the payload):

* ``robots(id, vendor, status, battery, pos, speed, task_kind, health,
  motor_temp, tasks_done, fault_msg, updated_at)``
* ``alerts(id, sev, msg, src, tlabel, ack, created_at)``
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Battery percentage at/below which a low-battery alert is raised.
BATTERY_ALERT_THRESHOLD = 20.0
#: Battery percentage at/below which the low-battery alert is critical.
BATTERY_CRIT_THRESHOLD = 10.0
#: Battery must recover above threshold + hysteresis before a fresh
#: low-battery alert may fire again (prevents flapping around the threshold).
BATTERY_HYSTERESIS = 5.0

# VDA 5050 v2.1 errorLevel -> alerts.sev  (v3.0 levels included for
# forward-compat; the translator is version-tolerant by design).
_SEV_MAP = {
    "WARNING": "warn",
    "FATAL": "crit",
    "CRITICAL": "crit",  # v3.0
    "URGENT": "crit",    # v3.0
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Lowercase, alphanumeric-and-dash slug for use inside alert ids."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def _short_hash(*parts: str) -> str:
    """Deterministic 6-hex-char hash used to keep alert ids unique."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:6]


def _tlabel(timestamp: str | None) -> str:
    """Human time label (HH:MM:SS) for the alerts.tlabel column."""
    dt = _parse_ts(timestamp)
    return dt.strftime("%H:%M:%S") if dt else ""


def _parse_ts(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        # Python 3.11 fromisoformat handles the trailing 'Z'.
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# Robot-row translation
# ---------------------------------------------------------------------------

def _derive_status(msg: dict[str, Any]) -> str:
    """Map VDA state to a single dashboard status string.

    Priority (per real fleet-manager practice): FATAL errors first, then
    safety stops, then charging, then motion state.
    """
    errors = msg.get("errors") or []
    if any((e.get("errorLevel") or "").upper() in ("FATAL", "CRITICAL", "URGENT")
           for e in errors):
        return "fault"

    safety = msg.get("safetyState") or {}
    if (safety.get("eStop") or "NONE") != "NONE" or safety.get("fieldViolation"):
        return "estop"

    battery = msg.get("batteryState") or {}
    if battery.get("charging"):
        return "charging"

    if msg.get("paused"):
        return "paused"
    if msg.get("driving"):
        return "active"
    return "idle"


def _derive_speed(msg: dict[str, Any]) -> float:
    """Planar speed magnitude from the optional velocity block."""
    vel = msg.get("velocity") or {}
    vx = float(vel.get("vx") or 0.0)
    vy = float(vel.get("vy") or 0.0)
    return round(math.hypot(vx, vy), 3)


def _derive_task_kind(msg: dict[str, Any]) -> str | None:
    """Best-effort current task label: the first RUNNING/INITIALIZING action's
    actionType, else 'transit' while driving on an order, else None."""
    for a in msg.get("actionStates") or []:
        if (a.get("actionStatus") or "").upper() in ("RUNNING", "INITIALIZING"):
            kind = a.get("actionType")
            if kind:
                return str(kind)
    if msg.get("orderId") and msg.get("driving"):
        return "transit"
    return None


def _derive_health(msg: dict[str, Any]) -> float:
    """Heuristic 0..100 health score from errors, safety and battery state."""
    score = 100.0
    for e in msg.get("errors") or []:
        level = (e.get("errorLevel") or "").upper()
        score -= 40.0 if level in ("FATAL", "CRITICAL", "URGENT") else 10.0

    safety = msg.get("safetyState") or {}
    if (safety.get("eStop") or "NONE") != "NONE" or safety.get("fieldViolation"):
        score -= 15.0

    battery = msg.get("batteryState") or {}
    charge = battery.get("batteryCharge")
    if charge is not None and float(charge) <= BATTERY_ALERT_THRESHOLD:
        score -= 10.0

    return max(0.0, min(100.0, round(score, 1)))


def _derive_fault_msg(msg: dict[str, Any]) -> str | None:
    """First fatal error's description (falls back to its errorType)."""
    for e in msg.get("errors") or []:
        if (e.get("errorLevel") or "").upper() in ("FATAL", "CRITICAL", "URGENT"):
            return e.get("errorDescription") or e.get("errorType") or "fatal error"
    return None


def translate_state(msg: dict[str, Any]) -> dict[str, Any]:
    """Translate one VDA 5050 state message into a ``robots`` upsert row.

    Raises ``ValueError`` when the message has no serialNumber (we cannot key
    a row without it). Optional vendor-extension fields ``motorTemp`` /
    ``motorTemperature`` and ``tasksDone`` / ``tasksCompleted`` are passed
    through when present; otherwise those columns are omitted so an upsert
    never clobbers values written by another writer.
    """
    serial = msg.get("serialNumber")
    if not serial:
        raise ValueError("state message has no serialNumber")

    battery = msg.get("batteryState") or {}
    pos = msg.get("agvPosition") or {}

    row: dict[str, Any] = {
        "id": str(serial),
        "vendor": msg.get("manufacturer") or "unknown",
        "status": _derive_status(msg),
        "battery": battery.get("batteryCharge"),
        "pos": [pos.get("x"), pos.get("y")] if pos else None,
        "speed": _derive_speed(msg),
        "task_kind": _derive_task_kind(msg),
        "health": _derive_health(msg),
        "fault_msg": _derive_fault_msg(msg),
        "updated_at": msg.get("timestamp") or _now_iso(),
    }

    # Vendor extensions (not part of core VDA 5050) — include only if present.
    motor_temp = msg.get("motorTemp", msg.get("motorTemperature"))
    if motor_temp is not None:
        row["motor_temp"] = motor_temp
    tasks_done = msg.get("tasksDone", msg.get("tasksCompleted"))
    if tasks_done is not None:
        row["tasks_done"] = tasks_done

    return row


# ---------------------------------------------------------------------------
# Alert generation with dedup
# ---------------------------------------------------------------------------

@dataclass
class AlertDeduper:
    """Tracks which alert conditions are currently active per robot.

    Dedup rule: an alert (keyed by ``(serial, condition-key)``) is emitted
    only when the condition transitions from inactive -> active. While it
    stays active across successive state messages, no further alert rows are
    produced. When the condition clears (error disappears from ``errors[]``,
    or battery recovers above threshold + hysteresis), the key is retired so
    a later recurrence raises a fresh alert.
    """

    battery_threshold: float = BATTERY_ALERT_THRESHOLD
    battery_hysteresis: float = BATTERY_HYSTERESIS
    #: currently-active condition keys, per robot serial
    _active: dict[str, set[str]] = field(default_factory=dict)

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def _error_key(err: dict[str, Any]) -> str:
        """Stable identity of one VDA error for dedup purposes.

        errorType + errorLevel identifies the condition; the description is
        deliberately excluded so a vendor rewording the text of an ongoing
        error does not re-alert.
        """
        return "err:{}:{}".format(
            err.get("errorType") or "unknown",
            (err.get("errorLevel") or "WARNING").upper(),
        )

    _BATTERY_KEY = "battery:low"

    # -- main entry ---------------------------------------------------------

    def process(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        """Return new ``alerts`` rows for this state message (may be empty)."""
        serial = str(msg.get("serialNumber") or "unknown")
        active = self._active.setdefault(serial, set())
        ts = msg.get("timestamp") or _now_iso()
        alerts: list[dict[str, Any]] = []

        # --- VDA errors[] ---
        seen_error_keys: set[str] = set()
        for err in msg.get("errors") or []:
            key = self._error_key(err)
            seen_error_keys.add(key)
            if key in active:
                continue  # already alerted; still ongoing
            active.add(key)
            alerts.append(self._error_alert(serial, err, ts))

        # Retire error keys that no longer appear -> future recurrence re-alerts.
        cleared = {k for k in active if k.startswith("err:") and k not in seen_error_keys}
        active.difference_update(cleared)

        # --- battery threshold with hysteresis ---
        charge = (msg.get("batteryState") or {}).get("batteryCharge")
        if charge is not None:
            charge = float(charge)
            if charge <= self.battery_threshold:
                if self._BATTERY_KEY not in active:
                    active.add(self._BATTERY_KEY)
                    alerts.append(self._battery_alert(serial, charge, ts))
            elif charge > self.battery_threshold + self.battery_hysteresis:
                active.discard(self._BATTERY_KEY)

        return alerts

    # -- row builders -------------------------------------------------------

    def _error_alert(self, serial: str, err: dict[str, Any], ts: str) -> dict[str, Any]:
        etype = err.get("errorType") or "unknownError"
        level = (err.get("errorLevel") or "WARNING").upper()
        desc = err.get("errorDescription") or etype
        hint = err.get("errorHint")
        msg_text = f"{desc} (hint: {hint})" if hint else desc
        return {
            # timestamp in the hash => a recurrence gets a distinct row id
            "id": f"al-{_slug(serial)}-{_slug(etype)}-{_short_hash(serial, etype, level, ts)}",
            "sev": _SEV_MAP.get(level, "warn"),
            "msg": f"[{etype}] {msg_text}",
            "src": serial,
            "tlabel": _tlabel(ts),
            "ack": False,
            "created_at": ts,
        }

    def _battery_alert(self, serial: str, charge: float, ts: str) -> dict[str, Any]:
        sev = "crit" if charge <= BATTERY_CRIT_THRESHOLD else "warn"
        return {
            "id": f"al-{_slug(serial)}-battery-low-{_short_hash(serial, 'battery', ts)}",
            "sev": sev,
            "msg": f"Battery low: {charge:.1f}% (threshold {self.battery_threshold:.0f}%)",
            "src": serial,
            "tlabel": _tlabel(ts),
            "ack": False,
            "created_at": ts,
        }


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

class Translator:
    """Stateful facade combining row translation and alert dedup.

    ``feed()`` accepts one parsed VDA state message and returns
    ``(robot_row, new_alert_rows)``. State kept: the dedup ledger only.
    """

    def __init__(self, battery_threshold: float = BATTERY_ALERT_THRESHOLD) -> None:
        self.deduper = AlertDeduper(battery_threshold=battery_threshold)

    def feed(self, msg: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        robot = translate_state(msg)
        alerts = self.deduper.process(msg)
        return robot, alerts

    def feed_many(
        self, msgs: Iterable[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Translate a batch; robot rows are last-write-wins per serial."""
        robots: dict[str, dict[str, Any]] = {}
        alerts: list[dict[str, Any]] = []
        for msg in msgs:
            robot, new_alerts = self.feed(msg)
            robots[robot["id"]] = robot
            alerts.extend(new_alerts)
        return list(robots.values()), alerts
