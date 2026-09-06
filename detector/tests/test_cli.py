"""CLI (`python -m yantradetect`) against a MockTransport — offline."""
from __future__ import annotations

import json

import httpx

from yantradetect.__main__ import build_parser, run


def make_client(robot_polls: list[list[dict]], requests: list[httpx.Request],
                open_incidents: list[dict] | None = None) -> httpx.Client:
    """Serve successive robots snapshots; record every request."""
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/robots"):
            snap = robot_polls[min(polls["n"], len(robot_polls) - 1)]
            polls["n"] += 1
            return httpx.Response(200, json=snap)
        if request.method == "GET" and request.url.path.endswith("/incidents"):
            return httpx.Response(200, json=open_incidents or [])
        if request.method == "GET" and request.url.path.endswith("/app_config"):
            return httpx.Response(200, json=[])
        return httpx.Response(201)

    return httpx.Client(transport=httpx.MockTransport(handler))


FAULTED = [{"id": "AMR-02", "status": "fault", "fault_msg": "overtemp"}]


def test_parser_defaults():
    # v0.18: these flags default to None (not explicitly passed) so run()
    # can prefer a live public.app_config value over the hardcoded
    # default — see yantradetect.__main__.DEFAULT_* and resolve_value().
    args = build_parser().parse_args([])
    assert args.interval is None
    assert not args.once and not args.dry_run
    assert args.pending_polls is None


def test_once_polls_exactly_one_snapshot():
    requests: list[httpx.Request] = []
    client = make_client([FAULTED], requests)
    assert run(["--once", "--url", "https://x.test"], client=client) == 0
    gets = [r for r in requests if r.method == "GET"]
    # v0.18: one app_config fetch (live tuning) + one seed fetch
    # (incidents) + one robots poll, no writes yet (flap guard).
    # v0.18.1: a second, independent app_config fetch for the live
    # YANTRA_SITE_ID sync (yantracore.site.start_site_sync) started
    # alongside the tuning poller.
    assert [g.url.path.rsplit("/", 1)[-1] for g in gets] == [
        "app_config", "app_config", "incidents", "robots"]
    assert all(r.method == "GET" for r in requests)


def test_two_polls_open_incident_written():
    requests: list[httpx.Request] = []
    client = make_client([FAULTED, FAULTED], requests)
    run(["--interval", "0", "--url", "https://x.test"], client=client, max_polls=2)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert len(posts) == 1
    (row,) = json.loads(posts[0].content)
    assert row["src"] == "AMR-02" and row["sev"] == "crit"
    assert row["state"] == "Open" and row["id"].startswith("INC-")


def test_recovery_patches_resolved():
    requests: list[httpx.Request] = []
    healthy = [{"id": "AMR-02", "status": "active", "fault_msg": None}]
    client = make_client([FAULTED, FAULTED, healthy], requests)
    run(["--interval", "0", "--url", "https://x.test"], client=client, max_polls=3)
    patches = [r for r in requests if r.method == "PATCH"]
    assert len(patches) == 1
    body = json.loads(patches[0].content)
    assert body["state"] == "Resolved" and "recovered" in body["impact"]


def test_dry_run_prints_and_never_writes(capsys):
    requests: list[httpx.Request] = []
    client = make_client([FAULTED, FAULTED], requests)
    run(["--interval", "0", "--dry-run", "--url", "https://x.test"],
        client=client, max_polls=2)
    assert all(r.method == "GET" for r in requests)  # reads only
    out = capsys.readouterr().out
    assert "[dry-run] OPEN " in out and "AMR-02" in out


def test_seed_prevents_duplicate_open_after_restart():
    requests: list[httpx.Request] = []
    seeded = [{"id": "INC-4242", "sev": "crit", "src": "AMR-02", "dur": 0,
               "created_at": "2026-09-01T13:59:00Z"}]
    client = make_client([FAULTED, FAULTED, FAULTED], requests,
                         open_incidents=seeded)
    run(["--interval", "0", "--url", "https://x.test"], client=client, max_polls=3)
    posts = [r for r in requests
             if r.method == "POST" and r.url.path.endswith("/incidents")]
    assert posts == []  # dedup held across the restart


def test_poll_failure_is_survived():
    requests: list[httpx.Request] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/incidents") and request.method == "GET":
            return httpx.Response(200, json=[])
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json=FAULTED)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert run(["--interval", "0", "--url", "https://x.test"],
               client=client, max_polls=3) == 0
