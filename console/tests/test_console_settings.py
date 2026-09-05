"""v0.13 admin settings panel browser tests (console/index.html).

Runs the console in headless Chromium against ``FakePostgREST(rbac=True)``
— the supabase/0007_rbac.sql + 0008_app_settings.sql emulation from
``e2e/fakerest.py`` — extended IN THIS FILE ONLY with a GoTrue-style
``POST /auth/v1/token`` endpoint. Per this repo's stated convention ("the
fake/CORS plumbing is copied, not imported" — see test_polish.py's own
docstring and test_console_rbac.py), the AuthFake/CORS scaffolding is
copied here rather than imported from test_console_rbac.py, with an
``admin@yf.test`` user added to a locally-defined ``USERS`` dict.

Covered scenarios (design doc section 5c):
  * operator/manager login -> #nav-settings stays hidden; direct
    navigation (go('settings') / rSettings()) never fires
    rpc/admin_list_settings and only ever shows the "Admin role required"
    placeholder;
  * admin login -> #nav-settings visible; the view renders 8 rows, all
    "not set" against a fresh fake backend;
  * save flow -> the fake's admin_set_setting RPC receives the typed
    value, the re-rendered row shows "configured · ****<last4>", and the
    full secret never appears anywhere in the rendered view (the "never
    echoes a saved secret back in full" requirement);
  * clear flow -> a configured row reverts to "not set";
  * 403 handling -> a simulated mid-session role downgrade surfaces a
    toast and leaves the row's displayed state unchanged (no false
    "saved").
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

CONSOLE_DIR = Path(__file__).resolve().parent.parent          # console/
REPO_ROOT = CONSOLE_DIR.parent                                # repo root
FAKEREST_PY = REPO_ROOT / "e2e" / "fakerest.py"

pytest.importorskip("playwright.sync_api")
pytest.importorskip("pytest_playwright")
from playwright.sync_api import Page, expect  # noqa: E402

# --- import e2e/fakerest.py by path (e2e/ is not a package) -----------------
_spec = importlib.util.spec_from_file_location("yf_fakerest_settings", FAKEREST_PY)
assert _spec and _spec.loader, f"cannot load {FAKEREST_PY}"
fakerest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakerest)

SERVICE_KEY = "sk-console-settings-test"

#: email -> (password, yf_role, site) known to the auth stub
USERS: dict[str, tuple[str, str, str]] = {
    "operator@yf.test": ("op-pass", "operator", "BLR-DC1"),
    "manager@yf.test": ("mgr-pass", "manager", "BLR-DC1"),
    "admin@yf.test": ("admin-pass", "admin", "BLR-DC1"),
}


class _CORSHandler(fakerest._Handler):
    """The console page is served from another origin — answer preflights."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "apikey, authorization, content-type, prefer")
        self.send_header("Access-Control-Max-Age", "600")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class AuthFake(fakerest.FakePostgREST):
    """rbac=True fakerest + a GoTrue-style /auth/v1/token endpoint +
    a test-only hook to simulate a role-downgraded 403 on admin_set_setting
    mid-session (real 0007/0008 posture: the RPC itself re-checks
    yf_has_role('admin') on every call, so a role change between two saves
    is enforced server-side, not just at login time)."""

    def __init__(self) -> None:
        super().__init__(rbac=True, service_key=SERVICE_KEY, anon_key="test")
        self.users = dict(USERS)
        self.auth_calls: list[tuple[str, dict]] = []
        self.refresh_tokens: dict[str, str] = {}
        self._rt_serial = 0
        #: test hook only — real 0008 has no such flag; flips one
        #: admin_set_setting call to 403 to simulate a role downgrade.
        self.force_settings_403 = False

    def issue_tokens(self, email: str) -> dict[str, Any]:
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

    def rpc_admin_set_setting(self, ident: Any, args: dict) -> tuple[int, Any]:
        if self.force_settings_403:
            return 403, {"message": "admin_set_setting: requires admin role",
                        "code": "42501"}
        return super().rpc_admin_set_setting(ident, args)

    def start(self) -> str:  # same shape as the parent, auth-aware handler
        store = self

        class Handler(_AuthCORSHandler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="settings-fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


class _AuthCORSHandler(_CORSHandler):
    """CORS fakerest handler + POST /auth/v1/token (password grant only —
    this suite never needs signup/refresh)."""

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
            return self._reply(400, {"error": "unsupported_grant_type"})


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def rbac_fake():
    """A freshly started rbac-mode fake (with auth stub) per test."""
    f = AuthFake()
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
    expect(modal).to_be_visible(timeout=15_000)
    page.locator("#si-email").fill(email)
    page.locator("#si-pass").fill(password)
    page.locator("#si-submit").click()
    expect(modal).to_be_hidden(timeout=15_000)
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)


def admin_list_calls(store: AuthFake) -> list[tuple[str, str]]:
    with store.lock:
        return [(m, p) for m, p in store.requests
               if p.endswith("/rpc/admin_list_settings")]


def admin_set_calls(store: AuthFake) -> list[tuple[str, str]]:
    with store.lock:
        return [(m, p) for m, p in store.requests
               if p.endswith("/rpc/admin_set_setting")]


# --------------------------------------------------------------------- tests

@pytest.mark.parametrize("email,password", [
    ("operator@yf.test", "op-pass"),
    ("manager@yf.test", "mgr-pass"),
])
def test_non_admin_nav_hidden_and_direct_nav_blocked(
        page: Page, rbac_url: str, rbac_fake, email: str, password: str) -> None:
    """Operator/manager: #nav-settings stays hidden; driving go('settings')
    directly never renders the settings view as active and never fires
    admin_list_settings; calling rSettings() directly (bypassing the go()
    guard entirely) still only ever shows the "Admin role required"
    placeholder, with the RPC still never fired — the real gate is the
    RPC's own yf_has_role('admin') check, not the UI."""
    _, store = rbac_fake
    sign_in(page, rbac_url, email, password)
    expect(page.locator("#nav-settings")).to_be_hidden()

    page.evaluate("go('settings')")
    # UI-level defense in depth: go() redirects a non-admin away from
    # 'settings' rather than switching the active view to it.
    expect(page.locator("#v-overview")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#v-settings")).not_to_have_class(re.compile(r"\bon\b"))
    assert admin_list_calls(store) == [], "non-admin go('settings') must never call the RPC"

    # Even called directly (bypassing go() entirely) rSettings() gates on
    # its own — belt-and-suspenders, since the RPC is the real boundary.
    page.evaluate("rSettings()")
    expect(page.locator("#v-settings")).to_contain_text("Admin role required")
    assert admin_list_calls(store) == [], \
        "rSettings() must never call admin_list_settings for a non-admin"


def test_admin_sees_nav_and_settings_view_shows_all_unconfigured(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Admin login: #nav-settings visible; clicking it renders 8 rows, all
    "not set" against a fresh fake backend, via the real RPC."""
    _, store = rbac_fake
    sign_in(page, rbac_url, "admin@yf.test", "admin-pass")
    expect(page.locator("#nav-settings")).to_be_visible()
    page.locator("#nav-settings").click()
    view = page.locator("#v-settings")
    expect(view.locator("h1")).to_have_text("Settings", timeout=15_000)
    expect(view.locator("input[type=password]")).to_have_count(8)
    for key, label, *_ in [
        ("GEMINI_API_KEY", "Gemini API key"),
        ("SARATHI_TOKEN", "Sarathi bearer token"),
        ("WEBHOOK_URL", "Webhook URL"),
        ("YANTRA_WEBHOOK_SECRET", "Webhook signing secret"),
        ("TWILIO_SID", "Twilio Account SID"),
        ("TWILIO_TOKEN", "Twilio Auth Token"),
        ("TWILIO_FROM", "Twilio From number"),
        ("TWILIO_TO", "Twilio To number"),
    ]:
        row = view.locator(f"#setting-row-{key}")
        expect(row).to_contain_text(label)
        expect(row).to_contain_text("not set")
        expect(row.locator("button", has_text="Clear")).to_have_count(0)
    assert len(admin_list_calls(store)) >= 1


def test_save_flow_masks_the_secret_and_never_echoes_it_in_full(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Typing a value and clicking Save persists it through
    admin_set_setting; the row flips to "configured · ****<last4>" and —
    the key assertion for the "never echoes a saved secret back in full"
    requirement — the full secret text never appears anywhere on the page,
    while its last-4-char masked suffix does."""
    _, store = rbac_fake
    sign_in(page, rbac_url, "admin@yf.test", "admin-pass")
    page.locator("#nav-settings").click()
    expect(page.locator("#v-settings input[type=password]")).to_have_count(
        8, timeout=15_000)

    secret = "sk-super-secret-gemini-value-ab12"
    page.locator("#set-GEMINI_API_KEY").fill(secret)
    page.locator("#setting-row-GEMINI_API_KEY button", has_text="Save").click()

    status = page.locator("#setting-status-GEMINI_API_KEY")
    expect(status).to_contain_text("configured", timeout=15_000)
    expect(status).to_contain_text("ab12")
    expect(page.locator("#v-settings")).not_to_contain_text(secret)
    expect(page.locator("#setting-row-GEMINI_API_KEY button", has_text="Clear")) \
        .to_have_count(1)

    with store.lock:
        assert store.settings["GEMINI_API_KEY"]["value"] == secret
        assert store.settings["GEMINI_API_KEY"]["updated_by"] == "admin@yf.test"
    assert len(admin_set_calls(store)) == 1


def test_clear_flow_reverts_a_configured_row_to_not_set(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """Clear on a configured row deletes it server-side and the row goes
    back to "not set — using deploy-time env var, if any"."""
    _, store = rbac_fake
    with store.lock:
        store.settings["WEBHOOK_URL"] = {
            "value": "https://hooks.example/T00/B00/xxxxxxxxxxxxwxyz",
            "updated_at": "2026-01-01T00:00:00+00:00", "updated_by": "admin@yf.test",
        }
    sign_in(page, rbac_url, "admin@yf.test", "admin-pass")
    page.locator("#nav-settings").click()
    row = page.locator("#setting-row-WEBHOOK_URL")
    expect(row).to_contain_text("configured", timeout=15_000)
    row.locator("button", has_text="Clear").click()
    status = page.locator("#setting-status-WEBHOOK_URL")
    expect(status).to_contain_text("not set", timeout=15_000)
    expect(row.locator("button", has_text="Clear")).to_have_count(0)
    with store.lock:
        assert "WEBHOOK_URL" not in store.settings


def test_403_on_save_shows_toast_and_leaves_row_unchanged(
        page: Page, rbac_url: str, rbac_fake) -> None:
    """A simulated mid-session role downgrade (admin_set_setting returns
    403) surfaces a toast and must NOT flip the row to "configured" — no
    false "saved" state."""
    _, store = rbac_fake
    sign_in(page, rbac_url, "admin@yf.test", "admin-pass")
    page.locator("#nav-settings").click()
    expect(page.locator("#v-settings input[type=password]")).to_have_count(
        8, timeout=15_000)

    with store.lock:
        store.force_settings_403 = True
    page.locator("#set-TWILIO_SID").fill("AC-should-not-save")
    page.locator("#setting-row-TWILIO_SID button", has_text="Save").click()

    expect(page.locator(".toast", has_text="admin role")).to_be_visible(timeout=15_000)
    expect(page.locator("#setting-status-TWILIO_SID")).to_contain_text(
        "not set", timeout=5_000)
    with store.lock:
        assert "TWILIO_SID" not in store.settings
