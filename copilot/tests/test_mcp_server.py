"""Offline tests for the MCP server surface.

Uses the mcp SDK's in-memory transport
(``mcp.shared.memory.create_connected_server_and_client_session``) so a
real client session talks to the real FastMCP server — no subprocess, no
network. Data comes from the shared StaticTransport fixture.
"""
from __future__ import annotations

import json

import anyio
import pytest

from mcp.shared.memory import create_connected_server_and_client_session

from sarathi.mcp_server import SERVER_NAME, create_server
from sarathi.transport import FailingTransport, StaticTransport

EXPECTED_TOOLS = {
    "fleet_summary",
    "query_robots",
    "query_alerts",
    "query_incidents",
    "query_commands",
    "robot_history",
}


def _call(server, name: str, args: dict | None = None) -> str:
    """Run one tool call through an in-memory client session; return text."""

    async def run() -> str:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            result = await session.call_tool(name, args or {})
            assert not result.isError
            assert result.content and result.content[0].type == "text"
            return result.content[0].text

    return anyio.run(run)


@pytest.fixture()
def server(transport: StaticTransport):
    return create_server(transport=transport)


def test_tool_registry_names(server) -> None:
    async def run() -> set[str]:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            listed = await session.list_tools()
            return {t.name for t in listed.tools}

    assert anyio.run(run) == EXPECTED_TOOLS


def test_fleet_summary_json_payload(server) -> None:
    payload = json.loads(_call(server, "fleet_summary"))
    assert payload["tool"] == "get_fleet_summary"
    assert payload["source_id"].startswith("get_fleet_summary:")
    data = payload["data"]
    assert data["robots_total"] == 6
    assert data["by_status"]["fault"] == 1
    assert data["faulted_ids"] == ["R-004"]
    assert data["unacked_alerts"] == 2
    assert data["throughput"] == 41.5


def test_query_robots_filters(server) -> None:
    payload = json.loads(_call(server, "query_robots", {"status": "charging"}))
    assert payload["source_id"].startswith("query_robots:")
    assert payload["data"]["count"] == 2
    assert {r["id"] for r in payload["data"]["rows"]} == {"R-002", "R-006"}
    assert payload["data"]["filters"] == {"status": "charging"}


def test_robot_history_window_and_stats(server) -> None:
    payload = json.loads(_call(server, "robot_history", {"robot_id": "R-004"}))
    assert payload["source_id"].startswith("query_telemetry:")
    stats = payload["data"]["stats"]
    # 13 R-004 samples exist; the 09:30 one falls outside the 30-min window.
    assert stats["samples"] == 12
    assert stats["battery_min"] == 8
    assert stats["last_status"] == "fault"
    assert all(r["robot_id"] == "R-004" for r in payload["data"]["rows"])


def test_backend_down_returns_structured_error() -> None:
    server = create_server(transport=FailingTransport())
    payload = json.loads(_call(server, "fleet_summary"))
    assert payload["data"] is None
    assert "backend down" in payload["error"]
