# YantraFleet Academy

Single-file learning app (`index.html`) — *learn Physical AI by operating a
live fleet*. Same design language and zero-build philosophy as the console:
one HTML file, one inline `<script>` block, no dependencies.

## How to open

```bash
cd academy
python3 -m http.server 8090
# browse to http://localhost:8090/
```

Opening `index.html` via `file://` also works: the content-pack fetch fails
there, so the app falls back to its **embedded starter pack** (3 lessons +
a checkride) — everything except backend-verified practicals works offline.

## URL parameters (same conventions as the console)

| param    | effect                                                          |
|----------|-----------------------------------------------------------------|
| `?supa=` | PostgREST base URL used by practical/checkride Verify checks    |
| `?key=`  | anon key sent as `apikey` + `Authorization: Bearer`             |
| `?site=` | exposed as `window.SITE` (no filtering yet, matching console)   |
| `?token=`| bearer token for the sarathi tutor tier (`localhost:8001`)      |
| `?auth=` | auth base URL (`{auth}/auth/v1/token`); defaults to `?supa=`'s URL — tests point it at a stub |

The **⬡ Open console** header button links back to the console and carries
`supa`/`key`/`site`/`token` through, so both apps talk to the same backend.
The href is computed from `location`: served under the production docroot
(yantraops static server / nginx template — console at `/index.html`,
academy at `/academy/index.html`) it is `../index.html`; opened from a
`file://` repo checkout it is the sibling `../console/index.html`.

## Content pack schema (v1)

The lesson engine loads `./content/pack-physical-ai.json`. On any failure
(404, `file://`, invalid JSON, schema mismatch) it silently uses the embedded
pack, and the header pill shows `embedded starter pack` instead of the pack
title/version. **Content authors: this is the contract.**

```jsonc
{
  "pack": {
    "id": "physical-ai-101",          // stable pack id (string)
    "title": "Physical AI 101",       // shown in the header pill
    "version": "1.0.0",               // shown in the header pill
    "tracks": [
      {
        "id": "trk-operator",
        "title": "Fleet Operator Foundations",
        "role": "Operator",           // audience label shown in the rail
        "lesson_ids": ["l-1", "l-2"]  // ordered; lessons not referenced by
      }                               // any track still render under
    ],                                // a "More lessons" rail section
    "lessons": [
      {
        "id": "l-1",                  // unique within the pack
        "title": "What is Physical AI?",
        "minutes": 8,                 // estimated reading time
        "track": "trk-operator",      // informational back-reference
        "body_html": "<p>…</p>",      // SANITIZED subset — see below
        "key_points": ["…", "…"],     // bullet card + canned-tutor corpus
                                      // + grading rubric for self_check
        "quiz": [                     // optional; [] or omitted = no quiz
          {
            "q": "Question text?",
            "options": ["A", "B", "C", "D"],
            "answer_idx": 1,          // 0-based index into options
            "explain": "Shown instantly after answering (right or wrong)."
          }
        ],
        "practical": {                // optional; omitted = reading lesson
          "instructions_html": "<p>…</p>",   // sanitized subset too
          "verify":                   // ONE of the two shapes:
            { "kind": "backend_check",
              "table": "alerts",                       // PostgREST table
              "filter": "ack=eq.true&select=id&limit=10", // raw query string
              "expect": "rows>0" }                     // see expect table
            // or
            // { "kind": "self_check",
            //   "prompt": "Free-text question graded by the tutor rubric" }
        },
        "tutor_context": "Plain text, <= 1500 chars. The canned tutor answers\nby keyword-matching sentences from here + key_points; smarter tiers\nprepend it to the model prompt."
      }
    ],
    "checkride": {                    // optional; omitted = no exam section
      "id": "cr-1",
      "title": "Operator Checkride I",
      "pass_threshold": 0.66,         // fraction of steps that must verify
      "steps": [
        {
          "instruction_html": "<p>…</p>",       // sanitized subset
          "verify": { "kind": "backend_check", "table": "…",
                      "filter": "…", "expect": "rows>0" },
                                      // same shape as lesson verify;
                                      // self_check steps show a
                                      // "Mark step done" honour button
          "rubric_points": ["…"]      // listed on the step; earned points
        }                             // of passed steps show in the result
      ]
    }
  }
}
```

### `verify.expect` values (backend_check)

The check runs `GET <supa>/rest/v1/<table>?<filter>` with the configured key.

| expect         | passes when                                  | extra fields        |
|----------------|----------------------------------------------|---------------------|
| `rows>0`       | at least one row matched                     | —                   |
| `rows==0`      | zero rows matched                            | —                   |
| `field_equals` | `String(rows[0][field]) === String(value)`   | `field`, `value`    |

`filter` is a **raw PostgREST query string** (e.g.
`sev=eq.crit&ack=eq.false&select=id&limit=10`) — always include a `select=`
and a `limit=` to keep checks cheap. Backend errors/timeouts render a clear
"not verified" message; they never mark a practical passed.

### `body_html` / `instructions_html` sanitized subset

Allowed tags: `p br b strong i em code pre ul ol li h3 h4 span a`.
All attributes are stripped except `href` on `<a>` (http(s)/relative/`#`
only; `javascript:` etc. is removed). `script/style/iframe/form/…` elements
are deleted outright; other unknown tags are unwrapped (text kept). Don't
rely on classes or inline styles — they will not survive.

## Progress

Quiz answers/scores, verified practicals, checkride results and the issued
certificate live in `localStorage` (`yfa_progress_v1`, all access
try/catch-guarded — private-mode browsers just don't persist). The rail
footer has **Export** (downloads
`yantrafleet-academy-progress.json`) and **Import** (accepts that file, or a
raw progress object) plus **Reset**.

- A lesson is *complete* (✓ in the rail) when its quiz is fully answered
  **and** its practical is verified/marked done (missing parts count as done).
- The quiz records the **first** answer per question; explanations always show.

## Accounts (`window.YFAuth`) — optional; guest mode is the default

The header's **Sign in** button opens a modal with **Sign in | Create
account** tabs (or **Continue as guest**). Sign-in is a Supabase password
grant —
`POST {AUTH_URL}/auth/v1/token?grant_type=password` with the anon key as
`apikey` — and the session `{jwt, refresh, email, role}` is stored under the
localStorage key **`yf_auth_v1`**, *shared with the console*, so one login
serves both apps on the same origin. `role` comes from the JWT claims
(`yf_role`, as the test tokens and `user_roles`-aware deployments provide).
On any 401 the access token is refreshed **once**
(`grant_type=refresh_token`) and the request retried; a failed refresh signs
out back to guest mode.

**Create account** (email + password + confirm) POSTs
`{AUTH_URL}/auth/v1/signup` — the same contract as the console — and handles
both real Supabase outcomes: a **session** in the response (email
confirmation disabled) signs in immediately; a **user without a session**
(confirmation on — the Supabase default) shows *“Account created — check
your email to confirm, then sign in. (Project owners: disable Confirm email
in Supabase → Authentication → Sign In / Up for instant signup.)”* Server
errors (weak password, *User already registered*) surface verbatim; on the
loopback demo backend (no `/auth/v1`) both tabs explain guest/demo mode
instead of dumping a raw error.

**Guest mode is exactly the pre-accounts behaviour** — progress in
localStorage only, all requests on the anon key, nothing degraded.

While signed in:

- **Requests carry the JWT** — every PostgREST GET/RPC sends
  `Authorization: Bearer <jwt>` (`apikey` stays the anon key). This is what
  makes practical/checkride verifies work against RBAC-hardened backends
  (`supabase/0007_rbac.sql`), where anon reads return nothing.
- **Progress sync is save-only and local-first.** localStorage remains the
  source of truth; every change pushes the whole progress blob via
  `POST /rest/v1/rpc/save_progress` (debounced 2 s), plus one immediate push
  right after login so pre-login progress lands on the account.
  *Deliberate tradeoff:* the app never pulls the server copy — under 0007
  the read path may be revoked or RLS-filtered per deployment, so a reliable
  pull can't be assumed; save-only last-writer-wins is the honest contract.
  A brand-new browser therefore starts empty even if the account has
  server-side rows — use **Export**/**Import** to migrate a browser.
- **Certificates are recorded to the account.** A checkride pass calls
  `rpc/issue_certificate` (track, score, verification code) and the cert
  shows *"Recorded to your account"*. A duplicate-code `409` (same
  name+date+score already recorded) regenerates the code once with a salt
  and retries; other failures keep the local certificate and say so.

## Checkride, certificate, Open Badge

The checkride runs its steps sequentially with a visible timer. Each step is
verified against the live backend (retry allowed; **Skip** records a fail).
Score = verified steps / total steps; passing `pass_threshold` unlocks the
certificate: enter a name and a printable certificate is issued (print CSS
hides the app chrome — `Ctrl/Cmd+P` prints just the cert).

- **Verification code** — deterministic hash (djb2 + FNV-1a) of
  `name|date|score%`, printed on the cert so the fields can be re-hashed to
  spot tampering. Demo-grade, not cryptographic.
- **Download Open Badge JSON** — an
  [Open Badges 3.0](https://www.imsglobal.org/spec/ob/v3p0)-shaped
  `OpenBadgeCredential` assertion. **Unsigned demo**: it carries no `proof`
  property and a `yantrafleet:note` saying so — a real deployment would sign
  it server-side.

## Tutor tiers (`window.YFTutor`)

Stable interface — the UI only ever calls these four methods, so the tutor
internals can be replaced without touching the rest of the app:

```js
YFTutor.status()                    // -> 'canned' | 'sarathi' | 'webllm'
YFTutor.init(opts?)                 // probes tiers; opts.sarathiUrl override
YFTutor.ask(question, lessonCtx)    // -> Promise<{answer, source}>
YFTutor.grade(answer, rubric[])     // -> Promise<{score /*0..1*/, feedback}>
```

| tier      | when active                              | behaviour |
|-----------|------------------------------------------|-----------|
| `canned`  | always (fallback for every other tier)   | keyword match over the open lesson's `tutor_context` sentences + `key_points`; grading counts rubric keyword hits and is explicitly labelled *needs review* |
| `sarathi` | `GET http://localhost:8001/health` reachable at init (add `?token=` for auth) | `POST /ask` with a lesson-context prefix; any failure falls back to canned |
| `webllm`  | user opts in via the tutor panel card (WebGPU browsers only) | in-browser model via [WebLLM](https://github.com/mlc-ai/web-llm); streams answers grounded on the open lesson, JSON-grades free-text practicals; any failure demotes to sarathi/canned |

The ladder is `webllm > sarathi > canned`. The active tier shows in the
header chip and on the tutor panel pill (`on-device` / `server` /
`offline rules`); every answer bubble is stamped with its `source`.

### On-device tier (`webllm`)

Strictly **opt-in**: the page loads no model code up front. The tutor
panel shows an *"🧠 Enable on-device tutor"* card when `navigator.gpu`
(WebGPU) exists; clicking Enable dynamic-imports `@mlc-ai/web-llm` from
`esm.run` (falling back to the jsDelivr `+esm` mirror), preflights free
space with `navigator.storage.estimate()`, then `CreateMLCEngine`
downloads the chosen model **once** — the browser caches the weights, so
later visits run fully offline and free. Progress streams into the card's
bar via `initProgressCallback`; the choice persists in `localStorage`
(`yfa_webllm_v1`). *Disable on-device tutor* unloads the engine
(`engine.unload()`) and clears the flag.

**Model ladder by device** (dropdown on the card):

| model                                | download | offered when |
|--------------------------------------|----------|--------------|
| `Llama-3.2-1B-Instruct-q4f16_1-MLC`  | ~0.9 GB  | always (floor) |
| `Qwen2.5-1.5B-Instruct-q4f16_1-MLC`  | ~1.7 GB  | always — **default** |
| `Llama-3.2-3B-Instruct-q4f16_1-MLC`  | ~2.3 GB  | `navigator.deviceMemory >= 16` |

**Browser support** (WebGPU): Chrome/Edge 113+ and Safari 26+ work;
Firefox ships WebGPU off by default (no card shown); mobile browsers
should stay on the server/canned tiers — the card simply never appears
where `navigator.gpu` is missing.

**Privacy**: on the `webllm` tier, questions and graded answers never
leave the machine — inference runs in the tab. The only network traffic
is the one-time runtime + weight download from the CDN.

**Behaviour details**: `ask()` sends a "You are Guru…" system prompt that
restricts answers to a LESSON section built from the open lesson's
`tutor_context` + `key_points` (≤ 2000 chars) at temperature 0.2, and
streams tokens into the chat bubble. `grade()` requests
`response_format: {type:"json_object"}` (`{score, hits, feedback}`) with
the rubric, question, answer and a few-shot example; unparseable replies
fall back to the canned grader (marked *needs review*). Init or runtime
errors (OOM, no adapter, CDN down) toast once and demote the ladder
without breaking the chat.

**Test / self-host seam**: if `window.__YF_WEBLLM_FACTORY` is a function
it is used *instead of* the CDN import — it must return (or resolve to)
an object shaped like the `@mlc-ai/web-llm` module (i.e. exposing
`CreateMLCEngine`). The Playwright suite injects a fake engine through it
so the tier is tested with zero network; deployments can point it at a
self-hosted bundle.

## Tests

```bash
pip install --break-system-packages pytest playwright pytest-playwright
node academy/tests/check_academy.mjs          # syntax check (extracted script)
python3 -m pytest academy/tests -q            # headless-Chromium suite
```

`academy/tests/test_academy.py` runs the app in headless Chromium against the
in-process fake PostgREST from `e2e/fakerest.py` (fixture code copied into
`academy/tests/conftest.py`; browsers are preinstalled under
`/opt/pw-browsers` — nothing downloads). Covered: boot without errors,
embedded-pack fallback, external pack loading, lesson nav + quiz feedback,
practical Verify pass/fail against the seeded backend, progress persistence
and export, checkride → certificate → badge happy path, canned tutor answers
and grading, the param-preserving console link (`../index.html?…` when
served), the round-trip from a stand-in production docroot (console at `/`,
`academy/` subdir): clicking **⬡ Open console** lands on `/index.html` with
`supa/key/site/token` intact and the console boots LIVE — and the backend
status chip.

`academy/tests/test_accounts.py` covers the accounts layer, hermetically:
guest mode unchanged (local progress, zero RPC traffic), sign-in via a tiny
GoTrue stand-in in `conftest.py` (fakerest serves only `/rest/v1/*`, so the
page gets `?auth=<stub>`; the stub returns `make_test_jwt` tokens that
`FakePostgREST(rbac=True)` accepts), email + role in the header and the
shared `yf_auth_v1` session, `save_progress` pushes on login and debounced
on change (asserted against the fake's progress store), checkride pass
recording via `issue_certificate` (row + code asserted), duplicate-code 409
regeneration, and RBAC-mode practical verifies passing with the JWT but
failing as guest. The stub also implements `POST /auth/v1/signup` (both
GoTrue outcomes switchable via `signup_mode`), covering: instant-session
signup signs in with email + role in the header; confirmation-required
signup shows the “check your email” message with no session and a follow-up
sign-in works; and a duplicate email surfaces *User already registered*.

`academy/tests/test_webllm.py` covers the on-device tier without any
network: an `add_init_script` stubs WebGPU + storage quota and installs a
fake engine through the `window.__YF_WEBLLM_FACTORY` seam. Covered: the
opt-in card (shown with WebGPU, hidden without; device-dependent model
ladder), enable → progress → `on-device` pill + persisted flag, streamed
grounded answers (system prompt asserted), JSON grading, init-failure
toast + graceful demotion, and disable → `engine.unload()` + demotion.
