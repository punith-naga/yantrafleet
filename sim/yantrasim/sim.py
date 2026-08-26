"""Pure simulation core: 10 AMRs from 3 vendors on the waypoint graph.

No I/O here — ``FleetSim.tick()`` advances the world and returns a
``TickOutput`` (VDA 5050 state messages + per-robot extension telemetry +
events). Transports consume that output; tests drive it directly.

Determinism: all randomness flows through one seeded ``random.Random``,
so a given seed always reproduces the same run (used by the tests).
"""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import vda, world

# --------------------------------------------------------------------------
# Fleet composition
# --------------------------------------------------------------------------

VENDORS: tuple[str, ...] = ("nexomotion", "agilus", "boturo")
FLEET_SIZE = 10
TASK_KINDS: tuple[str, ...] = ("pick", "drop", "move", "inventory")

# Random (non-scripted) fault kinds and their VDA error mapping.
RANDOM_FAULTS: tuple[str, ...] = ("motor_overtemp", "obstacle_blocked", "estop")

# Scripted fault: AMR-07 loses localization at this sim tick.
SCRIPTED_FAULT_ROBOT = "AMR-07"
SCRIPTED_FAULT_TICK = 20
SCRIPTED_FAULT_DURATION_TICKS = 12

# --------------------------------------------------------------------------
# Tunables (units in comments)
# --------------------------------------------------------------------------

SPEED_MPS = 1.2               # cruise speed while driving
MOVE_DRAIN = 0.09             # battery %/sim-second while driving
WORK_DRAIN = 0.05             # battery %/sim-second while executing a task action
IDLE_DRAIN = 0.004            # battery %/sim-second while idle
CHARGE_RATE = 0.45            # battery %/sim-second while docked at charger
LOW_BATTERY_GO_CHARGE = 20.0  # % at which a robot heads for a charger
LOW_BATTERY_ALERT = 15.0      # % at which a low-battery event/alert fires
CHARGED_ENOUGH = 90.0         # % at which a robot leaves the charger
WORK_SECONDS = 12.0           # time spent executing a pick/drop/etc. action
RANDOM_FAULT_P = 0.003        # per-robot, per-tick probability of a random fault
FAULT_TICKS_RANGE = (4, 10)   # random fault duration (ticks)
AMBIENT_TEMP = 34.0           # motor temp floor (deg C)


@dataclass
class Event:
    """Something alert-worthy that happened during a tick."""

    kind: str      # "fault" | "fault_cleared" | "low_battery"
    robot_id: str
    sev: str       # "info" | "warn" | "crit"
    msg: str
    tick: int


@dataclass
class Robot:
    """Mutable per-robot simulation state (internal; not a wire format)."""

    robot_id: str
    vendor: str
    node: str                       # last waypoint reached
    battery: float
    status: str = "idle"            # idle|moving|working|to_charger|charging|fault
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0              # heading, radians in [-pi, pi]
    speed: float = 0.0              # current linear speed m/s
    motor_temp: float = AMBIENT_TEMP
    health: float = 100.0
    tasks_done: int = 0
    task_kind: str | None = None
    # Path following: remaining nodes to visit (excludes current node).
    path: list[str] = field(default_factory=list)
    leg_progress_m: float = 0.0     # metres travelled along current leg
    work_left_s: float = 0.0
    resume_status: str = "idle"     # status to restore after a fault clears
    # Fault bookkeeping
    fault_kind: str | None = None
    fault_ticks_left: int = 0
    low_battery_alerted: bool = False
    # VDA order/header bookkeeping
    order_id: str = ""
    order_update_id: int = 0
    order_seq: int = 0              # per-robot order counter
    header_id: int = 0              # per-topic (state) headerId counter
    last_node_sequence_id: int = 0

    @property
    def localized(self) -> bool:
        """False while a localization fault is active (AMR-07 script)."""
        return self.fault_kind != "localization"


@dataclass
class TickOutput:
    """Everything one tick produced, ready for a transport to publish."""

    tick: int
    sim_time_s: float
    states: list[dict[str, Any]]              # VDA 5050 v2.1 state messages
    extras: dict[str, dict[str, Any]]         # robot_id -> non-VDA telemetry
    events: list[Event]
    throughput_per_h: float                   # fleet tasks/hour (rolling avg)


class FleetSim:
    """Steps the whole fleet; owns the only RNG for determinism."""

    def __init__(self, seed: int = 42, fleet_size: int = FLEET_SIZE) -> None:
        self.rng = random.Random(seed)
        self.tick_count = 0
        self.sim_time_s = 0.0
        self.writer_id = f"yantrasim-{uuid.UUID(int=self.rng.getrandbits(128)).hex[:8]}"
        self.robots: list[Robot] = []
        nodes = list(world.TASK_NODES)
        for i in range(1, fleet_size + 1):
            node = self.rng.choice(nodes)
            wp = world.WAYPOINTS[node]
            r = Robot(
                robot_id=f"AMR-{i:02d}",
                vendor=VENDORS[(i - 1) % len(VENDORS)],
                node=node,
                battery=round(self.rng.uniform(55.0, 95.0), 1),
                x=wp.x,
                y=wp.y,
                theta=0.0,
            )
            self.robots.append(r)

    # -- public API --------------------------------------------------------

    def tick(self, dt_s: float = 10.0, now: datetime | None = None) -> TickOutput:
        """Advance the world by ``dt_s`` simulated seconds and snapshot it."""
        self.tick_count += 1
        self.sim_time_s += dt_s
        events: list[Event] = []
        for r in self.robots:
            self._step_robot(r, dt_s, events)

        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
        ts = ts.replace("+00:00", "Z")
        states: list[dict[str, Any]] = []
        extras: dict[str, dict[str, Any]] = {}
        for r in self.robots:
            r.header_id += 1
            states.append(vda.build_state(r, header_id=r.header_id, timestamp=ts))
            extras[r.robot_id] = {
                "robot_id": r.robot_id,
                "task_kind": r.task_kind,
                "health": round(r.health, 1),
                "motor_temp": round(r.motor_temp, 1),
                "tasks_done": r.tasks_done,
                "internal_status": r.status,
            }
        hours = max(self.sim_time_s / 3600.0, 1e-9)
        throughput = sum(r.tasks_done for r in self.robots) / hours
        return TickOutput(
            tick=self.tick_count,
            sim_time_s=self.sim_time_s,
            states=states,
            extras=extras,
            events=events,
            throughput_per_h=round(throughput, 1),
        )

    # -- per-robot state machine ------------------------------------------

    def _step_robot(self, r: Robot, dt: float, events: list[Event]) -> None:
        self._maybe_fault(r, events)

        if r.status == "fault":
            r.speed = 0.0
            r.fault_ticks_left -= 1
            r.health = max(r.health - 1.5, 25.0)
            self._cool(r, dt)
            if r.fault_ticks_left <= 0:
                events.append(Event(
                    kind="fault_cleared", robot_id=r.robot_id, sev="info",
                    msg=f"{r.robot_id}: {r.fault_kind} cleared, resuming",
                    tick=self.tick_count,
                ))
                r.fault_kind = None
                r.status = r.resume_status
            return

        # Slow health recovery while running normally.
        r.health = min(r.health + 0.2, 100.0)

        if r.status == "charging":
            r.speed = 0.0
            r.battery = min(r.battery + CHARGE_RATE * dt, 100.0)
            self._cool(r, dt)
            if r.battery >= CHARGED_ENOUGH:
                r.status = "idle"
                r.task_kind = None
                r.low_battery_alerted = False
            return

        # Divert to charger when battery gets low (from any active state).
        if r.battery <= LOW_BATTERY_GO_CHARGE and r.status != "to_charger":
            self._route_to(r, world.nearest_charger(r.node), "to_charger")
            r.task_kind = None

        if r.battery <= LOW_BATTERY_ALERT and not r.low_battery_alerted:
            r.low_battery_alerted = True
            events.append(Event(
                kind="low_battery", robot_id=r.robot_id, sev="warn",
                msg=f"{r.robot_id} battery low ({r.battery:.0f}%), heading to charger",
                tick=self.tick_count,
            ))

        if r.status == "idle":
            r.speed = 0.0
            r.battery = max(r.battery - IDLE_DRAIN * dt, 0.0)
            self._cool(r, dt)
            # Pick up a new task most of the time (some idle dwell).
            if self.rng.random() < 0.8:
                self._assign_task(r)
            return

        if r.status == "working":
            r.speed = 0.0
            r.battery = max(r.battery - WORK_DRAIN * dt, 0.0)
            r.work_left_s -= dt
            if r.work_left_s <= 0:
                r.tasks_done += 1
                r.task_kind = None
                r.status = "idle"
            return

        if r.status in ("moving", "to_charger"):
            self._advance_along_path(r, dt)
            return

    # -- helpers -----------------------------------------------------------

    def _maybe_fault(self, r: Robot, events: list[Event]) -> None:
        if r.fault_kind is not None:
            return
        # Scripted, deterministic localization fault on AMR-07.
        if r.robot_id == SCRIPTED_FAULT_ROBOT and self.tick_count == SCRIPTED_FAULT_TICK:
            self._raise_fault(r, "localization", SCRIPTED_FAULT_DURATION_TICKS, events)
            return
        if self.rng.random() < RANDOM_FAULT_P:
            kind = self.rng.choice(RANDOM_FAULTS)
            dur = self.rng.randint(*FAULT_TICKS_RANGE)
            self._raise_fault(r, kind, dur, events)

    def _raise_fault(self, r: Robot, kind: str, ticks: int, events: list[Event]) -> None:
        r.fault_kind = kind
        r.fault_ticks_left = ticks
        r.resume_status = r.status if r.status in ("moving", "to_charger") else "idle"
        r.status = "fault"
        r.speed = 0.0
        sev = "crit" if vda.FAULT_LEVELS.get(kind) == "FATAL" else "warn"
        events.append(Event(
            kind="fault", robot_id=r.robot_id, sev=sev,
            msg=f"{r.robot_id}: {vda.FAULT_DESCRIPTIONS[kind]}",
            tick=self.tick_count,
        ))

    def _assign_task(self, r: Robot) -> None:
        targets = [n for n in world.TASK_NODES if n != r.node]
        target = self.rng.choice(targets)
        r.task_kind = self.rng.choice(TASK_KINDS)
        self._route_to(r, target, "moving")

    def _route_to(self, r: Robot, target: str, status: str) -> None:
        path = world.shortest_path(r.node, target)
        r.path = list(path[1:])  # exclude current node
        r.leg_progress_m = 0.0
        r.status = status
        r.order_seq += 1
        r.order_id = f"ord-{r.robot_id}-{r.order_seq}"
        r.order_update_id = 0
        r.last_node_sequence_id = 0
        if not r.path:  # already at target
            self._arrive(r)

    def _advance_along_path(self, r: Robot, dt: float) -> None:
        r.battery = max(r.battery - MOVE_DRAIN * dt, 0.0)
        # Motor warms toward ~55C while driving.
        r.motor_temp = min(r.motor_temp + 0.35 * dt, 58.0)
        r.speed = SPEED_MPS
        budget = SPEED_MPS * dt
        while budget > 0 and r.path:
            cur = world.WAYPOINTS[r.node]
            nxt = world.WAYPOINTS[r.path[0]]
            leg_len = math.hypot(nxt.x - cur.x, nxt.y - cur.y)
            r.theta = math.atan2(nxt.y - cur.y, nxt.x - cur.x)
            remaining = leg_len - r.leg_progress_m
            if budget >= remaining:
                budget -= remaining
                r.node = r.path.pop(0)
                r.leg_progress_m = 0.0
                r.last_node_sequence_id += 2  # VDA: nodes even, edges odd
                r.x, r.y = nxt.x, nxt.y
            else:
                r.leg_progress_m += budget
                frac = r.leg_progress_m / leg_len
                r.x = cur.x + (nxt.x - cur.x) * frac
                r.y = cur.y + (nxt.y - cur.y) * frac
                budget = 0.0
        if not r.path:
            self._arrive(r)

    def _arrive(self, r: Robot) -> None:
        wp = world.WAYPOINTS[r.node]
        r.x, r.y = wp.x, wp.y
        r.speed = 0.0
        if r.status == "to_charger":
            r.status = "charging"
        elif r.status == "moving":
            r.status = "working"
            r.work_left_s = WORK_SECONDS
        else:  # routed while idle with zero-length path
            r.status = "idle"

    @staticmethod
    def _cool(r: Robot, dt: float) -> None:
        r.motor_temp = max(r.motor_temp - 0.25 * dt, AMBIENT_TEMP)
