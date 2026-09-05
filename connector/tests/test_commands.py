"""Offline tests for CommandPublisher: approved rows -> instantActions,
actionState acks -> PATCH executed/failed. httpx.MockTransport + a fake
publish callable — nothing leaves the process."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

import httpx

from yantrabridge.commands import CommandPublisher


class FakeBackend:
    """Answers the PostgREST subset CommandPublisher uses, recording all."""

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.robots: list[dict[str, Any]] = []
        self.patches: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.fail_gets = False
        self.fail_patches = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(parse_qsl(request.url.query.decode()))
        if request.method == "GET" and request.url.path.endswith("/commands"):
            if self.fail_gets:
                return httpx.Response(500)
            rows = [r for r in self.commands
                    if f"eq.{r['status']}" == params.get("status", "eq.")]
            if "site_id" in params:
                rows = [r for r in rows
                        if f"eq.{r.get('site_id')}" == params["site_id"]]
            return httpx.Response(200, json=rows)
        if request.method == "GET" and request.url.path.endswith("/robots"):
            want = params.get("id", "")
            rows = [r for r in self.robots if f"eq.{r['id']}" == want]
            return httpx.Response(200, json=rows)
        if request.method == "PATCH" and request.url.path.endswith("/commands"):
            if self.fail_patches:
                return httpx.Response(500)
            body = json.loads(request.content.decode())
            self.patches.append((params, body))
            cid = params.get("id", "").removeprefix("eq.")
            for row in self.commands:
                if str(row["id"]) == cid:
                    row.update(body)
            return httpx.Response(204)
        return httpx.Response(404)


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_publish = False

    def publish(self, topic: str, payload: str) -> None:
        if self.raise_on_publish:
            raise RuntimeError("broker gone")
        self.published.append((topic, json.loads(payload)))


def make(site: str | None = None) -> tuple[FakeBackend, FakeMqtt, CommandPublisher]:
    backend = FakeBackend()
    mqtt = FakeMqtt()
    pub = CommandPublisher(
        mqtt.publish, "https://example.supabase.co", "test-key", site=site,
        transport=httpx.MockTransport(backend.handler))
    return backend, mqtt, pub


CMD = {"id": "11111111-2222-3333-4444-555555555555",
       "robot_id": "AMR_01", "cmd": "pause", "status": "approved"}
STATE = {"manufacturer": "nexomotion", "serialNumber": "AMR_01",
         "actionStates": []}


class TestPublishing:
    def test_approved_command_becomes_instant_actions(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append(dict(CMD))
        pub.handle_state(STATE)  # bridge learned the robot from a state msg
        assert pub.poll() == 1
        topic, msg = mqtt.published[0]
        assert topic == "uagv/v2/nexomotion/AMR_01/instantActions"
        assert msg["manufacturer"] == "nexomotion"
        assert msg["serialNumber"] == "AMR_01"
        assert msg["version"] == "2.1.0"
        assert msg["headerId"] == 1 and msg["timestamp"]
        assert msg["actions"] == [{
            "actionType": "yantra.pause",
            "actionId": CMD["id"],
            "blockingType": "HARD",
            "actionParameters": [],
        }]
        # no invented 'sent' status: the row is untouched until the ack
        assert backend.patches == []
        assert backend.commands[0]["status"] == "approved"

    def test_dedupe_never_republishes_same_command(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append(dict(CMD))
        pub.handle_state(STATE)
        assert pub.poll() == 1
        assert pub.poll() == 0  # row still approved, but already published
        assert len(mqtt.published) == 1

    def test_manufacturer_falls_back_to_robots_table(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append(dict(CMD))
        backend.robots.append({"id": "AMR_01", "vendor": "agilus"})
        assert pub.poll() == 1  # no state message seen yet
        topic, _ = mqtt.published[0]
        assert topic == "uagv/v2/agilus/AMR_01/instantActions"

    def test_unknown_manufacturer_retries_next_poll(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append(dict(CMD))
        assert pub.poll() == 0          # nowhere to route it yet
        assert mqtt.published == []
        pub.handle_state(STATE)         # robot appears
        assert pub.poll() == 1          # retried, not lost

    def test_raw_fleet_id_is_sanitized_for_the_topic(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append({**CMD, "robot_id": "AMR-01"})
        pub.handle_state(STATE)
        assert pub.poll() == 1
        assert mqtt.published[0][0] == "uagv/v2/nexomotion/AMR_01/instantActions"

    def test_site_filter_applied_when_set(self) -> None:
        backend, mqtt, pub = make(site="PNQ-DC2")
        backend.commands.append({**CMD, "site_id": "BLR-DC1"})
        assert pub.poll() == 0
        get = next(r for r in backend.requests
                   if r.url.path.endswith("/commands"))
        assert dict(parse_qsl(get.url.query.decode()))["site_id"] == "eq.PNQ-DC2"

    def test_unsupported_cmd_patched_failed_immediately(self) -> None:
        backend, mqtt, pub = make()
        backend.commands.append({**CMD, "cmd": "self_destruct"})
        assert pub.poll() == 0
        assert mqtt.published == []
        params, body = backend.patches[0]
        assert params["id"] == f"eq.{CMD['id']}"
        assert body["status"] == "failed"
        assert "unsupported command" in body["note"]

    def test_backend_or_broker_failure_never_raises(self) -> None:
        backend, mqtt, pub = make()
        backend.fail_gets = True
        assert pub.poll() == 0
        backend.fail_gets = False
        backend.commands.append(dict(CMD))
        pub.handle_state(STATE)
        mqtt.raise_on_publish = True
        assert pub.poll() == 0          # publish failed -> not marked sent
        mqtt.raise_on_publish = False
        assert pub.poll() == 1          # retried


class TestPublishOrder:
    """The outbound half of the order contract: dispatch a mission as a
    real VDA 5050 ``order`` message (poll()/handle_state() above cover the
    instantActions request/response loop; this is the master-control ->
    AGV direction that request/response loop was missing)."""

    NODES = [
        {"nodeId": "n0_0", "sequenceId": 0, "released": True, "actions": []},
        {"nodeId": "n0_1", "sequenceId": 2, "released": True,
         "actions": [{"actionId": "act-1", "actionType": "drop",
                      "blockingType": "NONE", "actionParameters": []}]},
    ]

    def test_publish_order_shape_on_known_manufacturer(self) -> None:
        backend, mqtt, pub = make()
        pub.handle_state(STATE)  # learns AMR_01 -> nexomotion
        ok = pub.publish_order("AMR_01", "order-1", self.NODES)
        assert ok
        topic, msg = mqtt.published[0]
        assert topic == "uagv/v2/nexomotion/AMR_01/order"
        assert msg["manufacturer"] == "nexomotion"
        assert msg["serialNumber"] == "AMR_01"
        assert msg["version"] == "2.1.0"
        assert msg["orderId"] == "order-1"
        assert msg["orderUpdateId"] == 0
        assert msg["nodes"] == self.NODES
        assert msg["edges"] == []
        assert msg["headerId"] == 1 and msg["timestamp"]
        # no commands-table row involved
        assert backend.patches == []

    def test_publish_order_header_id_increments_per_topic(self) -> None:
        backend, mqtt, pub = make()
        pub.handle_state(STATE)
        pub.publish_order("AMR_01", "order-1", self.NODES)
        pub.publish_order("AMR_01", "order-1", self.NODES, order_update_id=1)
        headers = [msg["headerId"] for _, msg in mqtt.published]
        assert headers == [1, 2]

    def test_publish_order_raw_fleet_id_is_sanitized(self) -> None:
        backend, mqtt, pub = make()
        pub.handle_state(STATE)
        assert pub.publish_order("AMR-01", "order-2", self.NODES)
        topic, _ = mqtt.published[0]
        assert topic == "uagv/v2/nexomotion/AMR_01/order"

    def test_publish_order_unknown_manufacturer_returns_false(self) -> None:
        backend, mqtt, pub = make()
        ok = pub.publish_order("AMR_99", "order-3", self.NODES)
        assert not ok
        assert mqtt.published == []

    def test_publish_order_manufacturer_falls_back_to_robots_table(self) -> None:
        backend, mqtt, pub = make()
        backend.robots.append({"id": "AMR_02", "vendor": "agilus"})
        assert pub.publish_order("AMR_02", "order-4", self.NODES)
        topic, _ = mqtt.published[0]
        assert topic == "uagv/v2/agilus/AMR_02/order"

    def test_publish_order_broker_failure_returns_false(self) -> None:
        backend, mqtt, pub = make()
        pub.handle_state(STATE)
        mqtt.raise_on_publish = True
        assert not pub.publish_order("AMR_01", "order-5", self.NODES)


class TestActionStateAcks:
    def _published(self) -> tuple[FakeBackend, FakeMqtt, CommandPublisher]:
        backend, mqtt, pub = make()
        backend.commands.append(dict(CMD))
        pub.handle_state(STATE)
        assert pub.poll() == 1
        return backend, mqtt, pub

    def test_finished_action_state_patches_executed(self) -> None:
        backend, _, pub = self._published()
        pub.handle_state({**STATE, "actionStates": [{
            "actionId": CMD["id"], "actionType": "yantra.pause",
            "actionStatus": "FINISHED",
            "resultDescription": "AMR-01 paused (was idle)"}]})
        params, body = backend.patches[-1]
        assert params["id"] == f"eq.{CMD['id']}"
        assert body["status"] == "executed"
        assert body["note"] == "AMR-01 paused (was idle)"
        assert body["executed_at"]

    def test_failed_action_state_patches_failed(self) -> None:
        backend, _, pub = self._published()
        pub.handle_state({**STATE, "actionStates": [{
            "actionId": CMD["id"], "actionStatus": "FAILED",
            "resultDescription": "AMR-01 is not held (status=idle)"}]})
        _, body = backend.patches[-1]
        assert body["status"] == "failed"
        assert "not held" in body["note"]

    def test_repeated_action_state_patches_once(self) -> None:
        backend, _, pub = self._published()
        ack = {**STATE, "actionStates": [{
            "actionId": CMD["id"], "actionStatus": "FINISHED",
            "resultDescription": "ok"}]}
        pub.handle_state(ack)
        pub.handle_state(ack)  # actionState repeats in later state msgs
        assert len(backend.patches) == 1

    def test_non_terminal_and_foreign_action_states_ignored(self) -> None:
        backend, _, pub = self._published()
        pub.handle_state({**STATE, "actionStates": [
            {"actionId": CMD["id"], "actionStatus": "RUNNING"},
            {"actionId": "someone-elses-task", "actionStatus": "FINISHED"},
            "garbage",
        ]})
        assert backend.patches == []

    def test_patch_failure_retries_on_next_state(self) -> None:
        backend, _, pub = self._published()
        ack = {**STATE, "actionStates": [{
            "actionId": CMD["id"], "actionStatus": "FINISHED",
            "resultDescription": "ok"}]}
        backend.fail_patches = True
        pub.handle_state(ack)           # PATCH 500 -> stays pending
        backend.fail_patches = False
        pub.handle_state(ack)           # repeat closes it
        assert backend.commands[0]["status"] == "executed"
