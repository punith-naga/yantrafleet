"""Browser tests for the Academy's accounts layer (window.YFAuth).

Covers the accounts contract end to end, hermetically:

* guest mode is EXACTLY the pre-accounts behaviour (local progress only,
  zero RPC traffic, anon-key requests);
* sign-in via the GoTrue stand-in from conftest (fakerest serves only
  /rest/v1/*, so ?auth= points the page at the stub) puts the email in
  the header and the session under the SHARED localStorage key yf_auth_v1;
* signed-in progress pushes through rpc/save_progress — immediately on
  login and debounced (2 s) on later changes — asserted against the
  fake's ``progress`` store;
* a checkride pass while signed in records the certificate through
  rpc/issue_certificate (row + code asserted), and a duplicate
  verification code (409) regenerates the code exactly once;
* in RBAC mode (0007 emulation) the practical verify succeeds only with
  the JWT attached — as a guest the RLS'd read is empty and verify fails.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from conftest import AUTH_USERS, seed, wait_until

EMAIL = "priya@example.com"
PASSWORD = AUTH_USERS[EMAIL]["password"]
PACK_ID = "physical-ai-starter"          # the embedded starter pack
CERT_NAME = "Priya Operator"


def open_academy(page: Page, url: str) -> None:
    page.goto(url)
    expect(page.locator("#lesson-title")).to_be_visible(timeout=15_000)


def sign_in(page: Page, email: str = EMAIL, password: str = PASSWORD) -> None:
    page.locator("#btn-signin").click()
    expect(page.locator("#auth-modal")).to_be_visible()
    page.locator("#auth-em").fill(email)
    page.locator("#auth-pw").fill(password)
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-modal")).to_be_hidden(timeout=10_000)
    expect(page.locator("#auth-chip")).to_be_visible()


def rpc_posts(store, fn: str | None = None) -> list[str]:
    with store.lock:
        paths = [p for m, p in store.requests if m == "POST" and "/rpc/" in p]
    return [p for p in paths if fn is None or p.endswith("/rpc/" + fn)]


# ------------------------------------------------------------------ guest

def test_guest_mode_stays_local_and_makes_no_rpc_calls(
        page: Page, academy_url: str, fake) -> None:
    """Without signing in the app behaves exactly as before accounts:
    progress stays in localStorage, nothing is pushed, and 'continue as
    guest' simply closes the modal."""
    _, store = fake
    open_academy(page, academy_url)
    # header offers Sign in; no signed-in chip
    expect(page.locator("#btn-signin")).to_be_visible()
    expect(page.locator("#auth-chip")).to_be_hidden()
    # the modal's guest escape hatch closes it without a session
    page.locator("#btn-signin").click()
    expect(page.locator("#auth-modal")).to_be_visible()
    page.locator("#auth-guest").click()
    expect(page.locator("#auth-modal")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('yf_auth_v1')") is None
    # answer a quiz question -> progress lands locally
    page.locator(".q-opt[data-q='0'][data-i='1']").click()
    assert page.evaluate(
        "YFA.Progress.data.quiz['pai-101'].answers['0'].correct") is True
    expect(page.locator("#rail")).to_contain_text("localStorage")
    # outlive the 2 s sync debounce: still zero RPC traffic, empty store
    page.wait_for_timeout(2_600)
    assert rpc_posts(store) == []
    assert store.progress == {}


# ------------------------------------------------------------------ sign-in

def test_signin_shows_email_and_signout_restores_guest(
        page: Page, academy_url: str, auth_stub: str, fake) -> None:
    """Bad password -> inline error; good login -> email + role in the
    header and a yf_auth_v1 session (SHARED key with the console);
    sign-out returns to guest mode."""
    open_academy(page, f"{academy_url}&auth={auth_stub}")
    page.locator("#btn-signin").click()
    page.locator("#auth-em").fill(EMAIL)
    page.locator("#auth-pw").fill("wrong-password")
    page.locator("#auth-submit").click()
    err = page.locator("#auth-err")
    expect(err).to_be_visible(timeout=10_000)
    expect(err).to_contain_text("Invalid login credentials")
    expect(page.locator("#auth-modal")).to_be_visible()   # still open
    # correct password
    page.locator("#auth-pw").fill(PASSWORD)
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-modal")).to_be_hidden(timeout=10_000)
    expect(page.locator("#auth-email")).to_have_text(f"{EMAIL} · operator")
    expect(page.locator("#btn-signin")).to_be_hidden()
    expect(page.locator("#rail")).to_contain_text(f"progress syncs to {EMAIL}")
    sess = page.evaluate("JSON.parse(localStorage.getItem('yf_auth_v1'))")
    assert sess["email"] == EMAIL
    assert sess["role"] == "operator"
    assert sess["jwt"].startswith("yf-test.")
    assert sess["refresh"] == f"rt-{EMAIL}"
    # sign out -> guest again, session cleared
    page.locator("#btn-signout").click()
    expect(page.locator("#btn-signin")).to_be_visible()
    expect(page.locator("#auth-chip")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('yf_auth_v1')") is None


# ------------------------------------------------------------------ signup

NEW_EMAIL = "rookie@example.com"
NEW_PASS = "fleet-pass-9"


def open_signup(page: Page, email: str = NEW_EMAIL,
                password: str = NEW_PASS) -> None:
    """Open the modal, switch to 'Create account', fill and submit."""
    page.locator("#btn-signin").click()
    expect(page.locator("#auth-modal")).to_be_visible()
    page.locator("#auth-tab-up").click()
    expect(page.locator("#auth-pw2")).to_be_visible()   # confirm field appears
    page.locator("#auth-em").fill(email)
    page.locator("#auth-pw").fill(password)
    page.locator("#auth-pw2").fill(password)
    page.locator("#auth-submit").click()


def test_signup_instant_session_signs_in(
        page: Page, academy_url: str, auth_stub_obj, fake) -> None:
    """Signup outcome (a): the stub returns a session (email confirmation
    OFF) -> signed in immediately, email + role in the header, session under
    the shared yf_auth_v1 key."""
    base, stub = auth_stub_obj
    stub.signup_mode = "session"
    open_academy(page, f"{academy_url}&auth={base}")
    open_signup(page)
    expect(page.locator("#auth-modal")).to_be_hidden(timeout=10_000)
    expect(page.locator("#auth-email")).to_have_text(f"{NEW_EMAIL} · operator")
    sess = page.evaluate("JSON.parse(localStorage.getItem('yf_auth_v1'))")
    assert sess["email"] == NEW_EMAIL
    assert sess["jwt"].startswith("yf-test.")
    assert "signup" in stub.requests


def test_signup_confirmation_required_shows_message_then_signin(
        page: Page, academy_url: str, auth_stub_obj, fake) -> None:
    """Signup outcome (b): user created but NO session (confirmation ON, the
    Supabase default) -> the clear 'check your email' message, no session —
    and signing in afterwards on the other tab works."""
    base, stub = auth_stub_obj
    stub.signup_mode = "confirm"
    open_academy(page, f"{academy_url}&auth={base}")
    open_signup(page)
    msg = page.locator("#auth-msg")
    expect(msg).to_be_visible(timeout=10_000)
    expect(msg).to_contain_text(
        "Account created — check your email to confirm, then sign in")
    expect(msg).to_contain_text("disable Confirm email in Supabase")
    expect(page.locator("#auth-modal")).to_be_visible()   # still open
    assert page.evaluate("localStorage.getItem('yf_auth_v1')") is None
    # ... then sign in (account exists server-side after 'confirming')
    page.locator("#auth-tab-in").click()
    page.locator("#auth-submit").click()                  # fields still filled
    expect(page.locator("#auth-modal")).to_be_hidden(timeout=10_000)
    expect(page.locator("#auth-email")).to_have_text(f"{NEW_EMAIL} · operator")


def test_signup_duplicate_user_error_surfaces(
        page: Page, academy_url: str, auth_stub: str, fake) -> None:
    """Signup with an existing email surfaces GoTrue's 422 message and the
    page stays in guest mode."""
    open_academy(page, f"{academy_url}&auth={auth_stub}")
    open_signup(page, email=EMAIL)                        # already registered
    err = page.locator("#auth-err")
    expect(err).to_be_visible(timeout=10_000)
    expect(err).to_contain_text("User already registered")
    expect(page.locator("#auth-modal")).to_be_visible()
    assert page.evaluate("localStorage.getItem('yf_auth_v1')") is None


# ------------------------------------------------------------- progress sync

def test_progress_pushes_on_login_then_debounced_on_change(
        page: Page, academy_url: str, auth_stub: str, fake) -> None:
    """Pre-login local progress is pushed via rpc/save_progress right after
    sign-in; later changes push again, debounced — the fake's progress
    store ends up holding the local blob (local-first, save-only sync)."""
    _, store = fake
    open_academy(page, f"{academy_url}&auth={auth_stub}")
    page.locator(".q-opt[data-q='0'][data-i='1']").click()   # local, pre-login
    sign_in(page)
    wait_until(lambda: PACK_ID in store.progress.get(EMAIL, {}),
               "save_progress upsert after login")
    row = store.progress[EMAIL][PACK_ID]
    assert row["pack_id"] == PACK_ID
    assert row["data"]["quiz"]["pai-101"]["answers"]["0"]["correct"] is True
    assert len(rpc_posts(store, "save_progress")) >= 1
    # a new change pushes again within the 2 s debounce window
    page.locator(".q-opt[data-q='1'][data-i='1']").click()
    wait_until(
        lambda: "1" in store.progress[EMAIL][PACK_ID]
                       ["data"]["quiz"]["pai-101"]["answers"],
        "debounced save_progress push after a quiz answer")
    assert store.progress[EMAIL][PACK_ID]["data"]["quiz"]["pai-101"]["score"] == 2


# ----------------------------------------------------- checkride certificate

def _pass_checkride(page: Page) -> None:
    page.locator("#btn-checkride").click()
    page.locator("#cr-start").click()
    for step in (1, 2, 3):
        expect(page.locator("#cr-step h3").first).to_have_text(f"Step {step}")
        page.locator("#cr-verify").click()
    expect(page.locator("#v-checkride")).to_contain_text("PASSED",
                                                         timeout=10_000)


def test_checkride_cert_recorded_to_account(
        page: Page, academy_url: str, auth_stub: str, fake) -> None:
    """Passing the checkride while signed in calls rpc/issue_certificate:
    the fake gains a certificates row whose code matches the rendered one,
    and the page confirms 'Recorded to your account'."""
    _, store = fake
    seed(store, acked_alert=True, pause_cmd=True, crit_unacked=False)
    open_academy(page, f"{academy_url}&auth={auth_stub}")
    sign_in(page)
    _pass_checkride(page)
    page.locator("#cert-name").fill(CERT_NAME)
    page.locator("#btn-make-cert").click()
    expect(page.locator("#cert")).to_be_visible()
    wait_until(lambda: len(store.certificates) == 1,
               "issue_certificate row on the backend")
    row = store.certificates[0]
    code = page.locator("#cert-code").inner_text().strip()
    assert re.fullmatch(r"[0-9A-F]{4}(-[0-9A-F]{4}){3}", code), code
    assert row["verification_code"] == code
    assert row["score"] == 100
    assert row["track"] == "Fleet Operator Foundations"
    assert row["user_id"] not in (None, "anon")     # tied to the account
    rec = page.locator("#cert-recorded")
    expect(rec).to_be_visible(timeout=10_000)
    expect(rec).to_contain_text(EMAIL)


def test_duplicate_cert_code_regenerates_once(
        page: Page, academy_url: str, auth_stub: str, fake) -> None:
    """A 409 from issue_certificate (verification code already recorded)
    regenerates the code once (salted hash) and records under the new one;
    the rendered certificate shows the new code."""
    _, store = fake
    seed(store, acked_alert=True, pause_cmd=True, crit_unacked=False)
    open_academy(page, f"{academy_url}&auth={auth_stub}")
    sign_in(page)
    _pass_checkride(page)
    # occupy the exact code the app is about to generate (name|date|score)
    clash = page.evaluate(
        "YFA.vcode(%r + '|' + new Date().toISOString().slice(0,10) + '|100')"
        % CERT_NAME)
    with store.lock:
        store.certificates.append(
            {"id": "pre-existing", "user_id": "someone-else",
             "verification_code": clash, "track": "x", "score": 1})
    page.locator("#cert-name").fill(CERT_NAME)
    page.locator("#btn-make-cert").click()
    expect(page.locator("#cert")).to_be_visible()
    wait_until(lambda: len(store.certificates) == 2,
               "regenerated certificate recorded after the 409")
    new_row = store.certificates[1]
    assert new_row["verification_code"] != clash
    shown = page.locator("#cert-code").inner_text().strip()
    assert shown == new_row["verification_code"]
    expect(page.locator("#cert-recorded")).to_be_visible(timeout=10_000)
    # exactly one 409 -> exactly two issue_certificate calls, no loop
    assert len(rpc_posts(store, "issue_certificate")) == 2


# ------------------------------------------------------------------ RBAC

def test_rbac_practical_verify_needs_jwt(
        page: Page, bare_academy_server: str, rbac_fake, auth_stub: str) -> None:
    """Against an RBAC (0007) backend the anon key sees nothing, so a guest
    verify fails even though the row exists; signing in attaches the JWT to
    the same GET and the verify passes."""
    base, store = rbac_fake
    seed(store, acked_alert=True)         # an acked alert exists server-side
    url = (f"{bare_academy_server}/index.html"
           f"?supa={base}&key=test&site=BLR-DC1&auth={auth_stub}")
    open_academy(page, url)
    # guest: RLS'd read comes back empty -> not verified
    page.locator("#btn-verify").click()
    res = page.locator("#verify-result")
    expect(res).to_be_visible(timeout=10_000)
    expect(res).to_have_class(re.compile(r"\bbad\b"))
    expect(res).to_contain_text("0 matching row(s)")
    assert page.evaluate("!!YFA.Progress.data.practicals['pai-101']") is False
    # signed in: same button, same backend — now the JWT rides along
    sign_in(page)
    page.locator("#btn-verify").click()
    expect(res).to_have_class(re.compile(r"\bok\b"), timeout=10_000)
    expect(res).to_contain_text("Verified against the live backend")
    assert page.evaluate("!!YFA.Progress.data.practicals['pai-101']") is True
    # the passing GET carried the yf-test JWT (not the anon key)
    with store.lock:
        n_authed = sum(1 for m, p in store.requests
                       if m == "GET" and "ack=eq.true" in p)
    assert n_authed >= 2                  # one guest attempt + one signed-in
