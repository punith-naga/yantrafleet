"""Offline tests for the Yantrika console (console/index.html).

The console is a single static HTML file, so these tests verify:
  1. structure — the file exists, is a full HTML document, one inline script;
  2. sarathi bridge — Copilot.ask() tries POST http://localhost:8001/ask with
     a 4 s timeout and renders {answer, evidence[], tier};
  3. fallback — the original local answer(q) engine is still present, unchanged
     in its key behaviours, and wired as the failure path;
  4. syntax — the extracted inline script parses under `node --check`.

No network, no browser, no third-party Python packages required
(pytest + stdlib + the system Node runtime only).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE_DIR: Path = Path(__file__).resolve().parent.parent
INDEX: Path = CONSOLE_DIR / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    """The console page source."""
    assert INDEX.is_file(), f"missing {INDEX}"
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """The single inline <script> body."""
    blocks: list[str] = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, "console must keep exactly one inline script block"
    return blocks[0]


# ---------------------------------------------------------------- structure

def test_document_shell(html: str) -> None:
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "<title>" in html


def test_config_comment_block_at_top(html: str) -> None:
    """A comment block near the top documents the sarathi + Supabase config."""
    head = html[:3500]
    assert "CONFIG" in head
    assert "http://localhost:8001/ask" in head
    assert "SARATHI_URL" in head
    assert "Supabase" in head


def test_original_app_preserved(html: str) -> None:
    """Spot-check that the working app was copied, not rewritten."""
    for marker in (
        "FleetMind",          # branding
        "const Sync=",        # Supabase sync layer
        "function rOverview", # views
        "function rLiveOps",
        "openIncident('INC-1042')",
        "flwyvhsmgrrqpmhcqlzd.supabase.co",
    ):
        assert marker in html, f"expected original-app marker missing: {marker}"


# ------------------------------------------------------------ sarathi bridge

def test_bridge_endpoint_and_timeout(script: str) -> None:
    assert "SARATHI_URL:'http://localhost:8001/ask'" in script
    assert "SARATHI_TIMEOUT_MS:4000" in script
    # hard timeout via AbortController wired into fetch
    assert "AbortController" in script
    m = re.search(r"async askRemote\(q\)\{(.*?)\n  \},", script, re.S)
    assert m, "askRemote method missing"
    body = m.group(1)
    assert "fetch(this.SARATHI_URL" in body
    assert "method:'POST'" in body
    assert "signal:c.signal" in body
    assert "JSON.stringify({question:q})" in body


def test_bridge_renders_answer_evidence_tier(script: str) -> None:
    m = re.search(r"renderRemote\(d\)\{(.*?)\n  \},", script, re.S)
    assert m, "renderRemote method missing"
    body = m.group(1)
    assert "d.answer" in body
    assert "d.evidence" in body
    assert "this.tierPill(d.tier)" in body
    # evidence rendered with the console's existing chip style
    assert 'class="evd"' in body
    # untrusted service output is escaped before insertion into innerHTML
    assert "this.esc(" in body


def test_tier_pill_labels(script: str) -> None:
    """Pill maps both the spec labels and sarathi's actual tier ids."""
    m = re.search(r"tierPill\(tier\)\{(.*?)\n  \},", script, re.S)
    assert m, "tierPill method missing"
    body = m.group(1)
    for alias in ("live_agent", "grounded", "model_only", "llm_only", "offline"):
        assert alias in body, f"tier alias not handled: {alias}"
    for label in ("'live agent'", "'model only'", "'offline'"):
        assert label in body, f"pill label missing: {label}"


def test_ask_tries_remote_then_falls_back(script: str) -> None:
    m = re.search(r"async ask\(q\)\{(.*?)\n  \},", script, re.S)
    assert m, "async ask method missing"
    body = m.group(1)
    remote = body.index("await this.askRemote(q)")
    local = body.index("this.answer(q)")
    assert remote < local, "remote must be attempted before the local engine"
    assert "catch" in body, "fallback must trigger on any failure"


# ------------------------------------------------------- fallback unchanged

def test_local_answer_engine_unchanged(script: str) -> None:
    """The rule-based answer(q) engine keeps its original branches."""
    m = re.search(r"\n  answer\(q\)\{(.*?)\n  \}\n\};", script, re.S)
    assert m, "local answer(q) engine missing"
    body = m.group(1)
    for branch in (
        "q.includes('throughput')",
        "q.includes('service')",
        "q.includes('handover')",
        "q.includes('how')",
        "openIncident('INC-1042')",
    ):
        assert branch in body, f"local engine branch missing: {branch}"


# ----------------------------------------------------------------- syntax

def test_inline_script_parses(script: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node runtime required for syntax check"
    js = tmp_path / "console_inline.js"
    js.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [node, "--check", str(js)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"
