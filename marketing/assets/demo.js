// ===========================================================================
// YantraFleet marketing site — "Try it with a live fleet" button.
//
// PROGRESSIVE ENHANCEMENT, NOT THE FEATURE ITSELF.
//
// The feature is the plain <form method="post" action="/api/demo/session">
// in index.html. With this file blocked, broken, or never loaded, that form
// still posts, the door still answers 303 (it sees "text/html" in Accept),
// and the browser still lands in a running console. Everything below only
// makes that nicer. Nothing below is load-bearing.
//
// What it adds, in order of how much it matters:
//
//   1. VISIBILITY GATING. GET /api/demo/limits answers {available: bool}.
//      A deployment that never applied supabase/0009, or has switched the
//      demo off, or is at its live-sandbox ceiling, should not show a button
//      that is going to fail. If `available` is false we replace the button
//      with a sentence explaining why and a link that does work.
//
//      Note the failure direction: the button ships VISIBLE in the HTML,
//      because that is what a no-JS visitor must see. So this can only ever
//      *hide* it. A visitor with JS off, on a box where the demo is off,
//      will click and get an error — that is the price of the zero-JS
//      baseline and it is the right trade.
//
//   2. HUMAN REFUSALS. 429 and 503 are normal operating states here, not
//      bugs: the sandbox has a per-IP quota, a concurrency ceiling and a
//      live-sandbox cap, and nginx has its own rate limit in front of all
//      three. A stranger must never see a raw JSON body or a browser error
//      page for any of them. Each refusal gets a sentence and, where the
//      server told us how long to wait, a live countdown on the button.
//
//   3. NO DOUBLE MINT. A second click while one mint is in flight would
//      burn a second sandbox out of the visitor's quota of three.
//
// The door reads NOTHING from the request — no body, no query string. So
// this sends no body either. See ops/yantraops/sandbox_http.py.
// ===========================================================================
(function () {
  "use strict";

  var box = document.getElementById("demo");
  if (!box) return;
  var form = box.querySelector("form.demoform");
  var button = document.getElementById("demo-go");
  var note = document.getElementById("demo-note");
  var status = document.getElementById("demo-status");
  if (!form || !button || !status) return;

  // fetch() is the only modern API this file needs. Anything without it
  // keeps the untouched form, which works fine.
  if (typeof window.fetch !== "function") return;

  var LIMITS_URL = "/api/demo/limits";
  var MINT_URL = form.getAttribute("action") || "/api/demo/session";
  var DEFAULT_RETRY_S = 60;
  var LABEL = button.textContent.trim();

  var busy = false;
  var countdownTimer = null;

  function setStatus(text, kind) {
    status.textContent = text || "";
    status.className = "demostatus" + (kind ? " " + kind : "");
  }

  function stopCountdown() {
    if (countdownTimer) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  // Disable the button for `seconds`, counting down on its face, then put
  // it back exactly as it was. Honours Retry-After / retry_after so we tell
  // the visitor the server's number rather than one we made up.
  function coolDown(seconds) {
    stopCountdown();
    var left = Math.max(1, Math.min(Math.round(seconds), 3600));
    button.disabled = true;
    var tick = function () {
      if (left <= 0) {
        stopCountdown();
        button.disabled = false;
        button.textContent = LABEL;
        return;
      }
      button.textContent = "Try again in " + left + "s";
      left -= 1;
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  }

  // Retry-After is either a number of seconds or an HTTP date. The door
  // sends seconds; nginx's own 429 sends no header at all.
  function retryAfterSeconds(response, payload) {
    if (payload && typeof payload.retry_after === "number") {
      return payload.retry_after;
    }
    var header = response && response.headers
      ? response.headers.get("Retry-After") : null;
    if (header) {
      var asNumber = parseInt(header, 10);
      if (!isNaN(asNumber)) return asNumber;
      var asDate = Date.parse(header);
      if (!isNaN(asDate)) {
        return Math.max(1, Math.round((asDate - Date.now()) / 1000));
      }
    }
    return DEFAULT_RETRY_S;
  }

  function readJSON(response) {
    // nginx's own 429 is an HTML error page, not JSON. Never let a parse
    // failure become the thing the visitor sees.
    return response.text().then(function (text) {
      try { return JSON.parse(text); } catch (err) { return null; }
    }, function () { return null; });
  }

  // -- 1) visibility gating -------------------------------------------------

  function unavailable(message) {
    box.setAttribute("data-demo-state", "unavailable");
    form.hidden = true;
    if (note) {
      note.innerHTML = "";
      note.appendChild(document.createTextNode(message + " "));
      var link = document.createElement("a");
      link.href = "https://github.com/YOUR_GITHUB_USERNAME/yantrafleet";
      link.textContent = "Run the whole thing locally in three commands";
      note.appendChild(link);
      note.appendChild(document.createTextNode(" — it needs no account either."));
    }
  }

  fetch(LIMITS_URL, {
    method: "GET",
    headers: { "Accept": "application/json" },
    credentials: "omit"
  }).then(function (response) {
    if (!response.ok) return null;
    return readJSON(response);
  }).then(function (limits) {
    // A null here means the limits route did not answer usefully: the
    // service is off, or something in between ate the request. Leave the
    // button alone rather than hiding a working demo on a bad guess --
    // a click will get a real answer from the server in a moment anyway.
    if (!limits || limits.ok !== true) return;
    if (limits.available === true) {
      box.setAttribute("data-demo-state", "available");
      if (note && typeof limits.ttl_minutes === "number") {
        note.textContent =
          "Starts a private sandbox with its own simulated fleet and drops you "
          + "straight into the console. Nothing to install, nothing to fill in. "
          + "It runs for " + limits.ttl_minutes
          + " minutes, then deletes itself and its data.";
      }
      return;
    }
    if (limits.enabled === false) {
      unavailable("The live demo is switched off on this deployment.");
    } else {
      unavailable("Every live demo fleet is in use right now — they are "
                  + "short-lived, so one usually frees up within a few minutes.");
    }
  }).catch(function () {
    // Network hiccup. The plain form is still there and still correct.
  });

  // -- 2) + 3) the submit ---------------------------------------------------

  form.addEventListener("submit", function (event) {
    // No fetch support was handled above; from here the enhanced path owns
    // the submit and must therefore handle every outcome itself.
    event.preventDefault();
    if (busy || button.disabled) return;

    busy = true;
    stopCountdown();
    button.disabled = true;
    button.textContent = "Starting your fleet…";
    setStatus("");

    fetch(MINT_URL, {
      method: "POST",
      // "application/json" and NOT text/html: the door answers 303 to
      // anything that says text/html, which is the no-JS path. Here we
      // want the JSON body so a refusal can be explained properly.
      headers: { "Accept": "application/json" },
      credentials: "omit"
      // deliberately no body: the door never reads one.
    }).then(function (response) {
      return readJSON(response).then(function (payload) {
        return { response: response, payload: payload };
      });
    }).then(function (result) {
      var response = result.response;
      var payload = result.payload;

      if (response.status === 201 && payload && payload.url) {
        setStatus("Your fleet is up. Taking you to the console…", "ok");
        window.location.href = payload.url;
        return;                       // leave the button disabled, we are leaving
      }

      if (response.status === 429) {
        // Three different reasons land here, plus nginx's own limiter,
        // which sends no JSON at all. The server writes the sentence when
        // it can; we only supply one when it did not.
        var wait = retryAfterSeconds(response, payload);
        setStatus((payload && payload.error)
          || "That is a few too many demo fleets from this connection just now.",
          "warn");
        coolDown(wait);
        return;
      }

      if (response.status === 503) {
        setStatus((payload && payload.error)
          || "The live demo is switched off right now.", "warn");
        unavailable("The live demo is switched off on this deployment.");
        return;
      }

      // 502 from the door, 502 from nginx because the door is not running,
      // or anything else. Say so plainly and give them somewhere to go.
      setStatus((payload && payload.error)
        || "Could not start a demo fleet right now. Nothing is wrong on your "
        + "side — try again in a minute, or run the whole platform "
        + "locally in three commands.", "warn");
      button.disabled = false;
      button.textContent = LABEL;
    }).catch(function () {
      setStatus("Could not reach the demo service. Check your connection and "
        + "try again, or run the whole platform locally in three commands.",
        "warn");
      button.disabled = false;
      button.textContent = LABEL;
    }).then(function () {
      busy = false;
    });
  });
})();
