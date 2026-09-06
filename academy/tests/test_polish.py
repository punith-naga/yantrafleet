"""v0.12 polish suite: version chip, lesson search, print-lesson handout,
and the pinned WebLLM CDN import.

Uses the shared fixtures in ``conftest.py`` (bare/full academy servers +
the CORS-wrapped fake PostgREST).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from playwright.sync_api import Page, expect

ACADEMY_DIR = Path(__file__).resolve().parent.parent  # academy/
REPO_ROOT = ACADEMY_DIR.parent
VERSION_PY = REPO_ROOT / "core" / "yantracore" / "version.py"

#: The full content pack's perception lesson — its key_points mention SLAM.
SLAM_LESSON_ID = "lesson-14-sensing-and-perception"

# --- import core/yantracore/version.py by path (mirrors console's test_polish
# fix): this is the single source of truth the #yf-version chip is supposed
# to match, so read it live instead of a hardcoded literal that silently
# drifts on the next release bump — exactly what happened here, this test
# still expected v0.12.0 after the v0.12.1 single-sourcing release.
_vspec = importlib.util.spec_from_file_location("yf_version_academy_polish", VERSION_PY)
assert _vspec and _vspec.loader, f"cannot load {VERSION_PY}"
_version_mod = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(_version_mod)
YF_VERSION = _version_mod.__version__


# ------------------------------------------------------------ version chip

def test_version_chip_in_rail_foot(page: Page, academy_url: str) -> None:
    """The rail footer carries the release chip 'Yantrika v<x.y.z>'."""
    page.goto(academy_url)
    chip = page.locator("#yf-version")
    expect(chip).to_be_visible()
    expect(chip).to_have_text(f"Yantrika v{YF_VERSION}")


# ------------------------------------------------------------ lesson search

def _open_full_pack(page: Page, academy_server: str, fake) -> None:
    """The academy with the REAL content pack (academy/content/ served)."""
    base, _ = fake
    page.goto(f"{academy_server}/index.html?supa={base}&key=test&site=BLR-DC1")
    # the file pack loaded (not the 3-lesson embedded fallback)
    expect(page.locator("#pack-src")).not_to_contain_text(
        "embedded", timeout=15_000)
    expect(page.locator("#pack-src")).not_to_contain_text("loading")


def test_search_filters_lessons_by_key_points(
        page: Page, academy_server: str, fake) -> None:
    """Typing 'SLAM' filters the rail down to the perception lesson (the
    match lives in its key_points, not the title)."""
    _open_full_pack(page, academy_server, fake)
    lessons = page.locator("#rail button.lsn[data-lesson]")
    total = lessons.count()
    assert total > 10, f"expected the full pack in the rail, got {total}"

    page.fill("#lsn-search", "SLAM")
    expect(page.locator(
        f"#rail button.lsn[data-lesson='{SLAM_LESSON_ID}']")).to_be_visible()
    expect(lessons).to_have_count(1)
    expect(lessons.first).to_contain_text("Sensing and Perception")


def test_search_clears_with_escape(
        page: Page, academy_server: str, fake) -> None:
    """Esc in the search box restores the full rail instantly."""
    _open_full_pack(page, academy_server, fake)
    lessons = page.locator("#rail button.lsn[data-lesson]")
    total = lessons.count()
    page.fill("#lsn-search", "SLAM")
    expect(lessons).to_have_count(1)
    page.press("#lsn-search", "Escape")
    expect(lessons).to_have_count(total)
    assert page.input_value("#lsn-search") == ""


def test_search_no_match_note(page: Page, academy_url: str) -> None:
    """A filter that matches nothing says so instead of an empty void."""
    page.goto(academy_url)
    expect(page.locator("#rail button.lsn[data-lesson]").first).to_be_visible()
    page.fill("#lsn-search", "zzz-no-such-lesson")
    expect(page.locator("#rail-nomatch")).to_be_visible()
    expect(page.locator("#rail button.lsn[data-lesson]")).to_have_count(0)
    # typing focus survives the rail re-render (input lives outside #rail-list)
    assert page.evaluate("document.activeElement&&document.activeElement.id") \
        == "lsn-search"


# ------------------------------------------------------------ print lesson

def test_print_lesson_opens_handout_with_body(
        page: Page, academy_url: str) -> None:
    """'⎙ Print lesson' opens a popup holding only the lesson body (title,
    body_html, key points) plus a no-print toolbar — the ShiftReport
    pattern."""
    page.goto(academy_url)
    expect(page.locator("#lesson-title")).to_be_visible()
    title = page.locator("#lesson-title").inner_text()

    with page.expect_popup() as pop:
        page.click("#btn-print-lesson")
    w = pop.value
    expect(w.locator("#pl-title")).to_have_text(title)
    expect(w.locator("#pl-body")).to_contain_text("Physical AI")
    expect(w.locator("#pl-print")).to_be_visible()   # toolbar, hidden on paper
    # print CSS keeps only the lesson content
    assert "@media print{.toolbar" in w.content()


# ------------------------------------------------------ WebLLM pinned import

def test_webllm_cdn_import_is_pinned() -> None:
    """Both runtime import URLs pin the exact @mlc-ai/web-llm version
    (reproducible builds), and the __YF_WEBLLM_FACTORY test seam stays."""
    html = (ACADEMY_DIR / "index.html").read_text(encoding="utf-8")
    urls = re.findall(r"https://[^'\"]+@mlc-ai/web-llm[^'\"]*", html)
    cdn = [u for u in urls if "esm.run" in u or "cdn.jsdelivr.net" in u]
    assert len(cdn) >= 2, f"expected primary + fallback CDN URLs, got {cdn}"
    for u in cdn:
        assert "@mlc-ai/web-llm@0.2.79" in u, f"unpinned WebLLM import: {u}"
    assert "__YF_WEBLLM_FACTORY" in html  # the network-free test seam survives
