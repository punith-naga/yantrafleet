"""Shared fixtures for the YantraFleet Academy browser tests.

Mirrors the console suite's fixture style (console/tests/test_console.py):
the single-file academy app runs in headless Chromium (Python Playwright)
against the in-process fake PostgREST server from ``e2e/fakerest.py``:

  fakerest (ephemeral port, CORS-wrapped)   <--fetch--  academy page
  http.server serving academy/ (ephemeral)  -->  index.html?supa=...&key=test

The needed fixture code is COPIED here (per repo convention the academy
tests do not import the console tests); only the shared ``e2e/fakerest.py``
module is loaded by path.

Requires: pip install playwright pytest-playwright. Browsers are preinstalled
under /opt/pw-browsers — the suite never downloads any.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import (BaseHTTPRequestHandler, SimpleHTTPRequestHandler,
                         ThreadingHTTPServer)
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest

# --- environment: use the preinstalled browsers, never download -------------
PW_BROWSERS = os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

ACADEMY_DIR = Path(__file__).resolve().parent.parent          # academy/
REPO_ROOT = ACADEMY_DIR.parent                                # repo root
FAKEREST_PY = REPO_ROOT / "e2e" / "fakerest.py"


def _find_chromium() -> str | None:
    """Locate the preinstalled Chromium binary (never triggers a download)."""
    base = Path(PW_BROWSERS)
    candidates: list[Path] = [base / "chromium"]
    if base.is_dir():
        candidates += sorted(base.glob("chromium-*"))
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
        for sub in ("chrome-linux/chrome", "chrome"):
            p = cand / sub
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


CHROMIUM_EXE = _find_chromium()

# applied to every test module in this directory
collect_ignore_glob: list[str] = []


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if CHROMIUM_EXE is None:
        skip = pytest.mark.skip(
            reason=f"no Chromium binary found under {PW_BROWSERS}")
        for item in items:
            item.add_marker(skip)


pytest.importorskip("playwright.sync_api")
pytest.importorskip("pytest_playwright")

# --- import e2e/fakerest.py by path (e2e/ is not a package) -----------------
_spec = importlib.util.spec_from_file_location("yf_fakerest_academy", FAKEREST_PY)
assert _spec and _spec.loader, f"cannot load {FAKEREST_PY}"
fakerest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakerest)


def now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


class AcademyFake(fakerest.FakePostgREST):
    """fakerest + CORS (the academy page is served from another origin)."""

    def start(self) -> str:  # same shape as the parent, different handler
        store = self

        class Handler(_CORSHandler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="academy-fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


class _CORSHandler(fakerest._Handler):
    """Answer Chromium's cross-origin preflights (real PostgREST does too)."""

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


# ------------------------------------------------------------- auth stub
#
# fakerest.py serves ONLY /rest/v1/* (both _route and _rpc_name reject any
# /auth path with a 404), so sign-in gets its own tiny GoTrue stand-in.
# The academy page is pointed at it with the ?auth=<base> URL param; data
# requests keep going to the fake PostgREST via ?supa=.

#: email -> credentials + claims for the stub. Passwords are test-only.
AUTH_USERS: dict[str, dict[str, str]] = {
    "priya@example.com": {"password": "fleet-pass-1", "yf_role": "operator",
                          "site": fakerest.DEFAULT_SITE_ID},
    "mgr@example.com": {"password": "fleet-pass-2", "yf_role": "manager",
                        "site": fakerest.DEFAULT_SITE_ID},
}


class AuthStub:
    """Minimal Supabase-Auth (GoTrue) stand-in on an ephemeral localhost port.

    ``POST /auth/v1/token?grant_type=password`` checks the stub's users and
    answers with a :func:`fakerest.make_test_jwt` access token (which
    ``FakePostgREST(rbac=True)`` accepts as an authenticated identity) plus a
    refresh token; ``grant_type=refresh_token`` rotates the access token.
    Wrong credentials get GoTrue's 400 ``invalid_grant`` shape.
    ``POST /auth/v1/signup`` registers a new user and answers per
    ``signup_mode``: 'session' (email confirmation OFF) returns tokens for
    an instant login, 'confirm' (the Supabase default) returns the bare
    user with no session; duplicates and short passwords get GoTrue's 422
    shapes. CORS-open, like the real endpoint.
    """

    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.requests: list[str] = []          # grant_type / 'signup' audit log
        self.users = {k: dict(v) for k, v in AUTH_USERS.items()}
        self.signup_mode = "session"           # 'session' | 'confirm'

    def start(self) -> str:
        stub = self

        class Handler(_AuthHandler):
            auth = stub

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="academy-authstub", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


class _AuthHandler(BaseHTTPRequestHandler):
    auth: AuthStub  # set by subclass in AuthStub.start

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass

    def _reply(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "apikey, authorization, content-type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "apikey, authorization, content-type")
        self.end_headers()

    def _token_reply(self, email: str) -> dict[str, Any]:
        user = self.auth.users[email]
        return {
            "access_token": fakerest.make_test_jwt(
                email, user["yf_role"], user["site"]),
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": f"rt-{email}",
            "user": {"email": email},
        }

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if parts.path == "/auth/v1/signup":
            return self._signup(body)
        if parts.path != "/auth/v1/token":
            return self._reply(404, {"error": "not_found"})
        grant = dict(parse_qsl(parts.query)).get("grant_type", "")
        self.auth.requests.append(grant)
        if grant == "password":
            email = str(body.get("email") or "")
            user = self.auth.users.get(email)
            if user is None or user["password"] != body.get("password"):
                return self._reply(400, {
                    "error": "invalid_grant",
                    "error_description": "Invalid login credentials"})
            return self._reply(200, self._token_reply(email))
        if grant == "refresh_token":
            rt = str(body.get("refresh_token") or "")
            email = rt.removeprefix("rt-")
            if not rt.startswith("rt-") or email not in self.auth.users:
                return self._reply(400, {
                    "error": "invalid_grant",
                    "error_description": "Invalid Refresh Token"})
            return self._reply(200, self._token_reply(email))
        return self._reply(400, {"error": "unsupported_grant_type"})

    def _signup(self, body: dict) -> None:
        """GoTrue-style /auth/v1/signup, outcome per ``auth.signup_mode``."""
        self.auth.requests.append("signup")
        email = str(body.get("email") or "")
        password = str(body.get("password") or "")
        if not email or not password:
            return self._reply(400, {
                "code": 400,
                "msg": "Signup requires a valid email and password"})
        if email in self.auth.users:                     # GoTrue's 422 shape
            return self._reply(422, {
                "code": 422, "msg": "User already registered"})
        if len(password) < 6:
            return self._reply(422, {
                "code": 422, "msg": "Password should be at least 6 characters"})
        self.auth.users[email] = {
            "password": password, "yf_role": "operator",
            "site": fakerest.DEFAULT_SITE_ID}
        if self.auth.signup_mode == "session":           # confirmation OFF
            return self._reply(200, self._token_reply(email))
        # confirmation ON (Supabase default): bare user, NO session
        return self._reply(200, {
            "id": f"user-{email}", "aud": "authenticated", "email": email,
            "confirmation_sent_at": now_iso()})


# ---------------------------------------------------------------- seed data

def seed(fake: AcademyFake, *, acked_alert: bool = False,
         pause_cmd: bool = False, crit_unacked: bool = True) -> None:
    """Baseline backend state; keyword args flip the practical/checkride
    verifies between fail and pass."""
    with fake.lock:
        fake.tables["alerts"] = [{
            "id": "AL-C1", "sev": "crit",
            "msg": "Seeded critical alert - robot fault in aisle B3",
            "src": "AMR-07", "tlabel": "14:02", "ack": not crit_unacked,
            "created_at": now_iso(),
        }]
        if acked_alert:
            fake.tables["alerts"].append({
                "id": "AL-A1", "sev": "warn",
                "msg": "Seeded acked alert - battery low handled",
                "src": "AMR-09", "tlabel": "14:11", "ack": True,
                "created_at": now_iso(),
            })
        fake.tables["commands"] = []
        if pause_cmd:
            fake.tables["commands"].append({
                "id": "CMD-P1", "robot_id": "AMR-02", "cmd": "pause",
                "status": "pending", "requested_by": "console:PN",
                "created_at": now_iso(),
            })


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict, playwright) -> dict:
    """Headless Chromium from /opt/pw-browsers; explicit path if needed."""
    args = dict(browser_type_launch_args)
    args["args"] = list(args.get("args", [])) + [
        "--no-sandbox", "--disable-dev-shm-usage"]
    try:
        auto = playwright.chromium.executable_path
        auto_ok = bool(auto) and Path(auto).exists()
    except Exception:
        auto_ok = False
    if not auto_ok:  # auto-detect failed -> launch the known binary directly
        args["executable_path"] = CHROMIUM_EXE
    return args


def _serve_dir(directory: Path, name: str):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Quiet, directory=str(directory)))
    t = threading.Thread(target=httpd.serve_forever, name=name, daemon=True)
    t.start()
    return httpd


@pytest.fixture(scope="session")
def academy_server():
    """Serve academy/ statically on an ephemeral port.

    NOTE: content/pack-physical-ai.json may or may not exist in the repo —
    a pack-specific test copies the app into a tmp dir with a known pack.
    """
    httpd = _serve_dir(ACADEMY_DIR, "academy-http")
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="session")
def bare_academy_server(tmp_path_factory):
    """Serve a copy of index.html WITHOUT any content/ directory, so the
    content fetch always 404s and the embedded pack is guaranteed."""
    root = tmp_path_factory.mktemp("academy-bare")
    root.joinpath("index.html").write_text(
        (ACADEMY_DIR / "index.html").read_text(encoding="utf-8"),
        encoding="utf-8")
    httpd = _serve_dir(root, "academy-bare-http")
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def fake():
    """A fake PostgREST per test, seeded to the fail-state baseline."""
    f = AcademyFake()
    seed(f)
    base = f.start()
    yield base, f
    f.stop()


@pytest.fixture()
def academy_url(bare_academy_server: str, fake) -> str:
    """Deterministic app URL: embedded pack + live fake backend."""
    base, _ = fake
    return (f"{bare_academy_server}/index.html"
            f"?supa={base}&key=test&site=BLR-DC1")


@pytest.fixture()
def rbac_fake():
    """A fake PostgREST in 0007 RBAC mode: anon reads come back empty,
    JWT-carrying reads are site-scoped — the hardened-backend scenario."""
    f = AcademyFake(rbac=True)
    seed(f)
    base = f.start()
    yield base, f
    f.stop()


@pytest.fixture()
def auth_stub_obj():
    """The GoTrue stand-in; yields (base URL, stub) so tests can flip
    ``signup_mode`` or inspect the request log."""
    stub = AuthStub()
    base = stub.start()
    yield base, stub
    stub.stop()


@pytest.fixture()
def auth_stub(auth_stub_obj) -> str:
    """Back-compat: just the stub's base URL for the ?auth= param."""
    return auth_stub_obj[0]


@pytest.fixture(scope="session")
def docroot_server(tmp_path_factory):
    """Serve a stand-in for the PRODUCTION docroot layout (yantraops static
    server / nginx template): console files at /, the academy at /academy/ —
    the layout the header cross-links are computed for."""
    root = tmp_path_factory.mktemp("docroot")
    root.joinpath("index.html").symlink_to(REPO_ROOT / "console" / "index.html")
    root.joinpath("academy").symlink_to(ACADEMY_DIR, target_is_directory=True)
    httpd = _serve_dir(root, "docroot-http")
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


# -------------------------------------------------------------------- helpers

def rows(fake_store: AcademyFake, table: str) -> list[dict[str, Any]]:
    with fake_store.lock:
        return [dict(r) for r in fake_store.tables[table]]


def wait_until(cond: Callable[[], bool], desc: str, timeout: float = 10.0) -> None:
    """Poll a backend-side condition with a hard deadline (no fixed sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout}s waiting for: {desc}")
