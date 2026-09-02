"""`python -m yantranotify test` — verify a webhook in ten seconds.

The delivery test runs against a local ``http.server`` stub: the real
httpx client, the real delivery path, zero external network.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from yantranotify.__main__ import main, run_test_command


@pytest.fixture()
def hook_server():
    """Local webhook stub recording every POST body."""
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            posts.append(json.loads(self.rfile.read(length).decode()))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hook", posts
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_FORMAT", raising=False)


def test_no_url_prints_payloads_and_sends_nothing(capsys):
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "sample alert" in out and "sample incident" in out
    assert "nothing sent" in out
    # default format is the generic json contract
    assert '"text"' in out and "A-SAMPLE" in out and "INC-SAMPLE" in out


def test_no_url_slack_format_prints_block_kit(capsys):
    assert main(["test", "--format", "slack"]) == 0
    out = capsys.readouterr().out
    assert '"blocks"' in out and '"type": "header"' in out


def test_sends_two_posts_with_block_kit_shape_through_real_path(
        hook_server, capsys):
    url, posts = hook_server
    assert main(["test", "--channel", "webhook", "--url", url,
                 "--format", "slack"]) == 0
    assert len(posts) == 2  # ONE sample alert + ONE sample incident

    alert, incident = posts
    for body in (alert, incident):
        assert "blocks" in body and "text" in body
        assert body["blocks"][0]["type"] == "header"
        assert body["blocks"][0]["text"]["type"] == "plain_text"
        assert body["blocks"][-1]["type"] == "context"
        assert all(b["type"] != "actions" for b in body["blocks"])
    assert "Alert — A-SAMPLE" in alert["blocks"][0]["text"]["text"]
    assert "Incident — INC-SAMPLE" in incident["blocks"][0]["text"]["text"]
    # incident layout is distinct: it carries an Impact field
    inc_fields = next(b for b in incident["blocks"]
                      if b["type"] == "section" and "fields" in b)
    assert any(f["text"].startswith("*Impact*")
               for f in inc_fields["fields"])

    out = capsys.readouterr().out
    assert "send sample alert -> ok" in out
    assert "send sample incident -> ok" in out
    assert "webhook delivery OK" in out


def test_discord_format_posts_embeds(hook_server):
    url, posts = hook_server
    assert main(["test", "--url", url, "--format", "discord"]) == 0
    assert len(posts) == 2
    assert all("embeds" in body for body in posts)
    assert posts[0]["embeds"][0]["color"] != posts[1]["embeds"][0]["color"]


def test_url_from_env(hook_server, monkeypatch):
    url, posts = hook_server
    monkeypatch.setenv("WEBHOOK_URL", url)
    assert main(["test"]) == 0
    assert len(posts) == 2


def test_failure_exits_nonzero(capsys):
    class NoSleep:
        def monotonic(self):
            return 0.0

        def sleep(self, s):
            pass

    # unroutable per RFC 5737 test range; connection fails immediately
    rc = run_test_command(["--url", "http://127.0.0.1:1/hook"],
                          clock=NoSleep())
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out
