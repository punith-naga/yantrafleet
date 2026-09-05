"""Test plumbing: drive a vehicle implementation and the tester in lockstep.

Both fixtures -- the hand-written :mod:`fake_robots` and this repo's real
``yantrasim`` -- hang off the same in-process :class:`LoopbackBroker`, and both
are advanced by the SAME injected ``sleep_fn``. Nothing sleeps, nothing races,
and a test that fails fails for a protocol reason rather than a timing one.

The unit of time is a "tick". ``RunOptions``' second-valued knobs are read as
tick counts here, so ``settle_seconds=2`` means "advance the vehicle twice
after each probe, then drain".
"""
from __future__ import annotations

from typing import Any, Callable

from yantraconform.loopback import LoopbackBroker, LoopbackSession, PahoShimClient
from yantraconform.runner import ConformanceRunner, RunOptions

#: Enough ticks for a probe to be applied AND for the resulting state to be
#: published. yantrasim applies queued commands AFTER it publishes each tick's
#: state, so a response is never visible in fewer than two ticks.
SETTLE_TICKS = 3


def fast_options(**over: Any) -> RunOptions:
    """RunOptions with the wait knobs turned into small tick counts."""
    base = dict(discover_seconds=2, observe_seconds=4,
                settle_seconds=SETTLE_TICKS, retain_seconds=1)
    base.update(over)
    return RunOptions(**base)


def run_against(tick: Callable[[int], None], broker: LoopbackBroker,
                options: RunOptions | None = None) -> dict[str, Any]:
    """Run the full battery, advancing the vehicle via ``tick`` on every wait.

    Returns the machine-readable report document (``Report.to_dict()``).
    """
    session = LoopbackSession(broker)
    runner = ConformanceRunner(
        session=session,
        options=options or fast_options(),
        sleep_fn=lambda seconds: tick(max(1, int(seconds))),
        broker_label="loopback://in-process",
    )
    try:
        return runner.run().to_dict()
    finally:
        session.close()


def checks_by_id(document: dict[str, Any], robot_index: int = 0) -> dict[str, dict]:
    return {c["id"]: c for c in document["robots"][robot_index]["checks"]}


def statuses(document: dict[str, Any], robot_index: int = 0) -> dict[str, str]:
    return {cid: c["status"]
            for cid, c in checks_by_id(document, robot_index).items()}


def failing(document: dict[str, Any]) -> set[str]:
    return {c["id"] for r in document["robots"] for c in r["checks"]
            if c["status"] == "fail"}


# ---------------------------------------------------------------------------
# the real simulator
# ---------------------------------------------------------------------------

class SimFleet:
    """This repo's own ``yantrasim`` fleet, wired onto a LoopbackBroker.

    Reproduces exactly what ``python -m yantrasim --mqtt`` does per tick
    (publish, then drain instantActions, then drain orders), so the tester is
    grading the shipped simulator's real wire behaviour and not a re-implemen-
    tation of it.
    """

    def __init__(self, broker: LoopbackBroker, seed: int = 42,
                 robots: int | None = None, dt_s: float = 5.0) -> None:
        from yantrasim.commands import apply_command
        from yantrasim.sim import FleetSim
        from yantrasim.transports.mqtt import MqttTransport

        self.dt_s = dt_s
        self._apply_command = apply_command
        self.sim = FleetSim(seed=seed)
        if robots is not None:
            self.sim.robots = self.sim.robots[:robots]
        self.client = PahoShimClient(broker, name="yantrasim")
        # MqttTransport announces ONLINE (retained) for every robot here.
        self.transport = MqttTransport(self.sim.robots, client=self.client)

    @property
    def serials(self) -> list[str]:
        from yantrasim import vda
        return [vda.sanitize_serial(r.robot_id) for r in self.sim.robots]

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            out = self.sim.tick(dt_s=self.dt_s)
            self.transport.publish(out)
            self.transport.poll_commands(
                lambda rid, cmd: self._apply_command(self.sim, rid, cmd))
            self.transport.poll_orders(
                lambda rid, oid, ouid, nodes, actions:
                    self.sim.apply_order(rid, oid, ouid, nodes, actions))

    def close(self) -> None:
        self.transport.close()
