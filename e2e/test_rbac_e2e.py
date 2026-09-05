"""Offline RBAC end-to-end tests — fakerest's rbac-0007 emulation.

Exercises ``FakePostgREST(rbac=True, service_key=...)`` over *real* HTTP on
localhost (httpx client, no mocks), proving the fake approximates
``supabase/0007_rbac.sql`` faithfully enough for console / academy /
browser tests to exercise auth flows fully offline:

* anon (or a role-less session): reads come back empty, writes 401/403;
* non-admin reads are scoped to the JWT claim's ``site_id``; admin reads
  span every site;
* operators may PATCH ``alerts.ack`` (and nothing else) and INSERT their
  own *pending* commands (``requested_by`` must match the claim email);
* command decisions go only through the ``decide_command`` RPC — manager+
  at the command's site, pending rows only, decided_by/decided_at stamped;
* ``save_progress`` upserts per-user academy progress and
  ``issue_certificate`` inserts certificates with unique verification
  codes (duplicate -> 409/23505);
* the configured service key bypasses everything (writer path).

Tokens are the UNSIGNED test form from ``fakerest.make_test_jwt`` —
``yf-test.<base64url claims>.sig`` — never real JWTs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakerest import (DEFAULT_SITE_ID, FakePostgREST,  # noqa: E402
                      make_test_jwt)

SERVICE_KEY = "sk-test-service"
ANON_KEY = "anon-test-key"
SITE_B = "PNQ-W2"  # a second site, non-default

OPERATOR = "operator1@example.com"
ENGINEER = "eng@example.com"
MANAGER = "ops-manager@example.com"
ADMIN = "ops-lead@example.com"


def bearer(email: str, role: str, site: str = DEFAULT_SITE_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(email, role, site)}",
            "apikey": ANON_KEY}


SVC = {"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY}
ANON = {"apikey": ANON_KEY}  # publishable key only, no JWT


def seed_tables() -> dict:
    return {
        "robots": [
            {"id": "AMR-01", "status": "active", "battery": 80.0},
            {"id": "AMR-02", "status": "charging", "battery": 30.0},
            # explicit second-site row; the two above take the column
            # default (DEFAULT_SITE_ID) exactly like 0005's schema
            {"id": "AMR-51", "status": "active", "battery": 66.0,
             "site_id": SITE_B},
        ],
        "alerts": [
            {"id": "AL-1", "sev": "crit", "msg": "conveyor jam", "ack": False},
            {"id": "AL-51", "sev": "warn", "msg": "low battery",
             "ack": False, "site_id": SITE_B},
        ],
        "commands": [
            {"id": "CMD-1", "robot": "AMR-01", "action": "pause",
             "status": "pending", "requested_by": OPERATOR},
            {"id": "CMD-2", "robot": "AMR-02", "action": "charge",
             "status": "pending", "requested_by": OPERATOR},
        ],
        "fleet_meta": [{"id": 1, "writer_id": "rbac-e2e", "sim_min": 1}],
    }


@pytest.fixture()
def fake() -> Iterator[FakePostgREST]:
    f = FakePostgREST(seed_tables(), rbac=True,
                      service_key=SERVICE_KEY, anon_key=ANON_KEY)
    f.base_url = f.start()
    try:
        yield f
    finally:
        f.stop()


@pytest.fixture()
def http(fake: FakePostgREST) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{fake.base_url}/rest/v1", timeout=5.0) as c:
        yield c


# ---------------------------------------------------------------- reads

def test_anon_reads_empty_and_writes_rejected(http: httpx.Client) -> None:
    """anon key alone: RLS shows nothing (200 []), writes are 401."""
    for table in ("robots", "alerts", "commands", "fleet_meta"):
        r = http.get(f"/{table}", headers=ANON)
        assert r.status_code == 200 and r.json() == []
    r = http.post("/commands", headers=ANON, json={
        "robot": "AMR-01", "action": "pause", "status": "pending"})
    assert r.status_code == 401
    body = r.json()
    assert "message" in body and "code" in body
    r = http.patch("/alerts?id=eq.AL-1", headers=ANON, json={"ack": True})
    assert r.status_code == 401
    # nothing changed behind the scenes
    rows = http.get("/alerts?id=eq.AL-1", headers=SVC).json()
    assert rows[0]["ack"] is False


def test_no_role_session_reads_empty_writes_403(http: httpx.Client) -> None:
    """A signed-in user with no user_roles row sees NOTHING (0007 banner)."""
    norole = bearer("newhire@example.com", "no-such-role")
    assert http.get("/robots", headers=norole).json() == []
    r = http.patch("/alerts?id=eq.AL-1", headers=norole, json={"ack": True})
    assert r.status_code == 403
    r = http.post("/commands", headers=norole, json={
        "robot": "AMR-01", "action": "pause",
        "requested_by": "newhire@example.com"})
    assert r.status_code == 403


def test_operator_reads_scoped_to_claim_site(http: httpx.Client) -> None:
    op = bearer(OPERATOR, "operator")  # BLR-DC1
    ids = {r["id"] for r in http.get("/robots", headers=op).json()}
    assert ids == {"AMR-01", "AMR-02"}  # AMR-51 (PNQ-W2) invisible
    alerts = {a["id"] for a in http.get("/alerts", headers=op).json()}
    assert alerts == {"AL-1"}
    # fleet_meta has no site_id: any role holder may read it
    assert http.get("/fleet_meta", headers=op).json()[0]["writer_id"] == "rbac-e2e"
    # ...and the other site's operator sees only their own rows
    op_b = bearer("op2@example.com", "operator", SITE_B)
    assert {r["id"] for r in http.get("/robots", headers=op_b).json()} == {"AMR-51"}


def test_admin_reads_cross_site(http: httpx.Client) -> None:
    adm = bearer(ADMIN, "admin", SITE_B)  # admin anywhere = global
    ids = {r["id"] for r in http.get("/robots", headers=adm).json()}
    assert ids == {"AMR-01", "AMR-02", "AMR-51"}
    assert {a["id"] for a in http.get("/alerts", headers=adm).json()} \
        == {"AL-1", "AL-51"}


# ---------------------------------------------------------------- alerts.ack

def test_operator_can_ack_alert(fake: FakePostgREST, http: httpx.Client) -> None:
    r = http.patch("/alerts?id=eq.AL-1", headers=bearer(OPERATOR, "operator"),
                   json={"ack": True})
    assert r.status_code == 204
    with fake.lock:
        row = next(a for a in fake.tables["alerts"] if a["id"] == "AL-1")
        assert row["ack"] is True
        # the site guard kept the PNQ-W2 alert untouched even though the
        # filterless part of the query would have matched nothing anyway
        other = next(a for a in fake.tables["alerts"] if a["id"] == "AL-51")
        assert other["ack"] is False


def test_operator_cannot_patch_other_alert_columns(
        fake: FakePostgREST, http: httpx.Client) -> None:
    """Column-level grant: authenticated may update alerts.ack ONLY."""
    op = bearer(OPERATOR, "operator")
    for body in ({"ack": True, "msg": "defaced"}, {"sev": "info"},
                 {"msg": "defaced"}):
        r = http.patch("/alerts?id=eq.AL-1", headers=op, json=body)
        assert r.status_code == 403, body
        assert r.json()["code"] == "42501"
    # and no non-alerts table accepts an authenticated PATCH at all
    r = http.patch("/commands?id=eq.CMD-1", headers=bearer(MANAGER, "manager"),
                   json={"status": "approved"})
    assert r.status_code == 403
    with fake.lock:
        alert = next(a for a in fake.tables["alerts"] if a["id"] == "AL-1")
        assert alert["msg"] == "conveyor jam" and alert["sev"] == "crit"
        cmd = next(c for c in fake.tables["commands"] if c["id"] == "CMD-1")
        assert cmd["status"] == "pending"


def test_ack_scoped_to_own_site(fake: FakePostgREST, http: httpx.Client) -> None:
    """An ack PATCH from site A cannot touch site B's rows (RLS USING)."""
    r = http.patch("/alerts?id=eq.AL-51", headers=bearer(OPERATOR, "operator"),
                   json={"ack": True})
    assert r.status_code == 204  # PostgREST-style: 0 rows matched, still 204
    with fake.lock:
        assert next(a for a in fake.tables["alerts"]
                    if a["id"] == "AL-51")["ack"] is False


# ---------------------------------------------------------------- commands

def test_operator_inserts_own_pending_command(
        fake: FakePostgREST, http: httpx.Client) -> None:
    r = http.post("/commands", headers=bearer(OPERATOR, "operator"), json={
        "robot": "AMR-01", "action": "pause", "status": "pending",
        "requested_by": OPERATOR})
    assert r.status_code == 201
    with fake.lock:
        row = next(c for c in fake.tables["commands"]
                   if c.get("robot") == "AMR-01" and c.get("action") == "pause"
                   and c["id"] not in ("CMD-1", "CMD-2"))
        assert row["status"] == "pending"
        assert row["requested_by"] == OPERATOR
        assert row["site_id"] == DEFAULT_SITE_ID  # column default


def test_command_insert_rejections(fake: FakePostgREST,
                                   http: httpx.Client) -> None:
    op = bearer(OPERATOR, "operator")
    # spoofed requested_by
    r = http.post("/commands", headers=op, json={
        "robot": "AMR-01", "action": "resume", "status": "pending",
        "requested_by": MANAGER})
    assert r.status_code == 403 and r.json()["code"] == "42501"
    # non-pending status
    r = http.post("/commands", headers=op, json={
        "robot": "AMR-01", "action": "resume", "status": "approved",
        "requested_by": OPERATOR})
    assert r.status_code == 403
    # wrong site (with-check evaluates the row's site against the claim's)
    r = http.post("/commands", headers=op, json={
        "robot": "AMR-51", "action": "pause", "status": "pending",
        "requested_by": OPERATOR, "site_id": SITE_B})
    assert r.status_code == 403
    # pre-stamped decided_by
    r = http.post("/commands", headers=op, json={
        "robot": "AMR-01", "action": "pause", "status": "pending",
        "requested_by": OPERATOR, "decided_by": OPERATOR})
    assert r.status_code == 403
    with fake.lock:  # nothing slipped through
        assert len(fake.tables["commands"]) == 2


# ---------------------------------------------------------------- decide_command

def test_decide_command_as_manager(fake: FakePostgREST,
                                   http: httpx.Client) -> None:
    r = http.post("/rpc/decide_command", headers=bearer(MANAGER, "manager"),
                  json={"p_id": "CMD-1", "p_decision": "approved",
                        "p_note": "go ahead"})
    assert r.status_code == 200
    row = r.json()
    assert row["status"] == "approved"
    assert row["decided_by"] == MANAGER
    assert row["decided_at"]  # stamped
    assert row["note"] == "go ahead"
    with fake.lock:
        stored = next(c for c in fake.tables["commands"] if c["id"] == "CMD-1")
        assert stored["status"] == "approved"
    # rejection works the same way
    r = http.post("/rpc/decide_command", headers=bearer(MANAGER, "manager"),
                  json={"p_id": "CMD-2", "p_decision": "rejected"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_decide_command_denied_below_manager(fake: FakePostgREST,
                                             http: httpx.Client) -> None:
    for email, role in ((OPERATOR, "operator"), (ENGINEER, "engineer")):
        r = http.post("/rpc/decide_command", headers=bearer(email, role),
                      json={"p_id": "CMD-1", "p_decision": "approved"})
        assert r.status_code == 403, role
        assert "not authorized" in r.json()["message"]
    # a manager of ANOTHER site is not authorized either (admin would be)
    r = http.post("/rpc/decide_command",
                  headers=bearer("mgr-b@example.com", "manager", SITE_B),
                  json={"p_id": "CMD-1", "p_decision": "approved"})
    assert r.status_code == 403
    # anon has no session at all
    r = http.post("/rpc/decide_command", headers=ANON,
                  json={"p_id": "CMD-1", "p_decision": "approved"})
    assert r.status_code == 401
    with fake.lock:
        assert next(c for c in fake.tables["commands"]
                    if c["id"] == "CMD-1")["status"] == "pending"


def test_decide_command_state_errors(http: httpx.Client) -> None:
    mgr = bearer(MANAGER, "manager")
    # double-decide: second call hits the pending-only transition
    r = http.post("/rpc/decide_command", headers=mgr,
                  json={"p_id": "CMD-1", "p_decision": "approved"})
    assert r.status_code == 200
    r = http.post("/rpc/decide_command", headers=mgr,
                  json={"p_id": "CMD-1", "p_decision": "rejected"})
    assert r.status_code == 400
    assert "command not pending" in r.json()["message"]
    # unknown id
    r = http.post("/rpc/decide_command", headers=mgr,
                  json={"p_id": "NOPE-404", "p_decision": "approved"})
    assert r.status_code == 400
    assert "unknown command" in r.json()["message"]
    # invalid decision literal
    r = http.post("/rpc/decide_command", headers=mgr,
                  json={"p_id": "CMD-2", "p_decision": "maybe"})
    assert r.status_code == 400
    assert "invalid decision" in r.json()["message"]


# ---------------------------------------------------------------- service key

def test_service_key_bypasses_everything(fake: FakePostgREST,
                                         http: httpx.Client) -> None:
    """The writer path: service key = RLS bypass, like Supabase service_role."""
    # cross-site read
    assert len(http.get("/robots", headers=SVC).json()) == 3
    # arbitrary insert into a read-only-for-humans table
    r = http.post("/robots", json={"id": "AMR-99", "status": "active",
                                   "battery": 100.0}, headers=SVC)
    assert r.status_code == 201
    # arbitrary column PATCH (denied for authenticated above)
    r = http.patch("/alerts?id=eq.AL-1", json={"msg": "updated by writer",
                                               "sev": "warn"}, headers=SVC)
    assert r.status_code == 204
    with fake.lock:
        assert any(rb["id"] == "AMR-99" for rb in fake.tables["robots"])
        assert next(a for a in fake.tables["alerts"]
                    if a["id"] == "AL-1")["msg"] == "updated by writer"
    # the key also works via the apikey header alone (writer clients)
    assert len(http.get("/robots",
                        headers={"apikey": SERVICE_KEY}).json()) == 4


# ---------------------------------------------------------------- academy RPCs

def test_save_progress_upserts_per_user(fake: FakePostgREST,
                                        http: httpx.Client) -> None:
    op = bearer(OPERATOR, "operator")
    r = http.post("/rpc/save_progress", headers=op,
                  json={"p_pack": "physical-ai-101",
                        "p_data": {"module": 1, "score": 40}})
    assert r.status_code == 200
    assert r.json()["pack_id"] == "physical-ai-101"
    # upsert: same (user, pack) is replaced, not duplicated
    r = http.post("/rpc/save_progress", headers=op,
                  json={"p_pack": "physical-ai-101",
                        "p_data": {"module": 3, "score": 90}})
    assert r.status_code == 200
    assert r.json()["data"] == {"module": 3, "score": 90}
    with fake.lock:
        assert len(fake.progress[OPERATOR]) == 1
        assert fake.progress[OPERATOR]["physical-ai-101"]["data"]["module"] == 3
    # another user's progress is a separate bucket
    r = http.post("/rpc/save_progress", headers=bearer(ENGINEER, "engineer"),
                  json={"p_pack": "physical-ai-101", "p_data": {"module": 2}})
    assert r.status_code == 200
    with fake.lock:
        assert fake.progress[OPERATOR]["physical-ai-101"]["data"]["module"] == 3
    # anon: not authenticated; missing pack: 400
    assert http.post("/rpc/save_progress", headers=ANON,
                     json={"p_pack": "x", "p_data": {}}).status_code == 401
    r = http.post("/rpc/save_progress", headers=op, json={"p_data": {}})
    assert r.status_code == 400


def test_issue_certificate_and_duplicate_code(fake: FakePostgREST,
                                              http: httpx.Client) -> None:
    op = bearer(OPERATOR, "operator")
    r = http.post("/rpc/issue_certificate", headers=op,
                  json={"p_track": "checkride-ops", "p_score": 92,
                        "p_code": "YF-2026-0001"})
    assert r.status_code == 200
    row = r.json()
    assert row["verification_code"] == "YF-2026-0001" and row["score"] == 92
    # duplicate verification code -> 409-style unique violation
    r = http.post("/rpc/issue_certificate", headers=bearer(ENGINEER, "engineer"),
                  json={"p_track": "checkride-ops", "p_score": 88,
                        "p_code": "YF-2026-0001"})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "23505" and "already exists" in body["message"]
    with fake.lock:
        assert len(fake.certificates) == 1
    # score bounds + anon
    assert http.post("/rpc/issue_certificate", headers=op,
                     json={"p_track": "t", "p_score": 101,
                           "p_code": "YF-X"}).status_code == 400
    assert http.post("/rpc/issue_certificate", headers=ANON,
                     json={"p_track": "t", "p_score": 50,
                           "p_code": "YF-Y"}).status_code == 401


# ---------------------------------------------------------------- misc

def test_unknown_rpc_404(http: httpx.Client) -> None:
    r = http.post("/rpc/drop_all_tables", headers=SVC, json={})
    assert r.status_code == 404
    assert "drop_all_tables" in r.json()["message"]


def test_non_rbac_mode_rpcs_skip_role_checks() -> None:
    """Back-compat: default mode ignores auth, RPCs work without a session."""
    f = FakePostgREST({"commands": [
        {"id": "CMD-D", "robot": "AMR-01", "action": "pause",
         "status": "pending", "requested_by": "demo"}]})
    base = f.start()
    try:
        with httpx.Client(base_url=f"{base}/rest/v1", timeout=5.0) as c:
            # demo-open reads/writes: no headers at all, exactly as before
            assert len(c.get("/commands").json()) == 1
            assert c.patch("/alerts?id=eq.none",
                           json={"ack": True}).status_code == 204
            # RPC with no auth: allowed, state machine still enforced
            r = c.post("/rpc/decide_command",
                       json={"p_id": "CMD-D", "p_decision": "approved"})
            assert r.status_code == 200
            assert r.json()["status"] == "approved"
            r = c.post("/rpc/decide_command",
                       json={"p_id": "CMD-D", "p_decision": "approved"})
            assert r.status_code == 400  # not pending any more
            assert c.post("/rpc/save_progress",
                          json={"p_pack": "demo", "p_data": {"m": 1}}
                          ).status_code == 200
    finally:
        f.stop()


# -------------------------------------------- demo sandbox scope (0017)

DEMO_TOKEN_HEADER = "x-yf-demo-token"


def _sandbox(fake: FakePostgREST) -> tuple[str, str]:
    """Mint a live demo sandbox; returns (token, site_id)."""
    with httpx.Client(base_url=f"{fake.base_url}/rest/v1", timeout=5.0) as c:
        r = c.post("/rpc/demo_mint_session", headers=ANON,
                   json={"p_seed_robots": 2})
        r.raise_for_status()
        body = r.json()
    return body["token"], body["site_id"]


def _demo(token: str) -> dict[str, str]:
    return {**ANON, DEMO_TOKEN_HEADER: token}


def test_demo_cannot_queue_a_command_for_a_real_robot(
        fake: FakePostgREST, http: httpx.Client) -> None:
    """supabase/0017_demo_command_scope.sql, end to end over HTTP.

    Before 0017 an anonymous sandbox visitor could POST a ``commands``
    row naming ``AMR-01`` — a REAL robot at a REAL site — with
    ``status='approved'`` already set, because 0009's insert policy
    checked ``site_id`` and nothing else and ``commands.robot_id`` is a
    bare ``text`` column (0002). Reproduced against PostgreSQL 16 before
    the fix; refused after it.
    """
    token, site = _sandbox(fake)
    forged = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
              "robot_id": "AMR-01", "cmd": "estop", "params": {},
              "status": "approved", "requested_by": "demo-visitor",
              "site_id": site}
    r = http.post("/commands", headers=_demo(token), json=[forged])
    assert r.status_code == 403
    assert r.json()["code"] == "42501"
    assert not [c for c in fake.tables["commands"] if c["id"] == forged["id"]]

    # ...and a *pending* one naming the same real robot is refused too.
    r = http.post("/commands", headers=_demo(token),
                  json=[{**forged, "id": "bbbb", "status": "pending"}])
    assert r.status_code == 403

    # ...and a pre-approved one for its OWN robot: the approval gate
    # cannot be satisfied at insert time either.
    r = http.post("/commands", headers=_demo(token),
                  json=[{**forged, "id": "cccc",
                         "robot_id": f"{site}-R01"}])
    assert r.status_code == 403


def test_demo_may_still_request_and_decide_its_own_command(
        fake: FakePostgREST, http: httpx.Client) -> None:
    """The demo loop the sandbox exists to show still works."""
    token, site = _sandbox(fake)
    r = http.post("/commands", headers=_demo(token),
                  json=[{"id": "dddd", "robot_id": f"{site}-R01",
                         "cmd": "charge", "params": {}, "status": "pending",
                         "requested_by": "demo-visitor", "site_id": site}])
    assert r.status_code == 201
    r = http.patch("/commands?id=eq.dddd", headers=_demo(token),
                   json={"status": "approved", "decided_by": "demo-visitor"})
    assert r.status_code == 204
    row = next(c for c in fake.tables["commands"] if c["id"] == "dddd")
    assert (row["status"], row["site_id"]) == ("approved", site)


def test_demo_cannot_forge_telemetry_or_findings_for_a_real_robot(
        fake: FakePostgREST, http: httpx.Client) -> None:
    """The same class of gap on the other robot-naming tables.

    Forged telemetry matters because ``detector/yantradetect``'s
    ``fetch_telemetry`` has no site filter; a forged OPEN maintenance
    finding matters more, because the detector seeds its dedup set from
    open findings and one could SUPPRESS a genuine finding for that
    robot.
    """
    token, site = _sandbox(fake)
    bad_sample = http.post(
        "/robot_telemetry", headers=_demo(token),
        json=[{"robot_id": "AMR-01", "ts": "2026-09-05T10:00:00Z",
               "battery": 1, "status": "fault", "pos": [0, 0],
               "site_id": site}])
    assert bad_sample.status_code == 403
    assert not [t for t in fake.tables["robot_telemetry"]
                if t["robot_id"] == "AMR-01"]

    bad_finding = http.post(
        "/maintenance_findings", headers=_demo(token),
        json=[{"id": "MF-FORGED", "robot_id": "AMR-01",
               "component": "battery", "finding": "forged", "state": "Open",
               "site_id": site}])
    assert bad_finding.status_code == 403

    # Its own robots are still writable — the sandbox stays usable.
    ok = http.post("/robot_telemetry", headers=_demo(token),
                   json=[{"robot_id": f"{site}-R01",
                          "ts": "2026-09-05T10:00:00Z", "battery": 55,
                          "status": "active", "pos": [1, 2],
                          "site_id": site}])
    assert ok.status_code == 201


def test_demo_upsert_cannot_merge_onto_a_real_robots_primary_key(
        fake: FakePostgREST, http: httpx.Client) -> None:
    """``insert ... on conflict do update`` runs the UPDATE policy's USING
    clause against the EXISTING row, so a demo token cannot hijack a real
    robot by aiming an upsert at its id. Verified against PostgreSQL 16:
    ``ERROR: new row violates row-level security policy (USING expression)
    for table "robots"``. ``do nothing`` silently skips instead.
    """
    token, site = _sandbox(fake)
    before = dict(next(r for r in fake.tables["robots"] if r["id"] == "AMR-01"))
    merged = http.post(
        "/robots?on_conflict=id",
        headers={**_demo(token),
                 "Prefer": "return=minimal,resolution=merge-duplicates"},
        json=[{"id": "AMR-01", "vendor": "EVIL", "status": "estop",
               "site_id": site}])
    assert merged.status_code == 403
    assert merged.json()["code"] == "42501"

    ignored = http.post(
        "/robots?on_conflict=id",
        headers={**_demo(token),
                 "Prefer": "return=minimal,resolution=ignore-duplicates"},
        json=[{"id": "AMR-01", "vendor": "EVIL", "site_id": site}])
    assert ignored.status_code == 201        # PostgreSQL: DO NOTHING
    assert next(r for r in fake.tables["robots"]
                if r["id"] == "AMR-01") == before
