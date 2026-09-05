"""The "Try it with a live fleet" button's server side — one small HTTP
door in front of :mod:`yantraops.sandbox`.

A visitor on a *static* marketing page cannot shell out to argparse, so
this exposes the mint as three routes and nothing else::

    POST /api/demo/session   -> mint one sandbox, answer its console URL
    GET  /api/demo/limits    -> is the button worth showing right now?
    GET  /healthz            -> liveness for systemd / nginx

Dependency-light on purpose: stdlib ``http.server``, the same thing
``e2e/fakerest.py`` is built on. ``yantraops`` depends on httpx and
psycopg and nothing else, and this file does not change that — there is
no framework here to add, and none to learn.

WHAT THIS DOOR REFUSES, AND WHY
-------------------------------
The threat model is "the front page of Hacker News", plus the ordinary
hostile visitor. Five refusals, each independent:

* **The request body is never parsed.** Not for ``site_id``, not for
  ``ttl``, not for ``robots`` — a caller has no say in what is minted.
  The TTL, fleet size and origin marker are operator configuration
  (:class:`ServeConfig`), fixed when the process starts. This is the
  single most important property of this file: a caller-supplied
  ``site_id`` is how a demo door becomes a way to write somebody else's
  fleet, so the parameter simply does not exist. The body is drained and
  dropped so keep-alive still works.
* **Per-IP rate limit** (:class:`RateLimiter`) — a fixed window, with a
  bounded table so the limiter itself cannot be turned into a memory
  DoS. Behind nginx every peer is 127.0.0.1, so the real client is taken
  from ``X-Forwarded-For`` only when the operator says how many proxies
  to trust (``--trusted-proxy-hops``); with the default of 0 the header
  is ignored completely and cannot be spoofed into a fresh quota.
* **Two concurrency ceilings.** ``max_inflight`` bounds mints happening
  *right now* (each one forks a simulator), and the sandbox ceiling that
  :func:`yantraops.sandbox.mint_sandbox` already enforces bounds mints
  that are *alive*. Both answer 429 with ``Retry-After``, never a 500
  and never a hung socket.
* **One session's own URL, and nothing else.** The reply carries the
  console URL just minted, its site id and its expiry. There is no route
  that lists sandboxes, and no route that takes a token.
* **A disabled deployment answers 503**, from the database's own
  ``demo_limits.enabled`` — the button turns itself off.

The token appears in exactly one place: inside the ``console_url`` of
the reply to the request that minted it. It is never logged (the access
log records method, path and status only) and never stored.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .sandbox import (EXIT_ERROR, EXIT_OK, SandboxAPI, SandboxCeiling,
                      SandboxDisabled, SandboxError, SandboxRegistry,
                      _spawn_driver, mint_sandbox, resolve_console_base,
                      resolve_max_live)

#: Route names, in one place so the systemd unit, the nginx block and the
#: marketing button can all be checked against the same strings.
ROUTE_MINT = "/api/demo/session"
ROUTE_LIMITS = "/api/demo/limits"
ROUTE_HEALTH = "/healthz"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088

#: Per-IP quota: this many mints per this many seconds.
DEFAULT_RATE = 3
DEFAULT_RATE_WINDOW_S = 900.0

#: How many mints may be in flight at once. Each one forks a simulator, so
#: this is a CPU bound, not a politeness bound.
DEFAULT_MAX_INFLIGHT = 2

#: Nobody's request should sit here forever if PostgREST goes dark.
DEFAULT_TIMEOUT_S = 20.0

#: Body bytes read before the connection is dropped. Nothing in the body is
#: ever used; this exists only so keep-alive works and a huge POST cannot
#: be streamed at us.
MAX_BODY_BYTES = 8192

#: How long ``/api/demo/limits`` may reuse the database's answer.
LIMITS_CACHE_S = 30.0

#: Background sweep of dead/expired simulators on this box (local only —
#: purging database rows needs the service key and lives in sandbox-reap).
DEFAULT_SWEEP_S = 60.0


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Fixed-window per-key quota with a bounded table.

    Deliberately not a token bucket: a visitor either has mints left in
    this window or does not, and :meth:`retry_after` can tell them exactly
    how long to wait, which is what a 429 owes its caller.

    ``max_keys`` is the part that matters under attack. An attacker with a
    /64 of IPv6 can present unlimited distinct keys, so the table evicts
    the oldest windows rather than growing without bound; an evicted key
    gets a fresh quota, which is no worse than the unlimited quota it
    would have had if this door did not rate-limit at all.
    """

    def __init__(self, limit: int = DEFAULT_RATE,
                 window_s: float = DEFAULT_RATE_WINDOW_S,
                 max_keys: int = 4096,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = max(int(limit), 0)
        self.window_s = max(float(window_s), 1.0)
        self.max_keys = max(int(max_keys), 1)
        self._clock = clock
        self._lock = threading.Lock()
        #: key -> [window_started_at, count]
        self._hits: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        dead = [k for k, (start, _n) in self._hits.items()
                if now - start >= self.window_s]
        for key in dead:
            self._hits.pop(key, None)
        if len(self._hits) > self.max_keys:
            # Oldest windows first — they are closest to expiring anyway.
            for key in sorted(self._hits, key=lambda k: self._hits[k][0])[
                    :len(self._hits) - self.max_keys]:
                self._hits.pop(key, None)

    def allow(self, key: str) -> bool:
        """Count one attempt against ``key``. False when it is over quota."""
        if self.limit <= 0:
            return True                     # limit 0 disables the limiter
        now = self._clock()
        with self._lock:
            slot = self._hits.get(key)
            if slot is None or now - slot[0] >= self.window_s:
                self._hits[key] = [now, 1]
                out = True
            elif slot[1] >= self.limit:
                out = False
            else:
                slot[1] += 1
                out = True
            # Pruned AFTER the write, so the table is never over the cap
            # even for the instant between adding a key and tidying up.
            self._prune(now)
            return out

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key``'s window rolls over (at least 1)."""
        with self._lock:
            slot = self._hits.get(key)
            if slot is None:
                return 1
            return max(1, int(self.window_s - (self._clock() - slot[0])) + 1)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class ServeConfig:
    """Everything the door is allowed to decide. None of it comes from a
    caller — that is the point of the class existing."""

    base_url: str
    key: str
    console: str | None = None
    ttl: int | None = None                  # None = the database's default
    robots: int = 6
    origin: str = "web"
    max_live: int | None = None
    sim: bool = True
    history: bool = True
    interval: float = 2.0
    state_file: Path | str | None = None
    rate: int = DEFAULT_RATE
    rate_window_s: float = DEFAULT_RATE_WINDOW_S
    max_inflight: int = DEFAULT_MAX_INFLIGHT
    trusted_proxy_hops: int = 0
    allow_origin: str = "*"
    quiet: bool = False
    #: Injectable seam: a spawn that does not fork (tests, dry runs).
    spawn: Callable[..., int] = _spawn_driver
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class SandboxService:
    """The stateful half of the door: quota, in-flight count, limits cache.

    Kept apart from the request handler so it can be exercised directly,
    and so one instance is unambiguously shared by every worker thread.
    """

    def __init__(self, config: ServeConfig) -> None:
        self.config = config
        self.limiter = RateLimiter(config.rate, config.rate_window_s,
                                   clock=config._clock)
        self.registry = SandboxRegistry(config.state_file)
        self._mint_lock = threading.Lock()
        self._inflight_lock = threading.Lock()
        self._inflight = 0
        self._limits_lock = threading.Lock()
        self._limits_at = 0.0
        self._limits: dict[str, Any] = {}
        #: Counters for /healthz — operational, never per-visitor.
        self.minted = 0
        self.refused = 0

    # -- plumbing ---------------------------------------------------------

    def _api(self) -> SandboxAPI:
        return SandboxAPI(self.config.base_url, self.config.key)

    @property
    def ceiling(self) -> int:
        return resolve_max_live(self.config.max_live)

    def live_count(self) -> int:
        return self.registry.live_count()

    # -- limits -----------------------------------------------------------

    def limits(self) -> dict[str, Any]:
        """What the marketing page needs to decide whether to show the
        button. Cached briefly: a page view must not become a round trip
        to PostgREST for every visitor.
        """
        now = self.config._clock()
        with self._limits_lock:
            fresh = self._limits and now - self._limits_at < LIMITS_CACHE_S
            cached = dict(self._limits) if fresh else None
        if cached is None:
            api = self._api()
            try:
                cached = dict(api.limits())
            except SandboxError as exc:
                cached = {"enabled": False, "error": exc.message}
            finally:
                api.close()
            with self._limits_lock:
                self._limits, self._limits_at = dict(cached), now
        live, ceiling = self.live_count(), self.ceiling
        out = {"ok": True,
               "enabled": bool(cached.get("enabled")),
               "ttl_minutes": cached.get("ttl_minutes"),
               "live": live, "max_live": ceiling,
               "available": bool(cached.get("enabled")) and live < ceiling}
        if cached.get("error"):
            out["error"] = cached["error"]
        return out

    # -- minting ----------------------------------------------------------

    def mint(self, client_ip: str) -> tuple[int, dict[str, Any]]:
        """Mint one sandbox for ``client_ip``. Returns (status, body).

        Never raises: every failure this door can have is a status code a
        static page can act on. The ordering matters — quota first (it is
        free), then the in-flight ceiling (cheap), then the mint itself
        (the only expensive thing here).
        """
        if not self.limiter.allow(client_ip):
            self.refused += 1
            return 429, {
                "ok": False, "reason": "rate_limited",
                "error": ("You have started the maximum number of demo "
                          "sandboxes for now — try again a little later."),
                "retry_after": self.limiter.retry_after(client_ip)}

        with self._inflight_lock:
            if self._inflight >= max(self.config.max_inflight, 1):
                self.refused += 1
                return 429, {
                    "ok": False, "reason": "busy",
                    "error": ("Too many demo sandboxes are starting right "
                              "now — try again in a few seconds."),
                    "retry_after": 5}
            self._inflight += 1
        try:
            # Serialised: the local ceiling check and the registry write
            # inside mint_sandbox are a read-modify-write on one JSON file,
            # and two simultaneous mints would race straight past the cap.
            with self._mint_lock:
                return self._mint_one()
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    def _mint_one(self) -> tuple[int, dict[str, Any]]:
        cfg = self.config
        api = self._api()
        try:
            payload = mint_sandbox(
                cfg.base_url, cfg.key,
                # Not one of these comes from the request. See the module
                # docstring: a caller-supplied site_id or ttl is the whole
                # class of bug this door exists to not have.
                ttl=cfg.ttl, robots=cfg.robots, origin=cfg.origin,
                console=cfg.console, max_live=cfg.max_live, sim=cfg.sim,
                interval=cfg.interval, history=cfg.history,
                state_file=cfg.state_file, api=api, spawn=cfg.spawn)
        except SandboxCeiling as exc:
            self.refused += 1
            return 429, {"ok": False, "reason": "ceiling",
                         "error": ("All demo fleets are busy right now — "
                                   "try again in a few minutes."),
                         "detail": exc.message, "retry_after": 60}
        except SandboxDisabled as exc:
            self.refused += 1
            return 503, {"ok": False, "reason": "disabled",
                         "error": "The live demo is switched off right now.",
                         "detail": exc.message, "retry_after": 300}
        except SandboxError as exc:
            self.refused += 1
            return 502, {"ok": False, "reason": "error",
                         "error": "Could not start a demo fleet.",
                         "detail": exc.message}
        finally:
            api.close()
        self.minted += 1
        # Exactly this session, and nothing about any other one.
        return 201, {
            "ok": True,
            "url": payload["console_url"],
            "site_id": payload["site_id"],
            "expires_at": payload["expires_at"],
            "ttl_minutes": payload["ttl_minutes"],
            "robots": payload["seeded_robots"],
        }

    def health(self) -> dict[str, Any]:
        return {"ok": True, "service": "yantra-sandbox",
                "live": self.live_count(), "max_live": self.ceiling,
                "minted": self.minted, "refused": self.refused}

    def sweep(self) -> dict[str, Any]:
        """Stop the simulators of sandboxes whose TTL has run out."""
        return self.registry.sweep()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def client_ip(peer: str, forwarded: str | None, hops: int) -> str:
    """The address the quota is charged to.

    With ``hops == 0`` the socket peer wins and ``X-Forwarded-For`` is
    ignored outright — anything else would let a visitor mint a fresh
    quota per made-up header. With ``hops == n`` the caller has told us
    exactly how many proxies append to the header, so the n-th entry from
    the right is the address the outermost trusted proxy actually saw.
    """
    if hops <= 0 or not forwarded:
        return peer or "?"
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not parts:
        return peer or "?"
    return parts[max(0, len(parts) - hops)]


class _Handler(BaseHTTPRequestHandler):
    """Three routes. Everything else is 404 or 405, never a stack trace."""

    protocol_version = "HTTP/1.1"
    server_version = "yantra-sandbox"
    sys_version = ""                        # do not advertise the runtime

    service: SandboxService                 # set by make_server()

    # -- helpers ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not self.service.config.quiet:
            super().log_message(fmt, *args)

    def _cors(self) -> None:
        origin = self.service.config.allow_origin
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _drain(self) -> None:
        """Read and DROP the body. Nothing in it is ever looked at.

        Draining exists only so keep-alive works. Anything we decline to
        read whole — an oversized body, or a chunked one we do not decode —
        would otherwise be parsed as the *next* request on the socket, so
        the connection is closed instead of desynchronised.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            length = MAX_BODY_BYTES
        if length > 0:
            self.rfile.read(length)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if payload.get("retry_after"):
            self.send_header("Retry-After", str(int(payload["retry_after"])))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()

    def _wants_html(self) -> bool:
        """True for a plain ``<form method="post">`` submit.

        A form post is the zero-JavaScript version of the button, and it
        deserves a redirect rather than a page of JSON. ``fetch()`` sends
        ``Accept: */*`` or asks for JSON, and gets JSON.
        """
        accept = (self.headers.get("Accept") or "").lower()
        return "text/html" in accept

    @property
    def _ip(self) -> str:
        return client_ip(self.client_address[0] if self.client_address else "",
                         self.headers.get("X-Forwarded-For"),
                         self.service.config.trusted_proxy_hops)

    # -- routes -----------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
        self._drain()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._drain()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == ROUTE_HEALTH:
            return self._json(200, self.service.health())
        if path == ROUTE_LIMITS:
            return self._json(200, self.service.limits())
        if path == ROUTE_MINT:
            # Minting is not safe or idempotent, and a browser prefetching
            # a link must never burn a sandbox. POST it.
            return self._json(405, {
                "ok": False, "reason": "method",
                "error": f"POST {ROUTE_MINT} to start a demo sandbox"})
        return self._json(404, {"ok": False, "reason": "not_found",
                                "error": f"no route {path}"})

    def do_POST(self) -> None:  # noqa: N802
        self._drain()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != ROUTE_MINT:
            return self._json(404, {"ok": False, "reason": "not_found",
                                    "error": f"no route {path}"})
        status, payload = self.service.mint(self._ip)
        if status == 201 and self._wants_html():
            return self._redirect(payload["url"])
        return self._json(status, payload)


def make_server(config: ServeConfig, host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT) -> tuple[ThreadingHTTPServer,
                                                   SandboxService]:
    """Build (but do not start) the server and its shared service."""
    service = SandboxService(config)

    class Handler(_Handler):
        pass

    Handler.service = service               # one service, every thread
    Handler.timeout = DEFAULT_TIMEOUT_S
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.timeout = DEFAULT_TIMEOUT_S
    return httpd, service


class SandboxHTTP:
    """Context-managed server, so tests never leak a listening socket."""

    def __init__(self, config: ServeConfig, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT) -> None:
        self.httpd, self.service = make_server(config, host, port)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.1},
                                        daemon=True,
                                        name="yantra-sandbox-http")
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "SandboxHTTP":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _sweeper(service: SandboxService, every_s: float,
             stop: threading.Event) -> None:
    """Kill simulators whose sandbox expired, even if nobody mints again.

    Purely local: deleting the database rows needs the service key and is
    ``sandbox-reap``'s job (see yantra-sandbox-reap.timer). This only
    stops processes, so it is safe to run with the anon key.
    """
    while not stop.wait(every_s):
        try:
            service.sweep()
        except Exception:  # noqa: BLE001 - a sweep must never kill the door
            pass


def run_sandbox_serve(base_url: str, key: str, *, host: str = DEFAULT_HOST,
                      port: int = DEFAULT_PORT, console: str | None = None,
                      ttl: int | None = None, robots: int = 6,
                      origin: str = "web", max_live: int | None = None,
                      sim: bool = True, history: bool = True,
                      interval: float = 2.0,
                      state_file: Path | str | None = None,
                      rate: int = DEFAULT_RATE,
                      rate_window: float = DEFAULT_RATE_WINDOW_S,
                      max_inflight: int = DEFAULT_MAX_INFLIGHT,
                      trusted_proxy_hops: int = 0,
                      allow_origin: str = "*",
                      sweep_interval: float = DEFAULT_SWEEP_S,
                      duration: float | None = None,
                      quiet: bool = False) -> int:
    """``yantraops sandbox-serve`` — run the door until Ctrl-C / SIGTERM."""
    config = ServeConfig(
        base_url=base_url, key=key, console=console, ttl=ttl, robots=robots,
        origin=origin, max_live=max_live, sim=sim, history=history,
        interval=interval, state_file=state_file, rate=rate,
        rate_window_s=rate_window, max_inflight=max_inflight,
        trusted_proxy_hops=trusted_proxy_hops, allow_origin=allow_origin,
        quiet=quiet)
    try:
        server = SandboxHTTP(config, host, port)
    except OSError as exc:
        print(f"yantraops: sandbox-serve: cannot bind {host}:{port} ({exc})")
        return EXIT_ERROR

    stop = threading.Event()
    sweeper: threading.Thread | None = None
    if sweep_interval > 0:
        sweeper = threading.Thread(
            target=_sweeper, args=(server.service, sweep_interval, stop),
            daemon=True, name="yantra-sandbox-sweep")
        sweeper.start()

    if not quiet:
        console_base = resolve_console_base(console)
        print(f"yantraops: sandbox door on http://{host}:{port}\n"
              f"  mint      POST {ROUTE_MINT}\n"
              f"  limits    GET  {ROUTE_LIMITS}\n"
              f"  health    GET  {ROUTE_HEALTH}\n"
              f"  backend   {base_url}\n"
              f"  console   {console_base}\n"
              f"  ceiling   {server.service.ceiling} live sandboxes, "
              f"{rate}/{int(rate_window)}s per IP", flush=True)
    server.start()
    try:
        stop.wait(duration) if duration is not None else stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.stop()
        if sweeper is not None:
            sweeper.join(timeout=2)
    return EXIT_OK
