"""MQTT transport tests — offline, with a recording fake client.

paho-mqtt is optional: these tests never require it, because the
transport accepts an injected client and topic logic lives in vda.py.
"""
import json

from yantrasim.sim import FleetSim
from yantrasim.transports import mqtt as mqtt_mod
from yantrasim.transports.mqtt import MqttTransport

from conftest import FIXED_NOW


class FakeClient:
    def __init__(self):
        self.published = []  # (topic, payload, qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))


def test_optional_import_module_loads_without_paho():
    # Module import must never require paho; only runtime construction does.
    assert hasattr(mqtt_mod, "PAHO_AVAILABLE")


def test_online_published_retained_qos1_on_start():
    fleet = FleetSim(seed=7)
    fake = FakeClient()
    MqttTransport(fleet.robots, client=fake)
    conn = [p for p in fake.published if p[0].endswith("/connection")]
    assert len(conn) == 10
    for topic, msg, qos, retain in conn:
        assert qos == 1 and retain is True  # spec: connection QoS1 retained
        assert msg["connectionState"] == "ONLINE"
        iface, major, manuf, serial, sub = topic.split("/")
        assert (iface, major, sub) == ("uagv", "v2", "connection")
        assert msg["manufacturer"] == manuf
        assert msg["serialNumber"] == serial


def test_state_topics_and_qos():
    fleet = FleetSim(seed=7)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    fake.published.clear()
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))
    states = [p for p in fake.published if p[0].endswith("/state")]
    assert len(states) == 10
    for topic, msg, qos, retain in states:
        assert qos == 0 and retain is False  # spec: state QoS0 non-retained
        _, _, manuf, serial, _ = topic.split("/")
        # header must match topic path
        assert msg["manufacturer"] == manuf
        assert msg["serialNumber"] == serial
        assert "-" not in serial  # sanitized: AMR-07 -> AMR_07


def test_offline_on_close():
    fleet = FleetSim(seed=7)
    fake = FakeClient()
    t = MqttTransport(fleet.robots, client=fake)
    fake.published.clear()
    t.close()
    conn = [p for p in fake.published if p[0].endswith("/connection")]
    assert len(conn) == 10
    assert all(m["connectionState"] == "OFFLINE" for _, m, _, _ in conn)
