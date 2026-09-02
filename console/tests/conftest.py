"""Shared fixtures for the console browser test suites.

``test_console.py`` predates this conftest and keeps its own module-local
copies of these fixtures (module fixtures shadow conftest ones, with
identical behaviour); the RBAC/auth suite (``test_console_rbac.py``) uses
the versions below.
"""
from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# --- environment: use the preinstalled browsers, never download -------------
PW_BROWSERS = os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

CONSOLE_DIR = Path(__file__).resolve().parent.parent  # console/


def find_chromium() -> str | None:
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


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict, playwright) -> dict:
    """Headless Chromium from /opt/pw-browsers; explicit path if needed."""
    args = dict(browser_type_launch_args)
    args["args"] = list(args.get("args", [])) + ["--no-sandbox",
                                                 "--disable-dev-shm-usage"]
    try:
        auto = playwright.chromium.executable_path
        auto_ok = bool(auto) and Path(auto).exists()
    except Exception:
        auto_ok = False
    if not auto_ok:  # auto-detect failed -> launch the known binary directly
        exe = find_chromium()
        if exe:
            args["executable_path"] = exe
    return args


@pytest.fixture(scope="session")
def console_server():
    """Serve console/ statically on an ephemeral port."""
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Quiet, directory=str(CONSOLE_DIR)))
    t = threading.Thread(target=httpd.serve_forever, name="console-http",
                         daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()
