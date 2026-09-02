"""Real-browser tests for the YantraFleet console (console/index.html).

Runs the single-file console in headless Chromium (Python Playwright) against
the in-process fake PostgREST server from ``e2e/fakerest.py``:

  fakerest (ephemeral port, CORS-wrapped)  <--fetch--  console page
  http.server serving console/ (ephemeral port)  --> index.html?supa=...&key=test

The fake is seeded with robots / alerts / commands / incidents rows and
``fleet_meta.writer_id = 'yantrasim-e2e'`` so the console elects itself a
*follower* (Cloud: LIVE - viewing) and pulls everything from the fake backend.

Two small extensions live in this file only (the fake itself is untouched):
  * a CORS shim (the page and the fake are different origins, so Chromium
    preflights every request — real PostgREST answers OPTIONS itself);
  * DB-style column defaults for ``commands`` (``status='pending'``,
    ``created_at=now()``) which in production come from the SQL schema.

Requires: pip install playwright pytest-playwright. Browsers are preinstalled
under /opt/pw-browsers — the suite never downloads any.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

# --- environment: use the preinstalled browsers, never download -------------
PW_BROWSERS = os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

CONSOLE_DIR = Path(__file__).resolve().parent.parent          # console/
REPO_ROOT = CONSOLE_DIR.parent                                # repo root
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

pytestmark = pytest.mark.skipif(
    CHROMIUM_EXE is None,
    reason=f"no Chromium binary found under {PW_BROWSERS} — browser tests skipped",
)

pytest.importorskip("playwright.sync_api")
pytest.importorskip("pytest_playwright")
from playwright.sync_api import Page, expect  # noqa: E402

# --- import e2e/fakerest.py by path (e2e/ is not a package) -----------------
_spec = importlib.util.spec_from_file_location("yf_fakerest", FAKEREST_PY)
assert _spec and _spec.loader, f"cannot load {FAKEREST_PY}"
fakerest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakerest)


def _now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


class ConsoleFake(fakerest.FakePostgREST):
    """fakerest + CORS (cross-origin page) + SQL-schema column defaults."""

    def insert(self, table: str, rows: list[dict[str, Any]],
               on_conflict: str | None, resolution: str | None) -> None:
        if table == "commands":  # DB defaults from 0002_commands.sql
            rows = [{"status": "pending", "created_at": _now_iso(), **r} for r in rows]
        super().insert(table, rows, on_conflict, resolution)

    def start(self) -> str:  # same shape as the parent, different handler
        store = self

        class Handler(_CORSHandler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="console-fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


class _CORSHandler(fakerest._Handler):
    """The console page is served from another origin — answer preflights."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "apikey, authorization, content-type, prefer")
        self.send_header("Access-Control-Max-Age", "600")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


# ---------------------------------------------------------------- seed data

SEED_ALERT_MSG = "Seeded conveyor jam at PACK-2 - clear before wave 8"
SEED_INC_TITLE = "Seeded incident: dock door jam at OUTBOUND"


def _seed(fake: ConsoleFake) -> None:
    with fake.lock:
        # A fresh 'yantrasim' writer heartbeat => console becomes a follower
        # and renders what the backend serves. (Future timestamp keeps the
        # claim un-stale for the whole test without a heartbeat thread.)
        fake.tables["fleet_meta"] = [{
            "id": 1, "writer_id": "yantrasim-e2e",
            "updated_at": _now_iso(3600), "sim_min": 872, "throughput": 132,
        }]
        fake.tables["robots"] = [
            {"id": "AMR-01", "vendor": "TestBot X1", "status": "active",
             "battery": 77.0, "speed": 1.2, "task_kind": "Pick - Pack",
             "health": 95.0, "motor_temp": 41.0, "tasks_done": 12},
            {"id": "AMR-02", "vendor": "TestBot X2", "status": "charging",
             "battery": 41.0, "speed": 0.0, "task_kind": None,
             "health": 90.0, "motor_temp": 39.0, "tasks_done": 7},
        ]
        fake.tables["alerts"] = [{
            "id": "AL-T1", "sev": "crit", "msg": SEED_ALERT_MSG,
            "src": "AMR-01", "tlabel": "14:00", "ack": False,
            "created_at": _now_iso(),
        }]
        fake.tables["incidents"] = [{
            "id": "INC-9001", "sev": "serious", "title": SEED_INC_TITLE,
            "src": "DOCK-3", "tlabel": "13:55", "state": "Open",
            "impact": "Outbound wave 7 staged late by 6 min",
            "dur": 7, "created_at": _now_iso(),
        }]
        fake.tables["commands"] = []


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict, playwright) -> dict:
    """Headless Chromium from /opt/pw-browsers; explicit path if needed."""
    args = dict(browser_type_launch_args)
    args["args"] = list(args.get("args", [])) + ["--no-sandbox", "--disable-dev-shm-usage"]
    try:
        auto = playwright.chromium.executable_path
        auto_ok = bool(auto) and Path(auto).exists()
    except Exception:
        auto_ok = False
    if not auto_ok:  # auto-detect failed -> launch the known binary directly
        args["executable_path"] = CHROMIUM_EXE
    return args


@pytest.fixture(scope="session")
def console_server():
    """Serve console/ statically on an ephemeral port."""
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Quiet, directory=str(CONSOLE_DIR)))
    t = threading.Thread(target=httpd.serve_forever, name="console-http", daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def fake():
    """A freshly seeded fake PostgREST per test."""
    f = ConsoleFake()
    _seed(f)
    base = f.start()
    yield base, f
    f.stop()


@pytest.fixture()
def console_url(console_server: str, fake) -> str:
    base, _ = fake
    return (f"{console_server}/index.html"
            f"?supa={base}&key=test&site=BLR-DC1")


# -------------------------------------------------------------------- helpers

def rows(fake_store: ConsoleFake, table: str) -> list[dict[str, Any]]:
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


def _dead_port() -> int:
    """A localhost port with nothing listening (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_live(page: Page, console_url: str) -> None:
    page.goto(console_url)
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)


# ---------------------------------------------------------------------- tests

def test_boots_without_page_errors(page: Page, console_url: str) -> None:
    """The app boots against the fake backend with zero uncaught JS errors."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_live(page, console_url)
    expect(page.locator("#ov-health")).to_be_visible()      # overview rendered
    expect(page.locator("#h-health")).not_to_have_text("—")  # header KPI ticked
    assert errors == [], f"uncaught page errors: {errors}"


def test_cloud_chip_reaches_live_viewing(page: Page, console_url: str) -> None:
    """With a yantrasim writer seeded, the console follows the live feed."""
    page.goto(console_url)
    chip = page.locator("#cloud-lbl")
    expect(chip).to_contain_text("Cloud: LIVE", timeout=15_000)
    expect(chip).to_contain_text("viewing")  # follower, not leader


def test_seeded_robots_render_in_fleet_list(page: Page, console_url: str) -> None:
    """Backend robot rows (vendor et al.) show up in the Live Ops fleet list."""
    open_live(page, console_url)
    page.locator("#nav button[data-v='liveops']").click()
    fleet = page.locator("#fl-list")
    expect(fleet).to_contain_text("TestBot X1", timeout=15_000)
    expect(fleet).to_contain_text("TestBot X2")
    expect(fleet).to_contain_text("AMR-01")


def test_seeded_alert_visible_and_ack_patches_backend(
        page: Page, console_url: str, fake) -> None:
    """The seeded alert renders; Ack PATCHes alerts.ack=true in the fake."""
    _, store = fake
    open_live(page, console_url)
    alert = page.locator("#ov-alerts .al", has_text=SEED_ALERT_MSG)
    expect(alert).to_be_visible(timeout=15_000)
    alert.locator("button.ack").click()
    wait_until(
        lambda: any(r["id"] == "AL-T1" and r.get("ack") is True
                    for r in rows(store, "alerts")),
        "alerts row AL-T1 to be PATCHed to ack=true")
    audit = [p for m, p in store.requests if m == "PATCH" and "/alerts" in p]
    assert any("id=eq.AL-T1" in p for p in audit), f"no alert PATCH seen: {audit}"


def test_cmd_robot_inserts_pending_command(page: Page, console_url: str, fake) -> None:
    """cmdRobot() in live mode queues a command row (status=pending)."""
    _, store = fake
    open_live(page, console_url)
    page.evaluate("cmdRobot('AMR-02','pause')")
    wait_until(
        lambda: any(r.get("robot_id") == "AMR-02" and r.get("cmd") == "pause"
                    and r.get("status") == "pending"
                    for r in rows(store, "commands")),
        "a pending commands row for AMR-02/pause")
    row = next(r for r in rows(store, "commands") if r.get("robot_id") == "AMR-02")
    assert row["requested_by"] == "console:PN"


def test_approvals_card_renders_and_approve_patches(
        page: Page, console_url: str, fake) -> None:
    """A pending command shows on the approvals card; Approve => status=approved."""
    _, store = fake
    with store.lock:
        store.tables["commands"].append({
            "id": "CMD-T1", "robot_id": "AMR-05", "cmd": "charge",
            "status": "pending", "requested_by": "test-seed",
            "created_at": _now_iso(),
        })
    open_live(page, console_url)
    card = page.locator("#ov-approvals")
    expect(card).to_contain_text("Pending approvals", timeout=15_000)
    expect(card).to_contain_text("CHARGE")
    expect(card).to_contain_text("AMR-05")
    card.locator("button", has_text="Approve").click()
    wait_until(
        lambda: any(r.get("id") == "CMD-T1" and r.get("status") == "approved"
                    for r in rows(store, "commands")),
        "commands row CMD-T1 to be PATCHed to status=approved")
    row = next(r for r in rows(store, "commands") if r.get("id") == "CMD-T1")
    assert row["decided_by"] == "console:PN"
    assert row.get("decided_at")


def test_seeded_incident_listed(page: Page, console_url: str) -> None:
    """Backend incidents replace the mock queue and render in the view."""
    open_live(page, console_url)
    page.locator("#nav button[data-v='incidents']").click()
    queue = page.locator("#v-incidents")
    expect(queue).to_contain_text(SEED_INC_TITLE, timeout=15_000)
    expect(queue).to_contain_text("INC-9001")
    expect(queue).to_contain_text("Open")


def test_offline_fallback_local_sim(page: Page, console_server: str) -> None:
    """Dead backend port => 'Local sim' chip, and the fleet still renders."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    url = f"{console_server}/index.html?supa=http://127.0.0.1:{_dead_port()}&key=test"
    page.goto(url)
    expect(page.locator("#cloud-lbl")).to_contain_text("Local sim", timeout=15_000)
    page.locator("#nav button[data-v='liveops']").click()
    fleet = page.locator("#fl-list")
    expect(fleet).to_contain_text("AMR-01", timeout=15_000)
    expect(fleet).to_contain_text("AMR-10")  # all 10 local sim robots
    assert errors == [], f"offline fallback raised page errors: {errors}"


def test_site_param_exposed_on_window(page: Page, console_url: str) -> None:
    """?site=... lands on window.SITE (exposure only — no filtering yet)."""
    open_live(page, console_url)
    assert page.evaluate("window.SITE") == "BLR-DC1"
    # and the backend override actually took effect
    assert page.evaluate("SUPA_URL").startswith("http://127.0.0.1:")
    assert page.evaluate("SUPA_KEY") == "test"


def test_defaults_unchanged_without_params(page: Page, console_server: str) -> None:
    """With no query params the shipped Supabase constants still apply."""
    page.goto(f"{console_server}/index.html")
    assert page.evaluate("SUPA_URL") == "https://flwyvhsmgrrqpmhcqlzd.supabase.co"
    assert page.evaluate("SUPA_KEY").startswith("sb_publishable_")
    assert page.evaluate("window.SITE") is None
