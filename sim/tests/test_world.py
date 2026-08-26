"""Waypoint graph invariants."""
from yantrasim import world


def test_graph_size_and_kinds():
    assert len(world.WAYPOINTS) == world.GRID_ROWS * world.GRID_COLS
    kinds = {wp.kind for wp in world.WAYPOINTS.values()}
    assert kinds == {"aisle", "charger", "dock"}
    for ch in world.CHARGER_NODES:
        assert world.WAYPOINTS[ch].kind == "charger"
        assert ch not in world.TASK_NODES  # robots don't get tasks at chargers


def test_adjacency_is_symmetric():
    for nid, nbrs in world.ADJACENCY.items():
        for nbr in nbrs:
            assert nid in world.ADJACENCY[nbr]


def test_shortest_path_endpoints_and_steps():
    path = world.shortest_path("n0_0", "n4_7")
    assert path[0] == "n0_0" and path[-1] == "n4_7"
    assert len(path) == 12  # manhattan distance 11 hops + start
    # each hop is a graph edge
    for a, b in zip(path, path[1:]):
        assert b in world.ADJACENCY[a]


def test_shortest_path_trivial():
    assert world.shortest_path("n2_3", "n2_3") == ("n2_3",)


def test_nearest_charger():
    assert world.nearest_charger("n0_1") == "n0_0"
    assert world.nearest_charger("n4_6") == "n4_7"
