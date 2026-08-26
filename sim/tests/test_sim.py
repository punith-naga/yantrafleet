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
