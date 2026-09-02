"""RBAC + real-login browser tests for the console (v0.11 YFAuth module).

Runs console/index.html against ``FakePostgREST(rbac=True)`` — the
supabase/0007_rbac.sql policy emulation from e2e/fakerest.py — extended
IN THIS FILE ONLY with a GoTrue-style ``POST /auth/v1/token`` endpoint
(fakerest itself implements no /auth/v1 routes; the stub issues access
tokens built with ``fakerest.make_test_jwt`` so the rbac identity
resolution accepts them, plus opaque refresh tokens it remembers).

Covered flows:
  * anon load       -> sign-in modal auto-prompts, chip = 'Sign in required',
                       no backend data rendered, no page errors / loops;
  * bad password    -> GoTrue error surfaced in the modal, stays anon;
  * operator login  -> data loads, ack PATCH succeeds under RBAC, the
                       Approve/Reject buttons are HIDDEN, and a queued
                       command carries requested_by=<the operator's email>;
  * manager login   -> Approve visible, decision goes through the
                       decide_command RPC (never the legacy PATCH);
  * expired access token -> one grant_type=refresh_token round-trip, then
                       the original request is retried and succeeds;
  * session persistence across reload; sign-out back to the anon state.

The default (rbac=False) fakerest mode — and every pre-existing test in
test_console.py — is untouched.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

import test_console as tc

fakerest = tc.fakerest
pytestmark = tc.pytestmark  # skip the module when no Chromium is preinstalled

pytest.importorskip("playwright.sync_api")
pytest.importorskip("pytest_playwright")
from playwright.sync_api import Page, expect  # noqa: E402

SERVICE_KEY = "sk-console-test"

#: email -> (password, yf_role, site) known to the auth stub
USERS: dict[str, tuple[str, str, str]] = {
    "operator@yf.test": ("op-pass", "operator", "BLR-DC1"),
    "manager@yf.test": ("mgr-pass", "manager", "BLR-DC1"),
}


class AuthFake(tc.ConsoleFake):
    """ConsoleFake in rbac mode + a GoTrue-style /auth/v1/token endpoint."""

    def __init__(self) -> None:
        super().__init__(rbac=True, service_key=SERVICE_KEY, anon_key="test")
        self.users = dict(USERS)
        self.auth_calls: list[tuple[str, dict]] = []  # (grant_type, body)
        self.refresh_tokens: dict[str, str] = {}      # refresh token -> email
        self._rt_serial = 0

    def issue_tokens(self, email: str) -> dict[str, Any]:
        """A GoTrue-shaped token response for a known user."""
        _, role, site = self.users[email]
        self._rt_serial += 1
        rt = f"rt-{self._rt_serial}-{email}"
        self.refresh_tokens[rt] = email
        return {
            "access_token": fakerest.make_test_jwt(email, role, site),
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": rt,
            "user": {"email": email},
        }

    def start(self) -> str:  # same shape as the parent, auth-aware handler
        store = self

        class Handler(_AuthCORSHandler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="rbac-fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


class _AuthCORSHandler(tc._CORSHandler):
    """CORS fakerest handler + POST /auth/v1/token (password / refresh)."""

    fake: AuthFake

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        parts = urlsplit(self.path)
        if parts.path == "/auth/v1/token":
            return self._auth_token(parts.query)
        super().do_POST()

    def _auth_token(self, query: str) -> None:
        grant = dict(parse_qsl(query)).get("grant_type")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        with self.fake.lock:
            self.fake.auth_calls.append((grant or "", dict(body)))
            if grant == "password":
                email, password = body.get("email"), body.get("password")
                user = self.fake.users.get(email)
                if user is None or user[0] != password:
                    return self._reply(400, {
                        "error": "invalid_grant",
                        "error_description": "Invalid login credentials"})
                return self._reply(200, self.fake.issue_tokens(email))
            if grant == "refresh_token":
                email = self.fake.refresh_tokens.get(
                    body.get("refresh_token") or "")
                if email is None:
                    return self._reply(400, {
                        "error": "invalid_grant",
                        "error_description": "Invalid Refresh Token"})
                return self._reply(200, self.fake.issue_tokens(email))
            return self._reply(400, {"error": "unsupported_grant_type"})


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def rbac_fake():
    """A freshly seeded rbac-mode fake (with auth stub) per test."""
    f = AuthFake()
    tc._seed(f)
    base = f.start()
    yield base, f
    f.stop()


@pytest.fixture()
def rbac_url(console_server: str, rbac_fake) -> str:
    base, _ = rbac_fake
    return (f"{console_server}/index.html"
            f"?supa={base}&key=test&site=BLR-DC1")


@pytest.fixture(autouse=True)
def _tour_already_seen(page: Page):
    """Keep the first-run tour out of the way (same as test_console.py)."""
    page.add_init_script(
        "try{localStorage.setItem('yf_tour_done','1')}catch(e){}")


# ------------------------------------------------------------------- helpers

def sign_in(page: Page, url: str, email: str, password: str) -> None:
    """Open the console, wait for the auto sign-in prompt, authenticate."""
    page.goto(url)
    modal = page.locator("#signin")
    expect(modal).to_be_visible(timeout=15_000)  # auto-prompt after the 401
    page.locator("#si-email").fill(email)
    page.locator("#si-pass").fill(password)
    page.locator("#si-submit").click()
    expect(modal).to_be_hidden(timeout=15_000)
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)


def seed_pending_command(store: AuthFake, cid: str, robot: str, cmd: str) -> None:
    with store.lock:
        store.tables["commands"].append({
            "id": cid, "robot_id": robot, "cmd": cmd, "status": "pending",
            "requested_by": "operator@yf.test", "site_id": "BLR-DC1",
            "created_at": tc._now_iso(),
        })


# --------------------------------------------------------------------- tests

def test_anon_load_prompts_signin_and_shows_no_data(
        page: Page, rbac_url: str) -> None:
    """Anon against an RBAC backend: sign-in modal auto-opens, the cloud chip
    says sign-in is required, and no backend row ever renders — gracefully,
    with zero uncaught errors (no 401 crash loops)."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(rbac_url)
    modal = page.locator("#signin")
    expect(modal).to_be_visible(timeout=15_000)
    expect(page.locator("#cloud-lbl")).to_contain_text(
        "Sign in required", timeout=15_000)
    # the backend refused anon => the 'continue in demo mode' link is hidden
    expect(page.locator("#si-demo")).to_be_hidden()
    # dismiss the modal and verify none of the seeded backend data leaked in
    page.locator(".si-box h2 button").click()
    expect(modal).to_be_hidden()
    expect(page.locator("#btn-signin")).to_be_visible()  # header entry point
    expect(page.locator("#ov-alerts")).not_to_contain_text(tc.SEED_ALERT_MSG)
    page.locator("#nav button[data-v='liveops']").click()
    expect(page.locator("#fl-list")).to_contain_text("AMR-01", timeout=15_000)
    expect(page.locator("#fl-list")).not_to_contain_text("TestBot X1")
    assert errors == [], f"anon RBAC load raised page errors: {errors}"


def test_bad_password_shows_error_and_stays_anon(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """A wrong password surfaces the GoTrue error inside the modal."""
    _, store = rbac_fake
    page.goto(rbac_url)
    expect(page.locator("#signin")).to_be_visible(timeout=15_000)
    page.locator("#si-email").fill("operator@yf.test")
    page.locator("#si-pass").fill("wrong-password")
    page.locator("#si-submit").click()
    expect(page.locator("#si-err")).to_contain_text(
        "Invalid login credentials", timeout=15_000)
    expect(page.locator("#signin")).to_be_visible()  # still on the modal
    assert page.evaluate("YFAuth.state.jwt") is None
    with store.lock:
        grants = [g for g, _ in store.auth_calls]
    assert grants == ["password"]


def test_operator_signin_loads_data_and_ack_works(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Operator login: header identity + role badge, backend rows render,
    and Ack PATCHes alerts.ack under the RBAC policies (JWT-authorized)."""
    _, store = rbac_fake
    sign_in(page, rbac_url, "operator@yf.test", "op-pass")
    expect(page.locator("#auth-user")).to_contain_text("operator@yf.test")
    expect(page.locator("#auth-role")).to_have_text("operator")
    expect(page.locator("#btn-signout")).to_be_visible()
    page.locator("#nav button[data-v='liveops']").click()
    expect(page.locator("#fl-list")).to_contain_text("TestBot X1",
                                                     timeout=15_000)
    page.locator("#nav button[data-v='overview']").click()
    alert = page.locator("#ov-alerts .al", has_text=tc.SEED_ALERT_MSG)
    expect(alert).to_be_visible(timeout=15_000)
    alert.locator("button.ack").click()
    tc.wait_until(
        lambda: any(r["id"] == "AL-T1" and r.get("ack") is True
                    for r in tc.rows(store, "alerts")),
        "alerts row AL-T1 to be acked through the RBAC alerts.ack policy")
    with store.lock:
        grants = [g for g, _ in store.auth_calls]
    assert "password" in grants


def test_operator_approve_buttons_hidden_and_command_carries_email(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Operators see pending approvals but NOT the Approve/Reject buttons;
    a command they queue is inserted as requested_by=<their email> (which
    the fake's 0007 WITH CHECK stand-in enforces server-side too)."""
    _, store = rbac_fake
    seed_pending_command(store, "CMD-R1", "AMR-01", "pause")
    sign_in(page, rbac_url, "operator@yf.test", "op-pass")
    card = page.locator("#ov-approvals")
    expect(card).to_contain_text("Pending approvals", timeout=15_000)
    expect(card).to_contain_text("PAUSE")
    expect(card.locator("button", has_text="Approve")).to_have_count(0)
    expect(card.locator("button", has_text="Reject")).to_have_count(0)
    expect(card).to_contain_text("manager+ approval")
    page.evaluate("cmdRobot('AMR-02','charge')")
    tc.wait_until(
        lambda: any(r.get("robot_id") == "AMR-02" and r.get("cmd") == "charge"
                    and r.get("status") == "pending"
                    and r.get("requested_by") == "operator@yf.test"
                    for r in tc.rows(store, "commands")),
        "a pending AMR-02/charge command requested_by operator@yf.test")


def test_manager_approves_via_decide_command_rpc(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Managers get the Approve button; clicking it decides via the
    decide_command RPC (never the legacy PATCH) and updates the row."""
    _, store = rbac_fake
    seed_pending_command(store, "CMD-R2", "AMR-01", "estop")
    sign_in(page, rbac_url, "manager@yf.test", "mgr-pass")
    expect(page.locator("#auth-role")).to_have_text("manager")
    card = page.locator("#ov-approvals")
    expect(card).to_contain_text("ESTOP", timeout=15_000)
    card.locator("button", has_text="Approve").click()
    tc.wait_until(
        lambda: any(r.get("id") == "CMD-R2" and r.get("status") == "approved"
                    for r in tc.rows(store, "commands")),
        "commands row CMD-R2 to be approved via the RPC")
    row = next(r for r in tc.rows(store, "commands") if r.get("id") == "CMD-R2")
    assert row["decided_by"] == "manager@yf.test"
    assert row.get("decided_at")
    with store.lock:
        reqs = list(store.requests)
    assert any(m == "POST" and p.endswith("/rpc/decide_command")
               for m, p in reqs), f"no decide_command RPC call seen: {reqs}"
    assert not any(m == "PATCH" and "/commands" in p for m, p in reqs), \
        "authenticated decision must not use the legacy commands PATCH"


def test_expired_token_refreshes_once_and_retries(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """A 401 with a dead access token triggers one grant_type=refresh_token
    round-trip; the original request is then retried and succeeds."""
    _, store = rbac_fake
    sign_in(page, rbac_url, "operator@yf.test", "op-pass")
    alert = page.locator("#ov-alerts .al", has_text=tc.SEED_ALERT_MSG)
    expect(alert).to_be_visible(timeout=15_000)
    # kill the access token in memory — the stored refresh token stays valid
    page.evaluate("YFAuth.state.jwt = 'yf-broken.token'")
    alert.locator("button.ack").click()
    tc.wait_until(
        lambda: any(r["id"] == "AL-T1" and r.get("ack") is True
                    for r in tc.rows(store, "alerts")),
        "ack to succeed after the refresh-token retry")
    with store.lock:
        grants = [g for g, _ in store.auth_calls]
    assert "refresh_token" in grants, f"no refresh grant seen: {grants}"
    # the console swapped in the freshly minted access token
    assert page.evaluate("YFAuth.state.jwt").startswith("yf-test.")
    # and stays signed in (no forced logout / re-prompt)
    expect(page.locator("#auth-user")).to_contain_text("operator@yf.test")
    expect(page.locator("#signin")).to_be_hidden()


def test_session_persists_across_reload(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """localStorage (yf_auth_v1) restores the session: after a reload the
    console is signed in immediately — no modal — and data still loads."""
    sign_in(page, rbac_url, "operator@yf.test", "op-pass")
    page.reload()
    expect(page.locator("#auth-user")).to_contain_text("operator@yf.test",
                                                       timeout=15_000)
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)
    expect(page.locator("#signin")).to_be_hidden()
    page.locator("#nav button[data-v='liveops']").click()
    expect(page.locator("#fl-list")).to_contain_text("TestBot X1",
                                                     timeout=15_000)


def test_sign_out_returns_to_anon_state(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Sign out clears the session + localStorage; the console falls back to
    the anon 'Sign in required' state and a reload prompts again."""
    sign_in(page, rbac_url, "operator@yf.test", "op-pass")
    page.locator("#btn-signout").click()
    expect(page.locator("#btn-signin")).to_be_visible()
    expect(page.locator("#cloud-lbl")).to_contain_text("Sign in required",
                                                       timeout=15_000)
    assert page.evaluate("YFAuth.state.jwt") is None
    assert page.evaluate("YFAuth.state.email") is None
    assert page.evaluate("localStorage.getItem('yf_auth_v1')") is None
    # a fresh load of the same URL is anonymous again -> auto-prompt
    page.reload()
    expect(page.locator("#signin")).to_be_visible(timeout=15_000)
