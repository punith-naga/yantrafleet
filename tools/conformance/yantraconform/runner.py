"""Discovery, probing and scoring: the conformance run itself.

The run is a fixed, auditable script:

    1. DISCOVER  subscribe to the AGV->master topics with wildcards and
                 collect whatever announces itself (the retained connection
                 message is what makes a vehicle discoverable at all).
    2. OBSERVE   watch passively for a while: this is what grades the state
                 topic, header monotonicity, timestamps and topic hygiene
                 without touching the vehicle.
    3. PROBE     (unless --passive) exercise instantActions and the order
                 topic against each discovered vehicle, one step at a time,
                 recording exactly when each probe went out so responses can
                 be attributed deterministically.
    4. RETAIN    re-subscribe to connection/factsheet/state. A broker MUST
                 replay retained messages on every new subscription, so this
                 is how the tester learns what is retained without needing
                 broker-side access.
    5. SCORE     run the whole check catalogue over the collected evidence.

Every wait goes through an injected ``sleep_fn``, so the same runner code that
waits on wall-clock seconds against a real broker can be driven by a test that
pumps a simulator instead. Nothing else in the engine touches a clock.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import checks, spec
from .model import Report, RobotReport
from .observe import Collector, Observation
from .session import Session

#: An order/action id prefix so anything this tester injects is identifiable
#: in a customer's own logs afterwards.
PROBE_PREFIX = "yantraconform"

#: A node id chosen to be impossible on any real map, for the invalid-order
#: probe. Deliberately long and self-describing so it is obvious in a log.
IMPOSSIBLE_NODE = "yantraconform-no-such-node-0000"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class RunOptions:
    """Everything that changes what a run does."""

    interface: str = spec.DEFAULT_INTERFACE
    major: str = spec.DEFAULT_MAJOR_SEGMENT
    manufacturer: str | None = None     # None -> '+' wildcard
    serial: str | None = None           # None -> '+' wildcard
    discover_seconds: float = 5.0
    observe_seconds: float = 8.0
    settle_seconds: float = 2.0         # wait after each active probe
    retain_seconds: float = 1.0
    active: bool = True                 # False == --passive
    max_robots: int = 5
    #: node id to use for the order probe; discovered from the vehicle's own
    #: state when not supplied.
    node: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "interface": self.interface, "major_version": self.major,
            "manufacturer": self.manufacturer, "serial": self.serial,
            "discover_seconds": self.discover_seconds,
            "observe_seconds": self.observe_seconds,
            "settle_seconds": self.settle_seconds,
            "retain_seconds": self.retain_seconds,
            "active_probing": self.active, "max_robots": self.max_robots,
            "node": self.node,
        }


ProgressFn = Callable[[str], None]


@dataclass
class ConformanceRunner:
    """Runs the conformance battery against one broker."""

    session: Session
    options: RunOptions = field(default_factory=RunOptions)
    sleep_fn: Callable[[float], None] = time.sleep
    progress: ProgressFn = lambda msg: None
    broker_label: str = ""

    def __post_init__(self) -> None:
        self.collector = Collector(self.options.interface, self.options.major)
        self.warnings: list[str] = []
        self._header_id = 0

    # -- wire helpers ------------------------------------------------------

    def _wildcard(self, subtopic: str) -> str:
        return spec.build_topic(
            self.options.interface, self.options.major,
            self.options.manufacturer or "+", self.options.serial or "+",
            subtopic)

    def _pump(self, seconds: float) -> None:
        """Let time pass, then move everything received into the collector."""
        if seconds > 0:
            self.sleep_fn(seconds)
        self.collector.feed_all(self.session.drain())

    def _header(self) -> dict[str, Any]:
        self._header_id += 1
        return {"headerId": self._header_id, "timestamp": _now_iso(),
                "version": spec.TARGET_VERSION}

    def _publish(self, obs: Observation, subtopic: str,
                 payload: dict[str, Any]) -> None:
        topic = spec.build_topic(obs.interface, obs.major, obs.manufacturer,
                                 obs.serial, subtopic)
        body = dict(self._header())
        body.update({"manufacturer": obs.manufacturer,
                     "serialNumber": obs.serial})
        body.update(payload)
        self.session.publish(topic, json.dumps(body), qos=0, retain=False)

    # -- phases ------------------------------------------------------------

    def discover(self) -> list[Observation]:
        for sub in spec.SUBTOPICS_FROM_AGV:
            self.session.subscribe(self._wildcard(sub), qos=1)
        self.progress(f"discovering for {self.options.discover_seconds:g}s ...")
        self._pump(self.options.discover_seconds)
        found = list(self.collector.robots.values())
        if self.collector.unroutable:
            bad = sorted({r.message.topic for r in self.collector.unroutable})
            self.warnings.append(
                f"{len(self.collector.unroutable)} message(s) arrived on "
                f"{len(bad)} topic(s) that are not five-segment VDA 5050 paths: "
                + ", ".join(bad[:3]))
        if len(found) > self.options.max_robots:
            self.warnings.append(
                f"{len(found)} vehicles discovered; testing the first "
                f"{self.options.max_robots} (raise --max-robots to widen)")
            found = found[:self.options.max_robots]
        for obs in found:
            obs.active_probing = self.options.active
        return found

    def observe(self) -> None:
        self.progress(f"observing for {self.options.observe_seconds:g}s ...")
        self._pump(self.options.observe_seconds)

    def probe(self, obs: Observation) -> None:
        """Exercise one vehicle's command surface, one step at a time."""
        if not self.options.active:
            return
        run_tag = uuid.uuid4().hex[:8]
        self._probe_instant(obs, "unknown", f"{PROBE_PREFIX}.unsupportedProbe",
                            run_tag)
        self._probe_instant(obs, "stateRequest", "stateRequest", run_tag)
        self._probe_instant(obs, "factsheetRequest", "factsheetRequest", run_tag)
        self._probe_orders(obs, run_tag)
        self._probe_instant(obs, "cancelOrder", "cancelOrder", run_tag)
        self._probe_instant(obs, "initPosition", "initPosition", run_tag,
                            parameters=self._init_position_parameters(obs))

    def _probe_instant(self, obs: Observation, name: str, action_type: str,
                       run_tag: str,
                       parameters: list[dict[str, Any]] | None = None) -> None:
        action_id = f"{PROBE_PREFIX}-{run_tag}-{name}"
        obs.note_probe(f"instant_{name}", action_id=action_id,
                       action_type=action_type, sent_at=time.monotonic())
        self.progress(f"  {obs.serial}: instantAction {action_type}")
        self._publish(obs, "instantActions", {
            "actions": [{
                "actionId": action_id,
                "actionType": action_type,
                "blockingType": "NONE",
                "actionParameters": parameters or [],
            }],
        })
        self._pump(self.options.settle_seconds)

    def _init_position_parameters(self, obs: Observation) -> list[dict[str, Any]]:
        """Re-assert the vehicle's OWN last reported pose.

        Deliberately non-destructive: the tester never invents a position, so
        a vehicle that accepts the action ends up exactly where it already
        believed it was.
        """
        for rec in reversed(obs.states):
            p = rec.payload or {}
            pos = p.get("agvPosition") or {}
            params: list[dict[str, Any]] = []
            if p.get("lastNodeId"):
                params.append({"key": "lastNodeId", "value": p["lastNodeId"]})
            for key in ("x", "y", "theta", "mapId"):
                if key in pos:
                    params.append({"key": key, "value": pos[key]})
            if params:
                return params
        return []

    def _order_node(self, obs: Observation) -> str | None:
        """A node id the vehicle itself has told us exists.

        Preference order: an explicit --node, then the furthest node on the
        vehicle's own current horizon (which gives a real move to observe),
        then its lastNodeId (a no-op order that is still valid). The tester
        never guesses a node id: dispatching a robot to a coordinate nobody
        confirmed exists is not something a free web tool should do.
        """
        if self.options.node:
            return self.options.node
        for rec in reversed(obs.states):
            p = rec.payload or {}
            node_ids = [n.get("nodeId") for n in (p.get("nodeStates") or [])
                        if isinstance(n, dict) and n.get("nodeId")]
            if node_ids:
                return str(node_ids[-1])
        for rec in reversed(obs.states):
            last = (rec.payload or {}).get("lastNodeId")
            if last:
                return str(last)
        return None

    def _publish_order(self, obs: Observation, order_id: str, update_id: int,
                       node_ids: list[str],
                       action: dict[str, Any] | None = None) -> None:
        nodes = []
        for i, nid in enumerate(node_ids):
            node: dict[str, Any] = {"nodeId": nid, "sequenceId": i * 2,
                                    "released": True, "actions": []}
            if action is not None and i == len(node_ids) - 1:
                node["actions"] = [action]
            nodes.append(node)
        self._publish(obs, "order", {
            "orderId": order_id, "orderUpdateId": update_id,
            "nodes": nodes, "edges": [],
        })

    def _probe_orders(self, obs: Observation, run_tag: str) -> None:
        node = self._order_node(obs)
        if node is None:
            self.warnings.append(
                f"{obs.serial}: no nodeId could be discovered from the "
                "vehicle's own state, so the order probes were skipped "
                "(pass --node <id> to test order handling explicitly)")
            return
        order_id = f"{PROBE_PREFIX}-{run_tag}"

        # 1. a brand-new order
        obs.note_probe("order_new", order_id=order_id, order_update_id=0,
                       node=node)
        self.progress(f"  {obs.serial}: order {order_id} (new, node {node})")
        self._publish_order(obs, order_id, 0, [node], action={
            "actionId": f"{PROBE_PREFIX}-{run_tag}-node", "actionType": "move",
            "blockingType": "NONE", "actionParameters": []})
        self._pump(self.options.settle_seconds)

        # 2. a legitimate update to it
        obs.note_probe("order_update", order_id=order_id, order_update_id=1,
                       node=node)
        self.progress(f"  {obs.serial}: order {order_id} update 1")
        self._publish_order(obs, order_id, 1, [node])
        self._pump(self.options.settle_seconds)

        # 3. a stale replay of update 0 -- must NOT be adopted
        obs.note_probe("order_stale", order_id=order_id, order_update_id=0,
                       current_update_id=1, node=node)
        self.progress(f"  {obs.serial}: order {order_id} stale replay of update 0")
        self._publish_order(obs, order_id, 0, [node])
        self._pump(self.options.settle_seconds)

        # 4. an order the vehicle cannot possibly execute
        invalid_id = f"{PROBE_PREFIX}-{run_tag}-invalid"
        obs.note_probe("order_invalid", order_id=invalid_id, order_update_id=0,
                       node=IMPOSSIBLE_NODE)
        self.progress(f"  {obs.serial}: order {invalid_id} (unreachable node)")
        self._publish_order(obs, invalid_id, 0, [IMPOSSIBLE_NODE])
        self._pump(self.options.settle_seconds)

    def retain_probe(self, targets: list[Observation]) -> None:
        """Re-subscribe per vehicle to learn which topics the broker retains.

        A broker replays retained messages on EVERY new subscription, so this
        works against any real broker without privileged access. Messages that
        come back with retain=1 identify the retained topics; the rest of the
        run's evidence is unaffected because the collector keys off the topic.
        """
        for obs in targets:
            for sub in ("connection", "factsheet", "state"):
                obs.retain_probe.setdefault(sub, False)
            obs.retain_probed = True
        for obs in targets:
            for sub in ("connection", "factsheet", "state"):
                self.session.subscribe(
                    spec.build_topic(obs.interface, obs.major,
                                     obs.manufacturer, obs.serial, sub), qos=1)
        self.progress("probing retained messages ...")
        batch = []
        if self.options.retain_seconds > 0:
            self.sleep_fn(self.options.retain_seconds)
        batch = self.session.drain()
        for msg in batch:
            parts = spec.parse_topic(msg.topic)
            if msg.retain and parts is not None:
                obs = self.collector.robots.get((parts.manufacturer, parts.serial))
                if obs is not None and parts.subtopic in obs.retain_probe:
                    obs.retain_probe[parts.subtopic] = True
        # the messages themselves are still evidence
        self.collector.feed_all(batch)

    # -- the whole thing ---------------------------------------------------

    def run(self) -> Report:
        targets = self.discover()
        self.observe()
        if not targets:
            targets = list(self.collector.robots.values())[:self.options.max_robots]
            for obs in targets:
                obs.active_probing = self.options.active
        for obs in targets:
            self._capture_wills(obs)
            self.probe(obs)
        self._pump(0)
        self.retain_probe(targets)
        self._capture_wills_all(targets)

        report = Report(
            broker=self.broker_label,
            generated_at=_now_iso(),
            options=self.options.to_dict(),
            warnings=list(self.warnings),
        )
        if not targets:
            report.warnings.append(
                "no vehicles discovered: nothing published on "
                f"{self._wildcard('connection')} or {self._wildcard('state')}")
        for obs in targets:
            rr = RobotReport(
                manufacturer=obs.manufacturer, serial=obs.serial,
                interface=obs.interface, major=obs.major,
                version_reported=obs.version_reported(),
                topics_seen=obs.topics_seen(),
                message_counts=obs.message_counts(),
            )
            if not self.options.active:
                rr.notes.append("active probing disabled: order, instantAction "
                                "and actionState checks were skipped")
            rr.checks = checks.run_checks(obs)
            report.robots.append(rr)
        return report

    # -- optional broker introspection -------------------------------------

    def _capture_wills(self, obs: Observation) -> None:
        wills = getattr(self.session, "wills", None)
        if callable(wills):
            obs.wills_observable = True
            obs.wills = list(wills())

    def _capture_wills_all(self, targets: list[Observation]) -> None:
        for obs in targets:
            self._capture_wills(obs)


def run_conformance(session: Session, options: RunOptions | None = None,
                    sleep_fn: Callable[[float], None] = time.sleep,
                    progress: ProgressFn | None = None,
                    broker_label: str = "") -> Report:
    """Library entry point: run the battery and return a :class:`Report`."""
    runner = ConformanceRunner(
        session=session, options=options or RunOptions(), sleep_fn=sleep_fn,
        progress=progress or (lambda msg: None), broker_label=broker_label)
    return runner.run()
