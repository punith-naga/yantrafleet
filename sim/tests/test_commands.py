"""v0.2: operator commands + approval-gate poller (offline)."""
from __future__ import annotations

import json

import httpx

from yantrasim.commands import apply_command
from yantrasim.sim import FleetSim
from yantrasim.transports.supabase import SupabaseTransport


def make_sim() -> FleetSim:
    s = FleetSim(seed=7)
    s.tick(dt_s=5.0)
    return s


def get(sim: FleetSim, rid: str):
    return next(r for r in sim.robots if r.robot_id == rid)


class TestApplyCommand:
    def test_pause_and_resume(self):
        sim = make_sim()
        r = get(sim, "AMR-01")
        ok, detail = apply_command(sim, "AMR-01", "pause")
        assert ok and r.status == "paused" and r.speed == 0.0
        # Paused robot holds position across ticks.
        x0, y0 = r.x, r.y
        for _ in range(5):
            sim.tick(dt_s=10.0)
        assert (r.x, r.y) == (x0, y0) and r.status == "paused"
        ok, _ = apply_command(sim, "AMR-01", "resume")
        assert ok and r.status in ("idle",)  # held robots restart clean

    def test_estop_freezes_and_sets_vda_estop(self):
        sim = make_sim()
        r = get(sim, "AMR-02")
        ok, _ = apply_command(sim, "AMR-02", "estop")
        assert ok and r.status == "estopped"
        out = sim.tick(dt_s=5.0)
        state = next(s for s in out.states
                     if s["serialNumber"].replace("_", "-") == "AMR-02")
        assert state["safetyState"]["eStop"] == "MANUAL"
        # Canonical wire status via translate:
        from yantrasim.translate import derive_status
        assert derive_status(state) == "estop"

    def test_charge_routes_to_charger(self):
        sim = make_sim()
        r = get(sim, "AMR-03")
        ok, detail = apply_command(sim, "AMR-03", "charge")
        assert ok
        assert r.status in ("to_charger", "charging")

    def test_bad_inputs_fail_softly(self):
        sim = make_sim()
        ok, why = apply_command(sim, "AMR-99", "pause")
        assert not ok and "unknown robot" in why
        ok, why = apply_command(sim, "AMR-01", "self_destruct")
        assert not ok and "unknown command" in why
        ok, why = apply_command(sim, "AMR-01", "resume")
        assert not ok  # not held


class TestPoller:
    def test_poll_applies_and_acks(self):
        sim = make_sim()
        seen: list[tuple[str, str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/commands"):
                return httpx.Response(200, json=[
                    {"id": "c-1", "robot_id": "AMR-01", "cmd": "pause"},
                    {"id": "c-2", "robot_id": "AMR-99", "cmd": "pause"},
                ])
            if request.method == "PATCH":
                seen.append((request.method, str(request.url),
                             json.loads(request.content)))
                return httpx.Response(204)
            return httpx.Response(200, json=[])

        t = SupabaseTransport(client=httpx.Client(
            transport=httpx.MockTransport(handler)))
        n = t.poll_commands(lambda rid, cmd: apply_command(sim, rid, cmd))
        assert n == 2
        assert get(sim, "AMR-01").status == "paused"
        bodies = {url.split("id=eq.")[1].split("&")[0]: body
                  for _, url, body in seen}
        assert bodies["c-1"]["status"] == "executed"
        assert bodies["c-2"]["status"] == "failed"
        assert "unknown robot" in bodies["c-2"]["note"]

    def test_poll_survives_network_failure(self):
        sim = make_sim()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        t = SupabaseTransport(client=httpx.Client(
            transport=httpx.MockTransport(handler)))
        assert t.poll_commands(lambda rid, cmd: (True, "")) == 0


class TestTelemetryHistory:
    def test_history_rows_posted_every_nth_tick(self):
        from yantrasim.sim import FleetSim
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append((request.url.path, json.loads(request.content)))
                return httpx.Response(201)
            return httpx.Response(200, json=[])

        t = SupabaseTransport(client=httpx.Client(
            transport=httpx.MockTransport(handler)), history_every=2)
        sim = FleetSim(seed=3)
        for _ in range(4):
            t.publish(sim.tick(dt_s=5.0))
        hist = [rows for path, rows in posts if path.endswith("/robot_telemetry")]
        assert len(hist) == 2                      # ticks 2 and 4
        sample = hist[0][0]
        assert set(sample) == {"robot_id", "ts", "battery", "speed",
                               "motor_temp", "status", "pos"}
        assert sample["status"] in ("active", "idle", "charging", "paused",
                                    "estop", "degraded", "fault")

    def test_history_disabled_with_zero(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request.url.path)
            return httpx.Response(201) if request.method == "POST" else httpx.Response(200, json=[])

        from yantrasim.sim import FleetSim
        t = SupabaseTransport(client=httpx.Client(
            transport=httpx.MockTransport(handler)), history_every=0)
        sim = FleetSim(seed=3)
        t.publish(sim.tick(dt_s=5.0))
        assert not any(p.endswith("/robot_telemetry") for p in posts)
