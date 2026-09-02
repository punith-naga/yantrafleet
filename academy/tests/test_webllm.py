"""WebLLM tutor-tier tests (academy/index.html) — fully network-free.

The real tier dynamic-import()s @mlc-ai/web-llm from a CDN only after the
user opts in. These tests never touch the network: before the page loads,
``page.add_init_script`` installs

  * a WebGPU stub (``navigator.gpu``) so the opt-in card's preflight passes,
  * a generous ``navigator.storage.estimate`` stub so the quota preflight
    passes deterministically (container disk quotas are tiny), and
  * ``window.__YF_WEBLLM_FACTORY`` — the documented test/self-host seam the
    app checks BEFORE any CDN import — returning a scripted fake engine
    (streamed chat chunks + a JSON grading reply).

Every request the app makes to the fake engine is recorded on
``window.__YF_WEBLLM_CALLS`` so prompts/params can be asserted.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from test_academy import open_academy

# --------------------------------------------------------------- stubs

GPU_STUB = """
Object.defineProperty(navigator, 'gpu',
  {value: {requestAdapter: async () => ({})}, configurable: true});
Object.defineProperty(navigator, 'deviceMemory',
  {value: 16, configurable: true});
if (navigator.storage) {
  navigator.storage.estimate = async () => ({quota: 50e9, usage: 1e9});
}
"""

NO_GPU_STUB = """
Object.defineProperty(navigator, 'gpu', {value: undefined, configurable: true});
"""

FAKE_ENGINE = """
window.__YF_WEBLLM_CALLS = [];
window.__YF_WEBLLM_UNLOADED = false;
window.__YF_WEBLLM_FACTORY = () => ({
  CreateMLCEngine: async (model, opts) => {
    window.__YF_WEBLLM_MODEL = model;
    if (opts && opts.initProgressCallback) {
      opts.initProgressCallback({progress: 0.42, text: 'Fetching params 42%'});
      opts.initProgressCallback({progress: 1, text: 'Finish loading on WebGPU'});
    }
    return {
      chat: {completions: {create: async (req) => {
        window.__YF_WEBLLM_CALLS.push(req);
        if (req.response_format && req.response_format.type === 'json_object') {
          return {choices: [{message: {content: JSON.stringify({
            score: 0.8, hits: ['rubric-hit-1'],
            feedback: 'Good triage instincts - FAKE-GRADE-OK.'})}}]};
        }
        const sys = (req.messages && req.messages[0] &&
                     req.messages[0].content) || '';
        const grounded = sys.includes('LiDAR') ? 'LiDAR' : 'ungrounded';
        const chunks = ['GROUNDED-FAKE ', 'streamed ', 'answer: ',
                        grounded, '.'];
        if (req.stream) {
          return (async function* () {
            for (const c of chunks) {
              yield {choices: [{delta: {content: c}}]};
            }
          })();
        }
        return {choices: [{message: {content: chunks.join('')}}]};
      }}},
      unload: async () => { window.__YF_WEBLLM_UNLOADED = true; },
    };
  },
});
"""

FAILING_FACTORY = """
window.__YF_WEBLLM_FACTORY = () => ({
  CreateMLCEngine: async () => { throw new Error('fake OOM: device lost'); },
});
"""


def enable_webllm(page: Page) -> None:
    """Opt in via the panel card and wait for the tier to flip."""
    expect(page.locator("#webllm-card")).to_be_visible()
    page.locator("#webllm-enable").click()
    expect(page.locator("#tutor-tier")).to_have_text("webllm", timeout=10_000)


# --------------------------------------------------------------- tests

def test_optin_card_appears_when_webgpu_present(
        page: Page, academy_url: str) -> None:
    """WebGPU stubbed present -> the opt-in card renders with the download
    disclosure and the device-appropriate model ladder (16 GB -> 3B offered),
    defaulting to Qwen 1.5B. Nothing is enabled yet: tier stays canned."""
    page.add_init_script(GPU_STUB + FAKE_ENGINE)
    open_academy(page, academy_url)
    card = page.locator("#webllm-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Enable on-device tutor")
    expect(card).to_contain_text("~1.7 GB once")
    expect(card).to_contain_text("fully offline")
    opts = page.locator("#webllm-model option")
    expect(opts).to_have_count(3)                       # deviceMemory=16 -> 3B too
    values = [opts.nth(i).get_attribute("value") for i in range(3)]
    assert values == [
        "Llama-3.2-1B-Instruct-q4f16_1-MLC",
        "Qwen2.5-1.5B-Instruct-q4f16_1-MLC",
        "Llama-3.2-3B-Instruct-q4f16_1-MLC",
    ]
    assert page.locator("#webllm-model").input_value() == \
        "Qwen2.5-1.5B-Instruct-q4f16_1-MLC"
    # opt-in only: no factory call, no engine, tier still canned
    assert page.evaluate("window.__YF_WEBLLM_MODEL") is None
    expect(page.locator("#tutor-tier")).to_have_text("canned")
    expect(page.locator("#tutor-tier-pill")).to_have_text("offline rules")


def test_optin_card_hidden_without_webgpu(page: Page, academy_url: str) -> None:
    """No navigator.gpu -> preflight fails -> no card, tutor works as before.
    Chrome clamps deviceMemory to 8, so the 3B model is never offered here."""
    page.add_init_script(NO_GPU_STUB)
    open_academy(page, academy_url)
    expect(page.locator("#webllm-card")).to_be_hidden()
    # model ladder floor: without >=16 GB only 1B + 1.5B are listed
    expect(page.locator("#webllm-model option")).to_have_count(2)
    expect(page.locator("#tutor-tier")).to_have_text("canned")


def test_enable_shows_progress_then_on_device_tier(
        page: Page, academy_url: str) -> None:
    """Enabling drives initProgressCallback into the progress bar/status and
    flips the ladder to webllm: pill 'on-device', choice persisted."""
    page.add_init_script(GPU_STUB + FAKE_ENGINE)
    open_academy(page, academy_url)
    enable_webllm(page)
    assert page.evaluate("window.__YF_WEBLLM_MODEL") == \
        "Qwen2.5-1.5B-Instruct-q4f16_1-MLC"
    # progress reached 100% and the bar + status reflect it
    assert page.evaluate("YFWebLLM.progress") == 1
    expect(page.locator("#webllm-prog")).to_be_visible()
    assert page.locator("#webllm-prog-fill").evaluate(
        "el => el.style.width") == "100%"
    expect(page.locator("#webllm-status")).to_contain_text("ready")
    # tier surfaces: header chip raw tier, panel pill friendly label
    expect(page.locator("#tutor-tier-pill")).to_have_text("on-device")
    expect(page.locator("#tutor-tier-pill")).to_have_class(
        re.compile(r"\bwebllm\b"))
    assert page.evaluate("YFTutor.status()") == "webllm"
    # card switched to the active row; opt-in persisted in localStorage
    expect(page.locator("#webllm-active")).to_be_visible()
    expect(page.locator("#webllm-optin")).to_be_hidden()
    pref = page.evaluate(
        "JSON.parse(localStorage.getItem('yfa_webllm_v1'))")
    assert pref == {"enabled": True,
                    "model": "Qwen2.5-1.5B-Instruct-q4f16_1-MLC"}


def test_ask_streams_grounded_answer(page: Page, academy_url: str) -> None:
    """ask() streams the fake's chunks into the chat bubble; the final bubble
    carries the grounded marker (fake echoes 'LiDAR' only when the system
    prompt contained the lesson context) and a webllm source pill."""
    page.add_init_script(GPU_STUB + FAKE_ENGINE)
    open_academy(page, academy_url)
    enable_webllm(page)
    page.locator("#tutor-in").fill("how does localization work?")
    page.locator("#tutor-send").click()
    ans = page.locator("#tutor-log .msg.ai").last
    expect(ans).to_contain_text("GROUNDED-FAKE streamed answer: LiDAR.",
                                timeout=10_000)
    expect(ans.locator(".src")).to_have_text("webllm")
    # the request the engine saw: Guru system prompt + LESSON grounding, streamed
    req = page.evaluate("window.__YF_WEBLLM_CALLS[0]")
    assert req["stream"] is True
    assert req["temperature"] == 0.2
    sys = req["messages"][0]["content"]
    assert sys.startswith("You are Guru, the YantraFleet Academy tutor.")
    assert "Answer ONLY from the LESSON section" in sys
    assert "suggest asking Sarathi" in sys
    assert "LiDAR" in sys                     # pai-101 tutor_context made it in
    assert req["messages"][1]["content"] == "how does localization work?"


def test_grade_renders_json_score_and_feedback(
        page: Page, academy_url: str) -> None:
    """grade() uses response_format json_object; the parsed score + feedback
    render in the practical, replacing the canned 'needs review' grader."""
    page.add_init_script(GPU_STUB + FAKE_ENGINE)
    open_academy(page, academy_url)
    enable_webllm(page)
    page.locator("#rail button.lsn[data-lesson='pai-102']").click()
    page.locator("#sc-answer").fill("Triage the fault first, clear the aisle.")
    page.locator("#btn-grade").click()
    res = page.locator("#grade-result")
    expect(res).to_be_visible(timeout=10_000)
    expect(res).to_contain_text("Score 80%")
    expect(res).to_contain_text("FAKE-GRADE-OK")
    expect(res).to_contain_text("on-device grade")
    expect(res).not_to_contain_text("needs review")     # not the canned grader
    expect(res).to_have_class(re.compile(r"\bok\b"))
    grade_req = page.evaluate(
        "window.__YF_WEBLLM_CALLS.find("
        "c => c.response_format && c.response_format.type === 'json_object')")
    assert grade_req is not None
    assert "RUBRIC:" in grade_req["messages"][1]["content"]
    assert "Triage the fault first" in grade_req["messages"][1]["content"]
    # rubric (key_points) + the self_check question reached the prompt
    assert "predictive maintenance" in grade_req["messages"][1]["content"]
    assert "localization fault" in grade_req["messages"][1]["content"]


def test_init_failure_toasts_once_and_demotes(
        page: Page, academy_url: str) -> None:
    """CreateMLCEngine rejecting (fake OOM) -> one toast, tier stays on the
    previous rung (canned here), chat keeps answering, opt-in not persisted."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(GPU_STUB + FAILING_FACTORY)
    open_academy(page, academy_url)
    page.locator("#webllm-enable").click()
    toast = page.locator("#toasts .toast")
    expect(toast).to_have_count(1, timeout=10_000)
    expect(toast).to_contain_text("On-device tutor failed to start")
    expect(toast).to_contain_text("fake OOM")
    expect(toast).to_contain_text("canned tier")
    # demoted gracefully: still canned, card back to the opt-in state
    expect(page.locator("#tutor-tier")).to_have_text("canned")
    expect(page.locator("#tutor-tier-pill")).to_have_text("offline rules")
    expect(page.locator("#webllm-enable")).to_be_enabled()
    assert page.evaluate("localStorage.getItem('yfa_webllm_v1')") is None
    # chat is not broken: the canned tier still answers
    page.locator("#tutor-in").fill("what is localization confidence?")
    page.locator("#tutor-send").click()
    ans = page.locator("#tutor-log .msg.ai").last
    expect(ans).to_contain_text("LiDAR", timeout=10_000)
    expect(ans.locator(".src")).to_have_text("canned")
    assert errors == [], f"uncaught page errors: {errors}"


def test_disable_unloads_engine_and_demotes(
        page: Page, academy_url: str) -> None:
    """'Disable on-device tutor' calls engine.unload(), clears the persisted
    flag and demotes the ladder back to the previous tier."""
    page.add_init_script(GPU_STUB + FAKE_ENGINE)
    open_academy(page, academy_url)
    enable_webllm(page)
    page.locator("#webllm-disable").click()
    expect(page.locator("#tutor-tier")).to_have_text("canned", timeout=10_000)
    expect(page.locator("#tutor-tier-pill")).to_have_text("offline rules")
    assert page.evaluate("window.__YF_WEBLLM_UNLOADED") is True
    assert page.evaluate("localStorage.getItem('yfa_webllm_v1')") is None
    expect(page.locator("#webllm-optin")).to_be_visible()
    # asking again falls back to the canned tier cleanly
    page.locator("#tutor-in").fill("what is localization confidence?")
    page.locator("#tutor-send").click()
    expect(page.locator("#tutor-log .msg.ai").last.locator(".src")
           ).to_have_text("canned", timeout=10_000)
