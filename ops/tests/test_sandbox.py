"""Tests for the zero-signup live demo sandbox (yantraops.sandbox + 0009).

Every test runs against ``e2e/fakerest.py`` started with ``rbac=True`` — the
same in-process PostgREST stand-in the offline e2e suites use — on a
127.0.0.1 ephemeral port. No cloud, no network egress, no mocking of our own
behaviour.

The first half is the part that matters: we are handing anonymous internet
strangers write access to a database, so the isolation promises of
supabase/0009_demo_sandbox.sql are asserted directly (other sites are
invisible and unwritable, privileged tables are unreachable, no role can be
granted, and an expired token is inert), not assumed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from yantraops import sandbox as sb
from yantraops import sandbox_http as sh
from yantraops.__main__ import build_parser
from yantraops.orchestrator import load_fakerest, repo_root

ANON_KEY = "anon-publishable-key"
SERVICE_KEY = "sk-test-service"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def fakerest():
    """A running FakePostgREST in RBAC mode with one REAL site's rows."""
    mod = load_fakerest(repo_root())
    fake = mod.FakePostgREST(
        tables={
            "robots": [
                {"id": "AMR-01", "vendor": "MiR", "status": "active",
                 "battery": 88.0, "pos": [3, 4], "speed": 1.1,
                 "health": 97.0, "motor_temp": 44.0, "tasks_done": 12,
                 "site_id": "BLR-DC1"},
            ],
            "alerts": [
                {"id": "al-real-1", "sev": "warn", "msg": "real site alert",
                 "src": "AMR-01", "tlabel": "10:00", "ack": False,
                 "site_id": "BLR-DC1"},
            ],
            "incidents": [
                {"id": "INC-2001", "sev": "crit", "title": "real incident",
                 "src": "AMR-01", "state": "Open", "site_id": "BLR-DC1"},
            ],
        },
        rbac=True, service_key=SERVICE_KEY, anon_key=ANON_KEY)
    base = fake.start()
    fake.base_url = base
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture()
def api(fakerest):
    """Anon-key API client — exactly what a visitor's browser holds."""
    with sb.SandboxAPI(fakerest.base_url, ANON_KEY) as client:
        yield client


@pytest.fixture()
def service_api(fakerest):
    with sb.SandboxAPI(fakerest.base_url, SERVICE_KEY) as client:
        yield client


@pytest.fixture()
def state_file(tmp_path):
    return tmp_path / "sandboxes.json"


def _get(base_url: str, table: str, token: str | None = None,
         key: str = ANON_KEY, **params):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if token:
        headers[sb.DEMO_TOKEN_HEADER] = token
    return httpx.get(f"{base_url}/rest/v1/{table}", params=params,
                     headers=headers, timeout=5.0)


def _post(base_url: str, table: str, body, token: str | None = None,
          key: str = ANON_KEY, **params):
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    if token:
        headers[sb.DEMO_TOKEN_HEADER] = token
    return httpx.post(f"{base_url}/rest/v1/{table}", json=body, params=params,
                      headers=headers, timeout=5.0)


def _expire(fakerest, token: str, minutes_ago: int = 5) -> None:
    """Push a live session's expiry into the past (no clock mocking)."""
    past = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    fakerest.demo_sessions[token]["expires_at"] = past.isoformat()


# ==========================================================================
# SECURITY — the isolation promises of 0009, asserted not assumed
# ==========================================================================

def test_demo_token_cannot_read_another_site(fakerest, api):
    session = api.mint(seed_robots=4)
    rows = _get(fakerest.base_url, "robots", token=session.token).json()
    assert rows, "the sandbox's own robots must be visible"
    assert {r["site_id"] for r in rows} == {session.site_id}
    assert all(r["id"].startswith(session.site_id) for r in rows)
    # ...and the same read with no token sees nothing at all.
    assert _get(fakerest.base_url, "robots").json() == []
    for table in ("alerts", "incidents"):
        seen = _get(fakerest.base_url, table, token=session.token).json()
        assert all(r["site_id"] == session.site_id for r in seen)


def test_demo_token_cannot_write_another_site(fakerest, api):
    session = api.mint(seed_robots=2)
    resp = _post(fakerest.base_url, "robots",
                 [{"id": "AMR-99", "vendor": "MiR", "site_id": "BLR-DC1"}],
                 token=session.token)
    assert resp.status_code == 403
    assert "row-level security" in resp.text
    # No row leaked into the real site.
    assert not [r for r in fakerest.tables["robots"] if r["id"] == "AMR-99"]


def test_demo_token_cannot_patch_a_real_site_row(fakerest, api):
    session = api.mint(seed_robots=2)
    before = dict(next(r for r in fakerest.tables["robots"]
                       if r["id"] == "AMR-01"))
    headers = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
               sb.DEMO_TOKEN_HEADER: session.token}
    resp = httpx.patch(f"{fakerest.base_url}/rest/v1/robots",
                       params={"id": "eq.AMR-01"},
                       json={"status": "fault"}, headers=headers, timeout=5.0)
    assert resp.status_code in (204, 403)
    after = next(r for r in fakerest.tables["robots"] if r["id"] == "AMR-01")
    assert after == before, "a demo token must not mutate a real fleet row"


def test_demo_token_cannot_move_a_row_between_sites(fakerest, api):
    session = api.mint(seed_robots=2)
    rid = f"{session.site_id}-R01"
    headers = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
               sb.DEMO_TOKEN_HEADER: session.token}
    resp = httpx.patch(f"{fakerest.base_url}/rest/v1/robots",
                       params={"id": f"eq.{rid}"},
                       json={"site_id": "BLR-DC1"}, headers=headers,
                       timeout=5.0)
    assert resp.status_code == 403
    row = next(r for r in fakerest.tables["robots"] if r["id"] == rid)
    assert row["site_id"] == session.site_id


def test_demo_token_cannot_reach_privileged_tables(fakerest, api):
    """app_settings / user_roles / demo_sessions are not even routes."""
    session = api.mint(seed_robots=1)
    for table in ("app_settings", "user_roles", "demo_sessions",
                  "demo_limits", "share_links"):
        resp = _get(fakerest.base_url, table, token=session.token)
        assert resp.status_code >= 400, f"{table} must not be readable"
    # ...and the admin RPC that reads them refuses too.
    with pytest.raises(sb.SandboxError):
        api.rpc("admin_list_settings", token=session.token)


def test_demo_token_cannot_grant_itself_a_role(fakerest, api):
    session = api.mint(seed_robots=1)
    for fn, args in (("admin_set_demo_limits", {"p_max_live_sessions": 9999}),
                     ("admin_list_demo_sessions", {"p_limit": 100}),
                     ("share_mint_link", {"p_kind": "fleet",
                                          "p_site": "BLR-DC1"})):
        with pytest.raises(sb.SandboxError):
            api.rpc(fn, args, token=session.token)
    # The knobs really did not move.
    assert fakerest.demo_limits["max_live_sessions"] == 25
    # A demo caller is not a role holder anywhere.
    assert not fakerest.has_role(
        fakerest.identity({"x-yf-demo-token": session.token}), "operator")


def test_demo_token_cannot_delete_anything(fakerest, api):
    session = api.mint(seed_robots=2)
    rid = f"{session.site_id}-R01"
    resp = httpx.request(
        "DELETE", f"{fakerest.base_url}/rest/v1/robots",
        params={"id": f"eq.{rid}"},
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 sb.DEMO_TOKEN_HEADER: session.token}, timeout=5.0)
    assert resp.status_code >= 400
    assert any(r["id"] == rid for r in fakerest.tables["robots"])


def test_demo_token_cannot_outlive_its_ttl(fakerest, api):
    session = api.mint(seed_robots=3)
    _expire(fakerest, session.token)

    # Reads go blind...
    assert _get(fakerest.base_url, "robots", token=session.token).json() == []
    # ...writes are refused...
    resp = _post(fakerest.base_url, "alerts",
                 [{"id": "expired-write", "sev": "warn", "msg": "m",
                   "src": "s", "site_id": session.site_id}],
                 token=session.token)
    # An expired token is simply not a demo caller any more: the request is
    # plain anon, which 0009 gives no write path at all (401/403, never 2xx).
    assert resp.status_code in (401, 403)
    assert not [r for r in fakerest.tables["alerts"]
                if r["id"] == "expired-write"]
    # ...claiming raises...
    with pytest.raises(sb.SandboxError):
        api.claim(session.token)
    # ...and the never-raising probe reports why.
    info = api.info(session.token)
    assert info["live"] is False and info["reason"] == "expired"


def test_unknown_and_malformed_tokens_are_inert(fakerest, api):
    for bogus in ("", "not-hex", "z" * 64, "0" * 64):
        assert _get(fakerest.base_url, "robots", token=bogus).json() == []
    info = api.info("0" * 64)
    assert info["live"] is False and info["reason"] == "unknown"
    assert sb.valid_token("0" * 64) and not sb.valid_token("zz")


def test_driver_refuses_a_non_demo_site(api):
    with pytest.raises(sb.SandboxError) as exc:
        sb.SandboxDriver(api, "BLR-DC1", "a" * 64)
    assert "non-demo site" in str(exc.value)
    with pytest.raises(sb.SandboxError):
        sb.SandboxDriver(api, "DEMO-ABCDEF012345", "not-a-token")
    with pytest.raises(sb.SandboxError):
        sb.HistorySeeder(api, "BLR-DC1", "a" * 64)


def test_mint_reply_with_a_real_site_is_rejected():
    """A hostile/broken backend must never aim the driver at a real fleet."""
    with pytest.raises(sb.SandboxError):
        sb.SandboxSession.from_rpc({"token": "a" * 64, "site_id": "BLR-DC1"})
    with pytest.raises(sb.SandboxError):
        sb.SandboxSession.from_rpc({"token": "nope", "site_id": "DEMO-ABCDEF"})


def test_reap_purges_the_sandbox_and_nothing_else(fakerest, api, service_api):
    session = api.mint(seed_robots=3)
    _post(fakerest.base_url, "robot_telemetry",
          [{"robot_id": f"{session.site_id}-R01", "ts": "2026-01-01T00:00:00Z",
            "battery": 80, "status": "active", "site_id": session.site_id}],
          token=session.token)
    real_before = {t: len([r for r in rows if r.get("site_id") == "BLR-DC1"])
                   for t, rows in fakerest.tables.items()}
    _expire(fakerest, session.token)

    result = service_api.reap(0)
    assert result["sessions_reaped"] == 1
    assert result["sites"] == [session.site_id]
    assert result["total"] >= 5          # 3 robots + mission + alert + sample
    for table in ("robots", "alerts", "missions", "robot_telemetry"):
        assert not [r for r in fakerest.tables[table]
                    if r.get("site_id") == session.site_id]
    real_after = {t: len([r for r in rows if r.get("site_id") == "BLR-DC1"])
                  for t, rows in fakerest.tables.items()}
    assert real_after == real_before, "the real site must be untouched"

    # Idempotent: running again changes nothing.
    again = service_api.reap(0)
    assert again["sessions_reaped"] == 0 and again["total"] == 0


def test_ended_session_token_is_dead(fakerest, api):
    session = api.mint(seed_robots=2)
    ended = api.end(session.token)
    assert ended["already_reaped"] is False and ended["total"] >= 3
    assert _get(fakerest.base_url, "robots", token=session.token).json() == []
    assert api.info(session.token)["reason"] == "reaped"
    assert api.end(session.token)["already_reaped"] is True   # idempotent


# ==========================================================================
# The driver: the fleet actually moves, inside its own site
# ==========================================================================

def test_driver_moves_the_fleet_inside_its_sandbox(fakerest, api):
    session = api.mint(seed_robots=4)
    driver = sb.SandboxDriver(api, session.site_id, session.token,
                              robots=session.seeded_robots, interval=0.0,
                              history_every=1)
    assert [r.robot_id for r in driver.sim.robots] == [
        f"{session.site_id}-R{i:02d}" for i in range(1, 5)]

    before = {r["id"]: dict(r) for r in fakerest.tables["robots"]
              if r["site_id"] == session.site_id}
    counts = driver.tick_once()
    assert counts["robots"] == 4
    for _ in range(6):
        driver.tick_once()

    after = {r["id"]: r for r in fakerest.tables["robots"]
             if r["site_id"] == session.site_id}
    assert set(after) == set(before), "the driver upserts the seeded fleet"
    assert any(after[i]["pos"] != before[i]["pos"] or
               after[i]["updated_at"] != before[i]["updated_at"]
               for i in before), "the fleet must actually move"
    telemetry = [r for r in fakerest.tables["robot_telemetry"]]
    assert telemetry and all(r["site_id"] == session.site_id for r in telemetry)
    # Missions the sim spawned are namespaced, so two sandboxes never collide.
    missions = [r for r in fakerest.tables["missions"]
                if r["site_id"] == session.site_id]
    assert missions and all(m["id"].startswith(session.site_id)
                            for m in missions)


def test_driver_never_writes_outside_its_site(fakerest, api):
    session = api.mint(seed_robots=3)
    snapshot = {t: [dict(r) for r in rows if r.get("site_id") != session.site_id]
                for t, rows in fakerest.tables.items()}
    driver = sb.SandboxDriver(api, session.site_id, session.token,
                              robots=3, interval=0.0, history_every=1)
    for _ in range(8):
        driver.tick_once()
    for table, rows in fakerest.tables.items():
        others = [dict(r) for r in rows if r.get("site_id") != session.site_id]
        assert others == snapshot[table], f"{table} outside the sandbox changed"
    # fleet_meta is off-limits to anon and must never be attempted.
    assert fakerest.tables["fleet_meta"] == []


def test_driver_stops_when_the_session_ends(fakerest, api):
    session = api.mint(seed_robots=2)
    driver = sb.SandboxDriver(api, session.site_id, session.token, robots=2,
                              interval=0.0, check_every=1)
    api.end(session.token)
    errors: list[Exception] = []
    ticks = driver.run(ticks=10, on_error=errors.append)
    assert ticks < 10, "the driver must give up on a dead sandbox"
    assert all(isinstance(e, sb.SandboxError) for e in errors)


def test_driver_honours_its_deadline(api, fakerest):
    session = api.mint(seed_robots=2)
    driver = sb.SandboxDriver(
        api, session.site_id, session.token, robots=2, interval=0.0,
        deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert driver.run(ticks=50) == 0


def test_history_seeder_backfills_and_survives_a_missing_column(fakerest, api):
    session = api.mint(seed_robots=3)
    seeder = sb.HistorySeeder(api, session.site_id, session.token)
    counts = seeder.seed(minutes=60, every_minutes=10)
    assert counts["robot_telemetry"] == 18       # 6 steps x 3 robots
    assert counts["incidents"] == 1 and counts["missions"] == 2
    samples = [r for r in fakerest.tables["robot_telemetry"]
               if r["site_id"] == session.site_id]
    assert len(samples) == 18
    assert all(r["robot_id"].startswith(session.site_id) for r in samples)
    resolved = [r for r in fakerest.tables["incidents"]
                if r["site_id"] == session.site_id]
    assert resolved and resolved[0]["state"] == "Resolved"

    # 0012 not applied: PostgREST answers PGRST204 for tasks_done. The
    # seeder must retry without the column instead of failing the mint.
    class _Picky:
        def __init__(self, inner):
            self.inner = inner
            self.rejected = 0

        def write(self, table, rows, **kw):
            if table == "robot_telemetry" and any("tasks_done" in r
                                                  for r in rows):
                self.rejected += 1
                raise sb.SandboxError(
                    "Could not find the 'tasks_done' column of "
                    "'robot_telemetry' in the schema cache", code="PGRST204")
            return self.inner.write(table, rows, **kw)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    picky = _Picky(api)
    seeder2 = sb.HistorySeeder(picky, session.site_id, session.token)
    counts2 = seeder2.seed(minutes=20, every_minutes=10)
    assert picky.rejected == 1
    assert counts2["robot_telemetry"] == 6
    assert seeder2.telemetry_has_tasks_done is False


# ==========================================================================
# mint / list / reap at the command level
# ==========================================================================

def test_mint_writes_a_token_free_registry_and_console_url(
        fakerest, api, state_file, capsys):
    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=3,
                             origin="marketing", console="https://x.test/c/",
                             sim=False, state_file=state_file,
                             json_output=True, api=api)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "token" not in payload
    site = payload["site_id"]
    url = payload["console_url"]
    assert url.startswith("https://x.test/c/?supa=")
    assert f"site={site}" in url and "&demo=" in url and "key=" in url
    demo_token = url.split("demo=")[1]
    assert sb.valid_token(demo_token)

    raw = state_file.read_text()
    assert site in raw
    assert demo_token not in raw, "the registry must never store a token"
    assert oct(state_file.stat().st_mode)[-3:] == "600"

    records = sb.SandboxRegistry(state_file).load()
    assert len(records) == 1 and records[0].site_id == site
    assert records[0].pid is None and records[0].origin == "marketing"
    # The stored link is the reference form: identifies, cannot open.
    assert "demo=" not in (records[0].console_url or "")
    assert f"site={site}" in (records[0].console_url or "")
    # The session was claimed on the visitor's behalf, and seeded.
    assert fakerest.demo_sessions[demo_token]["claimed"] is True
    assert payload["seeded"]["robot_telemetry"] > 0


def test_mint_refuses_past_the_local_ceiling(fakerest, api, state_file, capsys):
    for _ in range(2):
        assert sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=1,
                                   sim=False, history=False, max_live=2,
                                   state_file=state_file, json_output=True,
                                   api=api) == sb.EXIT_OK
    capsys.readouterr()
    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=1, sim=False,
                             history=False, max_live=2, state_file=state_file,
                             json_output=True, api=api)
    assert rc == sb.EXIT_CEILING
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "ceiling"
    assert "already running 2 of 2" in payload["error"]
    # Refused before the RPC: no third session was minted server-side.
    assert len(fakerest.demo_sessions) == 2


def test_ceiling_is_configurable_by_env(monkeypatch):
    monkeypatch.delenv("YANTRAOPS_SANDBOX_MAX", raising=False)
    assert sb.resolve_max_live() == sb.DEFAULT_MAX_LIVE
    monkeypatch.setenv("YANTRAOPS_SANDBOX_MAX", "3")
    assert sb.resolve_max_live() == 3
    assert sb.resolve_max_live(11) == 11          # flag wins
    monkeypatch.setenv("YANTRAOPS_SANDBOX_MAX", "garbage")
    assert sb.resolve_max_live() == sb.DEFAULT_MAX_LIVE


def test_mint_surfaces_the_database_ceiling(fakerest, api, state_file, capsys):
    fakerest.demo_limits["max_live_sessions"] = 1
    assert sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=1,
                               sim=False, history=False, max_live=50,
                               state_file=state_file, api=api) == sb.EXIT_OK
    capsys.readouterr()
    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=1, sim=False,
                             history=False, max_live=50,
                             state_file=state_file, json_output=True, api=api)
    assert rc == sb.EXIT_CEILING
    assert "too many live demo sandboxes" in json.loads(
        capsys.readouterr().out)["error"]


def test_mint_reports_a_disabled_sandbox(fakerest, api, state_file, capsys):
    fakerest.demo_limits["enabled"] = False
    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, sim=False,
                             history=False, state_file=state_file,
                             json_output=True, api=api)
    assert rc == sb.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "disabled"
    assert sb.SandboxRegistry(state_file).load() == []


def test_mint_ends_the_session_when_the_simulator_will_not_start(
        fakerest, api, state_file, capsys):
    def boom(**kwargs):
        raise OSError("no fork for you")

    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=2, sim=True,
                             history=False, state_file=state_file,
                             json_output=True, api=api, spawn=boom)
    assert rc == sb.EXIT_ERROR
    assert sb.SandboxRegistry(state_file).load() == []
    # The slot was handed back: the session is reaped, its rows purged.
    assert all(s["reaped_at"] for s in fakerest.demo_sessions.values())
    assert not [r for r in fakerest.tables["robots"]
                if r["site_id"].startswith(sb.DEMO_SITE_PREFIX)]


def test_reap_command_purges_stops_and_is_idempotent(
        fakerest, api, service_api, state_file, capsys):
    sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=2, sim=False,
                        history=False, state_file=state_file, api=api)
    capsys.readouterr()
    token = next(iter(fakerest.demo_sessions))
    site = fakerest.demo_sessions[token]["site_id"]

    # A stand-in simulator process for that sandbox.
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    registry = sb.SandboxRegistry(state_file)
    records = registry.load()
    records[0].pid = proc.pid
    registry.save(records)
    _expire(fakerest, token)

    rc = sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY, state_file=state_file,
                             json_output=True, api=service_api)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions_reaped"] == 1 and payload["sites"] == [site]
    assert site in payload["simulators_stopped"]
    assert payload["still_live"] == []
    assert sb.SandboxRegistry(state_file).load() == []
    assert proc.wait(timeout=10) is not None
    assert not [r for r in fakerest.tables["robots"]
                if r["site_id"] == site]

    rc2 = sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY,
                              state_file=state_file, json_output=True,
                              api=service_api)
    assert rc2 == sb.EXIT_OK
    again = json.loads(capsys.readouterr().out)
    assert again["sessions_reaped"] == 0 and again["simulators_stopped"] == []


def test_reap_on_an_empty_box_is_a_no_op(fakerest, service_api, state_file,
                                         capsys):
    assert sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY,
                               state_file=state_file, json_output=True,
                               api=service_api) == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"sessions_reaped": 0, "sites": [], "deleted": {},
                       "total": 0, "simulators_stopped": [], "still_live": []}
    assert not state_file.exists()


def test_reap_needs_a_privileged_key(fakerest, api, state_file, capsys):
    """demo_reap_expired is granted to authenticated/service_role only."""
    rc = sb.run_sandbox_reap(fakerest.base_url, ANON_KEY, state_file=state_file,
                             json_output=True, api=api)
    assert rc == sb.EXIT_ERROR
    assert "demo_reap_expired" in json.loads(capsys.readouterr().out)["error"]


def test_list_shows_local_state_without_tokens(fakerest, api, service_api,
                                               state_file, capsys):
    sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=2, sim=False,
                        history=False, console="https://x.test/c/",
                        state_file=state_file, api=api)
    capsys.readouterr()
    rc = sb.run_sandbox_list(fakerest.base_url, ANON_KEY,
                             state_file=state_file, json_output=True)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["live_on_this_box"] == 1
    row = payload["local"][0]
    assert row["site_id"].startswith(sb.DEMO_SITE_PREFIX)
    assert row["expired"] is False and row["sim_running"] is False
    token = next(iter(fakerest.demo_sessions))
    assert token not in json.dumps(payload), "a listing must never leak tokens"

    # --remote asks the backend too; an admin/service key sees the sessions.
    rc = sb.run_sandbox_list(fakerest.base_url, SERVICE_KEY,
                             state_file=state_file, json_output=True,
                             remote=True, api=service_api)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["remote"]) == 1
    assert "token" not in payload["remote"][0]


def test_list_reports_a_refused_remote_listing(fakerest, api, state_file,
                                               capsys):
    rc = sb.run_sandbox_list(fakerest.base_url, ANON_KEY,
                             state_file=state_file, json_output=True,
                             remote=True, api=api)
    assert rc == sb.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert "admin" in payload["remote_error"]
    assert payload["local"] == []


def test_list_on_a_fresh_box_is_empty(fakerest, state_file, capsys):
    assert sb.run_sandbox_list(fakerest.base_url, ANON_KEY,
                               state_file=state_file,
                               json_output=True) == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["local"] == [] and payload["live_on_this_box"] == 0


# ==========================================================================
# Registry, URL shape, CLI wiring
# ==========================================================================

def test_registry_sweep_stops_expired_simulators(state_file):
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    registry = sb.SandboxRegistry(state_file)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    registry.save([
        sb.SandboxRecord(site_id="DEMO-000000000001", expires_at=past,
                         pid=proc.pid),
        sb.SandboxRecord(site_id="DEMO-000000000002", expires_at=future),
    ])
    out = registry.sweep()
    assert out["stopped"] == ["DEMO-000000000001"]
    assert out["live"] == ["DEMO-000000000002"]
    assert proc.wait(timeout=10) is not None
    assert [r.site_id for r in registry.load()] == ["DEMO-000000000002"]
    assert registry.sweep()["stopped"] == []          # idempotent


def test_registry_tolerates_a_corrupt_file(state_file):
    state_file.write_text("{not json at all")
    assert sb.SandboxRegistry(state_file).load() == []
    state_file.write_text(json.dumps({"sandboxes": [{"nope": 1}]}))
    assert sb.SandboxRegistry(state_file).load() == []


def test_record_with_unparsable_expiry_counts_as_expired():
    rec = sb.SandboxRecord(site_id="DEMO-0001", expires_at="whenever")
    assert rec.expired() and rec.seconds_remaining() == 0


def test_console_url_shape():
    url = sb.sandbox_console_url("https://yantrika.ai/console/index.html",
                                 "https://db.example.com", "anon-key",
                                 "DEMO-0A1B2C3D4E5F", "a" * 64)
    assert url == ("https://yantrika.ai/console/index.html"
                   "?supa=https%3A%2F%2Fdb.example.com&key=anon-key"
                   "&site=DEMO-0A1B2C3D4E5F&demo=" + "a" * 64)
    # A base that already carries a query string keeps it.
    assert "?x=1&supa=" in sb.sandbox_console_url(
        "/c/index.html?x=1", "http://b", "k", "DEMO-01", "b" * 64)
    # Token-free reference form (what the registry and listings hold).
    ref = sb.sandbox_console_url("/c/index.html", "http://b", "k", "DEMO-01")
    assert ref.endswith("&site=DEMO-01") and "demo=" not in ref


def test_console_base_resolution(monkeypatch):
    monkeypatch.delenv(sb.DEFAULT_CONSOLE_ENV, raising=False)
    assert sb.resolve_console_base() == sb.DEFAULT_CONSOLE_URL
    monkeypatch.setenv(sb.DEFAULT_CONSOLE_ENV, "https://yantrika.ai/console/")
    assert sb.resolve_console_base() == "https://yantrika.ai/console/"
    assert sb.resolve_console_base("https://other/") == "https://other/"


def test_cli_parses_every_sandbox_subcommand():
    parser = build_parser()
    args = parser.parse_args(["sandbox-mint", "--ttl", "20", "--robots", "4",
                              "--origin", "marketing", "--max-live", "3",
                              "--no-sim", "--json"])
    assert args.command == "sandbox-mint" and args.ttl == 20
    assert args.robots == 4 and args.max_live == 3
    assert args.no_sim and args.json_output

    args = parser.parse_args(["sandbox-reap", "--grace", "5"])
    assert args.command == "sandbox-reap" and args.grace == 5

    args = parser.parse_args(["sandbox-list", "--remote"])
    assert args.command == "sandbox-list" and args.remote

    args = parser.parse_args(["sandbox-drive", "--site", "DEMO-01",
                              "--ticks", "2"])
    assert args.command == "sandbox-drive" and args.site == "DEMO-01"


def test_cli_dispatches_sandbox_list(fakerest, state_file, capsys):
    from yantraops.__main__ import main
    rc = main(["sandbox-list", "--url", fakerest.base_url, "--key", ANON_KEY,
               "--state-file", str(state_file), "--json"])
    assert rc == sb.EXIT_OK
    assert json.loads(capsys.readouterr().out)["local"] == []


def test_drive_command_reads_the_token_from_the_environment(
        fakerest, api, monkeypatch):
    session = api.mint(seed_robots=2)
    monkeypatch.setenv(sb.DEMO_TOKEN_ENV, session.token)
    rc = sb.run_sandbox_drive(fakerest.base_url, ANON_KEY, session.site_id,
                              robots=2, interval=0.0, ticks=3, api=api)
    assert rc == sb.EXIT_OK
    moved = [r for r in fakerest.tables["robots"]
             if r["site_id"] == session.site_id]
    assert len(moved) == 2 and all(r["speed"] is not None for r in moved)


def test_drive_command_refuses_without_a_token(fakerest, api, monkeypatch,
                                               capsys):
    monkeypatch.delenv(sb.DEMO_TOKEN_ENV, raising=False)
    rc = sb.run_sandbox_drive(fakerest.base_url, ANON_KEY, "DEMO-0A1B2C3D4E5F",
                              robots=2, interval=0.0, ticks=1, api=api)
    assert rc == sb.EXIT_ERROR
    assert "demo token" in capsys.readouterr().err


def test_spawn_driver_keeps_the_token_out_of_argv(fakerest, api, monkeypatch):
    """The token is a bearer credential; /proc/<pid>/cmdline is public."""
    seen: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = env
            seen["kwargs"] = kwargs
            self.pid = 4242

    monkeypatch.setattr(sb.subprocess, "Popen", _FakePopen)
    session = api.mint(seed_robots=2)
    pid = sb._spawn_driver(base_url=fakerest.base_url, key=ANON_KEY,
                           session=session, robots=2, interval=1.5)
    assert pid == 4242
    cmd = seen["cmd"]
    assert "sandbox-drive" in cmd and session.site_id in cmd
    assert session.token not in cmd
    assert seen["env"][sb.DEMO_TOKEN_ENV] == session.token
    assert seen["kwargs"]["start_new_session"] is True


def test_mint_end_to_end_spawns_a_real_simulator(fakerest, api, state_file,
                                                 capsys):
    """The whole path: mint -> seed -> spawn -> the fleet moves -> reap."""
    rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=4,
                             origin="marketing", sim=True, interval=0.2,
                             state_file=state_file, json_output=True, api=api)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    site, pid = payload["site_id"], payload["pid"]
    assert pid and sb._pid_alive(pid)
    try:
        deadline = time.time() + 30
        moved = False
        seen: set[str] = set()
        while time.time() < deadline and not moved:
            rows = [r for r in fakerest.tables["robots"]
                    if r["site_id"] == site]
            stamps = {f"{r['id']}@{r.get('updated_at')}@{r.get('pos')}"
                      for r in rows}
            if seen and stamps - seen:
                moved = True
            seen |= stamps
            time.sleep(0.5)
        assert moved, "the spawned simulator never moved the sandbox fleet"
        assert all(r["site_id"] == site for r in fakerest.tables["robots"]
                   if r["id"].startswith(sb.DEMO_SITE_PREFIX))
    finally:
        sb._stop_pid(pid)
    assert not sb._pid_alive(pid)


# ==========================================================================
# SECURITY, part 2 — the holes a single-sandbox test cannot see
#
# Everything above proves one sandbox is isolated from the REAL site. These
# prove the harder cases: two sandboxes against each other (both are DEMO-*,
# so only the site_id *equality* separates them), the column-level grants
# inside a sandbox, the shared yf_can_read_site() gate that 0011-0016 all
# hang off, and the clamps that stop a visitor asking for more than the
# deployment offers.
# ==========================================================================

def test_two_sandboxes_cannot_see_each_other(fakerest, api):
    """The DEMO- prefix guard does not separate sandboxes — equality does."""
    a = api.mint(seed_robots=3)
    b = api.mint(seed_robots=3)
    assert a.site_id != b.site_id and a.token != b.token

    a_rows = _get(fakerest.base_url, "robots", token=a.token).json()
    b_rows = _get(fakerest.base_url, "robots", token=b.token).json()
    assert {r["site_id"] for r in a_rows} == {a.site_id}
    assert {r["site_id"] for r in b_rows} == {b.site_id}
    assert not ({r["id"] for r in a_rows} & {r["id"] for r in b_rows})

    # A's token cannot write into B's sandbox...
    resp = _post(fakerest.base_url, "alerts",
                 [{"id": f"{b.site_id}-XA", "sev": "warn", "msg": "cross",
                   "src": "fleet", "site_id": b.site_id}], token=a.token)
    assert resp.status_code == 403
    assert not [r for r in fakerest.tables["alerts"] if r["id"].endswith("-XA")]

    # ...nor PATCH one of B's robots.
    victim = f"{b.site_id}-R01"
    before = dict(next(r for r in fakerest.tables["robots"]
                       if r["id"] == victim))
    resp = httpx.patch(
        f"{fakerest.base_url}/rest/v1/robots", params={"id": f"eq.{victim}"},
        json={"status": "fault"},
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 sb.DEMO_TOKEN_HEADER: a.token}, timeout=5.0)
    assert resp.status_code in (204, 403)
    assert next(r for r in fakerest.tables["robots"]
                if r["id"] == victim) == before

    # ...and ending A leaves B entirely alone.
    api.end(a.token)
    assert api.info(b.token)["live"] is True
    assert len([r for r in fakerest.tables["robots"]
                if r["site_id"] == b.site_id]) == 3


def test_demo_token_may_only_ack_alerts_and_decide_commands(fakerest, api):
    """0009's column-level UPDATE grants, inside the visitor's own sandbox."""
    session = api.mint(seed_robots=2)
    headers = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
               sb.DEMO_TOKEN_HEADER: session.token}
    alert_id = f"{session.site_id}-A01"

    def patch(table, row_id, body):
        return httpx.patch(f"{fakerest.base_url}/rest/v1/{table}",
                           params={"id": f"eq.{row_id}"}, json=body,
                           headers=headers, timeout=5.0)

    # Acking your own sandbox's alert is the whole point — that must work.
    assert patch("alerts", alert_id, {"ack": True}).status_code == 204
    assert next(r for r in fakerest.tables["alerts"]
                if r["id"] == alert_id)["ack"] is True

    # Rewriting its text or severity is not granted.
    for body in ({"msg": "pwned"}, {"sev": "crit"},
                 {"ack": True, "msg": "pwned"}):
        assert patch("alerts", alert_id, body).status_code == 403
    assert "pwned" not in json.dumps(fakerest.tables["alerts"])

    # commands: the five decision columns yes, the request itself no.
    cmd_id = "11111111-2222-3333-4444-555555555555"
    assert _post(fakerest.base_url, "commands",
                 [{"id": cmd_id, "robot_id": f"{session.site_id}-R01",
                   "cmd": "pause", "status": "pending",
                   "requested_by": "demo-visitor",
                   "site_id": session.site_id}],
                 token=session.token).status_code < 400
    assert patch("commands", cmd_id,
                 {"status": "approved", "decided_by": "demo-visitor"}
                 ).status_code == 204
    for body in ({"cmd": "estop"}, {"robot_id": "AMR-01"},
                 {"requested_by": "admin@yantrika.ai"}):
        assert patch("commands", cmd_id, body).status_code == 403
    row = next(r for r in fakerest.tables["commands"] if r["id"] == cmd_id)
    assert row["cmd"] == "pause" and row["robot_id"].startswith(
        session.site_id)


def test_demo_token_gets_exactly_its_own_site_through_the_shared_gate(
        fakerest, api):
    """`yf_can_read_site` is what 0011-0016 hang off — it must not over-grant.

    0011/0012's read RPCs are granted to `anon` on purpose; the grant itself
    authorises nothing, and the gate is the only thing standing between an
    anonymous caller and a real fleet's telemetry.
    """
    session = api.mint(seed_robots=3)
    sb.HistorySeeder(api, session.site_id, session.token).seed(
        minutes=60, every_minutes=10)

    for fn, args in (("replay_state_at",
                      {"p_site": session.site_id, "p_at": None}),
                     ("utilization_rollup", {"p_site": session.site_id})):
        out = api.rpc(fn, args, token=session.token)
        assert out["site_id"] == session.site_id
    # The sandbox really does have history to show on arrival.
    assert api.rpc("replay_state_at",
                   {"p_site": session.site_id, "p_at": None},
                   token=session.token)["counts"]["robots"] == 3
    assert api.rpc("utilization_rollup", {"p_site": session.site_id},
                   token=session.token)["fleet"]["robot_count"] == 3

    # The same calls aimed at the real fleet are refused, token or not.
    for token in (session.token, None):
        for fn in ("replay_state_at", "utilization_rollup"):
            with pytest.raises(sb.SandboxError) as exc:
                api.rpc(fn, {"p_site": "BLR-DC1", "p_at": None}, token=token)
            assert "BLR-DC1" in str(exc.value)

    # And an expired token loses the gate along with everything else.
    _expire(fakerest, session.token)
    with pytest.raises(sb.SandboxError):
        api.rpc("replay_state_at", {"p_site": session.site_id, "p_at": None},
                token=session.token)


def test_demo_token_reaches_no_table_outside_the_seven(fakerest, api):
    """Reads return nothing and every write is refused, on every other table.

    The real database answers `42501 permission denied for table` (anon holds
    no grant); the fake answers an empty result set. Both are "no rows, no
    writes", which is the invariant worth asserting — see the `needs` note
    about the divergence.
    """
    session = api.mint(seed_robots=2)
    outside = ("app_settings", "user_roles", "fleet_meta", "share_links",
               "site_cost_settings", "maintenance_feedback",
               "channel_identities", "inbound_actions", "certificates",
               "academy_progress", "site_status_pages", "demo_sessions",
               "demo_limits")
    for table in outside:
        got = _get(fakerest.base_url, table, token=session.token)
        assert got.status_code >= 400 or got.json() == [], \
            f"{table} leaked rows to a demo token"
        wrote = _post(fakerest.base_url, table,
                      [{"site_id": session.site_id, "id": "x", "key": "x",
                        "value": "x"}], token=session.token)
        assert wrote.status_code >= 400, f"{table} accepted a demo write"


def test_a_signed_in_user_is_never_a_demo_caller(fakerest, api):
    """0009's policies are `to anon`: a JWT wins, the demo header is inert."""
    session = api.mint(seed_robots=3)
    mod = load_fakerest(repo_root())
    jwt = mod.make_test_jwt("op@yantrika.ai", "operator", "BLR-DC1")
    headers = {"apikey": ANON_KEY, "Authorization": f"Bearer {jwt}",
               sb.DEMO_TOKEN_HEADER: session.token}
    rows = httpx.get(f"{fakerest.base_url}/rest/v1/robots", headers=headers,
                     timeout=5.0).json()
    # They see their OWN site, and gain nothing from carrying the header.
    assert {r["site_id"] for r in rows} == {"BLR-DC1"}
    assert not [r for r in rows if r["site_id"] == session.site_id]


def test_ttl_and_fleet_size_are_clamped_by_demo_limits(fakerest, api):
    """A visitor cannot ask for a sandbox bigger or longer-lived than allowed."""
    fakerest.demo_limits.update(ttl_minutes=30, max_seed_robots=4)

    greedy = api.mint(ttl_minutes=100_000, seed_robots=500)
    assert greedy.ttl_minutes == 30 and greedy.seeded_robots == 4
    assert len([r for r in fakerest.tables["robots"]
                if r["site_id"] == greedy.site_id]) == 4
    lifetime = (sb._parse_ts(greedy.expires_at)
                - sb._parse_ts(greedy.created_at)).total_seconds()
    assert 29 * 60 <= lifetime <= 31 * 60

    # The floor is a floor too: 5 minutes, not zero and not negative.
    tiny = api.mint(ttl_minutes=0, seed_robots=-3)
    assert tiny.ttl_minutes == 5 and tiny.seeded_robots == 0
    assert not [r for r in fakerest.tables["robots"]
                if r["site_id"] == tiny.site_id]
    # A robot-less sandbox gets no mission/alert either — nothing to point at.
    assert not [r for r in fakerest.tables["missions"]
                if r["site_id"] == tiny.site_id]

    # demo_limits_public tells the marketing page what it may promise.
    assert api.limits() == {"enabled": True, "ttl_minutes": 30}


def test_reap_honours_the_grace_window(fakerest, api, service_api, state_file,
                                       capsys):
    """`--grace N` keeps a just-expired sandbox alive for N more minutes."""
    session = api.mint(seed_robots=2)
    _expire(fakerest, session.token, minutes_ago=5)

    rc = sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY, grace=30,
                             state_file=state_file, json_output=True,
                             api=service_api)
    assert rc == sb.EXIT_OK
    assert json.loads(capsys.readouterr().out)["sessions_reaped"] == 0
    assert [r for r in fakerest.tables["robots"]
            if r["site_id"] == session.site_id]

    rc = sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY, grace=1,
                             state_file=state_file, json_output=True,
                             api=service_api)
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions_reaped"] == 1
    assert payload["sites"] == [session.site_id]
    assert not [r for r in fakerest.tables["robots"]
                if r["site_id"] == session.site_id]


# ==========================================================================
# The floor plan: the fleet stands somewhere the console can actually draw
# ==========================================================================

#: console/index.html's posToSvg(): metric pos -> the 1000x520 map viewBox.
def _to_svg(pos):
    return [80 + float(pos[0]) * 28, 50 + float(pos[1]) * 20]


def test_floor_plan_matches_the_simulator_world():
    """Drift guard: yantraops copies world.py's grid rather than importing it."""
    world = pytest.importorskip("yantrasim.world")
    plan = sb.FloorPlan()
    assert (sb.FLOOR_GRID_ROWS, sb.FLOOR_GRID_COLS, sb.FLOOR_SPACING_M) == (
        world.GRID_ROWS, world.GRID_COLS, world.SPACING_M)
    assert sb.FLOOR_CHARGER_NODES == world.CHARGER_NODES
    assert sb.FLOOR_DOCK_NODES == world.DOCK_NODES
    assert set(plan.nodes) == set(world.WAYPOINTS)
    for nid, wp in world.WAYPOINTS.items():
        assert plan.pos(nid) == [round(wp.x, 2), round(wp.y, 2)]
        assert plan.nodes[nid][2] == wp.kind


def test_floor_plan_gives_every_robot_its_own_node():
    plan = sb.FloorPlan()
    statuses = ["active", "active", "idle", "charging", "active", "idle",
                "active", "paused", "charging", "active", "idle", "active"]
    nodes = plan.assign(statuses)
    assert len(nodes) == len(statuses) == len(set(nodes))
    kinds = {n: plan.nodes[n][2] for n in nodes}
    # The two charge bays go to the two charging robots.
    charging = [n for n, s in zip(nodes, statuses) if s == "charging"]
    assert sorted(charging) == sorted(sb.FLOOR_CHARGER_NODES)
    # Nobody working is parked in a charge bay.
    assert all(kinds[n] != "charger"
               for n, s in zip(nodes, statuses) if s == "active")
    # Every node is inside the console's drawable map.
    for node in nodes:
        x, y = _to_svg(plan.pos(node))
        assert 0 <= x <= 1000 and 0 <= y <= 520
    # Deterministic.
    assert plan.assign(statuses) == nodes


def test_floor_plan_trail_walks_an_aisle_and_ends_where_the_robot_stands():
    plan = sb.FloorPlan()
    node = "n2_7"
    trail = plan.trail(node, 0, "active", 8)
    assert len(trail) == 8
    assert trail[-1] == plan.pos(node)
    xs = [p[0] for p in trail]
    assert xs == sorted(xs), "an aisle walk must not double back"
    assert len({p[1] for p in trail}) == 1, "and must stay in its own row"
    assert trail[0] != trail[-1], "an active robot moved"
    # A parked robot did not.
    for status in ("charging", "idle", "paused"):
        still = plan.trail("n0_0", 3, status, 6)
        assert still == [plan.pos("n0_0")] * 6
    assert plan.trail(node, 0, "active", 1) == [plan.pos(node)]


def test_seeded_fleet_is_parked_on_the_warehouse_floor(fakerest, api):
    """The arrival snapshot must read as a warehouse, not as scatter.

    demo_mint_session seeds `random()*40` metres, which the console's
    posToSvg maps past the right-hand edge of its 1000x520 map. The mint
    path parks the fleet before the visitor ever sees it.
    """
    fakerest.demo_limits["max_seed_robots"] = 12
    session = api.mint(seed_robots=10)
    seeder = sb.HistorySeeder(api, session.site_id, session.token)
    counts = seeder.seed(minutes=60, every_minutes=10)
    assert counts["robots_parked"] == 10

    rows = sorted((r for r in fakerest.tables["robots"]
                   if r["site_id"] == session.site_id),
                  key=lambda r: r["id"])
    assert len(rows) == 10
    seen = set()
    for row in rows:
        x, y = _to_svg(row["pos"])
        assert 0 <= x <= 1000 and 0 <= y <= 520, f"{row['id']} is off the map"
        assert tuple(row["pos"]) not in seen, "two robots on one pixel"
        seen.add(tuple(row["pos"]))
    plan = sb.FloorPlan()
    for row in rows:
        node = next(n for n in plan.nodes if plan.pos(n) == list(row["pos"]))
        if row["status"] == "charging":
            assert plan.nodes[node][2] == "charger"
        if row["status"] != "active":
            assert row["speed"] == 0


def test_seeded_history_traces_the_parked_fleet(fakerest, api):
    """Replay must agree with the live map, not contradict it."""
    session = api.mint(seed_robots=4)
    seeder = sb.HistorySeeder(api, session.site_id, session.token)
    seeder.seed(minutes=60, every_minutes=10)

    live = {r["id"]: r for r in fakerest.tables["robots"]
            if r["site_id"] == session.site_id}
    for robot_id, robot in live.items():
        track = sorted((t for t in fakerest.tables["robot_telemetry"]
                        if t["robot_id"] == robot_id),
                       key=lambda t: t["ts"])
        assert track, f"{robot_id} has no history"
        assert track[-1]["pos"] == list(robot["pos"]), \
            "the newest sample must land where the robot now stands"
        assert all(t["site_id"] == session.site_id for t in track)
        for point in track:
            x, y = _to_svg(point["pos"])
            assert 0 <= x <= 1000 and 0 <= y <= 520
        if robot["status"] == "charging":
            # Charging fills up over time; a demo that drains while charging
            # is the kind of detail an operator spots immediately.
            assert track[0]["battery"] <= track[-1]["battery"]


def test_seed_fills_the_approvals_and_maintenance_views(fakerest, api):
    session = api.mint(seed_robots=4)
    counts = sb.HistorySeeder(api, session.site_id,
                              session.token).seed(minutes=30, every_minutes=10)
    assert counts["commands"] == 1 and counts["maintenance_findings"] == 1

    cmd = next(c for c in fakerest.tables["commands"]
               if c["site_id"] == session.site_id)
    assert cmd["status"] == "pending"
    assert cmd["robot_id"].startswith(session.site_id)
    finding = next(f for f in fakerest.tables["maintenance_findings"]
                   if f["site_id"] == session.site_id)
    assert finding["state"] == "Open"
    assert finding["id"].startswith(session.site_id)
    assert finding["robot_id"].startswith(session.site_id)
    # Nothing landed on the real fleet.
    assert not [c for c in fakerest.tables["commands"]
                if c["site_id"] == "BLR-DC1"]


def test_seed_survives_a_deployment_without_the_optional_tables(fakerest, api):
    """A missing commands/maintenance surface must not fail the whole mint."""
    session = api.mint(seed_robots=3)

    class _Refuses:
        def __init__(self, inner):
            self.inner = inner

        def write(self, table, rows, **kw):
            if table in ("commands", "maintenance_findings"):
                raise sb.SandboxError(
                    f"Could not find the table 'public.{table}' in the "
                    "schema cache", code="PGRST205")
            return self.inner.write(table, rows, **kw)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    seeder = sb.HistorySeeder(_Refuses(api), session.site_id, session.token)
    counts = seeder.seed(minutes=30, every_minutes=10)
    assert counts["robot_telemetry"] > 0 and counts["missions"] == 2
    assert counts["commands"] == 0 and counts["maintenance_findings"] == 0
    assert len(seeder.warnings) == 2
    assert all("schema cache" in w for w in seeder.warnings)


def test_mint_reports_seeding_warnings_and_still_succeeds(fakerest, api,
                                                          state_file, capsys):
    real_seed = sb.HistorySeeder.seed

    def seed(self, **kw):
        out = real_seed(self, **kw)
        self.warnings.append("commands: pretend this deployment lacks 0002")
        return out

    sb.HistorySeeder.seed = seed
    try:
        rc = sb.run_sandbox_mint(fakerest.base_url, ANON_KEY, robots=2,
                                 sim=False, state_file=state_file,
                                 json_output=True, api=api)
    finally:
        sb.HistorySeeder.seed = real_seed
    assert rc == sb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == ["commands: pretend this deployment lacks 0002"]
    assert payload["seeded"]["robots_parked"] == 2


# ==========================================================================
# SECURITY, part 3 — the two promises a mint-and-look test cannot see:
# a sandbox cannot buy itself more time, and it cannot survive the reaper.
# ==========================================================================

def test_demo_token_cannot_extend_its_own_ttl(fakerest, api):
    """There is no lengthening path in 0009 — only shortening ones.

    `demo_mint_session` is the only writer of `expires_at`, and it writes
    it once at insert. `demo_claim_session` touches `claimed`/`claimed_at`;
    `demo_session_info` is `stable`; `demo_end_session` writes
    `least(expires_at, now())`. So the four anon-callable RPCs, in any
    order and any number of times, cannot move a sandbox's deadline out.
    """
    session = api.mint(seed_robots=2, ttl_minutes=15)
    original = fakerest.demo_sessions[session.token]["expires_at"]

    for _ in range(3):
        api.claim(session.token)
        api.info(session.token)
        api.limits()
    assert fakerest.demo_sessions[session.token]["expires_at"] == original

    # Minting again gets a DIFFERENT sandbox; it does not renew this one.
    other = api.mint(seed_robots=1)
    assert other.site_id != session.site_id and other.token != session.token
    assert fakerest.demo_sessions[session.token]["expires_at"] == original

    # And the write surface it does hold cannot reach the session row: the
    # sessions table is not a route, and no fleet table carries an expiry.
    assert _get(fakerest.base_url, "demo_sessions",
                token=session.token).status_code >= 400
    assert _post(fakerest.base_url, "demo_sessions",
                 [{"token": session.token, "site_id": session.site_id,
                   "expires_at": "2099-01-01T00:00:00Z"}],
                 token=session.token).status_code >= 400
    assert fakerest.demo_sessions[session.token]["expires_at"] == original

    # `demo_end_session` is the one write it *can* make, and it only ever
    # brings the deadline forward.
    api.end(session.token)
    ended = sb._parse_ts(fakerest.demo_sessions[session.token]["expires_at"])
    assert ended <= sb._parse_ts(original)


def test_demo_token_cannot_outlive_the_reaper(fakerest, api, service_api,
                                              state_file, capsys):
    """Expiry, reap, and the driver that was mid-flight when it happened."""
    session = api.mint(seed_robots=3)
    driver = sb.SandboxDriver(api, session.site_id, session.token, robots=3,
                              interval=0.0, check_every=1)
    driver.tick_once()                       # it works while the TTL holds
    assert [r for r in fakerest.tables["robots"]
            if r["site_id"] == session.site_id]

    _expire(fakerest, session.token)
    assert sb.run_sandbox_reap(fakerest.base_url, SERVICE_KEY,
                               state_file=state_file, json_output=True,
                               api=service_api) == sb.EXIT_OK
    assert json.loads(capsys.readouterr().out)["sites"] == [session.site_id]

    # Every scoped row is gone...
    for table in ("robots", "alerts", "missions", "incidents",
                  "robot_telemetry", "commands", "maintenance_findings"):
        assert not [r for r in fakerest.tables[table]
                    if r.get("site_id") == session.site_id], table
    # ...the token is inert for reads and writes...
    assert _get(fakerest.base_url, "robots", token=session.token).json() == []
    assert _post(fakerest.base_url, "robots",
                 [{"id": f"{session.site_id}-R09", "vendor": "MiR",
                   "site_id": session.site_id}],
                 token=session.token).status_code in (401, 403)
    # ...and a driver that was still ticking gives up instead of spinning.
    errors: list[Exception] = []
    assert driver.run(ticks=10, on_error=errors.append) < 10
    assert errors and all(isinstance(e, sb.SandboxError) for e in errors)
    assert api.info(session.token)["reason"] == "reaped"


def test_commands_insert_is_constrained_to_the_sandboxs_own_robots(
        fakerest, api):
    """0017 closed the schema gap this test used to pin open.

    0009's `demo_sandbox_insert` checked `site_id` and nothing else, and
    `commands.robot_id` is a bare `text` column (0002 — no foreign key, no
    same-site constraint). An anonymous visitor could therefore insert a
    command row that *named a real fleet's robot* while still living in
    their own demo site, with `status='approved'` already set (the
    column-level grants only narrow UPDATE, never INSERT). A service-key
    executor polling `commands?status=eq.approved` without a site filter
    would then have dispatched it as a VDA 5050 instantAction.

    supabase/0017_demo_command_scope.sql adds the missing WITH CHECK terms:
    same-site robot, `pending` only, no decision stamps. The behaviour
    below was verified against a real PostgreSQL 16 with 0001-0017 applied
    before it was mirrored into the fake.
    """
    session = api.mint(seed_robots=2)
    forged = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    resp = _post(fakerest.base_url, "commands",
                 [{"id": forged, "robot_id": "AMR-01",     # a REAL robot
                   "cmd": "estop", "params": {},
                   "status": "approved",                    # pre-approved
                   "requested_by": "demo-visitor",
                   "site_id": session.site_id}],
                 token=session.token)
    assert resp.status_code == 403
    assert "row-level security" in resp.text
    assert not [r for r in fakerest.tables["commands"] if r["id"] == forged]

    # Even a *pending* command is refused when it names a foreign robot.
    pending_at_a_real_robot = _post(
        fakerest.base_url, "commands",
        [{"id": str(uuid.uuid4()), "robot_id": "AMR-01", "cmd": "estop",
          "params": {}, "status": "pending", "requested_by": "demo-visitor",
          "site_id": session.site_id}],
        token=session.token)
    assert pending_at_a_real_robot.status_code == 403

    # ...and so is a pre-approved command for the sandbox's OWN robot:
    # the human approval gate cannot be satisfied at insert time.
    own = f"{session.site_id}-R01"
    preapproved = _post(
        fakerest.base_url, "commands",
        [{"id": str(uuid.uuid4()), "robot_id": own, "cmd": "estop",
          "params": {}, "status": "approved", "requested_by": "demo-visitor",
          "site_id": session.site_id}],
        token=session.token)
    assert preapproved.status_code == 403

    # What the sandbox may still do: request a pending command for its own
    # robot, then decide it — the loop the demo exists to show.
    good = str(uuid.uuid4())
    ok = _post(fakerest.base_url, "commands",
               [{"id": good, "robot_id": own, "cmd": "charge", "params": {},
                 "status": "pending", "requested_by": "demo-visitor",
                 "site_id": session.site_id}],
               token=session.token)
    assert ok.status_code < 400
    patched = httpx.patch(
        f"{fakerest.base_url}/rest/v1/commands", params={"id": f"eq.{good}"},
        json={"status": "approved", "decided_by": "demo-visitor"},
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 sb.DEMO_TOKEN_HEADER: session.token}, timeout=5.0)
    assert patched.status_code == 204
    row = next(r for r in fakerest.tables["commands"] if r["id"] == good)
    assert (row["status"], row["site_id"]) == ("approved", session.site_id)

    # The real site's own rows are untouched, and no approved command
    # anywhere names a robot outside its own site.
    assert next(r for r in fakerest.tables["robots"]
                if r["id"] == "AMR-01")["site_id"] == "BLR-DC1"
    scoped = [c for c in fakerest.tables["commands"]
              if c.get("site_id") == "BLR-DC1" and c.get("status") == "approved"]
    assert scoped == []


# ==========================================================================
# THE HTTP DOOR — what the "Try it with a live fleet" button actually hits
# ==========================================================================

def _door(fakerest, state_file, **overrides):
    """A running sandbox_http server against the fake backend."""
    config = sh.ServeConfig(
        base_url=fakerest.base_url, key=ANON_KEY, console="https://x.test/c/",
        state_file=state_file, sim=False, history=False, robots=3,
        quiet=True, **overrides)
    return sh.SandboxHTTP(config, host="127.0.0.1", port=0)


@pytest.fixture()
def door(fakerest, state_file):
    with _door(fakerest, state_file) as server:
        yield server


def _mint(door_url: str, **kw):
    return httpx.post(f"{door_url}{sh.ROUTE_MINT}", timeout=20.0, **kw)


def test_http_mint_hands_back_one_working_console_url(fakerest, door):
    resp = _mint(door.base_url)
    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is True
    assert set(body) == {"ok", "url", "site_id", "expires_at", "ttl_minutes",
                         "robots"}
    site, url = body["site_id"], body["url"]
    assert site.startswith(sb.DEMO_SITE_PREFIX)
    assert url.startswith("https://x.test/c/?supa=")
    assert f"site={site}" in url

    # The URL is not a promise — the token in it really opens that sandbox,
    # and only that sandbox.
    token = url.split("&demo=")[1]
    assert sb.valid_token(token)
    rows = _get(fakerest.base_url, "robots", token=token).json()
    assert len(rows) == body["robots"] == 3
    assert {r["site_id"] for r in rows} == {site}
    assert resp.headers["cache-control"] == "no-store"


def test_http_never_takes_a_site_or_a_ttl_from_the_caller(fakerest, door):
    """The one bug this door exists not to have."""
    fakerest.demo_limits.update(ttl_minutes=30, max_seed_robots=12)
    hostile = {"site_id": "BLR-DC1", "site": "BLR-DC1", "ttl_minutes": 100000,
               "ttl": 100000, "robots": 500, "seed_robots": 500,
               "origin": "../../etc/passwd", "console": "https://evil.test/"}
    resp = _mint(door.base_url, json=hostile,
                 params={"site_id": "BLR-DC1", "ttl": "100000",
                         "robots": "500"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["site_id"].startswith(sb.DEMO_SITE_PREFIX)
    assert body["site_id"] != "BLR-DC1"
    assert body["ttl_minutes"] == 30          # the server's clamp, not theirs
    assert body["robots"] == 3                # the operator's --robots
    assert body["url"].startswith("https://x.test/c/")   # not evil.test
    # Nothing was minted against, or written into, the real site.
    assert {s["site_id"] for s in fakerest.demo_sessions.values()} == {
        body["site_id"]}
    assert [r["id"] for r in fakerest.tables["robots"]
            if r["site_id"] == "BLR-DC1"] == ["AMR-01"]
    assert fakerest.demo_sessions[
        next(iter(fakerest.demo_sessions))]["origin"] == "web"


def test_http_returns_only_this_sessions_own_url(fakerest, door):
    first = _mint(door.base_url).json()
    second = _mint(door.base_url).json()
    assert first["site_id"] != second["site_id"]
    assert first["site_id"] not in json.dumps(second)
    assert second["site_id"] not in json.dumps(first)
    # No route lists sandboxes, and none accepts a token.
    for path in ("/api/demo/sessions", "/api/demo/session/list",
                 f"/api/demo/session?token={first['url'].split('&demo=')[1]}"):
        got = httpx.get(f"{door.base_url}{path}", timeout=5.0)
        assert got.status_code in (404, 405)
        assert first["site_id"] not in got.text


def test_http_refuses_cleanly_when_every_sandbox_slot_is_full(
        fakerest, state_file):
    """A front page on Hacker News gets 429s, not a dead box."""
    with _door(fakerest, state_file, max_live=2) as door:
        assert _mint(door.base_url).status_code == 201
        assert _mint(door.base_url).status_code == 201
        resp = _mint(door.base_url)
        assert resp.status_code == 429
        body = resp.json()
        assert body["ok"] is False and body["reason"] == "ceiling"
        assert int(resp.headers["retry-after"]) > 0
        # Refused before the RPC: the backend minted exactly two.
        assert len(fakerest.demo_sessions) == 2
        # ...and the door is still answering, not wedged.
        assert httpx.get(f"{door.base_url}{sh.ROUTE_HEALTH}",
                         timeout=5.0).json()["live"] == 2


def test_http_surfaces_the_database_ceiling_as_429(fakerest, state_file):
    fakerest.demo_limits["max_live_sessions"] = 1
    with _door(fakerest, state_file, max_live=50) as door:
        assert _mint(door.base_url).status_code == 201
        resp = _mint(door.base_url)
        assert resp.status_code == 429 and resp.json()["reason"] == "ceiling"
        assert "too many live demo sandboxes" in resp.json()["detail"]


def test_http_rate_limits_by_ip(fakerest, state_file):
    with _door(fakerest, state_file, rate=2, rate_window_s=3600.0,
               max_live=50) as door:
        assert _mint(door.base_url).status_code == 201
        assert _mint(door.base_url).status_code == 201
        resp = _mint(door.base_url)
        assert resp.status_code == 429
        assert resp.json()["reason"] == "rate_limited"
        assert int(resp.headers["retry-after"]) > 0
        assert len(fakerest.demo_sessions) == 2, "no third session was minted"
        # Health still works — a rate-limited visitor is not an outage.
        assert httpx.get(f"{door.base_url}{sh.ROUTE_HEALTH}",
                         timeout=5.0).json()["refused"] == 1


def test_http_forwarded_for_cannot_buy_a_fresh_quota(fakerest, state_file):
    """With no trusted proxy configured, the header is simply not read."""
    with _door(fakerest, state_file, rate=1, rate_window_s=3600.0,
               max_live=50) as door:
        assert _mint(door.base_url).status_code == 201
        for spoof in ("1.2.3.4", "8.8.8.8, 9.9.9.9", ""):
            resp = _mint(door.base_url, headers={"X-Forwarded-For": spoof})
            assert resp.status_code == 429
            assert resp.json()["reason"] == "rate_limited"
        assert len(fakerest.demo_sessions) == 1


def test_client_ip_trusts_exactly_as_many_proxies_as_configured():
    xff = "1.1.1.1, 2.2.2.2, 3.3.3.3"
    assert sh.client_ip("127.0.0.1", xff, 0) == "127.0.0.1"   # default
    assert sh.client_ip("127.0.0.1", xff, 1) == "3.3.3.3"     # one nginx
    assert sh.client_ip("127.0.0.1", xff, 2) == "2.2.2.2"
    assert sh.client_ip("127.0.0.1", xff, 9) == "1.1.1.1"     # never IndexError
    assert sh.client_ip("127.0.0.1", None, 1) == "127.0.0.1"
    assert sh.client_ip("127.0.0.1", " , ", 1) == "127.0.0.1"
    assert sh.client_ip("", None, 0) == "?"


def test_rate_limiter_windows_and_stays_bounded():
    now = [1000.0]
    lim = sh.RateLimiter(limit=2, window_s=60.0, max_keys=8,
                         clock=lambda: now[0])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert 1 <= lim.retry_after("a") <= 61
    assert lim.allow("b"), "one visitor's quota is not another's"
    now[0] += 61
    assert lim.allow("a"), "the window rolled over"

    # A flood of distinct keys must not grow the table without bound.
    for i in range(500):
        lim.allow(f"ip-{i}")
    assert len(lim._hits) <= 8
    # limit=0 turns the limiter off entirely.
    off = sh.RateLimiter(limit=0, window_s=60.0)
    assert all(off.allow("a") for _ in range(50))


def test_http_reports_a_disabled_deployment_as_503(fakerest, state_file):
    fakerest.demo_limits["enabled"] = False
    with _door(fakerest, state_file) as door:
        resp = _mint(door.base_url)
        assert resp.status_code == 503
        assert resp.json()["reason"] == "disabled"
        limits = httpx.get(f"{door.base_url}{sh.ROUTE_LIMITS}",
                           timeout=5.0).json()
        assert limits["enabled"] is False and limits["available"] is False


def test_http_limits_tells_the_page_whether_to_show_the_button(
        fakerest, state_file):
    fakerest.demo_limits["ttl_minutes"] = 45
    with _door(fakerest, state_file, max_live=1) as door:
        limits = httpx.get(f"{door.base_url}{sh.ROUTE_LIMITS}",
                           timeout=5.0).json()
        assert limits == {"ok": True, "enabled": True, "ttl_minutes": 45,
                          "live": 0, "max_live": 1, "available": True}
        assert _mint(door.base_url).status_code == 201
        after = httpx.get(f"{door.base_url}{sh.ROUTE_LIMITS}",
                          timeout=5.0).json()
        assert after["live"] == 1 and after["available"] is False


def test_http_health_needs_no_backend(fakerest, door):
    body = httpx.get(f"{door.base_url}{sh.ROUTE_HEALTH}", timeout=5.0).json()
    assert body["ok"] is True and body["service"] == "yantra-sandbox"
    assert body["minted"] == 0 and body["refused"] == 0
    assert body["max_live"] == sb.DEFAULT_MAX_LIVE


def test_http_form_post_redirects_straight_into_the_console(fakerest, door):
    """The zero-JavaScript button: <form method="post"> and nothing else."""
    resp = httpx.post(f"{door.base_url}{sh.ROUTE_MINT}",
                      headers={"Accept": "text/html,application/xhtml+xml"},
                      follow_redirects=False, timeout=20.0)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://x.test/c/?supa=")
    assert "&demo=" in location
    assert sb.valid_token(location.split("&demo=")[1])


def test_http_rejects_every_other_route_and_method(fakerest, door):
    # Minting must not be reachable by a link a browser might prefetch.
    got = httpx.get(f"{door.base_url}{sh.ROUTE_MINT}", timeout=5.0)
    assert got.status_code == 405 and "POST" in got.json()["error"]
    for path in ("/", "/admin", "/api/demo", "/rest/v1/robots"):
        assert httpx.get(f"{door.base_url}{path}",
                         timeout=5.0).status_code == 404
        assert httpx.post(f"{door.base_url}{path}",
                          timeout=5.0).status_code == 404
    assert httpx.request("DELETE", f"{door.base_url}{sh.ROUTE_MINT}",
                         timeout=5.0).status_code >= 400
    assert fakerest.demo_sessions == {}
    # CORS preflight is answered so a cross-origin marketing page works.
    pre = httpx.request("OPTIONS", f"{door.base_url}{sh.ROUTE_MINT}",
                        timeout=5.0)
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "*"


def test_http_survives_a_backend_that_is_down(state_file, tmp_path):
    """PostgREST unreachable is a 502, not a traceback and not a hang."""
    config = sh.ServeConfig(base_url="http://127.0.0.1:1", key=ANON_KEY,
                            state_file=state_file, sim=False, history=False,
                            quiet=True)
    with sh.SandboxHTTP(config, host="127.0.0.1", port=0) as door:
        resp = _mint(door.base_url)
        assert resp.status_code == 502 and resp.json()["reason"] == "error"
        assert httpx.get(f"{door.base_url}{sh.ROUTE_HEALTH}",
                         timeout=5.0).json()["ok"] is True


def test_http_concurrent_bursts_never_exceed_the_ceiling(fakerest, state_file):
    """Twelve browsers at once, four slots: exactly four sandboxes exist."""
    import concurrent.futures as cf

    with _door(fakerest, state_file, max_live=4, rate=0,
               max_inflight=4) as door:
        with cf.ThreadPoolExecutor(max_workers=12) as pool:
            codes = [f.result().status_code
                     for f in [pool.submit(_mint, door.base_url)
                               for _ in range(12)]]
        assert sorted(codes) == [201] * 4 + [429] * 8
        assert len(fakerest.demo_sessions) == 4
        assert len(sb.SandboxRegistry(state_file).load()) == 4
        assert len({r["site_id"] for r in fakerest.tables["robots"]
                    if r["site_id"].startswith(sb.DEMO_SITE_PREFIX)}) == 4


def test_http_mints_a_moving_fleet_end_to_end(fakerest, state_file):
    """THE arrival test: one POST, then robots that are actually moving.

    Nothing is faked past the button — a real HTTP request, the real mint,
    the real backfill, a real `sandbox-drive` child process — and the
    assertion is that the fleet the visitor lands on CHANGES over time.
    """
    config = sh.ServeConfig(
        base_url=fakerest.base_url, key=ANON_KEY, console="https://x.test/c/",
        state_file=state_file, sim=True, history=True, robots=4, interval=0.2,
        quiet=True)
    with sh.SandboxHTTP(config, host="127.0.0.1", port=0) as door:
        body = _mint(door.base_url).json()
    site = body["site_id"]
    pid = sb.SandboxRegistry(state_file).load()[0].pid
    assert pid and sb._pid_alive(pid), "the door started no simulator"
    try:
        # 1) The visitor arrives to a seeded fleet, not an empty console.
        fleet = [r for r in fakerest.tables["robots"] if r["site_id"] == site]
        assert len(fleet) == 4
        assert [r for r in fakerest.tables["robot_telemetry"]
                if r["site_id"] == site], "no history to render on arrival"
        assert [r for r in fakerest.tables["missions"]
                if r["site_id"] == site]
        assert [r for r in fakerest.tables["alerts"] if r["site_id"] == site]

        # 2) ...and it is moving.
        def snapshot():
            return {f"{r['id']}@{r.get('updated_at')}@{r.get('pos')}"
                    for r in fakerest.tables["robots"]
                    if r["site_id"] == site}

        seen, moved = snapshot(), False
        deadline = time.time() + 30
        while time.time() < deadline and not moved:
            time.sleep(0.5)
            moved = bool(snapshot() - seen)
        assert moved, "the fleet behind the Try-it button never moved"

        # 3) ...and it moved only inside its own sandbox.
        assert [r["id"] for r in fakerest.tables["robots"]
                if r["site_id"] == "BLR-DC1"] == ["AMR-01"]
        assert all(r["site_id"] == site for r in fakerest.tables["robots"]
                   if r["id"].startswith(sb.DEMO_SITE_PREFIX))
    finally:
        sb._stop_pid(pid)
    assert not sb._pid_alive(pid)


def test_http_sweep_stops_the_simulator_of_an_expired_sandbox(
        fakerest, state_file):
    """The door's own janitor: no orphaned sim, even with no reap cron."""
    config = sh.ServeConfig(base_url=fakerest.base_url, key=ANON_KEY,
                            state_file=state_file, sim=False, history=False,
                            quiet=True)
    service = sh.SandboxService(config)
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    sb.SandboxRegistry(state_file).save(
        [sb.SandboxRecord(site_id="DEMO-000000000001", expires_at=past,
                          pid=proc.pid)])
    out = service.sweep()
    assert out["stopped"] == ["DEMO-000000000001"]
    assert proc.wait(timeout=10) is not None
    assert service.sweep()["stopped"] == []       # idempotent


def test_cli_parses_sandbox_serve():
    parser = build_parser()
    args = parser.parse_args([
        "sandbox-serve", "--host", "0.0.0.0", "--port", "9099",
        "--robots", "5", "--max-live", "4", "--rate", "2",
        "--rate-window", "60", "--max-inflight", "3",
        "--trusted-proxy-hops", "1", "--allow-origin", "https://yantrika.ai",
        "--no-sim", "--quiet"])
    assert args.command == "sandbox-serve" and args.port == 9099
    assert args.host == "0.0.0.0" and args.robots == 5 and args.max_live == 4
    assert args.rate == 2 and args.rate_window == 60 and args.max_inflight == 3
    assert args.trusted_proxy_hops == 1
    assert args.allow_origin == "https://yantrika.ai"
    assert args.no_sim and args.quiet
    # There is deliberately no way to hand the door a site or a token.
    for flag in ("--site", "--token", "--demo-token"):
        with pytest.raises(SystemExit):
            parser.parse_args(["sandbox-serve", flag, "X"])


def test_cli_dispatches_sandbox_serve(fakerest, state_file, capsys):
    """`main()` really binds a socket and really serves, then exits."""
    from yantraops.__main__ import main
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    result: dict[str, object] = {}

    def hit():
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                result["health"] = httpx.get(f"{base}{sh.ROUTE_HEALTH}",
                                             timeout=2.0).json()
                break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.05)
        result["mint"] = _mint(base).json()

    import threading
    t = threading.Thread(target=hit, daemon=True)
    t.start()
    rc = main(["sandbox-serve", "--url", fakerest.base_url, "--key", ANON_KEY,
               "--state-file", str(state_file), "--port", str(port),
               "--console", "https://x.test/c/", "--robots", "2",
               "--no-sim", "--no-history", "--sweep-interval", "0",
               "--duration", "3", "--quiet"])
    t.join(timeout=10)
    assert rc == sb.EXIT_OK
    assert result["health"]["ok"] is True
    assert result["mint"]["site_id"].startswith(sb.DEMO_SITE_PREFIX)
    assert result["mint"]["url"].startswith("https://x.test/c/")


def test_http_drops_an_oversized_or_chunked_body_without_desyncing(
        fakerest, door):
    """A body we do not read whole must close the socket, not confuse it."""
    big = _mint(door.base_url, content=b"x" * 200_000,
                headers={"Content-Type": "application/octet-stream"})
    assert big.status_code == 201
    assert big.json()["site_id"].startswith(sb.DEMO_SITE_PREFIX)

    def chunks():
        yield b"a" * 64
        yield b"b" * 64

    streamed = httpx.post(f"{door.base_url}{sh.ROUTE_MINT}", content=chunks(),
                          timeout=20.0)
    assert streamed.status_code == 201
    assert streamed.json()["site_id"] != big.json()["site_id"]
    # Two clean mints, two sandboxes, no third request invented from the
    # leftover bytes of the first.
    assert len(fakerest.demo_sessions) == 2


def test_upsert_onto_a_real_robots_primary_key_is_refused(fakerest, api):
    """The fake now models what PostgreSQL actually does with ON CONFLICT.

    ``robots.id`` is a global primary key, so a demo visitor can aim an
    upsert (``on_conflict=id``, ``resolution=merge-duplicates``) at a REAL
    robot's id while declaring their own ``site_id``. Real PostgreSQL
    refuses: for ``insert ... on conflict do update`` the UPDATE policy's
    USING expression is evaluated against the EXISTING row, and
    ``demo_sandbox_update`` requires ``site_id = yf_demo_site()`` — which
    ``AMR-01`` is not — so the statement errors instead of merging. (The
    row is not even SELECT-visible to that token, which fails it twice.)
    Confirmed by hand against PostgreSQL 16 with 0001-0017 applied:
    ``ERROR: new row violates row-level security policy (USING expression)
    for table "robots"``.

    ``e2e/fakerest.py`` used to validate only the *incoming* rows'
    ``site_id`` and then merge, which is the divergence this test was
    originally named for. It now checks the existing row's owner too.
    """
    session = api.mint(seed_robots=2)
    resp = httpx.post(
        f"{fakerest.base_url}/rest/v1/robots", params={"on_conflict": "id"},
        json=[{"id": "AMR-01", "vendor": "EVIL", "status": "estop",
               "site_id": session.site_id}],
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 sb.DEMO_TOKEN_HEADER: session.token,
                 "Content-Type": "application/json",
                 "Prefer": "return=minimal,resolution=merge-duplicates"},
        timeout=5.0)
    assert resp.status_code == 403
    assert resp.json()["code"] == "42501"
    victim = next(r for r in fakerest.tables["robots"] if r["id"] == "AMR-01")
    assert (victim["site_id"], victim["vendor"]) == ("BLR-DC1", "MiR"), \
        "the real robot was hijacked by a demo upsert"

    # ``do nothing`` is what PostgreSQL silently skips rather than errors,
    # so the fake skips it too — the row is still not touched.
    ignored = httpx.post(
        f"{fakerest.base_url}/rest/v1/robots", params={"on_conflict": "id"},
        json=[{"id": "AMR-01", "vendor": "EVIL", "site_id": session.site_id}],
        headers={"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
                 sb.DEMO_TOKEN_HEADER: session.token,
                 "Content-Type": "application/json",
                 "Prefer": "return=minimal,resolution=ignore-duplicates"},
        timeout=5.0)
    assert ignored.status_code == 201
    assert next(r for r in fakerest.tables["robots"]
                if r["id"] == "AMR-01")["vendor"] == "MiR"

    # What DOES hold in both, and is the property that matters: no id this
    # module writes can ever address a row outside its own sandbox. (A
    # fresh sandbox, so the row hijacked above is not in the sample.)
    other = api.mint(seed_robots=2)
    sb.HistorySeeder(api, other.site_id, other.token).seed(
        minutes=20, every_minutes=10)
    driver = sb.SandboxDriver(api, other.site_id, other.token, robots=2,
                              interval=0.0, history_every=1)
    for _ in range(3):
        driver.tick_once()
    for table in ("robots", "incidents", "missions",
                  "maintenance_findings"):
        rows = [r for r in fakerest.tables[table]
                if r.get("site_id") == other.site_id]
        assert rows, table
        for row in rows:
            assert other.site_id in str(row["id"]), (table, row["id"])


def test_driver_continues_from_where_the_visitor_found_the_fleet(fakerest, api):
    """No teleport on tick one — the arrival snapshot has to stay true.

    FleetSim scatters its robots over its own randomly chosen task nodes,
    which are not where HistorySeeder parked the seeded fleet. Without
    adoption the first tick moved robots up to 28 m across the floor and
    orphaned the telemetry trail written seconds earlier.
    """
    session = api.mint(seed_robots=4)
    sb.HistorySeeder(api, session.site_id, session.token).seed(
        minutes=60, every_minutes=10)
    parked = {r["id"]: list(r["pos"]) for r in fakerest.tables["robots"]
              if r["site_id"] == session.site_id}
    assert len(parked) == 4

    driver = sb.SandboxDriver(api, session.site_id, session.token, robots=4,
                              interval=0.0, history_every=1)
    assert driver.adopt_live_fleet() == 4
    driver.tick_once()
    after = {r["id"]: list(r["pos"]) for r in fakerest.tables["robots"]
             if r["site_id"] == session.site_id}
    assert after == parked, "the fleet jumped the instant the driver started"

    # The trail the seeder wrote still ends where each robot stands.
    for robot_id, pos in parked.items():
        track = sorted((t for t in fakerest.tables["robot_telemetry"]
                        if t["robot_id"] == robot_id), key=lambda t: t["ts"])
        assert track[-1]["pos"] == pos

    # ...and adoption did not freeze anything: the fleet still moves.
    for _ in range(10):
        driver.tick_once()
    moving = {r["id"]: list(r["pos"]) for r in fakerest.tables["robots"]
              if r["site_id"] == session.site_id}
    assert any(moving[i] != parked[i] for i in parked)


def test_adoption_is_best_effort_and_never_fails_the_driver(fakerest, api):
    """An empty or unreachable sandbox leaves the simulator's own layout."""
    session = api.mint(seed_robots=0)          # nothing seeded to adopt
    driver = sb.SandboxDriver(api, session.site_id, session.token, robots=2,
                              interval=0.0)
    assert driver.adopt_live_fleet() == 0
    assert driver.tick_once()["robots"] == 2   # and it still drives

    class _Dead:
        def __getattr__(self, name):
            if name in ("rest", "headers"):
                return getattr(api, name)
            raise sb.SandboxError("backend unreachable")

        @property
        def client(self):
            raise sb.SandboxError("backend unreachable")

    blind = sb.SandboxDriver(api, session.site_id, session.token, robots=2,
                             interval=0.0)
    blind.api = _Dead()
    assert blind.adopt_live_fleet() == 0
