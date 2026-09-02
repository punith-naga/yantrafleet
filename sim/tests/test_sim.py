"""Simulation core behaviour: determinism, battery, charging, tasks, faults."""
from yantrasim import sim as simmod
from yantrasim.sim import FleetSim
from yantrasim.world import CHARGER_NODES, WAYPOINTS

from conftest import FIXED_NOW


def run_ticks(fleet: FleetSim, n: int, dt: float = 10.0):
    outs = []
    for _ in range(n):
        outs.append(fleet.tick(dt_s=dt, now=FIXED_NOW))
    return outs


def test_fleet_composition():
    fleet = FleetSim(seed=1)
    assert len(fleet.robots) == 10
    assert {r.vendor for r in fleet.robots} == set(simmod.VENDORS)  # 3 vendors
    assert [r.robot_id for r in fleet.robots][0] == "AMR-01"
    assert all(55.0 <= r.battery <= 95.0 for r in fleet.robots)


def test_determinism_same_seed_same_run():
    a = FleetSim(seed=99)
    b = FleetSim(seed=99)
    outs_a = run_ticks(a, 30)
    outs_b = run_ticks(b, 30)
    assert [o.states for o in outs_a] == [o.states for o in outs_b]
    assert [[e.__dict__ for e in o.events] for o in outs_a] == \
           [[e.__dict__ for e in o.events] for o in outs_b]


def test_battery_drains_over_time():
    fleet = FleetSim(seed=3)
    start = {r.robot_id: r.battery for r in fleet.robots}
    run_ticks(fleet, 15)
    # Nobody starts low enough to charge within 15 ticks, so all drain.
    assert all(r.battery < start[r.robot_id] for r in fleet.robots)


def test_low_battery_routes_to_charger_and_charges():
    fleet = FleetSim(seed=4)
    r = fleet.robots[0]
    r.battery = 18.0  # force below the go-charge threshold
    for _ in range(200):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        if r.status == "charging":
            break
    assert r.status == "charging"
    assert r.node in CHARGER_NODES
    before = r.battery
    fleet.tick(dt_s=10.0, now=FIXED_NOW)
    assert r.battery > before  # actually charging
    # Charge to full-enough and it goes back to work.
    for _ in range(300):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        if r.status != "charging":
            break
    assert r.battery >= simmod.CHARGED_ENOUGH - 1.0


def test_low_battery_event_fires_once():
    fleet = FleetSim(seed=5)
    r = fleet.robots[2]
    r.battery = 14.0
    events = []
    for _ in range(10):
        events += fleet.tick(dt_s=1.0, now=FIXED_NOW).events
    low = [e for e in events if e.kind == "low_battery" and e.robot_id == r.robot_id]
    assert len(low) == 1
    assert low[0].sev == "warn"


def test_tasks_complete_over_time():
    fleet = FleetSim(seed=6)
    run_ticks(fleet, 120)
    assert sum(r.tasks_done for r in fleet.robots) > 5


def test_scripted_localization_fault_on_amr07():
    fleet = FleetSim(seed=8)
    amr07 = next(r for r in fleet.robots if r.robot_id == "AMR-07")
    fault_events = []
    for _ in range(simmod.SCRIPTED_FAULT_TICK):
        fault_events += [e for e in fleet.tick(dt_s=10.0, now=FIXED_NOW).events
                         if e.robot_id == "AMR-07" and e.kind == "fault"]
    # Fault raised exactly at the scripted tick.
    assert amr07.fault_kind == "localization"
    assert amr07.status == "fault"
    assert amr07.localized is False
    assert any("Localization lost" in e.msg for e in fault_events)
    # It clears after the scripted duration and the robot resumes.
    cleared = []
    for _ in range(simmod.SCRIPTED_FAULT_DURATION_TICKS + 2):
        cleared += [e for e in fleet.tick(dt_s=10.0, now=FIXED_NOW).events
                    if e.robot_id == "AMR-07" and e.kind == "fault_cleared"]
    assert cleared and amr07.fault_kind is None
    assert amr07.status != "fault"


def test_position_stays_on_map():
    fleet = FleetSim(seed=10)
    xs = [wp.x for wp in WAYPOINTS.values()]
    ys = [wp.y for wp in WAYPOINTS.values()]
    for _ in range(100):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        for r in fleet.robots:
            assert min(xs) <= r.x <= max(xs)
            assert min(ys) <= r.y <= max(ys)
            assert 0.0 <= r.battery <= 100.0


def test_throughput_reported():
    fleet = FleetSim(seed=11)
    out = run_ticks(fleet, 120)[-1]
    assert out.throughput_per_h > 0


# ---------------------------------------------------------------------------
# Missions (v0.5): rolling groups of task assignments
# ---------------------------------------------------------------------------

def test_missions_spawned_at_init():
    fleet = FleetSim(seed=7)
    active = [m for m in fleet.missions if m.active]
    assert 2 <= len(active) <= simmod.MISSION_TARGET_CONCURRENT
    owned = []
    for m in active:
        assert m.state == "Queued"
        assert simmod.MISSION_MIN_ROBOTS <= len(m.robots) <= simmod.MISSION_MAX_ROBOTS
        lo, hi = simmod.MISSION_TASKS_RANGE
        assert lo <= m.planned <= hi
        owned += m.robots
    # A robot belongs to at most one mission.
    assert len(owned) == len(set(owned))
    for m in active:
        for rid in m.robots:
            r = next(rb for rb in fleet.robots if rb.robot_id == rid)
            assert r.mission_id == m.mission_id


def test_assign_task_tags_mission():
    fleet = FleetSim(seed=7)
    fleet.tick(dt_s=10.0, now=FIXED_NOW)
    tagged = [r for r in fleet.robots if r.task_mission is not None]
    assert tagged, "some owned robot should have picked up a mission-tagged task"
    for r in tagged:
        assert r.task_mission == r.mission_id
        m = fleet._mission_by_id(r.task_mission)
        assert m is not None and m.state == "Running"


def test_missions_snapshot_shape_on_tick_output():
    fleet = FleetSim(seed=7)
    out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    assert out.missions, "TickOutput must expose a missions snapshot"
    for row in out.missions:
        assert set(row) == {"id", "name", "robots", "state", "prog", "eta",
                            "site_id", "created_at"}
        assert row["state"] in ("Queued", "Running", "Done")
        assert isinstance(row["robots"], list) and row["robots"]
        assert isinstance(row["prog"], int) and 0 <= row["prog"] <= 100
        if row["state"] == "Queued":
            assert row["eta"] == "—"
        else:
            assert len(row["eta"]) == 5 and row["eta"][2] == ":"  # HH:MM
        assert row["created_at"].endswith("Z")


def test_mission_progress_increases_completes_and_respawns():
    fleet = FleetSim(seed=7)
    initial_ids = {m.mission_id for m in fleet.missions}
    saw_progress = False
    saw_done = False
    saw_new = False
    for _ in range(200):
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
        by_state = {}
        for row in out.missions:
            by_state.setdefault(row["state"], []).append(row)
            if row["state"] == "Running" and 0 < row["prog"] < 100:
                saw_progress = True
            if row["state"] == "Done":
                saw_done = True
                assert row["prog"] == 100
            if row["id"] not in initial_ids:
                saw_new = True
        # Rolling invariant: 2-3 concurrent (non-Done) missions at all times.
        active = [r for r in out.missions if r["state"] != "Done"]
        assert 2 <= len(active) <= simmod.MISSION_TARGET_CONCURRENT
    assert saw_progress and saw_done and saw_new


def test_mission_done_rows_pruned_after_linger():
    fleet = FleetSim(seed=7)
    done_tick = None
    for _ in range(300):
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
        done_ids = [r["id"] for r in out.missions if r["state"] == "Done"]
        if done_tick is None and done_ids:
            done_tick = out.tick
            done_id = done_ids[0]
        if done_tick is not None and out.tick > done_tick + simmod.MISSION_DONE_LINGER_TICKS:
            assert done_id not in [r["id"] for r in out.missions]
            break
    assert done_tick is not None


def test_missions_deterministic_same_seed():
    a, b = FleetSim(seed=99), FleetSim(seed=99)
    snaps_a = [a.tick(dt_s=10.0, now=FIXED_NOW).missions for _ in range(60)]
    snaps_b = [b.tick(dt_s=10.0, now=FIXED_NOW).missions for _ in range(60)]
    assert snaps_a == snaps_b
