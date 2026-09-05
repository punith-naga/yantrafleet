"""VDA 5050 v2.1 spec constants and pure validators.

WHY THIS IS A COPY, NOT AN IMPORT
---------------------------------
``sim/yantrasim/vda.py`` already encodes a lot of this (topic scheme, serial
charset, theta wrapping, the connection-state vocabulary, the factsheet block
list) and ``connector/yantrabridge/translate.py`` encodes the errorLevel and
actionStatus vocabularies. This module deliberately RE-STATES that knowledge
instead of importing it, for two reasons:

1. **Independence.** A conformance tester that imports the implementation it
   tests cannot detect a bug in that implementation -- yantrasim would be
   defining "compliant" rather than being measured against it. The spec has to
   live on its own side of the fence.
2. **Dependency hygiene.** ``yantrasim`` pulls in ``httpx`` and ``yantracore``
   for its Supabase transport. This package is meant to be pip-installable by
   a stranger evaluating their own vendor's AGV; it must not drag a warehouse
   simulator (and a Supabase client) along with it.

The values below were cross-checked against ``sim/yantrasim/vda.py`` (topic
builder, ``sanitize_serial`` charset, connection states, factsheet blocks) and
``connector/yantrabridge/translate.py`` (errorLevel -> severity map,
actionStatus vocabulary) so the two sides of the fence agree where they should.

Everything here is pure: no I/O, no MQTT, no clock.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Protocol identity
# ---------------------------------------------------------------------------

#: The protocol version this tester's rule set is written against.
TARGET_VERSION = "2.1.0"
#: Major versions the tester will grade. A 3.x AGV is reported as out of
#: scope rather than silently failed against 2.1 rules.
SUPPORTED_MAJORS = ("2",)
#: Default interfaceName (topic segment 1). "uagv" is the value the spec uses
#: throughout; a fleet is free to choose another, hence the CLI flag.
DEFAULT_INTERFACE = "uagv"
DEFAULT_MAJOR_SEGMENT = "v2"

# ---------------------------------------------------------------------------
# Topics  (VDA 5050 2.1 section 5.2 "Topic levels")
#   interfaceName / majorVersion / manufacturer / serialNumber / topic
# ---------------------------------------------------------------------------

TOPIC_SEGMENT_COUNT = 5

#: AGV -> master control.
SUBTOPICS_FROM_AGV = ("connection", "state", "factsheet", "visualization")
#: Master control -> AGV.
SUBTOPICS_TO_AGV = ("order", "instantActions")
SUBTOPICS = SUBTOPICS_FROM_AGV + SUBTOPICS_TO_AGV

#: serialNumber charset the spec restricts topic identity to.
SERIAL_CHARSET = re.compile(r"^[A-Za-z0-9_.:\-]+$")
#: Characters no MQTT topic segment may contain.
SEGMENT_FORBIDDEN = re.compile(r"[/+#$]")
_MAJOR_SEGMENT = re.compile(r"^v(\d+)$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

CONNECTION_STATES = ("ONLINE", "OFFLINE", "CONNECTIONBROKEN")
ACTION_STATUSES = ("WAITING", "INITIALIZING", "RUNNING", "PAUSED",
                   "FINISHED", "FAILED")
TERMINAL_ACTION_STATUSES = ("FINISHED", "FAILED")
OPERATING_MODES = ("AUTOMATIC", "SEMIAUTOMATIC", "MANUAL", "SERVICE", "TEACHIN")
ERROR_LEVELS = ("WARNING", "FATAL")
ESTOP_VALUES = ("AUTOACK", "MANUAL", "REMOTE", "NONE")
AGV_KINEMATICS = ("DIFF", "OMNI", "THREEWHEEL")
AGV_CLASSES = ("FORKLIFT", "CONVEYOR", "TUGGER", "CARRIER")
LOCALIZATION_TYPES = ("NATURAL", "REFLECTOR", "RFID", "DMC", "SPOT", "GRID")
NAVIGATION_TYPES = ("PHYSICAL_LINE_GUIDED", "VIRTUAL_LINE_GUIDED", "AUTONOMOUS")

#: Standard instantAction actionTypes every AGV is expected to answer.
STANDARD_INSTANT_ACTIONS = ("cancelOrder", "stateRequest", "factsheetRequest",
                            "initPosition")

#: errorType the spec names for a rejected order update (6.5 "order update").
ORDER_UPDATE_ERROR = "orderUpdateError"
#: errorType the spec names for an order the AGV cannot execute.
ORDER_VALIDATION_ERRORS = ("validationError", "orderError", "noRouteError")

# ---------------------------------------------------------------------------
# Message field requirements
# ---------------------------------------------------------------------------

#: Header carried by EVERY topic's payload (5.4 "header").
#: name -> accepted python type(s).
HEADER_FIELDS: dict[str, tuple[type, ...]] = {
    "headerId": (int,),
    "timestamp": (str,),
    "version": (str,),
    "manufacturer": (str,),
    "serialNumber": (str,),
}

#: state fields the spec marks REQUIRED (6.4). Value = accepted type(s).
STATE_REQUIRED_FIELDS: dict[str, tuple[type, ...]] = {
    "orderId": (str,),
    "orderUpdateId": (int,),
    "lastNodeId": (str,),
    "lastNodeSequenceId": (int,),
    "nodeStates": (list,),
    "edgeStates": (list,),
    "actionStates": (list,),
    "driving": (bool,),
    "batteryState": (dict,),
    "operatingMode": (str,),
    "errors": (list,),
    "safetyState": (dict,),
}

#: state fields that are optional in the letter of the spec but that every
#: usable fleet manager needs. Missing ones are graded as a MINOR finding,
#: never as a hard failure.
#: (``information`` and ``loads`` are genuinely optional -- an AGV with no
#: load handling has nothing to say -- so they are NOT listed here.)
STATE_RECOMMENDED_FIELDS: dict[str, tuple[type, ...]] = {
    "agvPosition": (dict,),
    "velocity": (dict,),
    "paused": (bool,),
    "newBaseRequest": (bool,),
}

BATTERY_STATE_REQUIRED: dict[str, tuple[type, ...]] = {
    "batteryCharge": (int, float),
    "charging": (bool,),
}
SAFETY_STATE_REQUIRED: dict[str, tuple[type, ...]] = {
    "eStop": (str,),
    "fieldViolation": (bool,),
}
AGV_POSITION_REQUIRED: dict[str, tuple[type, ...]] = {
    "x": (int, float),
    "y": (int, float),
    "theta": (int, float),
    "mapId": (str,),
    "positionInitialized": (bool,),
}
ACTION_STATE_REQUIRED: dict[str, tuple[type, ...]] = {
    "actionId": (str,),
    "actionStatus": (str,),
}
ERROR_REQUIRED: dict[str, tuple[type, ...]] = {
    "errorType": (str,),
    "errorLevel": (str,),
}

#: connection payload adds exactly one field to the header (5.3).
CONNECTION_REQUIRED_FIELDS: dict[str, tuple[type, ...]] = {
    "connectionState": (str,),
}

#: factsheet top-level blocks the spec marks required (section 7).
FACTSHEET_REQUIRED_BLOCKS = (
    "typeSpecification",
    "physicalParameters",
    "protocolLimits",
    "protocolFeatures",
    "agvGeometry",
    "loadSpecification",
)
FACTSHEET_TYPE_SPEC_REQUIRED: dict[str, tuple[type, ...]] = {
    "seriesName": (str,),
    "agvKinematic": (str,),
    "agvClass": (str,),
    "maxLoadMass": (int, float),
    "localizationTypes": (list,),
    "navigationTypes": (list,),
}
FACTSHEET_PHYSICAL_REQUIRED: dict[str, tuple[type, ...]] = {
    "speedMin": (int, float),
    "speedMax": (int, float),
    "accelerationMax": (int, float),
    "decelerationMax": (int, float),
    "heightMax": (int, float),
    "width": (int, float),
    "length": (int, float),
}

#: visualization is optional, but if published it must carry the header plus
#: at least one of these payload blocks to be of any use.
VISUALIZATION_PAYLOAD_BLOCKS = ("agvPosition", "velocity")

# ---------------------------------------------------------------------------
# QoS / retain expectations (5.2 "Quality of service", 5.3, 5.5)
# ---------------------------------------------------------------------------

#: sub-topic -> whether the broker copy is expected to be retained.
RETAIN_EXPECTED: dict[str, bool] = {
    "connection": True,
    "factsheet": True,
    "state": False,
    "visualization": False,
}


# ---------------------------------------------------------------------------
# Pure validators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TopicParts:
    """A parsed VDA 5050 topic path."""

    interface: str
    major: str
    manufacturer: str
    serial: str
    subtopic: str

    @property
    def prefix(self) -> str:
        return "/".join((self.interface, self.major, self.manufacturer, self.serial))

    @property
    def major_number(self) -> int | None:
        m = _MAJOR_SEGMENT.match(self.major)
        return int(m.group(1)) if m else None


def parse_topic(topic: str) -> TopicParts | None:
    """Split a VDA topic into its five segments, or ``None`` if malformed.

    Malformed means: not exactly five segments, or any segment empty. Segment
    *content* rules (serial charset, ``v<N>`` shape) are separate checks so
    they can be reported individually rather than collapsing into "bad topic".
    """
    parts = topic.split("/")
    if len(parts) != TOPIC_SEGMENT_COUNT or not all(parts):
        return None
    return TopicParts(*parts)  # type: ignore[arg-type]


def build_topic(interface: str, major: str, manufacturer: str, serial: str,
                subtopic: str) -> str:
    return "/".join((interface, major, manufacturer, serial, subtopic))


def is_legal_serial(serial: str) -> bool:
    return bool(SERIAL_CHARSET.match(serial or ""))


def is_legal_segment(segment: str) -> bool:
    return bool(segment) and not SEGMENT_FORBIDDEN.search(segment)


def is_major_segment(segment: str) -> bool:
    return bool(_MAJOR_SEGMENT.match(segment or ""))


def parse_version(version: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(version or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a VDA timestamp. Must be ISO-8601 **UTC with a trailing 'Z'**.

    The spec (5.4) mandates ISO8601 UTC, e.g. ``2026-08-26T10:15:32.512Z``.
    A ``+00:00`` offset is the same instant but is NOT the spec's stated form,
    so it is parsed (so downstream ordering checks still work) but flagged by
    :func:`timestamp_problem`.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def timestamp_problem(value: Any) -> str | None:
    """Return a human reason the timestamp is non-conformant, else ``None``."""
    if not isinstance(value, str) or not value:
        return "missing or not a string"
    dt = parse_timestamp(value)
    if dt is None:
        return "not parseable as ISO-8601"
    if not value.endswith("Z"):
        return "not in the spec's UTC 'Z' form (e.g. 2026-08-26T10:15:32.512Z)"
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(None):
        return "not UTC"
    if "T" not in value:
        return "missing the 'T' date/time separator"
    return None


def type_name(types: tuple[type, ...]) -> str:
    names = {int: "integer", float: "number", str: "string", bool: "boolean",
             list: "array", dict: "object"}
    return "/".join(names.get(t, t.__name__) for t in types)


def field_problems(payload: dict[str, Any],
                   required: dict[str, tuple[type, ...]],
                   where: str = "") -> list[str]:
    """Missing/mistyped field report for one object against a requirement map.

    ``bool`` is a subclass of ``int`` in Python, so an integer requirement
    explicitly rejects ``True``/``False`` -- an AGV sending ``"headerId": true``
    is not sending an integer, and silently accepting it would hide a real bug.
    """
    prefix = f"{where}." if where else ""
    problems: list[str] = []
    for name, types in required.items():
        if name not in payload or payload[name] is None:
            problems.append(f"{prefix}{name} missing (expected {type_name(types)})")
            continue
        value = payload[name]
        if bool not in types and isinstance(value, bool):
            problems.append(
                f"{prefix}{name} is boolean, expected {type_name(types)}")
            continue
        if not isinstance(value, types):
            problems.append(
                f"{prefix}{name} is {type(value).__name__}, "
                f"expected {type_name(types)}")
    return problems


#: Slack allowed on the [-pi, pi] bound. A vendor that rounds theta to four
#: decimals publishes 3.1416, which is 7.4e-6 past pi -- a rounding artifact,
#: not a protocol violation, and failing it would be a pure nuisance finding.
#: The tolerance covers 4-decimal rounding and nothing looser.
THETA_TOLERANCE = 1e-4


def theta_in_range(theta: Any) -> bool:
    """theta must be normalized to [-pi, pi] (6.4 agvPosition)."""
    if isinstance(theta, bool) or not isinstance(theta, (int, float)):
        return False
    return (-math.pi - THETA_TOLERANCE
            <= float(theta) <= math.pi + THETA_TOLERANCE)


def sequence_ids_wellformed(node_states: list[Any],
                            edge_states: list[Any]) -> list[str]:
    """Check the spec's sequenceId numbering (6.6): nodes even, edges odd,
    strictly ascending within each list.

    Returns a list of human problems (empty == conformant).
    """
    problems: list[str] = []
    for label, items, parity in (("nodeStates", node_states, 0),
                                 ("edgeStates", edge_states, 1)):
        last: int | None = None
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"{label}[{i}] is not an object")
                continue
            sid = item.get("sequenceId")
            if isinstance(sid, bool) or not isinstance(sid, int):
                problems.append(f"{label}[{i}].sequenceId missing or not an integer")
                continue
            if sid % 2 != parity:
                problems.append(
                    f"{label}[{i}].sequenceId={sid} has the wrong parity "
                    f"({'even' if parity == 0 else 'odd'} required)")
            if last is not None and sid <= last:
                problems.append(
                    f"{label}[{i}].sequenceId={sid} does not increase (previous {last})")
            last = sid
    return problems
