"""MCP server — Sarathi's Toolbox exposed over the Model Context Protocol.

The trinity vision: one agent core (the evidence-grounded ``Toolbox``),
three surfaces:

* the FastAPI ``/ask`` copilot (``sarathi.app``),
* the fleet console (``console/index.html``),
* and this MCP server, which lets *external* agents — Claude Desktop,
  Vegaduta, Quantum Lab — query the fleet directly.

Every tool delegates to the existing :class:`sarathi.tools.Toolbox` and
returns the full ``ToolResult`` as JSON text, including the ``source_id``
citation key, so external agents inherit the same evidence-grounding
contract as the in-house tiers: numbers originate in tool payloads, never
in the model.

Run (stdio transport, e.g. from a Claude Desktop config)::

    python -m sarathi.mcp_server

Environment: ``SUPABASE_URL`` / ``SUPABASE_KEY`` override the client-safe
defaults, exactly as for the FastAPI service.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings, load_settings
from .tools import ToolResult, Toolbox
from .transport import SupabaseTransport, Transport, TransportError

SERVER_NAME = "sarathi-fleet"

SERVER_INSTRUCTIONS = (
    "Read-only tools over the Yantrika robot fleet (robots, alerts, "
    "incidents, operator commands, telemetry history). Every result is a "
    "JSON object carrying the queried data plus a source_id citation key — "
    "quote numbers only from these payloads and cite the source_id as "
    "evidence. Canonical robot statuses: active, idle, charging, paused, "
    "estop, degraded, fault (fault/estop = not operating)."
)


def _dump(result: ToolResult) -> str:
    """Serialize one ToolResult as compact, citation-ready JSON text."""
    payload: dict[str, Any] = {
        "tool": result.tool,
        "args": {k: v for k, v in result.args.items() if v is not None},
        "data": result.data,
        "source_id": result.source_id,
        "ts": result.ts,
    }
    return json.dumps(payload, default=str)


def _error(tool: str, exc: Exception) -> str:
    """Serialize a backend failure so agents see a structured error."""
    return json.dumps({"tool": tool, "error": str(exc), "data": None})


def create_server(
    transport: Transport | None = None,
    settings: Settings | None = None,
) -> FastMCP:
    """Build the FastMCP server bound to one Transport.

    Tests pass a ``StaticTransport``; production defaults to Supabase —
    the same factory pattern as ``sarathi.app.create_app``.
    """
    settings = settings or load_settings()
    transport = transport or SupabaseTransport(
        settings.supabase_url, settings.supabase_key
    )
    toolbox = Toolbox(transport, row_limit=settings.tool_row_limit)

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def fleet_summary() -> str:
        """Aggregate fleet snapshot: robot counts by status, average/lowest
        battery, faulted robot ids, unacknowledged alert counts by severity,
        throughput and sim time. JSON with evidence source_id."""
        try:
            return _dump(toolbox.get_fleet_summary())
        except TransportError as exc:
            return _error("get_fleet_summary", exc)

    @mcp.tool()
    def query_robots(
        status: str | None = None,
        vendor: str | None = None,
        limit: int | None = None,
    ) -> str:
        """List robots, optionally filtered by status (active, idle, charging,
        paused, estop, degraded, fault) and/or vendor. JSON rows + count +
        evidence source_id."""
        try:
            return _dump(toolbox.query_robots(status=status, vendor=vendor, limit=limit))
        except TransportError as exc:
            return _error("query_robots", exc)

    @mcp.tool()
    def query_alerts(sev: str | None = None, limit: int | None = None) -> str:
        """List alerts newest first, optionally by severity (critical, warn,
        info). JSON rows + count + evidence source_id."""
        try:
            return _dump(toolbox.query_alerts(sev=sev, limit=limit))
        except TransportError as exc:
            return _error("query_alerts", exc)

    @mcp.tool()
    def query_incidents(state: str | None = None, limit: int | None = None) -> str:
        """List incidents, optionally by state (open, resolved). JSON rows +
        count + evidence source_id."""
        try:
            return _dump(toolbox.query_incidents(state=state, limit=limit))
        except TransportError as exc:
            return _error("query_incidents", exc)

    @mcp.tool()
    def query_commands(status: str | None = None, limit: int | None = None) -> str:
        """List operator commands in the human-approval gate, newest first,
        optionally by status (pending, approved, rejected, executed, failed).
        JSON rows + per-status counts + evidence source_id."""
        try:
            return _dump(toolbox.query_commands(status=status, limit=limit))
        except TransportError as exc:
            return _error("query_commands", exc)

    @mcp.tool()
    def robot_history(robot_id: str, minutes: int = 30) -> str:
        """Recent telemetry history for one robot (newest first) with stats:
        battery min/max/avg, average speed, max motor temp, status changes.
        Window is anchored on the newest sample. JSON + evidence source_id."""
        try:
            return _dump(toolbox.query_telemetry(robot_id=robot_id, minutes=minutes))
        except TransportError as exc:
            return _error("query_telemetry", exc)

    return mcp


def main() -> None:
    """Entry point: serve over stdio (the Claude Desktop transport)."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
