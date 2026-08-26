"""VDA 5050 v2.1 message builders (pure functions, no I/O).

Every state message carries the standard header (headerId, timestamp,
version, manufacturer, serialNumber) and the required state fields per
VDA 5050 2.1.0. ``manufacturer``/``serialNumber`` must match the MQTT
topic path, so the same sanitizer is used for both (topic segments may
not contain '/' or '$'; serialNumber charset is A-Za-z0-9_.:).

Forward-compat note (v3.0): renames like positionInitialized->localized
and batteryState->powerSupply live only here and in ``translate`` — the
sim core never touches wire field names, so adding a v3 builder is a
table change, not a sim change.
"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from . import world

if TYPE_CHECKING:  # avoid runtime circular import with sim.py
    from .sim import Robot

VDA_VERSION = "2.1.0"
VDA_MAJOR = "v2"
INTERFACE_NAME = "uagv"

#: Full battery reach estimate in metres (for batteryState.reach).
FULL_BATTERY_REACH_M = 6000.0

# Fault kind -> VDA errorType / errorLevel / description / hint.
FAULT_TYPES: dict[str, str] = {
    "localization": "localizationError",
    "motor_overtemp": "motorOvertemperature",
    "obstacle_blocked": "pathBlocked",
    "estop": "emergencyStop",
}
FAULT_LEVELS: dict[str, str] = {
    "localization": "FATAL",
    "motor_overtemp": "WARNING",
    "obstacle_blocked": "WARNING",
    "estop": "FATAL",
}
FAULT_DESCRIPTIONS: dict[str, str] = {
    "localization": "Localization lost: LiDAR scan does not match map",
    "motor_overtemp": "Drive motor over temperature threshold",
    "obstacle_blocked": "Path blocked by unexpected obstacle",
    "estop": "Emergency stop engaged",
}
FAULT_HINTS: dict[str, str] = {
    "localization": "Re-initialize position at nearest fiducial marker",
    "motor_overtemp": "Reduce duty cycle; check drive train for friction",
    "obstacle_blocked": "Clear obstacle or wait for automatic re-route",
    "estop": "Release e-stop and acknowledge on the vehicle HMI",
}

_SERIAL_BAD = re.compile(r"[^A-Za-z0-9_.:]")
_SEGMENT_BAD = re.compile(r"[/$]")


def sanitize_serial(robot_id: str) -> str:
    """Map a fleet robot id to a VDA-legal serialNumber (A-Za-z0-9_.:).

    e.g. ``AMR-07`` -> ``AMR_07`` (dash is not in the allowed charset).
    """
    return _SERIAL_BAD.sub("_", robot_id)


def sanitize_segment(segment: str) -> str:
    """Strip characters forbidden in any MQTT topic segment ('/', '$')."""
    return _SEGMENT_BAD.sub("_", segment)


def topic(manufacturer: str, serial_number: str, sub_topic: str,
          major: str = VDA_MAJOR) -> str:
    """VDA topic: interfaceName/majorVersion/manufacturer/serialNumber/topic."""
    return "/".join((
        INTERFACE_NAME,
        major,
        sanitize_segment(manufacturer),
        sanitize_serial(serial_number),
        sub_topic,
    ))


def _wrap_pi(theta: float) -> float:
    """Normalize an angle to [-pi, pi] as the spec requires for theta.

    Rounded to 4 decimals for the wire, then clamped so the rounding can
    never push the value past the +/-pi bound (e.g. pi -> 3.1416 > pi).
    """
    wrapped = math.atan2(math.sin(theta), math.cos(theta))
    return max(-math.pi, min(math.pi, round(wrapped, 4)))


def build_errors(fault_kind: str | None, serial_number: str) -> list[dict[str, Any]]:
    """errors[] for the current fault; empty array when healthy (required)."""
    if fault_kind is None:
        return []
    return [{
        "errorType": FAULT_TYPES[fault_kind],
        "errorLevel": FAULT_LEVELS[fault_kind],
        "errorDescription": FAULT_DESCRIPTIONS[fault_kind],
        "errorHint": FAULT_HINTS[fault_kind],  # 2.1 addition
        "errorReferences": [
            {"referenceKey": "serialNumber", "referenceValue": serial_number},
        ],
    }]


def build_state(r: "Robot", header_id: int, timestamp: str) -> dict[str, Any]:
    """One VDA 5050 v2.1 state message for robot ``r``."""
    serial = sanitize_serial(r.robot_id)
    driving = r.status in ("moving", "to_charger") and r.fault_kind is None

    # nodeStates/edgeStates: the horizon still to traverse (all released —
    # this simulator does not model base/horizon splits).
    node_states = []
    edge_states = []
    prev = r.node
    seq = r.last_node_sequence_id
    for nid in r.path:
        edge_states.append({
            "edgeId": world.edge_id(prev, nid),
            "sequenceId": seq + 1,      # edges take odd sequenceIds
            "released": True,
        })
        node_states.append({
            "nodeId": nid,
            "sequenceId": seq + 2,      # nodes take even sequenceIds
            "released": True,
        })
        prev = nid
        seq += 2

    action_states = []
    if r.status == "working" and r.task_kind is not None:
        action_states.append({
            "actionId": f"a-{serial}-{r.tasks_done + 1}",
            "actionType": r.task_kind,
            "actionStatus": "RUNNING",
        })

    return {
        # ---- header (must match topic path) ----
        "headerId": header_id,
        "timestamp": timestamp,
        "version": VDA_VERSION,
        "manufacturer": r.vendor,
        "serialNumber": serial,
        # ---- order context ----
        "orderId": r.order_id,
        "orderUpdateId": r.order_update_id,
        "lastNodeId": r.node,
        "lastNodeSequenceId": r.last_node_sequence_id,
        "nodeStates": node_states,
        "edgeStates": edge_states,
        # ---- motion ----
        "driving": driving,
        "paused": False,
        "newBaseRequest": False,
        "agvPosition": {
            "x": round(r.x, 3),
            "y": round(r.y, 3),
            "theta": _wrap_pi(r.theta),
            "mapId": world.MAP_ID,
            "positionInitialized": r.localized,  # v3.0 renames to "localized"
        },
        "velocity": {
            "vx": round(r.speed * math.cos(r.theta), 3),
            "vy": round(r.speed * math.sin(r.theta), 3),
            "omega": 0.0,
        },
        # ---- power ----  (v3.0: powerSupply.stateOfCharge)
        "batteryState": {
            "batteryCharge": round(r.battery, 1),
            "charging": r.status == "charging",
            "reach": round(FULL_BATTERY_REACH_M * r.battery / 100.0),
        },
        "operatingMode": "AUTOMATIC",
        "errors": build_errors(r.fault_kind, serial),
        "safetyState": {
            "eStop": "MANUAL" if r.fault_kind == "estop" else "NONE",
            "fieldViolation": r.fault_kind == "obstacle_blocked",
        },
        "actionStates": action_states,
    }


def build_connection(r: "Robot", header_id: int, timestamp: str,
                     connection_state: str) -> dict[str, Any]:
    """connection topic payload: ONLINE | OFFLINE | CONNECTIONBROKEN."""
    return {
        "headerId": header_id,
        "timestamp": timestamp,
        "version": VDA_VERSION,
        "manufacturer": r.vendor,
        "serialNumber": sanitize_serial(r.robot_id),
        "connectionState": connection_state,
    }
