"""v0.12 release-version surfacing.

One source of truth — ``core/yantracore/version.py`` — shows up in:
  * ``python -m yantraops --version``
  * the ``up`` banner header line
  * the ``status`` output header
  * the hardcoded ``YF_VERSION`` const at the top of each app's script
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from yantraops.__main__ import YF_VERSION
from yantraops.orchestrator import FleetStack

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_version_flag_prints_release() -> None:
    """`python -m yantraops --version` prints 'YantraFleet <version>'."""
    proc = subprocess.run(
        [sys.executable, "-m", "yantraops", "--version"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert proc.stdout.strip() == f"YantraFleet {YF_VERSION}"


def test_version_comes_from_yantracore() -> None:
    """The CLI version is yantracore's (single source), never a copy."""
    yantracore = pytest.importorskip("yantracore")
    assert YF_VERSION == yantracore.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", YF_VERSION), YF_VERSION


def test_app_version_consts_match_version_py() -> None:
    """Both single-file apps hardcode YF_VERSION — release commits must keep
    them in lockstep with core/yantracore/version.py."""
    yantracore = pytest.importorskip("yantracore")
    for app in ("console", "academy"):
        html = (REPO_ROOT / app / "index.html").read_text(encoding="utf-8")
        m = re.search(r"const YF_VERSION='([^']+)'", html)
        assert m, f"{app}/index.html lacks the YF_VERSION const"
        assert m.group(1) == yantracore.__version__, (
            f"{app}/index.html YF_VERSION={m.group(1)!r} != "
            f"yantracore {yantracore.__version__!r}")


class _FakeProc:
    """Popen stand-in (mirrors test_up.py): dies instantly on request."""

    _next_pid = 60000

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


def test_banner_header_carries_version(tmp_path, monkeypatch) -> None:
    """The up banner's header line reads 'YantraFleet <version> up — mode: …'."""
    from yantraops import orchestrator as orch

    monkeypatch.setattr(orch.subprocess, "Popen",
                        lambda cmd, **_kw: _FakeProc())
    stack = FleetStack(loopback=True, copilot=False,
                       state_file=tmp_path / "state.json",
                       quiet=True, open_browser=False)
    try:
        stack.start()
        banner = stack.banner()
        assert f"YantraFleet {YF_VERSION} up — mode: loopback" in banner
    finally:
        stack.stop()


def test_status_output_carries_version(tmp_path, capsys) -> None:
    """`yantraops status` names the release even before finding a stack."""
    from yantraops.status import run_status

    rc = run_status(tmp_path / "missing-state.json")
    assert rc == 1                       # no state file → exit 1, as before
    out = capsys.readouterr().out
    assert f"YantraFleet {YF_VERSION}" in out
    assert "no state file" in out
