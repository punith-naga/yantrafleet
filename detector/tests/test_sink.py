"""PostgRESTSink payloads via httpx.MockTransport — fully offline."""
from __future__ import annotations

import json

import httpx
import pytest

from yantradetect.engine import Action
from yantradetect.sink import DryRunSink, PostgRESTSink, resolve_config

URL = "https://example.test"


@pytest.fixture()
def capture():
    """(sink, requests) wired through a MockTransport; nothing leaves."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/robots"):
            return httpx.Response(200, json=[
                {"id": "AMR-01", "status": "active", "fault_msg": None},
                {"id": "AMR-02", "status": "fault", "fault_msg": "overtemp"},
            ])
        if request.method == "GET" and request.url.path.endswith("/incidents"):
            return httpx.Response(200, json=[
                {"id": "INC-4242", "sev": "crit", "src": "AMR-02",
                 "dur": 3, "created_at": "2026-09-01T13:55:00Z"}])
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = PostgRESTSink(url=URL, key="k", client=client)
    yield sink, requests
    sink.close()


OPEN = Action("open", "INC-3001", {
    "id": "INC-3001", "sev": "crit", "title": "AMR-02 fault — overtemp",
    "src": "AMR-02", "tlabel": "14:00", "state": "Open",
    "impact": "0 min so far · robot out of service", "dur": 0})
PATCH = Action("patch", "INC-3001", {
    "state": "Resolved", "dur": 7,
    "impact": "7 min robot out of service · recovered"})


def test_open_is_idempotent_upsert(capture):
    sink, requests = capture
    assert sink.apply([OPEN]) == 1
    (req,) = requests
    assert req.method == "POST"
    assert req.url.path == "/rest/v1/incidents"
    assert req.url.params["on_conflict"] == "id"
    assert "resolution=merge-duplicates" in req.headers["Prefer"]
    assert req.headers["apikey"] == "k"
    assert req.headers["Authorization"] == "Bearer k"
    assert json.loads(req.content) == [OPEN.row]


def test_patch_targets_row_by_id(capture):
    sink, requests = capture
    sink.apply([PATCH])
    (req,) = requests
    assert req.method == "PATCH"
    assert req.url.path == "/rest/v1/incidents"
    assert req.url.params["id"] == "eq.INC-3001"
    assert json.loads(req.content) == PATCH.row


def test_apply_preserves_order(capture):
    sink, requests = capture
    assert sink.apply([OPEN, PATCH]) == 2
    assert [r.method for r in requests] == ["POST", "PATCH"]


def test_apply_survives_http_errors():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    sink = PostgRESTSink(url=URL, key="k",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sink.apply([OPEN, PATCH]) == 2  # logged, never raised


def test_fetch_robots(capture):
    sink, requests = capture
    rows = sink.fetch_robots()
    assert [r["id"] for r in rows] == ["AMR-01", "AMR-02"]
    (req,) = requests
    assert req.url.params["select"] == "id,status,fault_msg,updated_at"


def test_fetch_open_incidents_filters_state(capture):
    sink, requests = capture
    rows = sink.fetch_open_incidents()
    assert rows[0]["id"] == "INC-4242"
    assert requests[0].url.params["state"] == "eq.Open"


def test_fetch_open_incidents_swallows_failure():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    sink = PostgRESTSink(url=URL, key="k",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sink.fetch_open_incidents() == []


def test_resolve_config_precedence(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://env.test/")
    monkeypatch.setenv("SUPABASE_KEY", "envkey")
    assert resolve_config() == ("https://env.test", "envkey")
    assert resolve_config("https://arg.test/", "argkey") == \
        ("https://arg.test", "argkey")


def test_dry_run_sink_prints(capsys):
    sink = DryRunSink()
    assert sink.apply([OPEN, PATCH]) == 2
    out = capsys.readouterr().out
    assert "[dry-run] OPEN " in out and "INC-3001" in out
    assert "[dry-run] PATCH" in out
    assert sink.applied == [OPEN, PATCH]
