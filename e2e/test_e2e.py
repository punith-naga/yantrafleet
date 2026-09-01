"""Offline end-to-end loopback: sim -> fake PostgREST -> copilot.

Proves the whole stack against real HTTP on localhost, with zero network
egress — ``fakerest.FakePostgREST`` stands in for Supabase and both real
transports (yantrasim's writer, sarathi's reader) speak genuine httpx to
it over 127.0.0.1 sockets.

Story (tests run in order against one shared world):
  1. yantrasim publishes 8 ticks through its SupabaseTransport
     -> robots rows exist with canonical statuses, telemetry accumulates,
        alerts land.
  2. A pending ``pause AMR-01`` command is inserted (console path) and
     approved (human path); ``poll_commands`` + ``apply_command`` execute
     it -> the sim robot is paused AND the row is ``executed`` with a note.
  3. sarathi's Toolbox + OfflineEngine read the same fake through
     sarathi's own httpx transport -> grounded answers for approvals and
     fleet status.

Seed 9 is deterministic: AMR-09 faults at tick 4 and clears at tick 8,
guaranteeing alert rows inside the 8-tick window.
"""
from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "copilot"))   # sarathi (not pip-installed)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakerest import FakePostgREST  # noqa: E402
from yantracore import CANONICAL  # noqa: E402
from yantrasim.commands import apply_command  # noqa: E402
from yantrasim.sim import FleetSim  # noqa: E402
from yantrasim.transports.supabase import SupabaseTransport as SimTransport  # noqa: E402

from sarathi.offline import OfflineEngine  # noqa: E402
from sarathi.tools import Toolbox  # noqa: E402
from sarathi.transport import SupabaseTransport as CopilotTransport  # noqa: E402

SEED = 9          # deterministic fault(+clear) on AMR-09 within 8 ticks
TICKS = 8
HISTORY_EVERY = 3  # telemetry sampled at ticks 3 and 6


@dataclass
class World:
    """Everything the story tests share."""

    fake: FakePostgREST
    base_url: str
    sim: FleetSim
    transport: SimTransport
    http: httpx.Client
    command_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(scope="module")
def world() -> Iterator[World]:
    fake = FakePostgREST()
    base_url = fake.start()
    sim = FleetSim(seed=SEED)
    transport = SimTransport(url=base_url, key="test-key",
                             history_every=HISTORY_EVERY)
    http = httpx.Client(
        base_url=f"{base_url}/rest/v1",
        headers={"apikey": "test-key", "Content-Type": "application/json"},
        timeout=5.0,
    )
    w = World(fake=fake, base_url=base_url, sim=sim,
              transport=transport, http=http)
    for _ in range(TICKS):
        transport.publish(sim.tick())
    yield w
    http.close()
    transport.close()
    fake.stop()


def _rows(world: World, table: str, **params: str) -> list[dict[str, Any]]:
    resp = world.http.get(f"/{table}", params=params)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# 1. Simulator publishes through real httpx to the fake PostgREST
# --------------------------------------------------------------------------

def test_robots_upserted_with_canonical_statuses(world: World) -> None:
    robots = _rows(world, "robots", order="id.asc")
    assert len(robots) == 10, "upsert must keep one row per robot, not 80"
    assert [r["id"] for r in robots] == [f"AMR-{i:02d}" for i in range(1, 11)]
    for r in robots:
        assert r["status"] in CANONICAL, f"{r['id']} has non-canonical {r['status']!r}"
        assert 0.0 <= r["battery"] <= 100.0
        assert isinstance(r["pos"], list) and len(r["pos"]) == 2
        assert r["vendor"] in ("nexomotion", "agilus", "boturo")
        assert r["updated_at"]  # ISO timestamp from the last tick


def test_telemetry_accumulates_downsampled(world: World) -> None:
    telem = _rows(world, "robot_telemetry", order="ts.asc")
    # 8 ticks, sampled every 3rd -> ticks 3 and 6 -> 2 batches x 10 robots.
    assert len(telem) == (TICKS // HISTORY_EVERY) * 10
    assert {t["robot_id"] for t in telem} == {f"AMR-{i:02d}" for i in range(1, 11)}
    assert len({t["ts"] for t in telem}) == TICKS // HISTORY_EVERY
    for t in telem:
        assert t["status"] in CANONICAL
    # eq. + in. filters work on history reads (replay windowing shape).
    one = _rows(world, "robot_telemetry", robot_id="eq.AMR-01")
    assert len(one) == TICKS // HISTORY_EVERY
    two = _rows(world, "robot_telemetry",
                **{"robot_id": "in.(AMR-01,AMR-02)"})
    assert len(two) == 2 * (TICKS // HISTORY_EVERY)


def test_alerts_present_and_idempotent(world: World) -> None:
    alerts = _rows(world, "alerts", order="created_at.asc")
    assert alerts, "seed 9 must produce alert rows within 8 ticks"
    kinds = {a["id"].rsplit("-", 1)[0] for a in alerts}
    assert any("fault" in k for k in kinds)
    srcs = {a["src"] for a in alerts}
    assert "AMR-09" in srcs
    for a in alerts:
        assert a["sev"] in ("info", "warn", "crit")
        assert a["ack"] is False
    # ignore-duplicates: re-posting the same deterministic ids is a no-op.
    world.http.post("/alerts", params={"on_conflict": "id"},
                    headers={"Prefer": "resolution=ignore-duplicates"},
                    json=alerts).raise_for_status()
    assert len(_rows(world, "alerts")) == len(alerts)


def test_fleet_meta_heartbeat_singleton(world: World) -> None:
    meta = _rows(world, "fleet_meta")
    assert len(meta) == 1 and meta[0]["id"] == 1
    assert meta[0]["writer_id"] == world.transport.writer_id
    assert meta[0]["sim_min"] == pytest.approx(TICKS * 10 / 60.0, abs=0.1)
    assert meta[0]["throughput"] >= 0


# --------------------------------------------------------------------------
# 2. Human-in-the-loop command gate: pending -> approved -> executed
# --------------------------------------------------------------------------

def test_command_approved_then_executed(world: World) -> None:
    cmd_id = str(uuid.uuid4())
    world.command_id = cmd_id
    now = datetime.now(timezone.utc).isoformat()
    # Console inserts a pending command...
    world.http.post("/commands", json=[{
        "id": cmd_id, "robot_id": "AMR-01", "cmd": "pause",
        "status": "pending", "requested_by": "e2e-console",
        "note": None, "created_at": now,
    }]).raise_for_status()
    # ...the sim must NOT execute it while merely pending.
    assert world.transport.poll_commands(
        lambda rid, c: apply_command(world.sim, rid, c)) == 0

    # A human approves it (console PATCH path).
    world.http.patch("/commands", params={"id": f"eq.{cmd_id}"},
                     json={"status": "approved", "decided_by": "e2e-human",
                           "decided_at": now}).raise_for_status()

    # Executor side: poll approved commands and apply them to the sim.
    done = world.transport.poll_commands(
        lambda rid, c: apply_command(world.sim, rid, c))
    assert done == 1

    robot = next(r for r in world.sim.robots if r.robot_id == "AMR-01")
    assert robot.status == "paused"
    assert robot.speed == 0.0

    row = _rows(world, "commands", id=f"eq.{cmd_id}")[0]
    assert row["status"] == "executed"
    assert "AMR-01 paused" in row["note"]
    assert row["executed_at"]


def test_paused_status_round_trips_to_robots_table(world: World) -> None:
    # One more published tick carries the pause through VDA translation.
    world.transport.publish(world.sim.tick())
    row = _rows(world, "robots", id="eq.AMR-01")[0]
    assert row["status"] == "paused"
    assert row["speed"] == 0.0


# --------------------------------------------------------------------------
# 3. Copilot (sarathi) reads the same fake through its own transport
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(world: World) -> Iterator[tuple[OfflineEngine, CopilotTransport]]:
    transport = CopilotTransport(url=world.base_url, key="test-key")
    eng = OfflineEngine(toolbox_factory=lambda: Toolbox(transport=transport))
    yield eng, transport
    transport.close()


def test_copilot_sees_pending_approval(world: World, engine) -> None:
    eng, _ = engine
    # A second command sits pending (nobody approved it yet).
    world.http.post("/commands", json=[{
        "id": str(uuid.uuid4()), "robot_id": "AMR-02", "cmd": "charge",
        "status": "pending", "requested_by": "e2e-console",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }]).raise_for_status()

    ans = eng.answer("what is waiting for approval?")
    assert ans.intent == "approvals"
    assert "1 command(s) awaiting approval" in ans.answer
    assert "charge AMR-02" in ans.answer
    assert ans.tool_log, "answer must be grounded in an executed tool call"
    assert ans.tool_log[0].tool == "query_commands"
    # The executed pause from part 2 shows up in the same grounded data.
    statuses = ans.tool_log[0].data["by_status"]
    assert statuses.get("executed") == 1 and statuses.get("pending") == 1


def test_copilot_fleet_status_is_grounded(world: World, engine) -> None:
    eng, transport = engine
    ans = eng.answer("fleet status")
    assert ans.intent == "fleet_summary"
    assert "Fleet summary: 10 robots" in ans.answer
    assert "paused" in ans.answer          # AMR-01's pause is visible
    assert "unacknowledged alert" in ans.answer

    # Every headline number matches the fake's tables exactly.
    robots = transport.get("robots", [("select", "*")])
    alerts = transport.get("alerts", [("select", "*"), ("ack", "eq.false")])
    summary = ans.tool_log[0].data
    assert summary["robots_total"] == len(robots) == 10
    assert summary["unacked_alerts"] == len(alerts)
    assert sum(summary["by_status"].values()) == 10
    assert set(summary["by_status"]) <= CANONICAL
    assert f"{summary['unacked_alerts']} unacknowledged alert" in ans.answer

    meta = transport.get("fleet_meta", [("id", "eq.1")])[0]
    assert summary["throughput"] == meta["throughput"]


def test_copilot_robot_detail_and_alerts(world: World, engine) -> None:
    eng, _ = engine
    detail = eng.answer("how is AMR-01 doing?")
    assert detail.intent == "robot_detail"
    assert detail.answer.startswith("AMR-01: paused")

    alerts = eng.answer("any alerts?")
    assert alerts.intent == "alerts"
    assert "unacknowledged" in alerts.answer
    assert "AMR-09" in alerts.answer


# --------------------------------------------------------------------------
# 4. Detector (yantradetect) round-trip: robots -> incidents -> console shape
# --------------------------------------------------------------------------

def test_detector_incident_roundtrip() -> None:
    """Detector writes an incidents row the console reader can consume,
    against its own fake (isolated from the shared story world)."""
    from datetime import timedelta

    from yantradetect import IncidentEngine, PostgRESTSink

    fake = FakePostgREST()
    base = fake.start()
    sink = PostgRESTSink(url=base, key="test-key")
    eng = IncidentEngine(pending_polls=2)
    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    bad = [{"id": "AMR-07", "status": "fault", "fault_msg": "localization loss"}]
    try:
        sink.apply(eng.observe(bad, t0))
        sink.apply(eng.observe(bad, t0 + timedelta(seconds=5)))       # opens
        # idempotency: re-applying the identical open is a merge, not a dup
        sink.apply(eng.observe(bad, t0 + timedelta(seconds=10)))
        sink.apply(eng.observe(
            [{"id": "AMR-07", "status": "idle"}], t0 + timedelta(minutes=4)))  # resolves

        with httpx.Client(base_url=f"{base}/rest/v1",
                          headers={"apikey": "test-key"}, timeout=5.0) as c:
            rows = c.get("/incidents", params={"select": "*"}).json()
        assert len(rows) == 1
        row = rows[0]
        # every column the console's incidents reader consumes (rca/fix are
        # nullable; the console falls back to placeholder copy for them)
        for col in ("id", "sev", "title", "src", "tlabel", "state",
                    "impact", "dur", "created_at"):
            assert col in row, f"incidents row missing {col}"
        assert row["id"].startswith("INC-")
        assert row["sev"] == "crit"                 # fault -> crit
        assert row["state"] == "Resolved"
        assert row["dur"] == 3  # opened t0+5s, resolved t0+4min -> floor 3 min
        assert row["created_at"] == (t0 + timedelta(seconds=5)).isoformat()
    finally:
        sink.close()
        fake.stop()
