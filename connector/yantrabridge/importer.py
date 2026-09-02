"""Bring-your-own-recording import: MCAP (or JSONL) -> Supabase history.

This module reads a robot recording and turns it into:

* ``robots`` upsert rows           — final state per robot (last message wins)
* ``robot_telemetry`` insert rows  — one history sample per message, with the
  **original** recording timestamps preserved in ``ts``
* ``alerts`` insert rows           — from VDA 5050 ``errors[]``, deduped by
  the existing :class:`~yantrabridge.translate.AlertDeduper`

Two message shapes are understood out of the box:

1. **VDA 5050 state messages** (auto-detected per message) are translated via
   the existing :mod:`yantrabridge.translate` functions — the same code path
   the live MQTT bridge uses.
2. **Arbitrary JSON schemas** via a *topic map*: a small JSON spec mapping
   topic names to field paths for ``{robot_id, battery, pos.x, pos.y, speed,
   status, ...}``. See :data:`TOPIC_MAP_DOC` / the README for the spec.

MCAP reading needs the optional ``mcap`` package (``pip install
'yantrabridge[import]'``); importing this module without it is fine — only
:func:`read_mcap` fails, with an actionable message. No ROS installation is
required: raw channels/messages are read and JSON payloads decoded directly.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from yantrabridge.translate import Translator

# ---------------------------------------------------------------------------
# Topic-map spec (documented for --help / README)
# ---------------------------------------------------------------------------

#: Target fields a topic-map entry may populate.
MAPPABLE_FIELDS = (
    "robot_id", "battery", "pos.x", "pos.y", "speed", "status",
    "motor_temp", "ts",
)

TOPIC_MAP_DOC = """\
Topic map: JSON object of {"<topic-pattern>": {<target>: <source>, ...}}.
Topic patterns match exactly, or with '*' wildcards (fnmatch), e.g.
"/acme/*/telemetry". Targets: robot_id (required), battery, pos.x, pos.y,
speed, status, motor_temp, ts. Sources are one of:
  "a.b.c"      dotted path into the JSON payload
  "topic[N]"   Nth segment of the topic (0-based, leading '/' stripped)
  "=literal"   the literal string after '='
An entry may also carry "status_map": {"<raw>": "<status>", ...} to rename
status values (unmapped raw values pass through unchanged)."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """One decoded message from a recording."""

    topic: str            #: channel/topic name ("" for JSONL sources)
    log_time_ns: int      #: recorder timestamp, ns since epoch (0 if unknown)
    msg: dict[str, Any]   #: parsed JSON payload


def _ns_to_iso(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_to_ns(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def read_mcap(path: str | Path) -> Iterator[Record]:
    """Yield :class:`Record` for every JSON-decodable message in an MCAP file.

    Reads raw channels/messages (no ROS needed). Messages whose payload is
    not a JSON object are skipped silently — they are still counted by the
    pipeline as undecodable via the topics summary (their channel appears
    with zero decoded messages only if *no* message on it decodes).
    """
    try:
        from mcap.reader import make_reader  # noqa: WPS433 (optional dep)
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the 'mcap' package is required for MCAP import: "
            "pip install 'yantrabridge[import]'  (or: pip install 'mcap>=1.0')"
        ) from exc

    with open(path, "rb") as fh:
        reader = make_reader(fh)
        for _schema, channel, message in reader.iter_messages():
            try:
                msg = json.loads(message.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                yield Record(channel.topic, message.log_time, _UNDECODABLE)
                continue
            if not isinstance(msg, dict):
                yield Record(channel.topic, message.log_time, _UNDECODABLE)
                continue
            yield Record(channel.topic, message.log_time, msg)


#: Sentinel payload for messages that could not be decoded as a JSON object.
_UNDECODABLE: dict[str, Any] = {"__undecodable__": True}


def jsonl_records(msgs: Iterable[dict[str, Any]]) -> Iterator[Record]:
    """Wrap parsed JSONL state messages as records (topic unknown)."""
    for msg in msgs:
        ns = _iso_to_ns(msg.get("timestamp")) or 0
        yield Record("", ns, msg)


# ---------------------------------------------------------------------------
# VDA 5050 auto-detection
# ---------------------------------------------------------------------------

_VDA_HINT_KEYS = frozenset({
    "batteryState", "agvPosition", "errors", "driving", "actionStates",
    "operatingMode", "safetyState", "nodeStates", "edgeStates",
})


def looks_like_vda_state(msg: dict[str, Any]) -> bool:
    """Heuristic: does this payload match the VDA 5050 state schema?"""
    if "serialNumber" not in msg:
        return False
    return "manufacturer" in msg or bool(_VDA_HINT_KEYS & msg.keys())


# ---------------------------------------------------------------------------
# Topic map
# ---------------------------------------------------------------------------

class TopicMapError(ValueError):
    """Raised for a malformed topic-map spec."""


@dataclass
class TopicMap:
    """Compiled ``--topic-map`` spec. See :data:`TOPIC_MAP_DOC`."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TopicMap":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls.parse(raw, source=str(path))

    @classmethod
    def parse(cls, raw: Any, *, source: str = "<topic-map>") -> "TopicMap":
        if not isinstance(raw, dict):
            raise TopicMapError(f"{source}: topic map must be a JSON object")
        for pattern, entry in raw.items():
            if not isinstance(entry, dict):
                raise TopicMapError(
                    f"{source}: entry for {pattern!r} must be an object")
            if "robot_id" not in entry:
                raise TopicMapError(
                    f"{source}: entry for {pattern!r} needs 'robot_id'")
            unknown = set(entry) - set(MAPPABLE_FIELDS) - {"status_map"}
            if unknown:
                raise TopicMapError(
                    f"{source}: entry for {pattern!r} has unknown field(s) "
                    f"{sorted(unknown)}; allowed: {list(MAPPABLE_FIELDS)} "
                    "+ status_map")
        return cls(entries=dict(raw))

    def match(self, topic: str) -> dict[str, Any] | None:
        """Return the mapping entry for a topic, or None."""
        entry = self.entries.get(topic)
        if entry is not None:
            return entry
        for pattern, entry in self.entries.items():
            if "*" in pattern and fnmatch.fnmatchcase(topic, pattern):
                return entry
        return None


def _resolve(spec: Any, topic: str, msg: dict[str, Any]) -> Any:
    """Resolve one topic-map source spec against a message."""
    if not isinstance(spec, str):
        return spec  # literal number/bool straight from the JSON spec
    if spec.startswith("="):
        return spec[1:]
    if spec.startswith("topic[") and spec.endswith("]"):
        try:
            idx = int(spec[6:-1])
        except ValueError:
            raise TopicMapError(f"bad topic index spec: {spec!r}") from None
        segments = topic.lstrip("/").split("/")
        return segments[idx] if 0 <= idx < len(segments) else None
    # dotted path into the payload
    cur: Any = msg
    for part in spec.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def map_message(
    entry: dict[str, Any], topic: str, msg: dict[str, Any], fallback_ts: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Apply a topic-map entry to one message.

    Returns ``(robot_row, telemetry_row)``, or None when robot_id resolves
    to nothing (the message is counted as skipped).
    """
    rid = _resolve(entry["robot_id"], topic, msg)
    if rid in (None, ""):
        return None
    rid = str(rid)

    ts = entry.get("ts") and _resolve(entry["ts"], topic, msg)
    ts = str(ts) if ts else fallback_ts

    battery = _float_or_none(_resolve(entry.get("battery"), topic, msg)
                             if "battery" in entry else None)
    speed = _float_or_none(_resolve(entry.get("speed"), topic, msg)
                           if "speed" in entry else None)
    motor_temp = _float_or_none(_resolve(entry.get("motor_temp"), topic, msg)
                                if "motor_temp" in entry else None)

    status = None
    if "status" in entry:
        raw_status = _resolve(entry["status"], topic, msg)
        if raw_status is not None:
            status = str(raw_status)
            status = (entry.get("status_map") or {}).get(status, status)

    pos = None
    if "pos.x" in entry or "pos.y" in entry:
        x = _float_or_none(_resolve(entry.get("pos.x"), topic, msg)
                           if "pos.x" in entry else None)
        y = _float_or_none(_resolve(entry.get("pos.y"), topic, msg)
                           if "pos.y" in entry else None)
        if x is not None or y is not None:
            pos = [x, y]

    robot_row: dict[str, Any] = {"id": rid, "updated_at": ts}
    telem_row: dict[str, Any] = {"robot_id": rid, "ts": ts}
    for key, value in (("battery", battery), ("speed", speed),
                       ("motor_temp", motor_temp), ("status", status),
                       ("pos", pos)):
        if value is not None:
            robot_row[key] = value
            telem_row[key] = value
    return robot_row, telem_row


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def telemetry_from_robot_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a translated ``robots`` row onto a ``robot_telemetry`` row,
    preserving the original message timestamp in ``ts``."""
    telem: dict[str, Any] = {"robot_id": row["id"], "ts": row["updated_at"]}
    for col in ("battery", "speed", "motor_temp", "status", "pos"):
        if row.get(col) is not None:
            telem[col] = row[col]
    return telem


@dataclass
class TopicStat:
    messages: int = 0
    decoded: int = 0
    kind: str = "skipped"  # "vda" | "mapped" | "skipped"


@dataclass
class ImportSummary:
    """Aggregated result of one import pass (what --dry-run prints)."""

    topics: dict[str, TopicStat] = field(default_factory=dict)
    messages: int = 0
    first_ns: int | None = None
    last_ns: int | None = None
    robots: int = 0        # distinct robots (final-state upsert rows)
    telemetry: int = 0     # history rows
    alerts: int = 0
    skipped: int = 0       # undecodable / unmatched / no robot_id

    @property
    def time_range(self) -> tuple[str, str] | None:
        if self.first_ns is None or self.last_ns is None:
            return None
        return _ns_to_iso(self.first_ns), _ns_to_iso(self.last_ns)


@dataclass
class ImportEvent:
    """Per-message pipeline output (drives both bulk and replay modes)."""

    record: Record
    robot_row: dict[str, Any] | None
    telemetry_row: dict[str, Any] | None
    alert_rows: list[dict[str, Any]]


def iter_events(
    records: Iterable[Record],
    *,
    topic_map: TopicMap | None = None,
    translator: Translator | None = None,
    summary: ImportSummary | None = None,
) -> Iterator[ImportEvent]:
    """Run the import pipeline over decoded records, one event per message.

    Routing per message: a topic-map entry matching the topic wins;
    otherwise a payload that looks like a VDA 5050 state message goes through
    the existing translator (robots row + alerts w/ dedup); anything else is
    skipped. ``summary``, if given, is updated in place.
    """
    translator = translator or Translator()
    for rec in records:
        stat = None
        if summary is not None:
            summary.messages += 1
            stat = summary.topics.setdefault(rec.topic or "<jsonl>", TopicStat())
            stat.messages += 1

        if rec.msg is _UNDECODABLE:
            if summary is not None:
                summary.skipped += 1
            continue
        if stat is not None:
            stat.decoded += 1

        # Original recording timestamp: payload timestamp wins, else log_time.
        ts_ns = _iso_to_ns(rec.msg.get("timestamp")) or rec.log_time_ns or None
        fallback_iso = _ns_to_iso(ts_ns) if ts_ns else _ns_to_iso(0)

        robot_row: dict[str, Any] | None = None
        telem_row: dict[str, Any] | None = None
        alert_rows: list[dict[str, Any]] = []

        entry = topic_map.match(rec.topic) if (topic_map and rec.topic) else None
        if entry is not None:
            if stat is not None:
                stat.kind = "mapped"
            mapped = map_message(entry, rec.topic, rec.msg, fallback_iso)
            if mapped is None:
                if summary is not None:
                    summary.skipped += 1
                continue
            robot_row, telem_row = mapped
            ts_ns = _iso_to_ns(telem_row["ts"]) or ts_ns
        elif looks_like_vda_state(rec.msg):
            if stat is not None:
                stat.kind = "vda"
            msg = rec.msg
            if "timestamp" not in msg and ts_ns:
                # preserve the recorder timestamp when the payload has none
                msg = {**msg, "timestamp": _ns_to_iso(ts_ns)}
            robot_row, alert_rows = translator.feed(msg)
            telem_row = telemetry_from_robot_row(robot_row)
        else:
            if summary is not None:
                summary.skipped += 1
            continue

        if summary is not None:
            if ts_ns:
                if summary.first_ns is None or ts_ns < summary.first_ns:
                    summary.first_ns = ts_ns
                if summary.last_ns is None or ts_ns > summary.last_ns:
                    summary.last_ns = ts_ns
            summary.telemetry += 1
            summary.alerts += len(alert_rows)

        yield ImportEvent(rec, robot_row, telem_row, alert_rows)


def collect(
    records: Iterable[Record],
    *,
    topic_map: TopicMap | None = None,
    battery_threshold: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
           ImportSummary]:
    """Bulk pass: returns ``(robot_rows, telemetry_rows, alert_rows, summary)``.

    Robot rows are last-write-wins per robot id (the recording's final
    state); telemetry keeps every sample with original timestamps.
    """
    translator = (Translator(battery_threshold=battery_threshold)
                  if battery_threshold is not None else Translator())
    summary = ImportSummary()
    robots: dict[str, dict[str, Any]] = {}
    telemetry: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for ev in iter_events(records, topic_map=topic_map, translator=translator,
                          summary=summary):
        if ev.robot_row is not None:
            # merge so a sparse mapped update doesn't drop earlier fields
            merged = {**robots.get(ev.robot_row["id"], {}), **ev.robot_row}
            robots[ev.robot_row["id"]] = merged
        if ev.telemetry_row is not None:
            telemetry.append(ev.telemetry_row)
        alerts.extend(ev.alert_rows)
    summary.robots = len(robots)
    return list(robots.values()), telemetry, alerts, summary


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def replay(
    records: Iterable[Record],
    *,
    rate: float,
    on_event: Callable[[ImportEvent], None],
    topic_map: TopicMap | None = None,
    battery_threshold: float | None = None,
    sleep: Callable[[float], None] | None = None,
    summary: ImportSummary | None = None,
) -> ImportSummary:
    """Replay a recording at ``rate``x real time, invoking ``on_event`` per
    message in timestamp order (demo mode: robots update live).

    ``sleep`` is injectable for tests. Records are materialized and sorted by
    their effective timestamp first, since MCAP files are not guaranteed to
    be strictly log-time ordered.
    """
    if rate <= 0:
        raise ValueError("replay rate must be > 0 (use bulk import for rate 0)")
    if sleep is None:  # pragma: no cover - trivial default
        import time
        sleep = time.sleep

    def _key(rec: Record) -> int:
        return _iso_to_ns(rec.msg.get("timestamp")) or rec.log_time_ns or 0

    ordered = sorted(records, key=_key)
    summary = summary if summary is not None else ImportSummary()
    translator = (Translator(battery_threshold=battery_threshold)
                  if battery_threshold is not None else Translator())
    robots_seen: set[str] = set()

    prev_ns: int | None = None
    for ev in iter_events(ordered, topic_map=topic_map, translator=translator,
                          summary=summary):
        cur_ns = _key(ev.record)
        if prev_ns is not None and cur_ns > prev_ns:
            sleep((cur_ns - prev_ns) / 1e9 / rate)
        prev_ns = cur_ns
        if ev.robot_row is not None:
            robots_seen.add(ev.robot_row["id"])
        on_event(ev)
    summary.robots = len(robots_seen)
    return summary


# ---------------------------------------------------------------------------
# Dry-run rendering
# ---------------------------------------------------------------------------

def format_summary(summary: ImportSummary, *, dry_run: bool) -> str:
    """Human-readable import summary (what --dry-run prints)."""
    lines = ["-- import summary --",
             f"messages: {summary.messages} ({summary.skipped} skipped)"]
    if summary.topics:
        lines.append("topics:")
        for topic, stat in sorted(summary.topics.items()):
            lines.append(f"  {topic}: {stat.messages} msgs "
                         f"({stat.decoded} decoded, kind={stat.kind})")
    rng = summary.time_range
    lines.append(f"time range: {rng[0]} .. {rng[1]}" if rng
                 else "time range: (no timestamps)")
    verb = "would write" if dry_run else "wrote"
    lines.append(f"{verb}: {summary.robots} robots (upsert), "
                 f"{summary.telemetry} robot_telemetry rows, "
                 f"{summary.alerts} alerts")
    return "\n".join(lines)
