"""Simulation core behaviour: determinism, battery, charging, tasks, faults."""
from yantrasim import sim as simmod
from yantrasim import vda, world
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


def test_finished_task_action_state_reported_then_expires():
    """A completed task must report a terminal FINISHED actionStates entry
    for a few state messages (TASK_ACTION_STATE_REPEATS), not just vanish
    the instant status flips back to idle."""
    fleet = FleetSim(seed=6)
    working = None
    for _ in range(30):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        cand = [r for r in fleet.robots if r.status == "working"]
        if cand:
            working = cand[0]
            break
    assert working is not None, "no robot started working in 30 ticks"
    action_type = working.task_kind
    serial = vda.sanitize_serial(working.robot_id)
    working.work_left_s = 0.001  # force completion on the very next tick

    out = fleet.tick(dt_s=0.01, now=FIXED_NOW)
    s = next(st for st in out.states if st["serialNumber"] == serial)
    finished = [a for a in s["actionStates"] if a["actionStatus"] == "FINISHED"]
    assert len(finished) == 1
    assert finished[0]["actionType"] == action_type
    action_id = finished[0]["actionId"]

    # It keeps riding the next few state messages...
    for _ in range(simmod.TASK_ACTION_STATE_REPEATS - 1):
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
        s = next(st for st in out.states if st["serialNumber"] == serial)
        assert any(a["actionId"] == action_id for a in s["actionStates"])

    # ...then is dropped.
    out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    s = next(st for st in out.states if st["serialNumber"] == serial)
    assert all(a["actionId"] != action_id for a in s["actionStates"])


def test_apply_order_drives_robot_and_reports_progress():
    """The core VDA 5050 master-control contract: an inbound order adopts
    its orderId/orderUpdateId, drives the robot's path, and the order's own
    action reaches a terminal FINISHED actionState under that same
    actionId -- all observable purely from build_state() output."""
    fleet = FleetSim(seed=11)
    r = fleet.robots[0]
    target = next(n for n in world.TASK_NODES if n != r.node)
    path = list(world.shortest_path(r.node, target))

    ok, detail = fleet.apply_order(
        r.robot_id, "order-abc", 0, path,
        actions=[{"actionId": "act-1", "actionType": "pick"}])
    assert ok, detail
    assert r.order_id == "order-abc"
    assert r.order_update_id == 0
    assert r.status == "moving"
    assert r.path == path[1:]

    for _ in range(400):
        fleet.tick(dt_s=10.0, now=FIXED_NOW)
        if r.status == "working":
            break
    assert r.node == target
    assert r.task_kind == "pick"

    # The order's OWN actionId is what gets reported, from RUNNING through
    # to FINISHED -- master control correlates by that id and nothing else,
    # so an action must never run under one id and finish under another.
    seen: list[str] = []
    for _ in range(5):
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
        s = next(st for st in out.states
                 if st["serialNumber"] == vda.sanitize_serial(r.robot_id))
        assert s["orderId"] == "order-abc"
        assert s["orderUpdateId"] == 0
        # Every actionId in one message appears exactly once (2.1 6.9).
        ids = [a["actionId"] for a in s["actionStates"]]
        assert len(ids) == len(set(ids)), ids
        entries = [a for a in s["actionStates"] if a["actionId"] == "act-1"]
        assert entries, "the order's action was not reported at all"
        assert entries[0]["actionType"] == "pick"
        seen.append(entries[0]["actionStatus"])
        if entries[0]["actionStatus"] == "FINISHED":
            assert seen[0] == "RUNNING", seen
            return
    raise AssertionError(
        f"order action never reached a terminal actionState; saw {seen}")


def test_apply_order_rejects_stale_order_update_id():
    fleet = FleetSim(seed=12)
    r = fleet.robots[1]
    target = next(n for n in world.TASK_NODES if n != r.node)
    path = list(world.shortest_path(r.node, target))

    ok, _ = fleet.apply_order(r.robot_id, "order-1", 2, path)
    assert ok
    ok, detail = fleet.apply_order(r.robot_id, "order-1", 1, path)  # stale: lower
    assert not ok and "stale" in detail
    ok, detail = fleet.apply_order(r.robot_id, "order-1", 2, path)  # stale: equal
    assert not ok and "stale" in detail
    ok, detail = fleet.apply_order(r.robot_id, "order-1", 3, path)  # valid update
    assert ok, detail
    # a brand new orderId always replaces whatever was running, regardless
    # of the previous order's last orderUpdateId.
    ok, detail = fleet.apply_order(r.robot_id, "order-2", 0, path)
    assert ok, detail
    assert r.order_id == "order-2"


def test_apply_order_unknown_robot_fails_softly():
    fleet = FleetSim(seed=13)
    ok, detail = fleet.apply_order("AMR-99", "order-x", 0, ["n0_0"])
    assert not ok and "unknown robot" in detail


def test_apply_order_refused_while_held_or_faulted():
    fleet = FleetSim(seed=14)
    r = fleet.robots[2]
    r.status = "estopped"
    ok, detail = fleet.apply_order(r.robot_id, "order-y", 0, [r.node])
    assert not ok and "estopped" in detail


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
