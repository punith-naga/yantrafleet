"""End-to-end CLI runs, fully offline via the injected client."""
from __future__ import annotations

from yantranotify.__main__ import build_parser, run

from conftest import FakeRest, URL, alert_row


def test_once_dry_run_prints_and_exits(rest: FakeRest, monkeypatch, capsys):
    for var in ("WEBHOOK_URL", "TWILIO_SID", "TWILIO_TOKEN",
                "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.delenv(var, raising=False)
    rest.alerts = [alert_row(1)]
    args = build_parser().parse_args(
        ["--once", "--dry-run", "--url", URL, "--key", "k"])
    assert run(args, client=rest.client()) == 0
    out = capsys.readouterr().out
    assert "A-001" in out          # webhook/whatsapp dry-run printed it
    assert rest.webhook_posts == []
    assert rest.twilio_posts == []


def test_multiple_polls_dedup_across_loop(rest: FakeRest, monkeypatch, capsys):
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/services/x")
    for var in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.delenv(var, raising=False)
    rest.alerts = [alert_row(1), alert_row(2)]
    args = build_parser().parse_args(
        ["--interval", "0", "--url", URL, "--key", "k"])
    assert run(args, client=rest.client(), max_polls=3) == 0
    # 2 alerts notified once each despite 3 polls
    assert len(rest.webhook_posts) == 2


def test_poll_error_does_not_crash(monkeypatch):
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    args = build_parser().parse_args(["--once", "--url", URL, "--key", "k"])
    assert run(args, client=client) == 0
