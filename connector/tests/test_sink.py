"""Offline tests for SupabaseSink using httpx.MockTransport.

No request ever leaves the process: the mock transport records every request
so we can assert URLs, headers, query params and payloads.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from yantrabridge.sink import DEFAULT_SUPABASE_URL, SupabaseSink, _group_by_keyset


class Recorder:
    """Capture requests and answer 201 Created (PostgREST write success)."""

    def __init__(self, status_code: int = 201) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code)

    def body(self, i: int = 0) -> Any:
        return json.loads(self.requests[i].content.decode())


def make_sink(recorder: Recorder, **kwargs: Any) -> SupabaseSink:
    return SupabaseSink(
        "https://example.supabase.co", "test-key",
        transport=httpx.MockTransport(recorder.handler), **kwargs,
    )


class TestSupabaseSink:
    def test_upsert_robots_request_shape(self) -> None:
        rec = Recorder()
        rows = [{"id": "AGV-001", "vendor": "yantra", "battery": 72.5}]
        with make_sink(rec) as sink:
            assert sink.upsert_robots(rows) == 1

        req = rec.requests[0]
        assert req.method == "POST"
        assert req.url.scheme == "https"
        assert req.url.host == "example.supabase.co"
        assert req.url.path == "/rest/v1/robots"
        assert req.url.params["on_conflict"] == "id"
        assert req.headers["apikey"] == "test-key"
        assert req.headers["authorization"] == "Bearer test-key"
        assert "resolution=merge-duplicates" in req.headers["prefer"]
        assert "return=minimal" in req.headers["prefer"]
        assert rec.body() == rows

    def test_alerts_use_ignore_duplicates(self) -> None:
        rec = Recorder()
        alert = {"id": "al-x", "sev": "crit", "msg": "m", "src": "AGV-001",
                 "tlabel": "10:15:32", "ack": False,
                 "created_at": "2026-08-26T10:15:32Z"}
        with make_sink(rec) as sink:
            sink.insert_alerts([alert])
        req = rec.requests[0]
        assert req.url.path == "/rest/v1/alerts"
        assert "resolution=ignore-duplicates" in req.headers["prefer"]

    def test_insert_no_alerts_makes_no_request(self) -> None:
        rec = Recorder()
        with make_sink(rec) as sink:
            assert sink.insert_alerts([]) == 0
        assert rec.requests == []

    def test_heartbeat_row(self) -> None:
        rec = Recorder()
        with make_sink(rec, writer_id="bridge-7") as sink:
            sink.heartbeat()
        req = rec.requests[0]
        assert req.url.path == "/rest/v1/fleet_meta"
        assert req.url.params["on_conflict"] == "id"
        body = rec.body()
        assert body[0]["id"] == 1
        assert body[0]["writer_id"] == "bridge-7"
        assert "updated_at" in body[0]
        # must NOT touch sim-owned columns
        assert "sim_min" not in body[0] and "throughput" not in body[0]

    def test_push_writes_robots_alerts_heartbeat_in_order(self) -> None:
        rec = Recorder()
        with make_sink(rec) as sink:
            counts = sink.push(
                robots=[{"id": "AGV-001", "vendor": "y"}],
                alerts=[{"id": "al-1", "sev": "warn"}],
            )
        assert counts == {"robots": 1, "alerts": 1}
        paths = [r.url.path for r in rec.requests]
        assert paths == ["/rest/v1/robots", "/rest/v1/alerts", "/rest/v1/fleet_meta"]

    def test_mixed_keysets_batched_separately(self) -> None:
        # PostgREST requires identical keys per batch; vendor-extension rows
        # (motor_temp) must not be merged with plain rows.
        rec = Recorder()
        rows = [
            {"id": "AGV-001", "vendor": "y"},
            {"id": "AGV-002", "vendor": "y", "motor_temp": 60.1},
            {"id": "AGV-003", "vendor": "y"},
        ]
        with make_sink(rec) as sink:
            assert sink.upsert_robots(rows) == 3
        batches = [json.loads(r.content.decode()) for r in rec.requests]
        assert len(batches) == 2
        sizes = sorted(len(b) for b in batches)
        assert sizes == [1, 2]
        for batch in batches:
            keysets = {frozenset(r.keys()) for r in batch}
            assert len(keysets) == 1

    def test_http_error_raises(self) -> None:
        rec = Recorder(status_code=401)
        with make_sink(rec) as sink:
            with pytest.raises(httpx.HTTPStatusError):
                sink.upsert_robots([{"id": "AGV-001"}])

    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPABASE_URL", "https://other.supabase.co/")
        monkeypatch.setenv("SUPABASE_KEY", "env-key")
        sink = SupabaseSink(transport=httpx.MockTransport(Recorder().handler))
        assert sink.url == "https://other.supabase.co"  # trailing slash stripped
        assert sink.key == "env-key"
        sink.close()

    def test_embedded_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        sink = SupabaseSink(transport=httpx.MockTransport(Recorder().handler))
        assert sink.url == DEFAULT_SUPABASE_URL
        assert sink.key.startswith("sb_publishable_")
        sink.close()


def test_group_by_keyset_preserves_rows() -> None:
    rows = [{"a": 1}, {"a": 2, "b": 3}, {"a": 4}]
    groups = _group_by_keyset(rows)
    flat = [r for g in groups for r in g]
    assert sorted(flat, key=str) == sorted(rows, key=str)
