"""Real-browser tests for the Yantrika console (console/index.html).

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
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import (BaseHTTPRequestHandler, SimpleHTTPRequestHandler,
                         ThreadingHTTPServer)
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


@pytest.fixture(autouse=True)
def _tour_already_seen(page: Page):
    """Pre-mark the first-run tour as seen so it never overlays other tests.

    The tour test below uses ``context.new_page()`` (a page *without* this
    init script) to exercise the real first-visit behaviour.
    """
    page.add_init_script(
        "try{localStorage.setItem('yf_tour_done','1')}catch(e){}")


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


# ------------------------------------------------- command palette / shortcuts

def test_palette_opens_and_navigates(page: Page, console_url: str) -> None:
    """Ctrl+K opens the palette; fuzzy-typing a view + Enter navigates to it."""
    open_live(page, console_url)
    page.keyboard.press("Control+k")
    expect(page.locator("#palette")).to_be_visible()
    expect(page.locator("#pal-in")).to_be_focused()
    # every view is offered as an entry
    expect(page.locator("#pal-list")).to_contain_text("Go to Maintenance")
    page.locator("#pal-in").fill("maintenance")
    expect(page.locator("#pal-list .pal-it.sel")).to_contain_text("Go to Maintenance")
    page.keyboard.press("Enter")
    expect(page.locator("#palette")).to_be_hidden()
    expect(page.locator("#v-maint")).to_be_visible()
    expect(page.locator("#v-maint h1")).to_have_text("Maintenance")
    # Esc closes a re-opened palette
    page.keyboard.press("Control+k")
    expect(page.locator("#palette")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#palette")).to_be_hidden()


def test_palette_jumps_to_robot_drawer(page: Page, console_url: str) -> None:
    """A robot entry in the palette opens that robot's telemetry drawer."""
    open_live(page, console_url)
    page.keyboard.press("Control+k")
    page.locator("#pal-in").fill("amr-05")
    expect(page.locator("#pal-list .pal-it.sel")).to_contain_text("AMR-05")
    page.keyboard.press("Enter")
    drawer = page.locator("#drawer")
    expect(drawer).to_have_class(re.compile(r"\bopen\b"))
    expect(drawer.locator(".dh h2")).to_have_text("AMR-05")


def test_palette_ask_sarathi_routes_to_copilot(page: Page, console_url: str) -> None:
    """Free text becomes an 'Ask Sarathi' entry that lands in Copilot.ask()."""
    open_live(page, console_url)
    page.keyboard.press("Control+k")
    page.locator("#pal-in").fill("how is the fleet doing right now?")
    ask = page.locator("#pal-list .pal-it", has_text="Ask Sarathi")
    expect(ask).to_be_visible()
    ask.click()
    expect(page.locator("#palette")).to_be_hidden()
    expect(page.locator("#copilot")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#cp-body .msg.user")).to_contain_text(
        "how is the fleet doing right now?")
    # sarathi is unreachable in tests -> the local engine must still answer
    expect(page.locator("#cp-body .msg.ai").last).to_contain_text(
        "Fleet is", timeout=15_000)


def test_copilot_toggle_moved_to_ctrl_j(page: Page, console_url: str) -> None:
    """Ctrl+J now toggles the copilot; Ctrl+K opens the palette instead."""
    open_live(page, console_url)
    copilot = page.locator("#copilot")
    page.keyboard.press("Control+j")
    expect(copilot).to_have_class(re.compile(r"\bopen\b"))
    page.keyboard.press("Control+j")
    expect(copilot).not_to_have_class(re.compile(r"\bopen\b"))
    page.keyboard.press("Control+k")
    expect(page.locator("#palette")).to_be_visible()
    expect(copilot).not_to_have_class(re.compile(r"\bopen\b"))
    # header button hint updated to the new shortcut
    expect(page.locator("#btn-copilot")).to_contain_text("J")


def test_ack_all_info_alerts_patches_backend(
        page: Page, console_url: str, fake) -> None:
    """'Ack all info alerts' acks only info alerts and PATCHes the backend."""
    _, store = fake
    with store.lock:
        store.tables["alerts"].append({
            "id": "AL-T9", "sev": "info",
            "msg": "Seeded firmware notice for the whole fleet",
            "src": "fleet", "tlabel": "13:22", "ack": False,
            "created_at": _now_iso(),
        })
    open_live(page, console_url)
    page.wait_for_function("store.alerts.some(a=>a.id==='AL-T9' && !a.ack)")
    page.keyboard.press("Control+k")
    page.locator("#pal-in").fill("ack all info")
    expect(page.locator("#pal-list .pal-it.sel")).to_contain_text(
        "Ack all info alerts")
    page.keyboard.press("Enter")
    wait_until(
        lambda: any(r["id"] == "AL-T9" and r.get("ack") is True
                    for r in rows(store, "alerts")),
        "info alert AL-T9 to be PATCHed to ack=true")
    # the critical alert must NOT have been acked
    assert any(r["id"] == "AL-T1" and not r.get("ack")
               for r in rows(store, "alerts")), "crit alert was wrongly acked"


# ----------------------------------------------------------- shift report

def test_shift_report_window_opens_with_kpis(
        page: Page, console_url: str, context) -> None:
    """The Overview button opens a printable report window with live KPIs."""
    open_live(page, console_url)
    # wait for the first alerts pull so the report compiles backend state
    expect(page.locator("#ov-alerts")).to_contain_text(SEED_ALERT_MSG,
                                                       timeout=15_000)
    with context.expect_page() as popup_info:
        page.locator("#btn-shift-report").click()
    report = popup_info.value
    body = report.locator("body")
    expect(body).to_contain_text("Shift report — BLR-DC1", timeout=15_000)
    expect(body).to_contain_text("KPI summary")
    expect(body).to_contain_text("Fleet health")
    expect(body).to_contain_text("Unacknowledged alerts")
    expect(body).to_contain_text(SEED_ALERT_MSG)     # live alert made it in
    expect(body).to_contain_text(SEED_INC_TITLE)     # live incident made it in
    expect(body).to_contain_text("Mission summary")
    expect(body).to_contain_text("Generated by Yantrika")
    # print-CSS + both toolbar actions are present
    expect(report.locator("#sr-print")).to_be_visible()
    expect(report.locator("#sr-copy")).to_be_visible()
    assert "@media print" in report.content()
    report.close()


def test_shift_report_in_palette(page: Page, console_url: str, context) -> None:
    """'Shift report' is also a palette entry."""
    open_live(page, console_url)
    page.keyboard.press("Control+k")
    page.locator("#pal-in").fill("shift report")
    expect(page.locator("#pal-list .pal-it.sel")).to_contain_text("Shift report")
    with context.expect_page() as popup_info:
        page.keyboard.press("Enter")
    report = popup_info.value
    expect(report.locator("body")).to_contain_text(
        "Generated by Yantrika", timeout=15_000)
    report.close()


# ----------------------------------------------------------- first-run tour

def test_tour_shows_on_first_visit_not_after_dismissal(
        context, console_url: str) -> None:
    """The 5-step tour overlays a fresh browser profile once; skipping it sets
    the localStorage flag and it never comes back — the ? button replays it."""
    p2 = context.new_page()          # no init script -> genuine first visit
    p2.goto(console_url)
    card = p2.locator("#tour-card")
    expect(card).to_be_visible(timeout=15_000)
    expect(card).to_contain_text("Live map")
    expect(card).to_contain_text("1 / 5")
    p2.locator("#tour-next").click()
    expect(card).to_contain_text("Alerts & acknowledge")
    expect(card).to_contain_text("2 / 5")
    p2.locator("#tour-skip").click()
    expect(card).to_be_hidden()
    assert p2.evaluate("localStorage.getItem('yf_tour_done')") == "1"
    # reload: same origin + storage -> the tour must NOT reappear
    p2.goto(console_url)
    expect(p2.locator("#ov-health")).to_be_visible(timeout=15_000)
    p2.wait_for_timeout(1500)        # past the 600 ms first-run delay
    expect(card).to_be_hidden()
    # ...but the header ? button replays it on demand
    p2.locator("#btn-tour").click()
    expect(card).to_be_visible()
    expect(card).to_contain_text("1 / 5")
    p2.locator("#tour-skip").click()
    p2.close()


# ------------------------------------------------------------- audit view

AUDIT_CMDS = [
    {"id": "CMD-A1", "robot_id": "AMR-01", "cmd": "pause", "status": "pending",
     "requested_by": "console:PN", "created_at": _now_iso(-300)},
    {"id": "CMD-A2", "robot_id": "AMR-02", "cmd": "charge", "status": "executed",
     "requested_by": "console:PN", "decided_by": "console:PN",
     "note": "battery low", "executed_at": _now_iso(-100),
     "created_at": _now_iso(-200)},
    {"id": "CMD-A3", "robot_id": "AMR-01", "cmd": "estop", "status": "rejected",
     "requested_by": "ops:XY", "decided_by": "console:PN",
     "created_at": _now_iso(-50)},
]

ACKED_ALERT_MSG = "Seeded acked alert - conveyor cleared earlier"


def _seed_audit(store: ConsoleFake) -> None:
    with store.lock:
        store.tables["commands"] += [dict(r) for r in AUDIT_CMDS]
        store.tables["alerts"].append({
            "id": "AL-A2", "sev": "warn", "msg": ACKED_ALERT_MSG,
            "src": "AMR-02", "tlabel": "13:10", "ack": True,
            "created_at": _now_iso(-500),
        })


def test_audit_view_renders_command_history(page: Page, console_url: str, fake) -> None:
    """The Audit view lists ALL commands (every status) plus acked alerts."""
    _, store = fake
    _seed_audit(store)
    open_live(page, console_url)
    page.locator("#nav button[data-v='audit']").click()
    view = page.locator("#v-audit")
    expect(view.locator(".vhead h1")).to_have_text("Audit")
    table = view.locator("table.t")
    expect(table).to_contain_text("PAUSE", timeout=15_000)   # pending
    expect(table).to_contain_text("CHARGE")                  # executed
    expect(table).to_contain_text("ESTOP")                   # rejected
    for col in ("Created", "Cmd", "Robot", "Requested by", "Decided by",
                "Status", "Note", "Executed at"):
        expect(table).to_contain_text(col)
    expect(table).to_contain_text("console:PN")
    expect(table).to_contain_text("ops:XY")
    expect(table).to_contain_text("battery low")             # note column
    expect(table).to_contain_text("rejected")                # status badge text
    expect(table).to_contain_text("executed")
    # acked alerts land in the second card; the unacked crit one does not
    acked_card = view.locator(".card", has_text="Recently acknowledged alerts")
    expect(acked_card).to_contain_text(ACKED_ALERT_MSG, timeout=15_000)
    expect(acked_card).not_to_contain_text(SEED_ALERT_MSG)


def test_audit_filter_chips(page: Page, console_url: str, fake) -> None:
    """Status chips narrow the table; an empty status shows a graceful note."""
    _, store = fake
    _seed_audit(store)
    open_live(page, console_url)
    page.locator("#nav button[data-v='audit']").click()
    expect(page.locator("#v-audit table.t")).to_contain_text("PAUSE",
                                                             timeout=15_000)
    # all six chips render
    for f in ("all", "pending", "approved", "executed", "failed", "rejected"):
        expect(page.locator(f"#audit-chips button[data-f='{f}']")).to_be_visible()
    page.locator("#audit-chips button[data-f='executed']").click()
    tbody = page.locator("#v-audit table.t tbody")
    expect(tbody).to_contain_text("CHARGE")
    expect(tbody).not_to_contain_text("PAUSE")
    expect(tbody).not_to_contain_text("ESTOP")
    # no 'failed' rows seeded -> per-filter empty state, chips still shown
    page.locator("#audit-chips button[data-f='failed']").click()
    expect(page.locator("#v-audit .empty").first).to_contain_text(
        "No failed commands")
    page.locator("#audit-chips button[data-f='all']").click()
    expect(page.locator("#v-audit table.t tbody tr")).to_have_count(3)


def test_palette_navigates_to_audit(page: Page, console_url: str) -> None:
    """'Go to Audit' is a palette entry and lands on the Audit view."""
    open_live(page, console_url)
    page.keyboard.press("Control+k")
    page.locator("#pal-in").fill("go to audit")
    expect(page.locator("#pal-list .pal-it.sel")).to_contain_text("Go to Audit")
    page.keyboard.press("Enter")
    expect(page.locator("#palette")).to_be_hidden()
    expect(page.locator("#v-audit")).to_be_visible()
    expect(page.locator("#v-audit .vhead h1")).to_have_text("Audit")
    expect(page.locator("#nav button[data-v='audit']")).to_have_class(
        re.compile(r"\bon\b"))


# ------------------------------------------------------------ SLA card

def test_sla_card_shows_computed_availability(page: Page, console_url: str) -> None:
    """Analytics gains an SLA card: availability %, MTTR, open incidents —
    all computed from live session data and labelled per the existing
    computed-vs-demo pattern, with the approximation documented in a title."""
    open_live(page, console_url)
    page.locator("#nav button[data-v='analytics']").click()
    card = page.locator("#an-sla")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Fleet availability")
    expect(card).to_contain_text("MTTR")
    expect(card).to_contain_text("Open incidents")
    expect(card).to_contain_text("computed")
    avail = page.locator("#sla-avail")
    expect(avail).to_contain_text(re.compile(r"\d"), timeout=15_000)
    text = avail.inner_text().strip()
    m = re.match(r"(\d+(?:\.\d+)?)%$", text)
    assert m, f"availability is not a percentage: {text!r}"
    assert 0.0 <= float(m.group(1)) <= 100.0
    # the approximation is documented in a tooltip
    title = (avail.get_attribute("title") or "").lower()
    assert "sample" in title and "fault" in title, f"tooltip missing: {title!r}"
    # the single seeded incident is Open -> open-incident count is 1
    expect(page.locator("#sla-open")).to_have_text("1")


# ------------------------------------------------- sarathi bearer token

class _SarathiStub(BaseHTTPRequestHandler):
    """Tiny CORS-aware sarathi stand-in recording (method, path, headers)."""

    calls: list  # type: ignore[type-arg]  # set on the per-fixture subclass
    lock: threading.Lock

    def log_message(self, fmt: str, *a: Any) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "authorization, content-type")

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _record(self) -> None:
        with self.lock:
            self.calls.append((self.command, self.path,
                               {k.lower(): v for k, v in self.headers.items()}))

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._json({"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record()
        self._json({"answer": "stub answer from sarathi",
                    "evidence": ["stub-ev"], "tier": "grounded"})


@pytest.fixture()
def sarathi_stub():
    """An /ask + /health HTTP stub on an ephemeral port, with a call log."""
    calls: list = []
    lock = threading.Lock()
    handler = type("Handler", (_SarathiStub,), {"calls": calls, "lock": lock})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever,
                         name="sarathi-stub", daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}", calls, lock
    httpd.shutdown()
    httpd.server_close()


def _stub_calls(calls: list, lock: threading.Lock,
                method: str, path: str) -> list:
    with lock:
        return [c for c in calls if c[0] == method and c[1] == path]


def _point_copilot_at(page: Page, base: str) -> None:
    page.evaluate("Copilot.SARATHI_URL = %r; Copilot.SARATHI_HEALTH_URL = %r"
                  % (base + "/ask", base + "/health"))


def test_token_param_sends_authorization_header(
        page: Page, console_url: str, sarathi_stub) -> None:
    """?token=... => 'Authorization: Bearer <token>' on /ask AND /health."""
    base, calls, lock = sarathi_stub
    page.goto(console_url + "&token=tok-secret-42")
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)
    assert page.evaluate("Copilot.SARATHI_TOKEN") == "tok-secret-42"
    _point_copilot_at(page, base)
    page.evaluate("Copilot.ask('hello from the token test')")
    wait_until(lambda: _stub_calls(calls, lock, "POST", "/ask"),
               "stub to receive POST /ask")
    wait_until(lambda: _stub_calls(calls, lock, "GET", "/health"),
               "stub to receive GET /health")
    ask_headers = _stub_calls(calls, lock, "POST", "/ask")[0][2]
    assert ask_headers.get("authorization") == "Bearer tok-secret-42"
    health_headers = _stub_calls(calls, lock, "GET", "/health")[0][2]
    assert health_headers.get("authorization") == "Bearer tok-secret-42"
    # and the stub's reply rendered through the live-agent path
    expect(page.locator("#cp-body .msg.ai").last).to_contain_text(
        "stub answer from sarathi", timeout=15_000)
    expect(page.locator("#cp-body")).to_contain_text("live agent")


def test_no_token_param_no_authorization_header(
        page: Page, console_url: str, sarathi_stub) -> None:
    """Without ?token= the requests carry no Authorization header at all."""
    base, calls, lock = sarathi_stub
    open_live(page, console_url)
    assert page.evaluate("Copilot.SARATHI_TOKEN") is None
    _point_copilot_at(page, base)
    page.evaluate("Copilot.ask('hello without a token')")
    wait_until(lambda: _stub_calls(calls, lock, "POST", "/ask"),
               "stub to receive POST /ask")
    wait_until(lambda: _stub_calls(calls, lock, "GET", "/health"),
               "stub to receive GET /health")
    for method, path in (("POST", "/ask"), ("GET", "/health")):
        headers = _stub_calls(calls, lock, method, path)[0][2]
        assert "authorization" not in headers, (method, path, headers)
