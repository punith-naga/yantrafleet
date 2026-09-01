"""Offline tests for the StaticTransport filter engine and the four tools."""
from __future__ import annotations

import pytest

from sarathi.tools import Toolbox
from sarathi.transport import FailingTransport, StaticTransport, TransportError


def test_static_transport_filters(transport: StaticTransport) -> None:
    rows = transport.get("robots", [("status", "eq.charging")])
    assert sorted(r["id"] for r in rows) == ["R-002", "R-006"]

    rows = transport.get(
        "robots", [("battery", "lt.20"), ("order", "battery.asc")]
    )
    assert [r["id"] for r in rows] == ["R-004", "R-006"]

    rows = transport.get("alerts", [("ack", "eq.false"), ("order", "created_at.desc")])
    assert [r["id"] for r in rows] == ["AL-2", "AL-1"]

    rows = transport.get("robots", [("limit", "2"), ("order", "id.asc")])
    assert len(rows) == 2

    with pytest.raises(TransportError):
        transport.get("nope", [])


def test_fleet_summary_aggregates(transport: StaticTransport) -> None:
    box = Toolbox(transport=transport)
    d = box.get_fleet_summary().data
    assert d["robots_total"] == 6
    assert d["by_status"] == {"active": 2, "charging": 2, "idle": 1, "fault": 1}
    assert d["battery_avg"] == 44.5
    assert d["lowest_battery"] == {"id": "R-004", "battery": 8, "status": "fault"}
    assert d["faulted_ids"] == ["R-004"]
    assert d["unacked_alerts"] == 2
    assert d["throughput"] == 41.5 and d["sim_min"] == 342
    # source_id is the citation key: tool:argshash:timestamp
    result = box.log[0]
    assert result.source_id.startswith("get_fleet_summary:")
    assert len(result.source_id.split(":", 2)) == 3


def test_query_tools_filter_and_echo(transport: StaticTransport) -> None:
    box = Toolbox(transport=transport)
    r = box.query_robots(status="fault")
    assert r.data["count"] == 1 and r.data["rows"][0]["id"] == "R-004"
    assert r.data["filters"] == {"status": "fault"}  # threshold/filters echoed

    a = box.query_alerts(sev="critical", ack=False)
    assert a.data["count"] == 1 and a.data["rows"][0]["id"] == "AL-1"

    i = box.query_incidents(state="open")
    assert i.data["count"] == 1 and i.data["rows"][0]["id"] == "INC-1"

    # dynamic dispatch drops unknown args instead of crashing (LLM safety)
    r2 = box.call("query_robots", {"status": "idle", "bogus": 1})
    assert r2.data["rows"][0]["id"] == "R-005"
    with pytest.raises(ValueError):
        box.call("drop_tables", {})


def test_failing_transport_raises() -> None:
    box = Toolbox(transport=FailingTransport())
    with pytest.raises(TransportError):
        box.get_fleet_summary()


def test_query_commands_counts_pending(transport):
    box = Toolbox(transport=transport)
    res = box.query_commands()
    assert res.data["pending_count"] == 1
    assert res.data["by_status"]["executed"] == 1
    assert res.data["rows"][0]["cmd"] in ("estop", "charge")


def test_query_commands_filter_status(transport):
    box = Toolbox(transport=transport)
    res = box.query_commands(status="pending")
    assert all(r["status"] == "pending" for r in res.data["rows"])
