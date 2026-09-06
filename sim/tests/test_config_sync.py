"""v0.18: SIM_INTERVAL sourced live from public.app_config (Supabase mode
only) when --interval isn't explicitly passed. Fully offline via
httpx.MockTransport — supabase.co is never reached.
"""
from __future__ import annotations

import httpx

import yantrasim.__main__ as main_mod
from yantrasim.__main__ import main


def _client(app_config: list[dict] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=app_config or [])
        # robots/alerts/fleet_meta upserts and command polls — accept
        # everything else so the tick loop runs cleanly end to end.
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_interval_from_app_config_when_flag_not_passed(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: seen.append(s))
    client = _client([{"key": "SIM_INTERVAL", "value": "0.5"}])
    rc = main(["--supabase", "--ticks", "2", "--url", "https://x.test",
              "--key", "k"], client=client)
    assert rc == 0
    assert seen == [0.5]


def test_explicit_interval_flag_wins_over_app_config(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: seen.append(s))
    client = _client([{"key": "SIM_INTERVAL", "value": "0.5"}])
    rc = main(["--supabase", "--ticks", "2", "--interval", "0",
              "--url", "https://x.test", "--key", "k"], client=client)
    assert rc == 0
    assert seen == [0.0]


def test_no_flag_and_no_config_row_uses_hardcoded_default(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: seen.append(s))
    client = _client([])
    rc = main(["--supabase", "--ticks", "2", "--url", "https://x.test",
              "--key", "k"], client=client)
    assert rc == 0
    assert seen == [2.0]


def test_stdout_mode_never_polls_app_config():
    """mqtt/stdout modes have no Supabase project to poll for
    SIM_INTERVAL — must not attempt any network call."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rc = main(["--stdout", "--ticks", "2", "--interval", "0"], client=client)
    assert rc == 0
    assert calls == []


# -- v0.18.1: YANTRA_SITE_ID live sync (yantracore.site.start_site_sync) ----


def test_site_id_from_app_config_reflected_in_robot_rows():
    """yantrasim.__main__.main() starts a live YANTRA_SITE_ID sync
    alongside SIM_INTERVAL's; robot_row() reads yantracore.site_id() on
    every tick, so a site row in app_config lands on every upserted
    robot with no restart needed."""
    import json

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/app_config"):
            return httpx.Response(
                200, json=[{"key": "YANTRA_SITE_ID", "value": "PNQ-WH7"}])
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rc = main(["--supabase", "--ticks", "1", "--url", "https://x.test",
              "--key", "k"], client=client)
    assert rc == 0
    posts = [r for r in seen
             if r.method == "POST" and r.url.path.endswith("/robots")]
    assert posts, "expected a robots upsert"
    rows = json.loads(posts[0].content)
    assert rows and all(row["site_id"] == "PNQ-WH7" for row in rows)


def test_no_site_row_uses_env_default(monkeypatch):
    """No YANTRA_SITE_ID row in app_config -> falls back to the env/
    default behavior (BLR-DC1 here, since no env override is set)."""
    import json

    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rc = main(["--supabase", "--ticks", "1", "--url", "https://x.test",
              "--key", "k"], client=client)
    assert rc == 0
    posts = [r for r in seen
             if r.method == "POST" and r.url.path.endswith("/robots")]
    rows = json.loads(posts[0].content)
    assert rows and all(row["site_id"] == "BLR-DC1" for row in rows)
