"""`up --supabase` schema preflight — MockTransport + a tiny local HTTP stub."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from yantraops.__main__ import main
from yantraops.orchestrator import preflight_supabase_schema


def _transport(status: int, body: object) -> httpx.MockTransport:
    payload = json.dumps(body)
    return httpx.MockTransport(
        lambda request: httpx.Response(status, text=payload,
                                       headers={"content-type": "application/json"}))


def test_preflight_ok():
    state, _ = preflight_supabase_schema(
        "https://x.supabase.co", "anon", transport=_transport(200, []))
    assert state == "ok"


def test_preflight_probes_robots_with_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, text="[]")

    preflight_supabase_schema("https://x.supabase.co", "anon-key",
                              transport=httpx.MockTransport(handler))
    assert "/rest/v1/robots" in seen["url"] and "limit=1" in seen["url"]
    assert seen["apikey"] == "anon-key"


@pytest.mark.parametrize("status,body", [
    (404, {"message": "Not Found"}),
    # PostgREST's usual "table missing" answers:
    (404, {"code": "PGRST205",
           "message": "Could not find the table 'public.robots' in the schema cache"}),
    (400, {"code": "42P01", "message": 'relation "public.robots" does not exist'}),
])
def test_preflight_schema_missing_variants(status, body):
    state, detail = preflight_supabase_schema(
        "https://x.supabase.co", "anon", transport=_transport(status, body))
    assert state == "schema-missing"
    assert str(status) in detail


def test_preflight_auth_rejected():
    state, _ = preflight_supabase_schema(
        "https://x.supabase.co", "bad-key",
        transport=_transport(401, {"message": "JWSError"}))
    assert state == "auth"


def test_preflight_unreachable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked")
    state, _ = preflight_supabase_schema(
        "https://x.supabase.co", "anon", transport=httpx.MockTransport(boom))
    assert state == "unreachable"


# --------------------------------------------------------------------------
# End-to-end: `up --supabase` against a local stub that has no schema
# --------------------------------------------------------------------------

class _NoSchemaStub(BaseHTTPRequestHandler):
    """Answers every request the way PostgREST does when tables are missing."""

    def do_GET(self):  # noqa: N802
        body = json.dumps({"code": "PGRST205",
                           "message": "Could not find the table"}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


def test_up_supabase_exits_2_when_schema_missing(capsys):
    server = HTTPServer(("127.0.0.1", 0), _NoSchemaStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        rc = main(["up", "--supabase", "--url", url, "--key", "anon"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "schema missing" in err
        assert "python -m yantraops migrate --db-url" in err
    finally:
        server.shutdown()
        thread.join(timeout=5)
