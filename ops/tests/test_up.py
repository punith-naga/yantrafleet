"""Loopback smoke tests for the yantraops orchestrator.

Everything runs on 127.0.0.1 ephemeral ports — no cloud, no fixed ports.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from yantraops.orchestrator import FleetStack, build_docroot

POLL_BUDGET_S = 30.0

# LLM keys would upgrade sarathi to a non-offline tier; strip them so the
# smoke test deterministically exercises the offline (tier 3) path.
LLM_ENV = ("GEMINI_API_KEY", "OPENAI_API_KEY", "SARATHI_MODEL")


def _wait_for(predicate, deadline: float, interval: float = 0.4):
    """Poll ``predicate`` until it returns a truthy value or time runs out."""
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    for var in LLM_ENV:
        monkeypatch.delenv(var, raising=False)
    st = FleetStack(
        loopback=True,
        sim_interval=0.25,       # scripted AMR-07 fault hits at sim tick 20
        detect_interval=1.0,
        notify_interval=2.0,
        state_file=tmp_path / "state.json",
        quiet=True,
    )
    yield st
    st.stop()


def test_loopback_smoke(stack):
    info = stack.start()
    deadline = time.time() + POLL_BUDGET_S
    base = info.base_url
    assert base.startswith("http://127.0.0.1:")
    assert info.copilot_url and info.copilot_url.startswith("http://127.0.0.1:")

    with httpx.Client(timeout=5.0) as client:
        def table(name: str) -> list:
            resp = client.get(f"{base}/rest/v1/{name}")
            resp.raise_for_status()
            return resp.json()

        # 1. The simulator populates the robots table.
        robots = _wait_for(lambda: table("robots"), deadline)
        assert robots, "no robots rows appeared within the poll budget"
        assert "battery" in robots[0]

        # 2. An alert (sim event) or incident (detector) shows up.
        found = _wait_for(
            lambda: table("alerts") or table("incidents"), deadline)
        assert found, "neither an alert nor an incident appeared in time"

        # 3. The copilot answers a question, offline-tier, grounded in data.
        def health_ok():
            try:
                return client.get(f"{info.copilot_url}/health").status_code == 200
            except httpx.HTTPError:
                return False

        assert _wait_for(health_ok, deadline), "sarathi never became healthy"
        resp = client.post(f"{info.copilot_url}/ask",
                           json={"question": "How is the fleet doing?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "offline"
        assert data["evidence"], "offline answer should cite tool evidence"
        assert "unavailable" not in data["answer"].lower()

        # 4. The console is served, and its URL carries the backend params.
        console = client.get(info.services[-1]["url"] + "index.html")
        assert console.status_code == 200
        assert "supa=" in info.console_url and "key=" in info.console_url

        # 4b. Academy and docs ride the SAME static server (one docroot).
        static_base = info.services[-1]["url"]
        if (stack.root / "academy" / "index.html").is_file():
            assert info.academy_url, "academy exists but academy_url is unset"
            assert "/academy/index.html" in info.academy_url
            for param in ("supa=", "key=", "site="):
                assert param in info.academy_url
            acad = client.get(static_base + "academy/index.html")
            assert acad.status_code == 200
        if (stack.root / "docs" / "index.html").is_file():
            assert client.get(static_base + "docs/index.html").status_code == 200

    # 5. Clean shutdown: every child exits, the backend thread stops.
    stack.stop()
    for svc in stack.services:
        assert svc.proc.poll() is not None, f"{svc.name} still running after stop()"
    assert stack.fake is None
    assert not stack.state_file.exists()


def test_up_duration_subprocess(tmp_path, monkeypatch):
    """`python -m yantraops up --loopback --duration N` exits 0 on its own."""
    env = dict(**__import__("os").environ)
    for var in LLM_ENV:
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, "-m", "yantraops", "up", "--loopback",
         "--duration", "6", "--sim-interval", "0.5", "--no-copilot",
         "--state-file", str(tmp_path / "state.json")],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    # v0.12: the banner header carries the release version between the
    # product name and 'up' — assert version-agnostically here (the exact
    # version is covered by ops/tests/test_version.py).
    assert "YantraFleet" in out and "up — mode: loopback" in out
    assert "console" in out and "?supa=" in out and "&key=" in out
    assert "--duration reached" in out
    # ephemeral ports only — the classic fixed demo ports must not appear
    assert ":8000/" not in out and ":8001/" not in out
    assert not (tmp_path / "state.json").exists()
    # academy banner line whenever the checkout ships academy/index.html
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "academy" / "index.html").is_file():
        assert "academy" in out and "/academy/index.html?supa=" in out


# --------------------------------------------------------------------------
# Static docroot: one server for console + academy + docs
# --------------------------------------------------------------------------

def test_build_docroot_layout(tmp_path):
    """console/ entries land at the top level; academy/ + docs/ as subdirs."""
    root = tmp_path / "repo"
    (root / "console").mkdir(parents=True)
    (root / "console" / "index.html").write_text("console page")
    (root / "console" / ".pytest_cache").mkdir()   # dot-entries are skipped
    (root / "academy" / "content").mkdir(parents=True)
    (root / "academy" / "index.html").write_text("academy page")
    (root / "academy" / "content" / "lesson1.html").write_text("lesson")
    (root / "docs").mkdir()
    (root / "docs" / "index.html").write_text("docs page")

    doc = build_docroot(root)
    try:
        assert (doc / "index.html").read_text() == "console page"
        assert not (doc / ".pytest_cache").exists()
        assert (doc / "academy" / "index.html").read_text() == "academy page"
        assert (doc / "academy" / "content" / "lesson1.html").is_file()
        assert (doc / "docs" / "index.html").read_text() == "docs page"
    finally:
        shutil.rmtree(doc, ignore_errors=True)


def test_build_docroot_without_academy_or_docs(tmp_path):
    """A checkout without academy/ or docs/ still serves the console."""
    root = tmp_path / "repo"
    (root / "console").mkdir(parents=True)
    (root / "console" / "index.html").write_text("console page")
    doc = build_docroot(root)
    try:
        assert (doc / "index.html").is_file()
        assert not (doc / "academy").exists()
        assert not (doc / "docs").exists()
    finally:
        shutil.rmtree(doc, ignore_errors=True)


class _FakeProc:
    """Popen stand-in for argv/banner tests: dies instantly on request."""

    _next_pid = 50000

    def __init__(self) -> None:
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def terminate(self):
        self._dead = True

    def kill(self):
        self._dead = True

    def wait(self, timeout=None):
        self._dead = True
        return 0


def test_static_server_argv_banner_and_state(tmp_path, monkeypatch):
    """The console child serves the GENERATED docroot (not console/ itself);
    console + academy URLs carry supa/key/site; banner and state file both
    carry the academy URL; stop() removes the temp docroot."""
    from yantraops import orchestrator as orch

    spawned: list[list[str]] = []

    def fake_popen(cmd, **_kw):
        spawned.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    stack = FleetStack(loopback=True, copilot=False,
                       state_file=tmp_path / "state.json",
                       quiet=True, open_browser=False)
    try:
        info = stack.start()

        argv = spawned[-1]                      # console is spawned last
        assert argv[1:3] == ["-m", "http.server"]
        docroot = Path(argv[argv.index("--directory") + 1])
        assert docroot == stack.docroot
        assert docroot != stack.root / "console"
        assert (docroot / "index.html").is_file()

        urls = [info.console_url] + ([info.academy_url] if info.academy_url else [])
        for url in urls:
            for param in ("supa=", "key=", "site="):
                assert param in url, f"{param} missing from {url}"

        banner = stack.banner()
        assert info.console_url in banner
        state = json.loads((tmp_path / "state.json").read_text())
        assert "academy_url" in state
        assert state["academy_url"] == info.academy_url

        if (stack.root / "academy" / "index.html").is_file():
            assert info.academy_url and "/academy/index.html" in info.academy_url
            assert info.academy_url in banner
    finally:
        stack.stop()
    assert stack.docroot is None, "temp docroot must be removed on stop()"
