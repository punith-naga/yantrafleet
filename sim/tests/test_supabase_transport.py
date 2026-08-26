"""Supabase transport tests — fully offline via httpx.MockTransport."""
import json

import httpx
import pytest

from yantrasim.sim import FleetSim, SCRIPTED_FAULT_TICK
from yantrasim.transports.supabase import (
    DEFAULT_KEY, DEFAULT_URL, SupabaseTransport, resolve_config,
)

from conftest import FIXED_NOW


@pytest.fixture()
def recorder():
    """(requests, client): every POST is captured, nothing leaves the process."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return requests, client


def _bodies_by_table(requests):
    out = {}
    for req in requests:
        table = req.url.path.rsplit("/", 1)[-1]
        out.setdefault(table, []).append((req, json.loads(req.content)))
    return out


def test_publish_bulk_upserts_robots(recorder):
    requests, client = recorder
    fleet = FleetSim(seed=7)
    t = SupabaseTransport(client=client, writer_id=fleet.writer_id)
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))

    by_table = _bodies_by_table(requests)
    assert "robots" in by_table
    req, rows = by_table["robots"][0]
    # Endpoint + upsert semantics
    assert req.url.scheme == "https"
    assert req.url.host == "flwyvhsmgrrqpmhcqlzd.supabase.co"
    assert req.url.path == "/rest/v1/robots"
    assert req.url.params["on_conflict"] == "id"
    assert "resolution=merge-duplicates" in req.headers["Prefer"]
    assert "return=minimal" in req.headers["Prefer"]
    # Auth headers
    assert req.headers["apikey"] == DEFAULT_KEY
    assert req.headers["Authorization"] == f"Bearer {DEFAULT_KEY}"
    # One bulk body, 10 rows, correct shape
    assert len(rows) == 10
    assert {r["id"] for r in rows} == {f"AMR-{i:02d}" for i in range(1, 11)}
    assert all(isinstance(r["pos"], list) and len(r["pos"]) == 2 for r in rows)


def test_fleet_meta_heartbeat_upserted(recorder):
    requests, client = recorder
    fleet = FleetSim(seed=7)
    t = SupabaseTransport(client=client, writer_id="yantrasim-test")
    t.publish(fleet.tick(dt_s=60.0, now=FIXED_NOW))
    by_table = _bodies_by_table(requests)
    req, rows = by_table["fleet_meta"][0]
    assert req.url.params["on_conflict"] == "id"
    assert rows[0]["id"] == 1
    assert rows[0]["writer_id"] == "yantrasim-test"
    assert rows[0]["sim_min"] == 1.0


def test_alerts_inserted_on_scripted_fault(recorder):
    requests, client = recorder
    fleet = FleetSim(seed=8)
    t = SupabaseTransport(client=client)
    out = None
    while fleet.tick_count < SCRIPTED_FAULT_TICK:
        out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    requests.clear()
    t.publish(out)
    by_table = _bodies_by_table(requests)
    assert "alerts" in by_table
    req, rows = by_table["alerts"][0]
    # Insert-if-absent (idempotent retries), not merge
    assert "resolution=ignore-duplicates" in req.headers["Prefer"]
    fault_alerts = [r for r in rows if r["src"] == "AMR-07"]
    assert fault_alerts and fault_alerts[0]["sev"] == "crit"
    assert "Localization lost" in fault_alerts[0]["msg"]


def test_no_alerts_post_when_no_events(recorder):
    requests, client = recorder
    fleet = FleetSim(seed=7)
    t = SupabaseTransport(client=client)
    out = fleet.tick(dt_s=10.0, now=FIXED_NOW)
    out.events.clear()  # force a quiet tick
    t.publish(out)
    assert "alerts" not in _bodies_by_table(requests)


def test_network_error_does_not_crash(recorder):
    _, _ = recorder

    def boom(request):
        raise httpx.ConnectError("supabase.co unreachable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    fleet = FleetSim(seed=7)
    t = SupabaseTransport(client=client)
    t.publish(fleet.tick(dt_s=10.0, now=FIXED_NOW))  # must not raise


def test_resolve_config_env_override(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.test/")
    monkeypatch.setenv("SUPABASE_KEY", "sb_test_key")
    url, key = resolve_config()
    assert url == "https://example.test"  # trailing slash stripped
    assert key == "sb_test_key"
    # explicit args beat env
    url2, key2 = resolve_config("https://x.test", "k2")
    assert (url2, key2) == ("https://x.test", "k2")
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("SUPABASE_KEY")
    assert resolve_config() == (DEFAULT_URL, DEFAULT_KEY)
