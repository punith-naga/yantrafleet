"""v0.12 polish suite: version footer chip + empty-fleet Overview card.

Runs the console in headless Chromium against the in-process fake
PostgREST (``e2e/fakerest.py``), mirroring ``test_console.py``'s fixture
style (per repo convention the fake/CORS plumbing is copied, not
imported from the sibling test module).
"""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest
from playwright.sync_api import Page, expect

CONSOLE_DIR = Path(__file__).resolve().parent.parent  # console/
REPO_ROOT = CONSOLE_DIR.parent
FAKEREST_PY = REPO_ROOT / "e2e" / "fakerest.py"
VERSION_PY = REPO_ROOT / "core" / "yantracore" / "version.py"

pytest.importorskip("playwright.sync_api")
pytest.importorskip("pytest_playwright")

# --- import e2e/fakerest.py by path (e2e/ is not a package) -----------------
_spec = importlib.util.spec_from_file_location("yf_fakerest_polish", FAKEREST_PY)
assert _spec and _spec.loader, f"cannot load {FAKEREST_PY}"
fakerest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakerest)

# --- import core/yantracore/version.py by path, same reason as above:
# this is the single source of truth the console's #yf-version chip is
# supposed to match, so read it live instead of a hardcoded literal that
# silently drifts on the next release bump (exactly what happened here:
# this test still expected v0.12.0 after the v0.12.1 single-sourcing
# release bumped core/yantracore/version.py and console/index.html's own
# YF_VERSION constant, but not this test).
_vspec = importlib.util.spec_from_file_location("yf_version_polish", VERSION_PY)
assert _vspec and _vspec.loader, f"cannot load {VERSION_PY}"
_version_mod = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(_version_mod)
YF_VERSION = _version_mod.__version__


def _now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


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


class PolishFake(fakerest.FakePostgREST):
    """fakerest + CORS."""

    def start(self) -> str:
        store = self

        class Handler(_CORSHandler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="polish-fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


def _seed_empty_fleet(fake: PolishFake) -> None:
    """A live yantrasim writer heartbeat (console follows) but ZERO robot
    rows — the fresh-real-Supabase, nothing-publishing scenario."""
    with fake.lock:
        fake.tables["fleet_meta"] = [{
            "id": 1, "writer_id": "yantrasim-e2e",
            "updated_at": _now_iso(3600), "sim_min": 872, "throughput": 132,
        }]
        fake.tables["robots"] = []
        fake.tables["alerts"] = []
        fake.tables["incidents"] = []
        fake.tables["commands"] = []


@pytest.fixture()
def empty_fake():
    f = PolishFake()
    _seed_empty_fleet(f)
    base = f.start()
    yield base, f
    f.stop()


@pytest.fixture(autouse=True)
def _tour_already_seen(page: Page):
    """Pre-mark the first-run tour as seen so it never overlays these tests."""
    page.add_init_script(
        "try{localStorage.setItem('yf_tour_done','1')}catch(e){}")


def _dead_port() -> int:
    """A localhost port with nothing listening (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(cond: Callable[[], bool], desc: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout}s waiting for: {desc}")


# ---------------------------------------------------------------------- tests

def test_version_footer_chip_renders(page: Page, console_server: str) -> None:
    """The sidebar footer carries the release chip 'YantraFleet v<x.y.z>',
    backend or not (dead port → offline local sim)."""
    page.goto(f"{console_server}/index.html"
              f"?supa=http://127.0.0.1:{_dead_port()}&key=test")
    chip = page.locator("#yf-version")
    expect(chip).to_be_visible()
    expect(chip).to_have_text(f"YantraFleet v{YF_VERSION}")


def test_empty_fleet_card_appears_then_clears(
        page: Page, console_server: str, empty_fake) -> None:
    """Backend reachable + robots table empty beyond the grace window →
    the Overview shows the 'start the simulator' card; it disappears once
    robot rows arrive. The console never writes robots itself."""
    base, fake = empty_fake
    page.goto(f"{console_server}/index.html?supa={base}&key=test&site=BLR-DC1")
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)

    # Production grace is 10 s — shrink it so the suite stays fast. The
    # emptiness timer itself started with the first empty pull.
    page.evaluate("Sync.EMPTY_AFTER_MS = 400")
    card = page.locator("#ov-empty")
    expect(card).to_be_visible(timeout=15_000)   # overview repaints every ~3 s
    expect(card).to_contain_text("No fleet data yet")
    expect(card).to_contain_text("python -m yantrasim --supabase")
    expect(card).to_contain_text("yantraops up")

    # No auto-writes: the backend robots table is still empty.
    with fake.lock:
        assert fake.tables["robots"] == []

    # A robot row lands (the operator started the sim) → the card clears.
    with fake.lock:
        fake.tables["robots"] = [{
            "id": "AMR-01", "vendor": "TestBot X1", "status": "active",
            "battery": 77.0, "speed": 1.2, "task_kind": "Pick - Pack",
            "health": 95.0, "motor_temp": 41.0, "tasks_done": 12,
        }]
    expect(card).to_have_count(0, timeout=15_000)


def test_empty_card_not_shown_before_grace_window(
        page: Page, console_server: str, empty_fake) -> None:
    """Within the (default 10 s) grace window the Overview stays calm — a
    briefly-empty table on connect must not flash the card."""
    base, _ = empty_fake
    page.goto(f"{console_server}/index.html?supa={base}&key=test&site=BLR-DC1")
    expect(page.locator("#cloud-lbl")).to_contain_text("LIVE", timeout=15_000)
    expect(page.locator("#ov-health")).to_be_visible()
    assert page.locator("#ov-empty").count() == 0
