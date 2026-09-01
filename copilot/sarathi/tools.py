"""Fleet data tools.

Every tool reads Supabase (via the swappable Transport) and returns a
``ToolResult`` whose ``source_id`` is the citation key used in answer
evidence (Datadog/Dynatrace pattern: the LLM never originates numbers,
it only copies them out of these results).

Tools deliberately echo their filters back inside ``data`` so that every
number a template or model may quote (including thresholds) is present in
the evidence payload — this is what the grounding eval checks.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .transport import Params, Transport


@dataclass
class ToolResult:
    """One executed tool call: the citation unit."""

    tool: str
    args: dict[str, Any]
    data: dict[str, Any]
    source_id: str
    ts: str

    def label(self) -> str:
        """Human-readable evidence label, e.g. ``query_robots(status=fault)``."""
        shown = {k: v for k, v in self.args.items() if v is not None}
        inner = ", ".join(f"{k}={v}" for k, v in sorted(shown.items()))
        return f"{self.tool}({inner})"


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (Z-suffixed or offset); None on failure."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window_rows(rows: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Keep rows whose ``ts`` is within ``minutes`` of the newest sample.

    Anchoring on the newest sample (not wall clock) keeps the window
    meaningful for simulated/replayed telemetry. Rows with unparseable
    timestamps are kept — dropping data silently would hide problems.
    """
    stamps = [_parse_ts(r.get("ts")) for r in rows]
    known = [s for s in stamps if s is not None]
    if not known:
        return rows
    cutoff = max(known) - timedelta(minutes=minutes)
    return [r for r, s in zip(rows, stamps) if s is None or s >= cutoff]


def _make_source_id(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(
        json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()[:8]
    return f"{tool}:{digest}:{ts}", ts


@dataclass
class Toolbox:
    """The fleet tools, bound to one Transport, with a per-request log."""

    transport: Transport
    row_limit: int = 50
    log: list[ToolResult] = field(default_factory=list)

    # -- internals ---------------------------------------------------------

    def _record(self, tool: str, args: dict[str, Any], data: dict[str, Any]) -> ToolResult:
        source_id, ts = _make_source_id(tool, args)
        result = ToolResult(tool=tool, args=args, data=data, source_id=source_id, ts=ts)
        self.log.append(result)
        return result

    def _rows(self, table: str, params: Params) -> list[dict[str, Any]]:
        return self.transport.get(table, params)

    # -- tools -------------------------------------------------------------

    def get_fleet_summary(self) -> ToolResult:
        """Aggregate snapshot: robot counts, battery stats, alerts, throughput."""
        robots = self._rows("robots", [("select", "*")])
        meta_rows = self._rows("fleet_meta", [("select", "*"), ("id", "eq.1")])
        unacked = self._rows("alerts", [("select", "*"), ("ack", "eq.false")])

        by_status: dict[str, int] = {}
        for r in robots:
            by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
        batteries = [float(r["battery"]) for r in robots if r.get("battery") is not None]
        lowest = min(robots, key=lambda r: float(r.get("battery") or 1e9), default=None)
        by_sev: dict[str, int] = {}
        for a in unacked:
            by_sev[str(a.get("sev"))] = by_sev.get(str(a.get("sev")), 0) + 1
        meta = meta_rows[0] if meta_rows else {}

        data = {
            "robots_total": len(robots),
            "by_status": by_status,
            "battery_avg": round(statistics.mean(batteries), 1) if batteries else None,
            "lowest_battery": (
                {"id": lowest.get("id"), "battery": lowest.get("battery"),
                 "status": lowest.get("status")}
                if lowest else None
            ),
            "faulted_ids": [r.get("id") for r in robots if r.get("status") in ("fault", "estop")],
            "unacked_alerts": len(unacked),
            "unacked_by_sev": by_sev,
            "throughput": meta.get("throughput"),
            "sim_min": meta.get("sim_min"),
        }
        return self._record("get_fleet_summary", {}, data)

    def query_robots(
        self,
        status: str | None = None,
        vendor: str | None = None,
        max_battery: float | None = None,
        min_battery: float | None = None,
        robot_id: str | None = None,
        order: str | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """List robots with optional filters (status, vendor, battery range, id)."""
        args = {
            "status": status, "vendor": vendor, "max_battery": max_battery,
            "min_battery": min_battery, "robot_id": robot_id,
            "order": order, "limit": limit,
        }
        params: Params = [("select", "*")]
        if status:
            params.append(("status", f"eq.{status}"))
        if vendor:
            params.append(("vendor", f"eq.{vendor}"))
        if max_battery is not None:
            params.append(("battery", f"lt.{max_battery}"))
        if min_battery is not None:
            params.append(("battery", f"gte.{min_battery}"))
        if robot_id:
            params.append(("id", f"eq.{robot_id}"))
        params.append(("order", order or "id.asc"))
        params.append(("limit", str(limit or self.row_limit)))
        rows = self._rows("robots", params)
        data = {"rows": rows, "count": len(rows), "filters": {k: v for k, v in args.items() if v is not None}}
        return self._record("query_robots", args, data)

    def query_alerts(
        self,
        sev: str | None = None,
        ack: bool | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """List alerts, newest first, optionally by severity / ack state."""
        args = {"sev": sev, "ack": ack, "limit": limit}
        params: Params = [("select", "*")]
        if sev:
            params.append(("sev", f"eq.{sev}"))
        if ack is not None:
            params.append(("ack", f"eq.{str(ack).lower()}"))
        params.append(("order", "created_at.desc"))
        params.append(("limit", str(limit or self.row_limit)))
        rows = self._rows("alerts", params)
        data = {"rows": rows, "count": len(rows), "filters": {k: v for k, v in args.items() if v is not None}}
        return self._record("query_alerts", args, data)

    def query_incidents(
        self,
        state: str | None = None,
        sev: str | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """List incidents, optionally by state (open/resolved) and severity."""
        args = {"state": state, "sev": sev, "limit": limit}
        params: Params = [("select", "*")]
        if state:
            params.append(("state", f"eq.{state}"))
        if sev:
            params.append(("sev", f"eq.{sev}"))
        params.append(("limit", str(limit or self.row_limit)))
        rows = self._rows("incidents", params)
        data = {"rows": rows, "count": len(rows), "filters": {k: v for k, v in args.items() if v is not None}}
        return self._record("query_incidents", args, data)


    def query_commands(
        self,
        status: str | None = None,
        robot_id: str | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """List operator commands in the approval gate (v0.2 commands table).

        status: pending | approved | rejected | executed | failed.
        """
        args = {"status": status, "robot_id": robot_id, "limit": limit}
        params: Params = [("select", "*"), ("order", "created_at.desc")]
        if status:
            params.append(("status", f"eq.{status}"))
        if robot_id:
            params.append(("robot_id", f"eq.{robot_id}"))
        params.append(("limit", str(limit or self.row_limit)))
        rows = self._rows("commands", params)
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
        data = {"rows": rows, "count": len(rows), "by_status": by_status,
                "pending_count": by_status.get("pending", 0),
                "filters": {k: v for k, v in args.items() if v is not None}}
        return self._record("query_commands", args, data)

    def query_telemetry(
        self,
        robot_id: str,
        minutes: int = 30,
        limit: int = 500,
    ) -> ToolResult:
        """Recent telemetry samples for one robot, newest first, plus stats.

        The time window is anchored on the *newest sample's* timestamp (not
        wall clock) so it works against replayed/simulated data whose clock
        may lag real time. Stats: sample count, battery min/max/avg, average
        speed, max motor temp, and number of status transitions.
        """
        args = {"robot_id": robot_id, "minutes": minutes, "limit": limit}
        params: Params = [
            ("select", "*"),
            ("robot_id", f"eq.{robot_id}"),
            ("order", "ts.desc"),
            ("limit", str(limit)),
        ]
        rows = self._rows("robot_telemetry", params)
        rows = _window_rows(rows, minutes)

        batteries = [float(r["battery"]) for r in rows if r.get("battery") is not None]
        speeds = [float(r["speed"]) for r in rows if r.get("speed") is not None]
        temps = [float(r["motor_temp"]) for r in rows if r.get("motor_temp") is not None]
        # rows are newest-first; count transitions in chronological order.
        statuses = [r.get("status") for r in reversed(rows) if r.get("status") is not None]
        status_changes = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)

        stats = {
            "samples": len(rows),
            "battery_min": min(batteries) if batteries else None,
            "battery_max": max(batteries) if batteries else None,
            "battery_avg": round(statistics.mean(batteries), 1) if batteries else None,
            "speed_avg": round(statistics.mean(speeds), 2) if speeds else None,
            "motor_temp_max": max(temps) if temps else None,
            "status_changes": status_changes,
            "first_status": statuses[0] if statuses else None,
            "last_status": statuses[-1] if statuses else None,
        }
        data = {
            "rows": rows,
            "count": len(rows),
            "stats": stats,
            "window_minutes": minutes,
            "filters": {"robot_id": robot_id, "minutes": minutes, "limit": limit},
        }
        return self._record("query_telemetry", args, data)

    # -- dynamic dispatch (for the LLM agent loop) -------------------------

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch a tool by name, dropping unknown arguments defensively."""
        fn = {
            "get_fleet_summary": self.get_fleet_summary,
            "query_robots": self.query_robots,
            "query_alerts": self.query_alerts,
            "query_incidents": self.query_incidents,
            "query_commands": self.query_commands,
            "query_telemetry": self.query_telemetry,
        }.get(name)
        if fn is None:
            raise ValueError(f"unknown tool: {name}")
        import inspect

        allowed = set(inspect.signature(fn).parameters)
        return fn(**{k: v for k, v in args.items() if k in allowed})


# OpenAI-style function schemas handed to litellm in tier 1.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_fleet_summary",
            "description": "Aggregate fleet snapshot: robot counts by status, "
                           "average/lowest battery, faulted robots, unacked "
                           "alert counts, throughput and sim time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_robots",
            "description": "List robots with optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "e.g. active, idle, charging, fault"},
                    "vendor": {"type": "string"},
                    "max_battery": {"type": "number", "description": "battery strictly below this %"},
                    "min_battery": {"type": "number", "description": "battery at or above this %"},
                    "robot_id": {"type": "string", "description": "exact robot id"},
                    "order": {"type": "string", "description": "PostgREST order, e.g. battery.asc"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_alerts",
            "description": "List alerts, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sev": {"type": "string", "description": "severity, e.g. critical, warn, info"},
                    "ack": {"type": "boolean", "description": "acknowledged filter"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_incidents",
            "description": "List incidents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "description": "e.g. open, resolved"},
                    "sev": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_commands",
            "description": "List operator commands in the human-approval gate "
                           "(pending approvals, executed/failed history).",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                               "description": "pending, approved, rejected, executed, failed"},
                    "robot_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_telemetry",
            "description": "Recent telemetry history for one robot (newest "
                           "first) with computed stats: sample count, battery "
                           "min/max/avg, average speed, max motor temp, and "
                           "status-change count. Use for trend / history / "
                           "'over the last N minutes' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "robot_id": {"type": "string", "description": "exact robot id"},
                    "minutes": {"type": "integer",
                                "description": "window size in minutes (default 30)"},
                    "limit": {"type": "integer",
                              "description": "max samples (default 500)"},
                },
                "required": ["robot_id"],
            },
        },
    },
]

# Alias: some callers/docs refer to these as TOOL_SCHEMAS.
TOOL_SCHEMAS = TOOL_SPECS
