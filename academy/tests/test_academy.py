"""Real-browser tests for the YantraFleet Academy (academy/index.html).

Fixture style mirrors console/tests/test_console.py (fixtures live in
conftest.py): headless Chromium + the in-process fake PostgREST from
e2e/fakerest.py, everything on localhost sockets — fully hermetic.

The default ``academy_url`` serves a copy of index.html WITHOUT a content/
directory, so the content-pack fetch 404s and the app runs its EMBEDDED
starter pack — deterministic regardless of whether the parallel content
agent has landed academy/content/pack-physical-ai.json yet.
"""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect

from conftest import ACADEMY_DIR, seed, _serve_dir

CERT_NAME = "Priya Operator"


def _dead_port() -> int:
    """A localhost port with nothing listening (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_academy(page: Page, url: str) -> None:
    page.goto(url)
    expect(page.locator("#lesson-title")).to_be_visible(timeout=15_000)


# ------------------------------------------------------------------ boot

def test_boots_without_page_errors(page: Page, academy_url: str) -> None:
    """The app boots against the fake backend with zero uncaught JS errors."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_academy(page, academy_url)
    expect(page.locator("#rail button.lsn").first).to_be_visible()
    expect(page.locator("#tutor-log .msg.ai").first).to_be_visible()
    # backend chip reaches 'connected' against the fake
    expect(page.locator("#be-lbl")).to_have_text("backend: connected",
                                                 timeout=15_000)
    # the body_html sanitizer strips scripts/handlers, keeps the safe subset
    cleaned = page.evaluate(
        "YFA.sanitize('<p onclick=x()>a <b>b</b></p><script>bad()</scr'"
        " + 'ipt><img src=x onerror=y()>')")
    assert cleaned == "<p>a <b>b</b></p>"
    assert errors == [], f"uncaught page errors: {errors}"


def test_embedded_pack_renders_when_content_fetch_404s(
        page: Page, academy_url: str) -> None:
    """No content/ dir -> ./content/pack-physical-ai.json 404s -> the
    embedded starter pack renders: pill, 3 lessons, track, checkride."""
    open_academy(page, academy_url)
    expect(page.locator("#pack-src")).to_have_text("embedded starter pack")
    assert page.evaluate("YFA.source") == "embedded"
    expect(page.locator("#rail button.lsn[data-lesson]")).to_have_count(3)
    rail = page.locator("#rail")
    expect(rail).to_contain_text("Fleet Operator Foundations")
    expect(rail).to_contain_text("What is Physical AI?")
    expect(rail).to_contain_text("Operator Checkride I")
    # first lesson auto-opens
    expect(page.locator("#lesson-title")).to_have_text("What is Physical AI?")


def test_external_content_pack_loads_when_present(page: Page, fake,
                                                  tmp_path: Path) -> None:
    """With content/pack-physical-ai.json present the pack loads from file —
    the interface contract for the parallel content-pack agent."""
    pack = {"pack": {
        "id": "ext-pack", "title": "External Test Pack", "version": "9.9.9",
        "tracks": [{"id": "t1", "title": "Ext Track", "role": "Tester",
                    "lesson_ids": ["x1"]}],
        "lessons": [{
            "id": "x1", "title": "External Lesson One", "minutes": 3,
            "track": "t1",
            "body_html": "<p>Body from the <b>external</b> pack.</p>",
            "key_points": ["external key point"],
            "quiz": [{"q": "Q?", "options": ["a", "b"], "answer_idx": 0,
                      "explain": "because a"}],
            "practical": {
                "instructions_html": "<p>do it</p>",
                "verify": {"kind": "backend_check", "table": "alerts",
                           "filter": "select=id&limit=1", "expect": "rows>0"}},
            "tutor_context": "External pack context sentence."}],
    }}
    root = tmp_path / "app"
    (root / "content").mkdir(parents=True)
    (root / "index.html").write_text(
        (ACADEMY_DIR / "index.html").read_text(encoding="utf-8"),
        encoding="utf-8")
    (root / "content" / "pack-physical-ai.json").write_text(
        json.dumps(pack), encoding="utf-8")
    httpd = _serve_dir(root, "academy-extpack-http")
    try:
        host, port = httpd.server_address[:2]
        base, _ = fake
        page.goto(f"http://{host}:{port}/index.html?supa={base}&key=test")
        expect(page.locator("#pack-src")).to_have_text(
            "External Test Pack v9.9.9", timeout=15_000)
        assert page.evaluate("YFA.source") == "file"
        expect(page.locator("#lesson-title")).to_have_text("External Lesson One")
        expect(page.locator("#lesson-body")).to_contain_text(
            "Body from the external pack.")
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------- lessons + quiz

def test_lesson_nav_and_quiz_feedback_and_scoring(
        page: Page, academy_url: str) -> None:
    """Wrong answer -> instant 'Not quite' + explanation; right answer ->
    'Correct'; score line totals first-try answers; rail nav switches lessons."""
    open_academy(page, academy_url)
    # Q1: deliberately wrong (option 0; correct is 1)
    page.locator(".q-opt[data-q='0'][data-i='0']").click()
    q1 = page.locator(".qq[data-q='0']")
    expect(q1.locator(".q-explain")).to_be_visible()
    expect(q1.locator(".q-explain")).to_contain_text("Not quite")
    expect(q1.locator(".q-explain")).to_contain_text(
        "errors cost real throughput")            # the authored explanation
    expect(q1.locator(".q-opt[data-i='1']")).to_have_class(re.compile(r"\bok\b"))
    expect(q1.locator(".q-opt[data-i='0']")).to_have_class(re.compile(r"\bbad\b"))
    # Q2: correct
    page.locator(".q-opt[data-q='1'][data-i='1']").click()
    q2 = page.locator(".qq[data-q='1']")
    expect(q2.locator(".q-explain")).to_contain_text("Correct")
    expect(page.locator("#quiz-score")).to_contain_text("score")
    expect(page.locator("#quiz-score")).to_contain_text("1/2")
    # answered questions are locked (first answer counts)
    expect(page.locator(".q-opt[data-q='0'][data-i='2']")).to_be_disabled()
    # rail navigation to another lesson
    page.locator("#rail button.lsn[data-lesson='pai-102']").click()
    expect(page.locator("#lesson-title")).to_have_text("Reading Fleet Telemetry")
    expect(page.locator("#key-points")).to_contain_text("Triage order")


# ------------------------------------------------- practical verify

def test_practical_verify_fails_then_passes_against_backend(
        page: Page, academy_url: str, fake) -> None:
    """Verify hits the live fake: no acked alerts -> fail; after the backend
    gains an acked alert -> pass, and the practical is recorded."""
    _, store = fake                       # baseline seed: no acked alerts
    open_academy(page, academy_url)
    page.locator("#btn-verify").click()
    res = page.locator("#verify-result")
    expect(res).to_be_visible(timeout=10_000)
    expect(res).to_have_class(re.compile(r"\bbad\b"))
    expect(res).to_contain_text("Not verified")
    assert page.evaluate("!!YFA.Progress.data.practicals['pai-101']") is False
    # the operator acks the alert (simulated backend-side, as the console would)
    with store.lock:
        store.tables["alerts"][0]["ack"] = True
    page.locator("#btn-verify").click()
    expect(res).to_have_class(re.compile(r"\bok\b"), timeout=10_000)
    expect(res).to_contain_text("Verified against the live backend")
    assert page.evaluate("!!YFA.Progress.data.practicals['pai-101']") is True
    # the GET actually reached the fake with the pack's filter
    audit = [p for m, p in store.requests if m == "GET" and "/alerts" in p]
    assert any("ack=eq.true" in p for p in audit), f"no verify GET seen: {audit}"


# ------------------------------------------------- progress persistence

def test_progress_persists_across_reload(page: Page, academy_url: str) -> None:
    """Quiz answers + verified state survive a reload via localStorage."""
    open_academy(page, academy_url)
    page.locator(".q-opt[data-q='0'][data-i='1']").click()     # correct
    expect(page.locator(".qq[data-q='0'] .q-explain")).to_contain_text("Correct")
    page.reload()
    expect(page.locator("#lesson-title")).to_have_text(
        "What is Physical AI?", timeout=15_000)     # lastLesson restored
    q1 = page.locator(".qq[data-q='0']")
    expect(q1.locator(".q-explain")).to_be_visible()            # answer restored
    expect(q1.locator(".q-opt[data-i='1']")).to_have_class(re.compile(r"\bok\b"))
    expect(q1.locator(".q-opt[data-i='1']")).to_be_disabled()
    st = page.evaluate("YFA.Progress.data.quiz['pai-101']")
    assert st["answers"]["0"]["correct"] is True


def test_export_progress_downloads_json(page: Page, academy_url: str) -> None:
    """Export downloads a JSON progress file that Import would accept."""
    open_academy(page, academy_url)
    page.locator(".q-opt[data-q='0'][data-i='1']").click()
    with page.expect_download() as dl_info:
        page.locator("#btn-export").click()
    dl = dl_info.value
    assert dl.suggested_filename == "yantrafleet-academy-progress.json"
    data = json.loads(Path(dl.path()).read_text(encoding="utf-8"))
    assert data["app"] == "yantrafleet-academy"
    assert data["pack_id"] == "physical-ai-starter"
    assert data["progress"]["quiz"]["pai-101"]["answers"]["0"]["correct"] is True


# ------------------------------------------------- checkride -> certificate

def test_checkride_happy_path_to_certificate_and_badge(
        page: Page, academy_url: str, fake) -> None:
    """All three steps verify against the seeded backend; passing unlocks the
    printable certificate (deterministic code) and the Open Badge download."""
    _, store = fake
    seed(store, acked_alert=True, pause_cmd=True, crit_unacked=False)
    open_academy(page, academy_url)
    page.locator("#btn-checkride").click()
    expect(page.locator("#v-checkride")).to_contain_text("Operator Checkride I")
    page.locator("#cr-start").click()
    # timer runs, steps advance on verify
    expect(page.locator("#cr-timer")).to_have_text(re.compile(r"^\d\d:\d\d$"))
    for step in (1, 2, 3):
        expect(page.locator("#cr-step h3").first).to_have_text(f"Step {step}")
        page.locator("#cr-verify").click()
        if step < 3:
            expect(page.locator("#cr-step h3").first).to_have_text(
                f"Step {step + 1}", timeout=10_000)
    expect(page.locator("#v-checkride")).to_contain_text("PASSED",
                                                         timeout=10_000)
    expect(page.locator("#cr-score")).to_have_text("100%")
    expect(page.locator("#v-checkride")).to_contain_text("Rubric points earned")
    expect(page.locator("#v-checkride")).to_contain_text(
        "Cleared all critical alerts")
    # certificate
    page.locator("#cert-name").fill(CERT_NAME)
    page.locator("#btn-make-cert").click()
    cert = page.locator("#cert")
    expect(cert).to_be_visible()
    expect(page.locator("#cert-holder")).to_have_text(CERT_NAME)
    expect(page.locator("#cert-score")).to_have_text("100%")
    code = page.locator("#cert-code").inner_text().strip()
    assert re.fullmatch(r"[0-9A-F]{4}(-[0-9A-F]{4}){3}", code), code
    # deterministic: recomputing the hash from the printed fields matches
    date = page.locator("#cert-date").inner_text().strip()
    recomputed = page.evaluate(f"YFA.vcode({CERT_NAME!r} + '|' + {date!r} + '|100')")
    assert recomputed == code
    # print CSS present (certificate-only print view)
    assert "@media print" in page.content()
    # Open Badge JSON (unsigned demo, OB 3.0-shaped)
    with page.expect_download() as dl_info:
        page.locator("#btn-badge").click()
    badge = json.loads(Path(dl_info.value.path()).read_text(encoding="utf-8"))
    assert "OpenBadgeCredential" in badge["type"]
    assert badge["credentialSubject"]["achievement"]["type"] == ["Achievement"]
    assert badge["credentialSubject"]["identifier"][0]["identityHash"] == CERT_NAME
    assert "proof" not in badge                     # explicitly unsigned
    assert "UNSIGNED" in badge["yantrafleet:note"]
    assert badge["yantrafleet:verificationCode"] == code


def test_checkride_failed_steps_below_threshold(
        page: Page, academy_url: str) -> None:
    """Skipping every step scores 0% -> 'not passed' + retry, no cert form."""
    open_academy(page, academy_url)
    page.locator("#btn-checkride").click()
    page.locator("#cr-start").click()
    for _ in range(3):
        page.locator("#cr-skip").click()
    expect(page.locator("#v-checkride")).to_contain_text("not passed")
    expect(page.locator("#cr-score")).to_have_text("0%")
    expect(page.locator("#cr-fail")).to_be_visible()
    expect(page.locator("#cert-name")).to_have_count(0)


# ------------------------------------------------- tutor panel

def test_tutor_canned_answer_includes_lesson_content(
        page: Page, academy_url: str) -> None:
    """Canned tier answers quote the open lesson's tutor_context and stamp
    their source; the header chip shows the active tier."""
    open_academy(page, academy_url)
    expect(page.locator("#tutor-tier")).to_have_text("canned", timeout=15_000)
    page.locator("#tutor-in").fill("what is localization confidence?")
    page.locator("#tutor-send").click()
    ans = page.locator("#tutor-log .msg.ai").last
    expect(ans).to_contain_text("LiDAR", timeout=10_000)      # from tutor_context
    expect(ans).to_contain_text("confidence")
    expect(ans.locator(".src")).to_have_text("canned")
    # switching lessons re-grounds the tutor
    page.locator("#rail button.lsn[data-lesson='pai-103']").click()
    page.locator("#tutor-in").fill("why do commands need approval?")
    page.locator("#tutor-send").click()
    ans2 = page.locator("#tutor-log .msg.ai").last
    expect(ans2).to_contain_text("pending", timeout=10_000)   # lesson-3 context


def test_tutor_grades_free_text_practical(page: Page, academy_url: str) -> None:
    """The self_check lesson grades a free-text answer against key_points and
    flags it as needing review; marking complete ticks the practical."""
    open_academy(page, academy_url)
    page.locator("#rail button.lsn[data-lesson='pai-102']").click()
    expect(page.locator("#practical")).to_contain_text("localization fault")
    page.locator("#sc-answer").fill(
        "First triage the fault: the robot blocks the aisle and forces "
        "reroutes. Check battery levels, then look at vibration and motor "
        "temperature trends for predictive maintenance before re-localizing.")
    page.locator("#btn-grade").click()
    res = page.locator("#grade-result")
    expect(res).to_be_visible(timeout=10_000)
    expect(res).to_contain_text("Score")
    expect(res).to_contain_text("needs review")
    expect(res).to_have_class(re.compile(r"\bok\b"))    # >= 50% rubric hits
    score = page.evaluate(
        "YFTutor.grade(document.querySelector('#sc-answer').value,"
        " YFA.pack.lessons[1].key_points).then(g=>g.score)")
    assert score >= 0.5
    page.locator("#btn-selfdone").click()
    assert page.evaluate("!!YFA.Progress.data.practicals['pai-102']") is True


# ------------------------------------------------- links + backend chip

def test_open_console_link_carries_backend_params(
        page: Page, academy_url: str, fake) -> None:
    """Served over http the header link targets ../index.html (the docroot
    layout: console at /, academy at /academy/) with supa/key/site."""
    base, _ = fake
    open_academy(page, academy_url)
    href = page.locator("#btn-open-console").get_attribute("href")
    assert href is not None and href.startswith("../index.html?"), href
    q = parse_qs(urlsplit(href).query)
    assert q["supa"] == [base]
    assert q["key"] == ["test"]
    assert q["site"] == ["BLR-DC1"]


def test_console_link_returns_to_console_from_docroot(
        page: Page, docroot_server: str, fake) -> None:
    """From the production docroot layout (/academy/index.html) the '⬡ Open
    console' link navigates back to the console at /index.html with the
    supa/key/site/token params intact and the console app boots LIVE."""
    base, _ = fake
    page.goto(f"{docroot_server}/academy/index.html"
              f"?supa={base}&key=test&site=BLR-DC1&token=tok-e2e")
    expect(page.locator("#lesson-title")).to_be_visible(timeout=15_000)
    href = page.locator("#btn-open-console").get_attribute("href")
    assert href is not None and href.startswith("../index.html?"), href
    # keep the console's first-run tour out of the popup
    page.context.add_init_script(
        "try{localStorage.setItem('yf_tour_done','1')}catch(e){}")
    with page.expect_popup() as pop:
        page.locator("#btn-open-console").click()
    console = pop.value
    expect(console.locator("#cloud-lbl")).to_contain_text("LIVE",
                                                          timeout=15_000)
    parts = urlsplit(console.url)
    assert parts.path == "/index.html", console.url
    q = parse_qs(parts.query)
    assert q["supa"] == [base]
    assert q["key"] == ["test"]
    assert q["site"] == ["BLR-DC1"]
    assert q["token"] == ["tok-e2e"]


def test_backend_chip_offline_with_dead_backend(
        page: Page, bare_academy_server: str) -> None:
    """A dead backend port -> 'backend: offline' chip; lessons, quiz and the
    embedded pack still work; Verify reports a clear failure, no page errors."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{bare_academy_server}/index.html"
              f"?supa=http://127.0.0.1:{_dead_port()}&key=test")
    expect(page.locator("#lesson-title")).to_be_visible(timeout=15_000)
    expect(page.locator("#be-lbl")).to_have_text("backend: offline",
                                                 timeout=15_000)
    page.locator("#btn-verify").click()
    res = page.locator("#verify-result")
    expect(res).to_have_class(re.compile(r"\bbad\b"), timeout=10_000)
    expect(res).to_contain_text("backend unreachable")
    assert errors == [], f"uncaught page errors: {errors}"
