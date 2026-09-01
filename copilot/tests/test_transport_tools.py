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


def test_query_telemetry_window_and_stats(transport):
    box = Toolbox(transport=transport)
    res = box.query_telemetry(robot_id="R-004")
    d = res.data
    # 13 R-004 rows in the fixture; the 09:30 one is outside the 30-min
    # window anchored on the newest sample (10:13:30) and must be dropped,
    # as must all R-001 rows.
    assert d["count"] == 12
    assert all(r["robot_id"] == "R-004" for r in d["rows"])
    # newest first (order=ts.desc)
    ts = [r["ts"] for r in d["rows"]]
    assert ts == sorted(ts, reverse=True)
    assert d["rows"][0]["battery"] == 8 and d["rows"][-1]["battery"] == 30
    s = d["stats"]
    assert s["samples"] == 12
    assert s["battery_min"] == 8 and s["battery_max"] == 30
    assert s["battery_avg"] == 19.0
    assert s["speed_avg"] == 0.6
    assert s["motor_temp_max"] == 88
    assert s["status_changes"] == 2  # active -> degraded -> fault
    assert s["first_status"] == "active" and s["last_status"] == "fault"
    assert d["window_minutes"] == 30
    assert d["filters"] == {"robot_id": "R-004", "minutes": 30, "limit": 500}
    assert res.source_id.startswith("query_telemetry:")


def test_query_telemetry_narrow_window_and_dispatch(transport):
    box = Toolbox(transport=transport)
    # 5-minute window: samples at 10:08:30 .. 10:13:30 inclusive.
    res = box.query_telemetry(robot_id="R-004", minutes=5)
    assert res.data["count"] == 6
    assert res.data["stats"]["battery_max"] == 18

    # no telemetry for an unknown robot
    empty = box.query_telemetry(robot_id="R-999")
    assert empty.data["count"] == 0
    assert empty.data["stats"]["samples"] == 0
    assert empty.data["stats"]["battery_avg"] is None

    # dynamic dispatch works and drops unknown args
    via_call = box.call("query_telemetry", {"robot_id": "R-001", "bogus": 1})
    assert via_call.data["count"] == 2
    assert via_call.data["stats"]["status_changes"] == 0


def test_query_commands_filter_status(transport):
    box = Toolbox(transport=transport)
    res = box.query_commands(status="pending")
    assert all(r["status"] == "pending" for r in res.data["rows"])
