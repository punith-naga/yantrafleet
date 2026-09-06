"""v0.18: detector's five tuning values + --interval sourced live from
public.app_config when the matching CLI flag isn't explicitly passed.
Fully offline via httpx.MockTransport, same style as test_cli.py.
"""
from __future__ import annotations

import json

import yantradetect.__main__ as main_mod
from yantradetect.__main__ import build_parser, run

FAULTED = [{"id": "AMR-02", "status": "fault", "fault_msg": "overtemp"}]


def make_client(robot_polls, requests, app_config=None, open_incidents=None):
    polls = {"n": 0}

    def handler(request):
        import httpx

        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/robots"):
            snap = robot_polls[min(polls["n"], len(robot_polls) - 1)]
            polls["n"] += 1
            return httpx.Response(200, json=snap)
        if request.method == "GET" and request.url.path.endswith("/incidents"):
            return httpx.Response(200, json=open_incidents or [])
        if request.method == "GET" and request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=app_config or [])
        return httpx.Response(201)

    import httpx

    return httpx.Client(transport=httpx.MockTransport(handler))


def _cfg(**kv):
    return [{"key": k, "value": str(v)} for k, v in kv.items()]


# -- pending_polls: no flag -> app_config value wins over hardcoded default --


def test_pending_polls_from_app_config_opens_on_first_poll():
    requests = []
    client = make_client([FAULTED], requests,
                         app_config=_cfg(DETECTOR_PENDING_POLLS=1))
    run(["--url", "https://x.test"], client=client, max_polls=1)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert len(posts) == 1  # would need 2 polls at the hardcoded default


def test_explicit_pending_polls_flag_overrides_app_config():
    requests = []
    client = make_client([FAULTED], requests,
                         app_config=_cfg(DETECTOR_PENDING_POLLS=1))
    run(["--url", "https://x.test", "--pending-polls", "2"],
        client=client, max_polls=1)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert posts == []  # explicit flag (2) wins; one bad poll isn't enough


def test_no_flag_and_no_config_row_uses_hardcoded_default():
    """Hardcoded default (2 consecutive polls) when app_config has
    nothing configured — a project that hasn't run 0018 yet."""
    requests = []
    client = make_client([FAULTED], requests, app_config=[])
    run(["--url", "https://x.test"], client=client, max_polls=1)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert posts == []


# -- --interval: live from DETECTOR_INTERVAL ---------------------------------


def test_interval_from_app_config_when_flag_not_passed(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: seen.append(s))
    requests = []
    client = make_client([FAULTED, FAULTED], requests,
                         app_config=_cfg(DETECTOR_INTERVAL=1.5))
    run(["--url", "https://x.test"], client=client, max_polls=2)
    assert seen == [1.5]


def test_explicit_interval_flag_wins(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: seen.append(s))
    requests = []
    client = make_client([FAULTED, FAULTED], requests,
                         app_config=_cfg(DETECTOR_INTERVAL=1.5))
    run(["--url", "https://x.test", "--interval", "0"],
        client=client, max_polls=2)
    assert seen == [0.0]


# -- --maintenance mode: window-hours + interval -----------------------------


def test_maintenance_window_hours_from_app_config(monkeypatch):
    import httpx

    from yantradetect.maintenance import MaintenanceSink

    seen_windows = []
    real_fetch = MaintenanceSink.fetch_telemetry

    def spy(self, window_hours=6.0, now=None):
        seen_windows.append(window_hours)
        return real_fetch(self, window_hours, now)

    monkeypatch.setattr(MaintenanceSink, "fetch_telemetry", spy)

    def handler(request):
        if request.url.path.endswith("/robot_telemetry"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/maintenance_findings"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=_cfg(DETECTOR_WINDOW_HOURS=3))
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run(["--maintenance", "--once", "--url", "https://x.test"], client=client)
    assert seen_windows == [3.0]


# -- v0.18.1: YANTRA_SITE_ID live sync (yantracore.site.start_site_sync) ----


def test_site_id_from_app_config_reflected_in_incident_rows():
    """yantradetect.__main__.run() starts a live YANTRA_SITE_ID sync
    alongside its tuning poller's; engine.py reads yantracore.site_id()
    when opening an incident, so a site row in app_config lands on the
    written incident with no restart needed."""
    requests = []
    client = make_client([FAULTED, FAULTED], requests,
                         app_config=_cfg(YANTRA_SITE_ID="PNQ-WH7",
                                        DETECTOR_PENDING_POLLS=1))
    run(["--url", "https://x.test"], client=client, max_polls=1)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert len(posts) == 1
    rows = json.loads(posts[0].content)
    assert rows[0]["site_id"] == "PNQ-WH7"


def test_no_site_row_uses_env_default(monkeypatch):
    """No YANTRA_SITE_ID row in app_config -> falls back to the env/
    default behavior (BLR-DC1 here, since no env override is set)."""
    monkeypatch.delenv("YANTRA_SITE_ID", raising=False)
    requests = []
    client = make_client([FAULTED, FAULTED], requests,
                         app_config=_cfg(DETECTOR_PENDING_POLLS=1))
    run(["--url", "https://x.test"], client=client, max_polls=1)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert len(posts) == 1
    rows = json.loads(posts[0].content)
    assert rows[0]["site_id"] == "BLR-DC1"


def test_maintenance_explicit_window_hours_flag_wins(monkeypatch):
    import httpx

    from yantradetect.maintenance import MaintenanceSink

    seen_windows = []
    real_fetch = MaintenanceSink.fetch_telemetry

    def spy(self, window_hours=6.0, now=None):
        seen_windows.append(window_hours)
        return real_fetch(self, window_hours, now)

    monkeypatch.setattr(MaintenanceSink, "fetch_telemetry", spy)

    def handler(request):
        if request.url.path.endswith("/robot_telemetry"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/maintenance_findings"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=_cfg(DETECTOR_WINDOW_HOURS=3))
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run(["--maintenance", "--once", "--url", "https://x.test",
         "--window-hours", "12"], client=client)
    assert seen_windows == [12.0]
