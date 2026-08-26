"""Static warehouse waypoint graph.

An 8x5 grid of waypoints spaced 4 m apart (a small warehouse floor of
~28 m x 16 m), with two charging stations and two dock/staging nodes in
the corners. Edges connect 4-neighbours. All functions are pure so the
graph can be unit-tested without any simulation state.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache

MAP_ID = "warehouse-L1"

GRID_ROWS = 5
GRID_COLS = 8
SPACING_M = 4.0

#: Node kinds used to pick task targets / charging destinations.
CHARGER_NODES: tuple[str, ...] = ("n0_0", "n4_7")
DOCK_NODES: tuple[str, ...] = ("n0_7", "n4_0")


@dataclass(frozen=True)
class Waypoint:
    """A named position on the warehouse map."""

    node_id: str
    x: float
    y: float
    kind: str  # "aisle" | "charger" | "dock"


def _build_graph() -> tuple[dict[str, Waypoint], dict[str, tuple[str, ...]]]:
    waypoints: dict[str, Waypoint] = {}
    adjacency: dict[str, tuple[str, ...]] = {}
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            nid = f"n{r}_{c}"
            kind = "aisle"
            if nid in CHARGER_NODES:
                kind = "charger"
            elif nid in DOCK_NODES:
                kind = "dock"
            waypoints[nid] = Waypoint(nid, c * SPACING_M, r * SPACING_M, kind)
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            nbrs = []
            for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < GRID_ROWS and 0 <= cc < GRID_COLS:
                    nbrs.append(f"n{rr}_{cc}")
            adjacency[f"n{r}_{c}"] = tuple(nbrs)
    return waypoints, adjacency


WAYPOINTS, ADJACENCY = _build_graph()

#: Nodes robots may be sent to for pick/drop/inventory tasks.
TASK_NODES: tuple[str, ...] = tuple(
    nid for nid, wp in WAYPOINTS.items() if wp.kind != "charger"
)


def edge_id(a: str, b: str) -> str:
    """Deterministic edge name for the edge from node ``a`` to node ``b``."""
    return f"e{a}-{b}"


@lru_cache(maxsize=4096)
def shortest_path(start: str, goal: str) -> tuple[str, ...]:
    """BFS shortest path (inclusive of both endpoints). Grid is connected."""
    if start == goal:
        return (start,)
    prev: dict[str, str] = {}
    queue: deque[str] = deque([start])
    seen = {start}
    while queue:
        cur = queue.popleft()
        for nxt in ADJACENCY[cur]:
            if nxt in seen:
                continue
            seen.add(nxt)
            prev[nxt] = cur
            if nxt == goal:
                path = [goal]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return tuple(reversed(path))
            queue.append(nxt)
    raise ValueError(f"no path {start} -> {goal}")  # unreachable on a grid


def nearest_charger(node: str) -> str:
    """Charger node with the fewest hops from ``node``."""
    return min(CHARGER_NODES, key=lambda ch: len(shortest_path(node, ch)))
