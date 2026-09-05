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
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from yantraops import sandbox as sb
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
