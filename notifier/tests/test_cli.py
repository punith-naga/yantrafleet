"""End-to-end CLI runs, fully offline via the injected client."""
from __future__ import annotations

from yantranotify.__main__ import build_channels, build_parser, run
from yantranotify.reliability import ReliableChannel

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


def test_cli_site_filter_default_and_all_sites(rest: FakeRest, monkeypatch):
    """Default runs filter by this site; --all-sites drops the filter."""
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    for var in ("WEBHOOK_URL", "TWILIO_SID", "TWILIO_TOKEN",
                "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.delenv(var, raising=False)

    args = build_parser().parse_args(
        ["--once", "--dry-run", "--url", URL, "--key", "k"])
    assert args.all_sites is False
    assert run(args, client=rest.client()) == 0
    # Only alerts/incidents carry the site filter — the settings-panel poll
    # (app_settings) is a fleet-wide read with no site dimension.
    fleet_requests = [r for r in rest.requests
                      if r.url.path.endswith(("/alerts", "/incidents"))]
    assert fleet_requests
    assert all(r.url.params.get("site_id") == "eq.BLR-DC1"
               for r in fleet_requests)

    rest2 = FakeRest()
    args = build_parser().parse_args(
        ["--once", "--dry-run", "--all-sites", "--url", URL, "--key", "k"])
    assert args.all_sites is True
    assert run(args, client=rest2.client()) == 0
    fleet_requests2 = [r for r in rest2.requests
                       if r.url.path.endswith(("/alerts", "/incidents"))]
    assert fleet_requests2
    assert all("site_id" not in r.url.params for r in fleet_requests2)


def test_build_channels_wires_the_same_settings_into_both_channels():
    """The same SettingsSync object flows into both the webhook and
    whatsapp channel — one poll, both channels see rotations."""
    fake_settings = object()
    args = build_parser().parse_args(["--once", "--url", URL, "--key", "k"])
    webhook, whatsapp = build_channels(args, settings=fake_settings)[1:]
    assert isinstance(webhook, ReliableChannel) and isinstance(whatsapp, ReliableChannel)
    assert webhook.inner.settings is fake_settings
    assert whatsapp.inner.settings is fake_settings


def test_poll_error_does_not_crash(monkeypatch):
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    args = build_parser().parse_args(["--once", "--url", URL, "--key", "k"])
    assert run(args, client=client) == 0
