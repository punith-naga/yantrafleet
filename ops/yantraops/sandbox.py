"""Zero-signup live demo sandboxes — the server side of "Try it with a live
fleet" (supabase/0009_demo_sandbox.sql).

A visitor clicks one button on the marketing page and, seconds later, is
looking at a *real* console: real PostgREST rows, robots that move, alerts
and incidents that fire, a copilot that answers — with no email, no signup
and no sales call. The whole sandbox self-destructs when its TTL runs out.

Three commands (see ``yantraops --help``)::

    yantraops sandbox-mint   # mint + seed + start a scoped simulator
    yantraops sandbox-list   # what is live on this box right now
    yantraops sandbox-reap   # purge expired sandboxes + stop their sims

plus ``sandbox-drive``, the internal child command ``sandbox-mint`` spawns
to actually move one sandbox's fleet.

WHAT THIS MODULE TRUSTS, AND WHAT IT DOES NOT
---------------------------------------------
Everything an anonymous visitor is allowed to do is decided by 0009's RLS
policies, not by this file. A demo token is a bearer secret presented in
the ``x-yf-demo-token`` header; ``public.yf_demo_site()`` resolves it to
exactly one ``DEMO-*`` site, and every anon policy is pinned to that one
value. This module deliberately adds no privilege of its own:

* the driver writes with the **anon key plus the demo token** — the exact
  credential the visitor's browser holds — so if RLS is misconfigured the
  simulator breaks loudly instead of papering over the hole with a
  service key;
* it refuses to drive any site that is not ``DEMO-<hex>``, so a malformed
  or hostile ``demo_mint_session`` reply can never point the simulator at
  a real fleet;
* every id it writes (robots, missions, incidents, alerts) is namespaced
  with the sandbox's own site id, so two concurrent sandboxes — or a
  sandbox and the real fleet — can never collide on a primary key;
* ``fleet_meta`` is never written: it has no ``site_id``, so 0009 keeps it
  off-limits to anon and the driver must not need it;
* the local registry file stores **no tokens**. The token reaches the
  driver through the environment (never argv, which is world-readable in
  ``ps``), and reaping works from site ids alone.

RESOURCE CEILING
----------------
Two independent caps, because a front page on Hacker News must not take
the box down:

1. ``demo_limits.max_live_sessions`` in the database (global, admin-tunable
   via ``admin_set_demo_limits``) — enforced by ``demo_mint_session``;
2. ``--max-live`` / ``YANTRAOPS_SANDBOX_MAX`` here (per box, default
   :data:`DEFAULT_MAX_LIVE`) — enforced *before* the RPC, because this box
   is what runs the simulator processes.

Either refusal exits :data:`EXIT_CEILING` (3) with a one-line reason, so a
front-end can map it straight to HTTP 429 without parsing prose.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: 0009's disjoint site namespace (``public.yf_is_demo_site``).
DEMO_SITE_PREFIX = "DEMO-"

#: The header every sandbox request carries (``public.yf_demo_token``).
DEMO_TOKEN_HEADER = "x-yf-demo-token"

#: How the token reaches the driver child. NOT argv: ``ps`` is world-readable
#: on most boxes and the token is a bearer credential.
DEMO_TOKEN_ENV = "YANTRAOPS_DEMO_TOKEN"

#: Same shape guard as ``yf_demo_token()``: hex only, 32..128 chars.
TOKEN_RE = re.compile(r"^[0-9a-f]{32,128}$")

#: Same shape as ``demo_mint_session`` mints. Checked on the way *in* so a
#: bad reply can never aim the driver at a real site.
SITE_RE = re.compile(r"^DEMO-[0-9A-Za-z]{4,32}$")

#: Per-box ceiling on concurrent sandboxes (env ``YANTRAOPS_SANDBOX_MAX``).
DEFAULT_MAX_LIVE = 8

#: Where the per-box registry of live sandboxes lives.
DEFAULT_STATE_ENV = "YANTRAOPS_SANDBOX_STATE"
DEFAULT_STATE_PATH = "~/.yantraops-sandboxes.json"

#: Console the minted URL points at (env ``YANTRA_CONSOLE_URL``). The default
#: is site-relative so a marketing page on the same origin just works.
DEFAULT_CONSOLE_ENV = "YANTRA_CONSOLE_URL"
DEFAULT_CONSOLE_URL = "/console/index.html"

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_CEILING = 3

#: Seconds to wait for a driver child to die after SIGTERM before SIGKILL.
TERM_GRACE_S = 5.0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class SandboxError(RuntimeError):
    """Anything that stopped a sandbox operation, with the server's words."""

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class SandboxCeiling(SandboxError):
    """The concurrent-sandbox cap was hit (local or database). Exit 3."""


class SandboxDisabled(SandboxError):
    """``demo_limits.enabled`` is false on this deployment."""


def _classify(message: str, *, code: str | None = None,
              status: int | None = None) -> SandboxError:
    """Map an RPC error message onto the exception the CLI acts on.

    0009 signals both conditions with ``raise exception``, i.e. HTTP 400 +
    ``P0001``; the distinguishing detail is the prose, so match it — but
    only as a *refinement*, never as the thing that decides success.
    """
    low = message.lower()
    if "too many live demo sandboxes" in low:
        return SandboxCeiling(message, code=code, status=status)
    if "sandbox is disabled" in low:
        return SandboxDisabled(message, code=code, status=status)
    return SandboxError(message, code=code, status=status)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """ISO-8601 (PostgREST style) -> aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_demo_site(site: Any) -> bool:
    """Python mirror of ``public.yf_is_demo_site`` + the minted shape."""
    return isinstance(site, str) and bool(SITE_RE.match(site))


def valid_token(token: Any) -> bool:
    """Python mirror of ``yf_demo_token()``'s shape guard."""
    return isinstance(token, str) and bool(TOKEN_RE.match(token))


def default_state_file() -> Path:
    """Registry path — env first, so tests and packaging can redirect it."""
    return Path(os.environ.get(DEFAULT_STATE_ENV)
                or DEFAULT_STATE_PATH).expanduser()


def resolve_max_live(flag: int | None = None) -> int:
    """Per-box ceiling: flag > env ``YANTRAOPS_SANDBOX_MAX`` > default."""
    if flag is not None:
        return max(int(flag), 0)
    raw = os.environ.get("YANTRAOPS_SANDBOX_MAX", "").strip()
    if raw:
        try:
            return max(int(raw), 0)
        except ValueError:
            pass
    return DEFAULT_MAX_LIVE


def sandbox_console_url(console_base: str, base_url: str, key: str,
                        site_id: str, token: str | None = None) -> str:
    """The one URL shape the console is ever handed for a demo session.

    ``<console>?supa=<rest base>&key=<anon key>&site=<DEMO-…>&demo=<token>``

    ``supa``/``key``/``site`` are the params console/index.html already
    reads; ``demo`` is the new one — its presence is what tells the console
    to send ``x-yf-demo-token`` on every PostgREST request and to render
    the sandbox countdown.

    With ``token=None`` the ``demo`` param is omitted, giving the
    *token-free* reference form that is safe to write to the registry file
    or print in a listing. It identifies the sandbox but cannot open it —
    the working link is printed exactly once, by ``sandbox-mint``.
    """
    qs = (f"supa={quote(base_url, safe='')}"
          f"&key={quote(key, safe='')}"
          f"&site={quote(site_id, safe='')}")
    if token:
        qs += f"&demo={quote(token, safe='')}"
    sep = "&" if "?" in console_base else "?"
    return f"{console_base}{sep}{qs}"


def resolve_console_base(flag: str | None = None) -> str:
    return (flag or os.environ.get(DEFAULT_CONSOLE_ENV)
            or DEFAULT_CONSOLE_URL)


# --------------------------------------------------------------------------
# PostgREST access
# --------------------------------------------------------------------------

@dataclass
class SandboxSession:
    """What ``demo_mint_session`` handed back."""

    token: str
    site_id: str
    created_at: str | None = None
    expires_at: str | None = None
    ttl_minutes: int | None = None
    claimed: bool = False
    seeded_robots: int = 0

    @classmethod
    def from_rpc(cls, payload: Any) -> "SandboxSession":
        if not isinstance(payload, dict):
            raise SandboxError(
                f"demo_mint_session: unexpected reply {payload!r}")
        token, site = payload.get("token"), payload.get("site_id")
        # Defensive, and the whole reason the driver is safe to start: a
        # reply that does not carry a well-formed token + DEMO-* site is
        # never allowed to become write traffic.
        if not valid_token(token):
            raise SandboxError(
                "demo_mint_session: reply carried no usable demo token")
        if not is_demo_site(site):
            raise SandboxError(
                f"demo_mint_session: refusing a non-demo site {site!r} — "
                "the sandbox site namespace is DEMO-<hex>")
        return cls(token=token, site_id=site,
                   created_at=payload.get("created_at"),
                   expires_at=payload.get("expires_at"),
                   ttl_minutes=payload.get("ttl_minutes"),
                   claimed=bool(payload.get("claimed")),
                   seeded_robots=int(payload.get("seeded_robots") or 0))

    @property
    def expires(self) -> datetime | None:
        return _parse_ts(self.expires_at)

    def redacted(self) -> dict[str, Any]:
        """Everything except the bearer token — safe to log or print."""
        out = asdict(self)
        out.pop("token")
        return out


class SandboxAPI:
    """The narrow slice of PostgREST the sandbox needs.

    ``client`` is injectable (any object with ``post``/``get`` shaped like
    ``httpx.Client``), so every test in ``ops/tests/test_sandbox.py`` runs
    against ``e2e/fakerest.py`` on localhost with no cloud and no mocks of
    our own behaviour.
    """

    def __init__(self, base_url: str, key: str, *,
                 client: Any = None, timeout_s: float = 10.0,
                 demo_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.rest = f"{self.base_url}/rest/v1"
        self.key = key
        self.demo_token = demo_token
        self._timeout_s = timeout_s
        self._client = client
        self._owned = client is None

    # -- plumbing ---------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx  # lazy: `sandbox-list` on a broken install still runs
            self._client = httpx.Client(timeout=self._timeout_s)
        return self._client

    def close(self) -> None:
        if self._owned and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            self._client = None

    def __enter__(self) -> "SandboxAPI":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def headers(self, *, token: str | None = None,
                extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
             "Content-Type": "application/json"}
        tok = token if token is not None else self.demo_token
        if tok:
            h[DEMO_TOKEN_HEADER] = tok
        h.update(extra or {})
        return h

    @staticmethod
    def _error_from(resp: Any, what: str) -> SandboxError:
        body: Any
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON error bodies happen
            body = None
        if isinstance(body, dict) and body.get("message"):
            return _classify(str(body["message"]),
                             code=body.get("code"), status=resp.status_code)
        text = (getattr(resp, "text", "") or "")[:300]
        return SandboxError(f"{what}: HTTP {resp.status_code} {text}".strip(),
                            status=resp.status_code)

    # -- RPCs -------------------------------------------------------------

    def rpc(self, fn: str, args: dict[str, Any] | None = None, *,
            token: str | None = None) -> Any:
        """POST /rest/v1/rpc/<fn>; raise :class:`SandboxError` on failure."""
        try:
            resp = self.client.post(f"{self.rest}/rpc/{fn}",
                                    json=args or {},
                                    headers=self.headers(token=token))
        except Exception as exc:  # noqa: BLE001 - network/DNS/TLS all land here
            raise SandboxError(
                f"{fn}: backend unreachable ({type(exc).__name__}: {exc})"
            ) from exc
        if resp.status_code >= 400:
            raise self._error_from(resp, fn)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return None

    def mint(self, *, ttl_minutes: int | None = None,
             seed_robots: int = 6, origin: str | None = None) -> SandboxSession:
        return SandboxSession.from_rpc(self.rpc("demo_mint_session", {
            "p_ttl_minutes": ttl_minutes,
            "p_seed_robots": seed_robots,
            "p_origin": origin,
        }))

    def claim(self, token: str) -> dict[str, Any]:
        return self.rpc("demo_claim_session", {"p_token": token})

    def info(self, token: str) -> dict[str, Any]:
        """Never raises for an unknown/expired token — see the contract."""
        out = self.rpc("demo_session_info", {"p_token": token})
        return out if isinstance(out, dict) else {"live": False,
                                                  "reason": "unknown"}

    def end(self, token: str) -> dict[str, Any]:
        return self.rpc("demo_end_session", {"p_token": token})

    def reap(self, grace_minutes: int = 0) -> dict[str, Any]:
        out = self.rpc("demo_reap_expired", {"p_grace_minutes": grace_minutes})
        return out if isinstance(out, dict) else {}

    def limits(self) -> dict[str, Any]:
        out = self.rpc("demo_limits_public")
        return out if isinstance(out, dict) else {}

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        out = self.rpc("admin_list_demo_sessions", {"p_limit": limit})
        return out if isinstance(out, list) else []

    # -- table writes (driver side) ---------------------------------------

    def write(self, table: str, rows: Sequence[dict[str, Any]], *,
              on_conflict: str | None = None, merge: bool = True,
              token: str | None = None) -> None:
        """Bulk insert/upsert, same PostgREST dialect the sim transport uses."""
        if not rows:
            return
        params = {"on_conflict": on_conflict} if on_conflict else None
        prefer = "return=minimal"
        if on_conflict:
            prefer += (",resolution=merge-duplicates" if merge
                       else ",resolution=ignore-duplicates")
        try:
            resp = self.client.post(
                f"{self.rest}/{table}", json=list(rows), params=params,
                headers=self.headers(token=token, extra={"Prefer": prefer}))
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(
                f"{table}: write failed ({type(exc).__name__}: {exc})") from exc
        if resp.status_code >= 400:
            raise self._error_from(resp, f"insert into {table}")


# --------------------------------------------------------------------------
# Per-box registry of live sandboxes
# --------------------------------------------------------------------------

@dataclass
class SandboxRecord:
    """One live sandbox on this box. Deliberately token-free.

    ``console_url`` is the *reference* form (no ``demo=`` param): enough to
    tell which sandbox a row is, never enough to open it.
    """

    site_id: str
    expires_at: str | None = None
    pid: int | None = None
    console_url: str | None = None
    origin: str | None = None
    started_at: str | None = None
    robots: int = 0

    def expired(self, now: datetime | None = None) -> bool:
        exp = _parse_ts(self.expires_at)
        if exp is None:
            return True          # unparsable expiry: treat as gone, fail closed
        return exp <= (now or _now())

    def seconds_remaining(self, now: datetime | None = None) -> int:
        exp = _parse_ts(self.expires_at)
        if exp is None:
            return 0
        return max(0, int((exp - (now or _now())).total_seconds()))

    def sim_running(self) -> bool:
        return _pid_alive(self.pid)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # alive, owned by somebody else
        return True
    except OSError:
        return False
    return True


def _stop_pid(pid: int | None, grace_s: float = TERM_GRACE_S) -> bool:
    """SIGTERM then SIGKILL. True when the process is gone afterwards."""
    if not _pid_alive(pid):
        return True
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    # Best effort: reap the zombie if it is our own child.
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    time.sleep(0.1)
    return not _pid_alive(pid)


class SandboxRegistry:
    """A tiny JSON file listing this box's live sandboxes.

    It holds site ids, expiries and simulator pids — **never tokens**. That
    is what makes it safe to leave lying around, and it is enough: reaping
    is done by site id through ``demo_reap_expired``.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_state_file()

    # -- io ---------------------------------------------------------------

    def load(self) -> list[SandboxRecord]:
        try:
            raw = json.loads(self.path.read_text() or "[]")
        except (OSError, ValueError):
            return []
        rows = raw.get("sandboxes") if isinstance(raw, dict) else raw
        out: list[SandboxRecord] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("site_id"):
                continue
            out.append(SandboxRecord(
                site_id=str(row.get("site_id")),
                expires_at=row.get("expires_at"),
                pid=row.get("pid"),
                console_url=row.get("console_url"),
                origin=row.get("origin"),
                started_at=row.get("started_at"),
                robots=int(row.get("robots") or 0)))
        return out

    def save(self, records: Iterable[SandboxRecord]) -> None:
        payload = json.dumps(
            {"sandboxes": [asdict(r) for r in records]}, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - exotic filesystems
            pass
        os.replace(tmp, self.path)

    # -- operations -------------------------------------------------------

    def add(self, record: SandboxRecord) -> None:
        records = [r for r in self.load() if r.site_id != record.site_id]
        records.append(record)
        self.save(records)

    def live(self, now: datetime | None = None) -> list[SandboxRecord]:
        now = now or _now()
        return [r for r in self.load() if not r.expired(now)]

    def live_count(self, now: datetime | None = None) -> int:
        return len(self.live(now))

    def drop(self, sites: Iterable[str]) -> None:
        dead = set(sites)
        if not dead:
            return                # nothing to forget: do not create the file
        keep = [r for r in self.load() if r.site_id not in dead]
        self.save(keep)

    def stop_sites(self, sites: Iterable[str]) -> list[str]:
        """SIGTERM the simulators of ``sites``; return the ones stopped."""
        wanted = set(sites)
        stopped: list[str] = []
        for rec in self.load():
            if rec.site_id in wanted and rec.pid:
                if _stop_pid(rec.pid):
                    stopped.append(rec.site_id)
        return stopped

    def sweep(self, now: datetime | None = None) -> dict[str, Any]:
        """Stop + forget every expired record. Idempotent by construction."""
        now = now or _now()
        keep, gone = [], []
        for rec in self.load():
            if rec.expired(now):
                _stop_pid(rec.pid)
                gone.append(rec.site_id)
            else:
                keep.append(rec)
        if gone:
            self.save(keep)
        return {"stopped": gone, "live": [r.site_id for r in keep]}


# --------------------------------------------------------------------------
# The warehouse floor plan
# --------------------------------------------------------------------------
#
# ``demo_mint_session`` scatters the starter fleet over ``random() * 40`` by
# ``random() * 24`` metres. The console maps metric positions to its SVG
# floor with ``posToSvg`` — ``[80 + x*28, 50 + y*20]`` inside a 1000x520
# viewBox — so an x of 40 lands at 1200, i.e. *off the visible map*. A
# visitor's very first impression would be robots parked outside the
# warehouse.
#
# The fix lives here rather than in the SQL (which this agent does not own):
# right after minting we park the seeded fleet on the same waypoint grid
# ``yantrasim`` drives, so the arrival snapshot already looks like a real
# floor — charging robots on the charge bays, idle robots on the docks,
# active robots out in the aisles — and the simulator's first tick continues
# from exactly where the visitor found them instead of teleporting.
#
# The constants mirror ``sim/yantrasim/world.py``. ``yantrasim`` is NOT a
# declared dependency of ``yantraops`` (the orchestrator spawns it as a
# subprocess), so they are duplicated rather than imported — and
# ``test_floor_plan_matches_the_simulator_world`` fails the build if the two
# ever drift apart.

FLOOR_GRID_ROWS = 5
FLOOR_GRID_COLS = 8
FLOOR_SPACING_M = 4.0
FLOOR_CHARGER_NODES: tuple[str, ...] = ("n0_0", "n4_7")
FLOOR_DOCK_NODES: tuple[str, ...] = ("n0_7", "n4_0")


class FloorPlan:
    """The warehouse waypoint grid, and where a demo fleet stands on it."""

    def __init__(self, rows: int = FLOOR_GRID_ROWS,
                 cols: int = FLOOR_GRID_COLS,
                 spacing: float = FLOOR_SPACING_M) -> None:
        self.rows = max(int(rows), 1)
        self.cols = max(int(cols), 1)
        self.spacing = float(spacing)
        self.nodes: dict[str, tuple[float, float, str]] = {}
        for r in range(self.rows):
            for c in range(self.cols):
                nid = f"n{r}_{c}"
                kind = ("charger" if nid in FLOOR_CHARGER_NODES
                        else "dock" if nid in FLOOR_DOCK_NODES else "aisle")
                self.nodes[nid] = (c * self.spacing, r * self.spacing, kind)

    # -- geometry ---------------------------------------------------------

    def pos(self, node: str) -> list[float]:
        x, y, _ = self.nodes[node]
        return [round(x, 2), round(y, 2)]

    def of_kind(self, kind: str) -> list[str]:
        return sorted(n for n, (_x, _y, k) in self.nodes.items() if k == kind)

    # -- where the fleet stands -------------------------------------------

    #: Which node kinds a robot of each status prefers, best first.
    PREFERENCE: dict[str, tuple[str, ...]] = {
        "charging": ("charger", "aisle", "dock"),
        "idle":     ("dock", "aisle", "charger"),
        "paused":   ("dock", "aisle", "charger"),
    }
    DEFAULT_PREFERENCE: tuple[str, ...] = ("aisle", "dock", "charger")

    def assign(self, statuses: Sequence[str]) -> list[str]:
        """One **distinct** node per robot, chosen to suit its status.

        Fleet-wide rather than per-robot because two robot dots stacked on
        the same pixel is exactly the kind of detail that makes a demo look
        fake. Charging robots take the charge bays, idle ones the docks, and
        everything else spreads across the aisles; when a preferred pool runs
        out the next-best kind is used. Deterministic for a given fleet.
        """
        taken: set[str] = set()
        out: list[str] = []
        for i, status in enumerate(statuses):
            node = self._pick(i, str(status or "idle"), taken)
            taken.add(node)
            out.append(node)
        return out

    def _pick(self, index: int, status: str, taken: set[str]) -> str:
        for kind in self.PREFERENCE.get(status, self.DEFAULT_PREFERENCE):
            pool = self.of_kind(kind)
            if not pool:
                continue
            n = len(pool)
            # Stride by 3 so consecutive robots do not cluster in a corner.
            for k in range(n):
                candidate = pool[(index * 3 + k) % n]
                if candidate not in taken:
                    return candidate
        every = sorted(self.nodes)               # fleet bigger than the floor
        return every[index % len(every)]

    def route_to(self, node: str, index: int) -> list[str]:
        """The aisle run a robot walked to arrive at ``node``."""
        row, _, col = node[1:].partition("_")
        r, c = int(row), int(col)
        forward = [f"n{r}_{x}" for x in range(0, c + 1)]
        backward = [f"n{r}_{x}" for x in range(self.cols - 1, c - 1, -1)]
        first, second = ((forward, backward) if index % 2 == 0
                         else (backward, forward))
        if len(first) >= 2:
            return first
        return second if len(second) >= 2 else [node]

    def trail(self, node: str, index: int, status: str,
              steps: int) -> list[list[float]]:
        """``steps`` positions, oldest first, ending at ``node``.

        A charging or idle robot has not moved, so its trail is a constant —
        the replay scrubber should show it parked, not teleporting.
        """
        steps = max(int(steps), 1)
        end = self.pos(node)
        if status != "active" or steps == 1:
            return [list(end) for _ in range(steps)]
        pts = [self.pos(n) for n in self.route_to(node, index)]
        if len(pts) < 2:
            return [list(end) for _ in range(steps)]
        out: list[list[float]] = []
        span = len(pts) - 1
        for k in range(steps):
            t = (k / (steps - 1)) * span
            i = min(int(t), span - 1)
            f = t - i
            a, b = pts[i], pts[i + 1]
            out.append([round(a[0] + (b[0] - a[0]) * f, 2),
                        round(a[1] + (b[1] - a[1]) * f, 2)])
        return out


# --------------------------------------------------------------------------
# Seeding a believable history
# --------------------------------------------------------------------------

def _telemetry_sample(robot: dict[str, Any], ts: datetime, age: int,
                      site: str, pos: Sequence[float]) -> dict[str, Any]:
    """One backfilled ``robot_telemetry`` row for the given robot.

    ``age`` counts *backwards* — 1 is the most recent sample — so the series
    converges on the robot's live row instead of contradicting it.
    """
    battery = float(robot.get("battery") or 70.0)
    status = str(robot.get("status") or "idle")
    wobble = (age % 7) - 3
    if status == "charging":
        # A charging robot was emptier in the past, not fuller.
        sample_battery = max(5.0, battery - (age * 0.7))
    else:
        sample_battery = min(100.0, battery + (age * 0.8))
    return {
        "robot_id": robot.get("id"),
        "ts": _iso(ts),
        "battery": round(sample_battery, 1),
        "speed": round(0.9 + 0.05 * wobble, 2) if status == "active" else 0,
        "motor_temp": round(42.0 + 0.6 * abs(wobble), 1),
        "status": status,
        "pos": [round(float(pos[0]), 2), round(float(pos[1]), 2)],
        "site_id": site,
        "tasks_done": max(0, int(robot.get("tasks_done") or 0) - age),
    }


class HistorySeeder:
    """Backfill just enough past for the console not to look empty.

    Fresh sandboxes are the worst first impression a fleet product can
    make: every analytics tile reads zero and the replay scrubber has
    nothing to scrub. So after ``demo_mint_session`` seeds the live fleet,
    this writes a couple of hours of telemetry, one resolved incident and
    two finished missions — all inside the sandbox, all through the same
    anon key + demo token the visitor's browser uses.
    """

    #: Columns read back and re-written when the fleet is parked. A full row
    #: keeps the upsert unambiguous instead of relying on merge semantics.
    ROBOT_COLUMNS = ("id,vendor,status,battery,pos,speed,task_kind,health,"
                     "motor_temp,tasks_done,site_id")

    def __init__(self, api: SandboxAPI, site_id: str, token: str, *,
                 plan: FloorPlan | None = None) -> None:
        if not is_demo_site(site_id):
            raise SandboxError(
                f"HistorySeeder: refusing to seed non-demo site {site_id!r}")
        self.api = api
        self.site = site_id
        self.token = token
        self.plan = plan or FloorPlan()
        #: 0012 added ``robot_telemetry.tasks_done``. A deployment that
        #: applied 0009 but not 0012 answers PGRST204 — retry without it
        #: once, then remember.
        self.telemetry_has_tasks_done = True
        #: Non-fatal problems hit while seeding the optional extras, so the
        #: caller can report them instead of the mint silently under-seeding.
        self.warnings: list[str] = []
        #: Floor-plan node each robot was parked on, filled by park_fleet().
        self.nodes: list[str] = []

    def robots(self) -> list[dict[str, Any]]:
        """The seeded fleet, read back through the sandbox's own token."""
        try:
            resp = self.api.client.get(
                f"{self.api.rest}/robots",
                params={"select": self.ROBOT_COLUMNS,
                        "order": "id.asc", "limit": "60"},
                headers=self.api.headers(token=self.token))
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(
                f"robots: read failed ({type(exc).__name__}: {exc})") from exc
        if resp.status_code >= 400:
            raise SandboxAPI._error_from(resp, "select robots")
        rows = resp.json()
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def write_telemetry(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if not self.telemetry_has_tasks_done:
            rows = [{k: v for k, v in r.items() if k != "tasks_done"}
                    for r in rows]
        try:
            self.api.write("robot_telemetry", rows, token=self.token)
        except SandboxError as exc:
            # PGRST204 = "column not found in schema cache" (0012 not applied).
            if self.telemetry_has_tasks_done and (
                    exc.code == "PGRST204" or "tasks_done" in exc.message):
                self.telemetry_has_tasks_done = False
                return self.write_telemetry(rows)
            raise
        return len(rows)

    def park_fleet(self, robots: list[dict[str, Any]]) -> int:
        """Stand the seeded fleet on the warehouse floor plan.

        ``demo_mint_session`` places robots at random metres, some of which
        fall outside the console's map. This rewrites every seeded robot's
        ``pos`` onto the waypoint grid the simulator drives, so the arrival
        snapshot reads as a warehouse rather than as scatter. The rows are
        rewritten whole (not merged) and every one carries this sandbox's
        ``site_id``, so 0009's WITH CHECK sees exactly what it expects.
        """
        if not robots:
            return 0
        self.nodes = self.plan.assign(
            [str(r.get("status") or "idle") for r in robots])
        rows: list[dict[str, Any]] = []
        for robot, node in zip(robots, self.nodes):
            status = str(robot.get("status") or "idle")
            row = dict(robot)
            row["pos"] = self.plan.pos(node)
            row["site_id"] = self.site       # never trust the read-back
            if status != "active":
                row["speed"] = 0
            rows.append(row)
        self.api.write("robots", rows, on_conflict="id", merge=True,
                       token=self.token)
        for robot, row in zip(robots, rows):
            robot["pos"] = row["pos"]        # keep the caller's view honest
            robot["speed"] = row.get("speed", robot.get("speed"))
        return len(rows)

    def _seed_pending_command(self, robots: list[dict[str, Any]]) -> int:
        """One command waiting on a human, so the approvals view is not empty.

        ``commands.id`` is a uuid (0002), so it cannot carry the sandbox
        prefix the other ids use — collision-freedom comes from the uuid
        itself, and the reaper deletes by ``site_id`` regardless.
        """
        target = robots[min(2, len(robots) - 1)]
        self.api.write("commands", [{
            "id": str(uuid.uuid4()),
            "robot_id": target.get("id"),
            "cmd": "charge",
            "params": {},
            "status": "pending",
            "requested_by": "demo-visitor",
            "site_id": self.site,
            "created_at": _iso(_now() - timedelta(minutes=4)),
        }], token=self.token)
        return 1

    def _seed_maintenance_finding(self, robots: list[dict[str, Any]]) -> int:
        """One open predictive-maintenance finding for the copilot to explain."""
        target = robots[0]
        self.api.write("maintenance_findings", [{
            "id": f"{self.site}-MF-01",
            "robot_id": target.get("id"),
            "component": "drive motor",
            "finding": ("Motor temperature trending +6.2 C over 14 days at "
                        "unchanged duty cycle"),
            "rul_days": 21,
            "confidence": 0.72,
            "action": ("Inspect the drive-motor bearing and re-grease at the "
                       "next scheduled service window"),
            "state": "Open",
            "site_id": self.site,
            "created_at": _iso(_now() - timedelta(minutes=38)),
        }], on_conflict="id", merge=False, token=self.token)
        return 1

    def seed(self, *, minutes: int = 120, every_minutes: int = 6) -> dict[str, int]:
        """Write the backfill. Returns per-table row counts."""
        robots = self.robots()
        now = _now()
        counts = {"robot_telemetry": 0, "incidents": 0, "missions": 0,
                  "alerts": 0, "commands": 0, "maintenance_findings": 0,
                  "robots_parked": 0}
        samples: list[dict[str, Any]] = []
        steps = max(1, minutes // max(every_minutes, 1))
        counts["robots_parked"] = self.park_fleet(robots)
        for i, robot in enumerate(robots):
            trail = self.plan.trail(self.nodes[i], i,
                                    str(robot.get("status") or "idle"), steps)
            # trail[0] is the oldest point; ``age`` counts back from the live
            # row so the newest sample sits where the robot is parked.
            for k, age in enumerate(range(steps, 0, -1)):
                ts = now - timedelta(minutes=age * every_minutes)
                samples.append(
                    _telemetry_sample(robot, ts, age, self.site, trail[k]))
        counts["robot_telemetry"] = self.write_telemetry(samples)

        if robots:
            src = str(robots[0].get("id"))
            opened = now - timedelta(minutes=94)
            closed = now - timedelta(minutes=79)
            self.api.write("incidents", [{
                "id": f"{self.site}-INC-0001",
                "sev": "serious",
                "title": f"Conveyor handshake timeout on {src}",
                "src": src,
                "tlabel": opened.strftime("%H:%M"),
                "state": "Resolved",
                "impact": "15 min of lane 3 throughput lost",
                "rca": "Handshake ack window too tight for the new conveyor PLC",
                "fix": "Retry window widened to 900 ms; robot resumed on its own",
                "dur": 15,
                "site_id": self.site,
                "created_at": _iso(opened),
            }], on_conflict="id", merge=False, token=self.token)
            counts["incidents"] = 1
            # 0011's trigger stamps closed_at from the state change; a
            # deployment without 0011 simply keeps the row as-is.
            self.api.write("missions", [
                {"id": f"{self.site}-M90", "name": "Night shift replen",
                 "robots": [r.get("id") for r in robots[:2]], "state": "Done",
                 "prog": 100, "eta": "—", "site_id": self.site,
                 "created_at": _iso(now - timedelta(minutes=150))},
                {"id": f"{self.site}-M91", "name": "Dock 2 outbound wave",
                 "robots": [r.get("id") for r in robots[-2:]], "state": "Done",
                 "prog": 100, "eta": "—", "site_id": self.site,
                 "created_at": _iso(now - timedelta(minutes=70))},
            ], on_conflict="id", merge=True, token=self.token)
            counts["missions"] = 2

            # The two extras below are the difference between a console with
            # empty Approvals/Maintenance views and one that has something to
            # click on arrival. They are best-effort on purpose: a deployment
            # that applied 0009 but is missing 0004's maintenance table (or
            # has a stricter commands posture) must still get a live sandbox,
            # not a failed mint. Failures are surfaced, never swallowed.
            for key, fn in (("commands", self._seed_pending_command),
                            ("maintenance_findings",
                             self._seed_maintenance_finding)):
                try:
                    counts[key] = fn(robots)
                except SandboxError as exc:
                    self.warnings.append(f"{key}: {exc.message}")
        return counts


# --------------------------------------------------------------------------
# The driver: one sandbox's fleet, actually moving
# --------------------------------------------------------------------------

class SandboxDriver:
    """Runs ``yantrasim``'s :class:`FleetSim` scoped to one sandbox.

    The simulator library is reused verbatim — same physics, same fault
    scripts, same VDA 5050 state — but the rows it produces are rewritten
    twice before they are posted:

    * ``site_id`` is forced to this sandbox (never ``yantracore.site_id()``,
      which reads a process-wide env var and would be wrong here), and
    * every primary key is prefixed with the sandbox site, so ``AMR-01``
      becomes ``DEMO-0A1B2C3D4E5F-R01`` and can never collide with a real
      fleet's row or another sandbox's.

    ``fleet_meta`` is not written at all: it has no ``site_id``, so 0009
    keeps it off-limits to anon and there is nothing to scope it by.
    """

    def __init__(self, api: SandboxAPI, site_id: str, token: str, *,
                 robots: int = 6, interval: float = 2.0,
                 time_scale: float = 5.0, seed: int | None = None,
                 deadline: datetime | None = None,
                 history_every: int = 3, check_every: int = 15) -> None:
        if not is_demo_site(site_id):
            raise SandboxError(
                f"SandboxDriver: refusing to drive non-demo site {site_id!r} "
                "— sandbox sites are DEMO-<hex>")
        if not valid_token(token):
            raise SandboxError(
                "SandboxDriver: demo token is missing or malformed")
        self.api = api
        self.site = site_id
        self.token = token
        self.robots = max(int(robots), 1)
        self.interval = max(float(interval), 0.0)
        self.time_scale = max(float(time_scale), 1.0)
        self.deadline = deadline
        self.history_every = max(int(history_every), 0)
        self.check_every = max(int(check_every), 0)
        self.sim = self._build_sim(seed)
        self.telemetry_has_tasks_done = True
        self.ticks = 0

    # -- construction ------------------------------------------------------

    def _build_sim(self, seed: int | None) -> Any:
        try:
            from yantrasim.sim import FleetSim
        except ImportError as exc:  # pragma: no cover - yantrasim is a dep
            raise SandboxError(
                "yantrasim is not installed — mint with --no-sim, or "
                "`pip install -e sim/`") from exc
        # A per-site seed keeps two concurrent sandboxes visibly different
        # while each one stays reproducible for support.
        rng_seed = seed if seed is not None else (
            abs(hash(self.site)) % 100000)
        sim = FleetSim(seed=rng_seed, fleet_size=self.robots)
        for i, robot in enumerate(sim.robots, start=1):
            robot.robot_id = self.robot_id(i)
        return sim

    def robot_id(self, i: int) -> str:
        """The id ``demo_mint_session`` already seeded: ``<site>-R01``."""
        return f"{self.site}-R{i:02d}"

    # -- row shaping -------------------------------------------------------

    def _rows(self, out: Any) -> dict[str, list[dict[str, Any]]]:
        from yantrasim.translate import alert_row, robot_row, telemetry_row
        ts = out.states[0]["timestamp"] if out.states else _iso(_now())
        robots, telemetry = [], []
        # ``FleetSim.tick`` builds ``states`` by walking ``sim.robots`` in
        # order and keys ``extras`` by the (renamed) robot id, so pairing
        # them positionally is exact — and sidesteps the VDA serial mangling
        # that turns ``AMR-07`` into ``AMR_07`` on the wire.
        for state, robot in zip(out.states, self.sim.robots):
            extras = out.extras.get(robot.robot_id)
            if extras is None:
                continue
            robots.append(dict(robot_row(state, extras), site_id=self.site))
            if self.history_every and out.tick % self.history_every == 0:
                telemetry.append(dict(telemetry_row(state, extras, ts),
                                      site_id=self.site))
        alerts = [dict(alert_row(e, ts), site_id=self.site) for e in out.events]
        missions = [dict(m, id=f"{self.site}-{m['id']}", site_id=self.site)
                    for m in (out.missions or [])]
        incidents = [self._incident(e, ts) for e in out.events
                     if e.kind == "fault"]
        return {"robots": robots, "alerts": alerts, "missions": missions,
                "robot_telemetry": telemetry, "incidents": incidents}

    def _incident(self, event: Any, ts: str) -> dict[str, Any]:
        """A fault event becomes an open incident inside the sandbox.

        The detector is site-agnostic but runs against whatever site the
        deployment configured, so a sandbox cannot rely on it. Writing the
        incident here keeps the sandbox self-contained: the visitor sees
        the incident timeline fill up in front of them.
        """
        return {
            "id": f"{self.site}-INC-{event.tick:04d}",
            "sev": "crit",
            "title": event.msg,
            "src": event.robot_id,
            "tlabel": ts[11:16] if len(ts) >= 16 else ts,
            "state": "Open",
            "impact": "Robot stopped mid-task; queue backing up",
            "dur": 0,
            "site_id": self.site,
            "created_at": ts,
        }

    # -- running -----------------------------------------------------------

    def _write_telemetry(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if not self.telemetry_has_tasks_done:
            rows = [{k: v for k, v in r.items() if k != "tasks_done"}
                    for r in rows]
        try:
            self.api.write("robot_telemetry", rows, token=self.token)
        except SandboxError as exc:
            if self.telemetry_has_tasks_done and (
                    exc.code == "PGRST204" or "tasks_done" in exc.message):
                self.telemetry_has_tasks_done = False
                self._write_telemetry(rows)
                return
            raise

    def tick_once(self) -> dict[str, int]:
        """Advance the sandbox fleet once and publish it. Returns row counts."""
        dt = self.interval * self.time_scale if self.interval > 0 else self.time_scale
        out = self.sim.tick(dt_s=dt)
        self.ticks += 1
        rows = self._rows(out)
        self.api.write("robots", rows["robots"], on_conflict="id", merge=True,
                       token=self.token)
        if rows["alerts"]:
            self.api.write("alerts", rows["alerts"], on_conflict="id",
                           merge=False, token=self.token)
        if rows["incidents"]:
            self.api.write("incidents", rows["incidents"], on_conflict="id",
                           merge=False, token=self.token)
        if rows["missions"]:
            self.api.write("missions", rows["missions"], on_conflict="id",
                           merge=True, token=self.token)
        self._write_telemetry(rows["robot_telemetry"])
        return {k: len(v) for k, v in rows.items()}

    def session_live(self) -> bool:
        """Ask the backend whether this sandbox still exists.

        ``demo_session_info`` never raises, so an ended sandbox is a normal
        answer — the driver exits quietly instead of hammering a dead site.
        """
        try:
            return bool(self.api.info(self.token).get("live"))
        except SandboxError:
            return True   # a blip in the backend is not a reason to give up

    def run(self, *, ticks: int = 0, stop: threading.Event | None = None,
            on_error: Callable[[Exception], None] | None = None) -> int:
        """Tick until the deadline / ``ticks`` / ``stop``. Returns tick count."""
        stop = stop or threading.Event()
        done = 0
        while not stop.is_set():
            if ticks and done >= ticks:
                break
            if self.deadline is not None and _now() >= self.deadline:
                break
            try:
                self.tick_once()
            except SandboxError as exc:
                if on_error is not None:
                    on_error(exc)
                else:
                    print(f"yantraops: sandbox {self.site}: {exc}",
                          file=sys.stderr, flush=True)
                # A write refused by RLS means the sandbox is gone (or was
                # never ours). Either way, stop — do not spin on 403s.
                if exc.status in (401, 403):
                    break
            done += 1
            if self.check_every and done % self.check_every == 0:
                if not self.session_live():
                    break
            if self.interval:
                stop.wait(self.interval)
        return done


# --------------------------------------------------------------------------
# Spawning the driver as a child process
# --------------------------------------------------------------------------

def _spawn_driver(*, base_url: str, key: str, session: SandboxSession,
                  robots: int, interval: float,
                  python: str | None = None) -> int:
    """Start ``yantraops sandbox-drive`` detached; return its pid.

    The token goes through the environment, never argv: ``/proc/*/cmdline``
    is world-readable on a normal Linux box and this is a bearer credential.
    """
    cmd = [python or sys.executable, "-m", "yantraops", "sandbox-drive",
           "--url", base_url, "--key", key,
           "--site", session.site_id,
           "--robots", str(robots),
           "--interval", str(interval)]
    if session.expires_at:
        cmd += ["--until", str(session.expires_at)]
    env = dict(os.environ)
    env[DEMO_TOKEN_ENV] = session.token
    env.setdefault("YANTRA_LOG_LEVEL", "WARNING")
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return proc.pid


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _emit(payload: dict[str, Any], lines: list[str], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        for line in lines:
            print(line)


def _fail(payload: dict[str, Any], message: str, json_output: bool) -> None:
    """One failure report: JSON on stdout, or one human line on stderr."""
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"yantraops: {message}", file=sys.stderr)


def run_sandbox_mint(base_url: str, key: str, *, ttl: int | None = None,
                     robots: int = 6, origin: str | None = None,
                     console: str | None = None, max_live: int | None = None,
                     sim: bool = True, interval: float = 2.0,
                     history: bool = True, claim: bool = False,
                     state_file: Path | str | None = None,
                     json_output: bool = False, api: SandboxAPI | None = None,
                     spawn: Callable[..., int] = _spawn_driver) -> int:
    """Mint + seed + start one sandbox. Prints the console URL."""
    registry = SandboxRegistry(state_file)
    registry.sweep()                       # dead sandboxes never hold a slot
    ceiling = resolve_max_live(max_live)
    live = registry.live_count()
    owned = api is None
    api = api or SandboxAPI(base_url, key)
    try:
        if live >= ceiling:
            raise SandboxCeiling(
                f"sandbox-mint: this box is already running {live} of "
                f"{ceiling} sandboxes — raise --max-live "
                "(YANTRAOPS_SANDBOX_MAX) or wait for one to expire")
        session = api.mint(ttl_minutes=ttl, seed_robots=robots, origin=origin)
        # Claim immediately: the visitor is about to open it, and a claimed
        # session is what tells an admin the sandbox was actually used.
        try:
            api.claim(session.token)
            session.claimed = True
        except SandboxError:
            pass                           # not fatal — the sandbox is live

        seeded: dict[str, int] = {}
        warnings: list[str] = []
        pid: int | None = None
        try:
            if history:
                seeder = HistorySeeder(api, session.site_id, session.token)
                seeded = seeder.seed()
                warnings = seeder.warnings
            if sim:
                pid = spawn(base_url=api.base_url, key=key, session=session,
                            robots=max(session.seeded_robots or robots, 1),
                            interval=interval)
        except Exception as exc:  # noqa: BLE001 - includes spawn failures
            # Never leak a live slot for a sandbox nobody can use.
            try:
                api.end(session.token)
            except SandboxError:
                pass
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(
                f"sandbox-mint: could not start the sandbox simulator "
                f"({type(exc).__name__}: {exc}) — the session was ended"
            ) from exc

        base = resolve_console_base(console)
        console_url = sandbox_console_url(base, api.base_url, key,
                                          session.site_id, session.token)
        registry.add(SandboxRecord(
            site_id=session.site_id, expires_at=session.expires_at, pid=pid,
            # Token-free by design — see SandboxRegistry's docstring.
            console_url=sandbox_console_url(base, api.base_url, key,
                                            session.site_id),
            origin=origin, started_at=_iso(_now()),
            robots=session.seeded_robots))

        payload = dict(session.redacted(), console_url=console_url,
                       pid=pid, seeded=seeded, warnings=warnings,
                       live_on_this_box=registry.live_count(),
                       max_live=ceiling)
        lines = [
            f"  sandbox   {session.site_id}"
            f"  ({session.seeded_robots} robots, ttl {session.ttl_minutes}m)",
            f"  expires   {session.expires_at}",
            f"  simulator {'pid ' + str(pid) if pid else 'not started (--no-sim)'}",
            f"  history   {seeded.get('robot_telemetry', 0)} telemetry rows, "
            f"{seeded.get('incidents', 0)} incident, "
            f"{seeded.get('missions', 0)} missions, "
            f"{seeded.get('commands', 0)} pending command, "
            f"{seeded.get('maintenance_findings', 0)} finding",
        ]
        lines += [f"  warning   {w}" for w in warnings]
        lines.append(f"  open      {console_url}")
        _emit(payload, lines, json_output)
        return EXIT_OK
    except SandboxCeiling as exc:
        _fail({"error": exc.message, "reason": "ceiling"}, exc.message,
              json_output)
        return EXIT_CEILING
    except SandboxError as exc:
        _fail({"error": exc.message,
               "reason": "disabled" if isinstance(exc, SandboxDisabled)
                         else "error"}, exc.message, json_output)
        return EXIT_ERROR
    finally:
        if owned:
            api.close()


def run_sandbox_reap(base_url: str, key: str, *, grace: int = 0,
                     state_file: Path | str | None = None,
                     json_output: bool = False,
                     api: SandboxAPI | None = None) -> int:
    """Purge expired sandboxes and stop their simulators. Cron-safe.

    Idempotent in both directions: ``demo_reap_expired`` returns zero
    counts when there is nothing to purge, and stopping an already-dead pid
    is a no-op. Nothing here can touch a non-demo site — the delete path is
    ``_yf_demo_purge_sites``, which refuses outright.
    """
    registry = SandboxRegistry(state_file)
    owned = api is None
    api = api or SandboxAPI(base_url, key)
    try:
        result = api.reap(grace)
        sites = result.get("sites") or []
        stopped = registry.stop_sites(sites)
        registry.drop(sites)
        swept = registry.sweep()           # expired locally but not yet purged
        payload = {
            "sessions_reaped": result.get("sessions_reaped", 0),
            "sites": sites,
            "deleted": result.get("deleted", {}),
            "total": result.get("total", 0),
            "simulators_stopped": sorted(set(stopped) | set(swept["stopped"])),
            "still_live": swept["live"],
        }
        _emit(payload, [
            f"  reaped    {payload['sessions_reaped']} sandbox(es), "
            f"{payload['total']} rows",
            f"  stopped   {len(payload['simulators_stopped'])} simulator(s)",
            f"  live      {len(payload['still_live'])} sandbox(es) on this box",
        ], json_output)
        return EXIT_OK
    except SandboxError as exc:
        _fail({"error": exc.message}, exc.message, json_output)
        return EXIT_ERROR
    finally:
        if owned:
            api.close()


def run_sandbox_list(base_url: str, key: str, *,
                     state_file: Path | str | None = None,
                     json_output: bool = False, remote: bool = False,
                     max_live: int | None = None,
                     api: SandboxAPI | None = None) -> int:
    """List this box's sandboxes (and, with ``--remote``, the backend's).

    Tokens are never shown: the registry does not store them and
    ``admin_list_demo_sessions`` deliberately does not return them.
    """
    registry = SandboxRegistry(state_file)
    records = registry.load()
    ceiling = resolve_max_live(max_live)
    local = [{"site_id": r.site_id, "expires_at": r.expires_at,
              "seconds_remaining": r.seconds_remaining(),
              "expired": r.expired(), "pid": r.pid,
              "sim_running": r.sim_running(), "origin": r.origin,
              "robots": r.robots, "console_url": r.console_url}
             for r in records]
    payload: dict[str, Any] = {
        "local": local, "max_live": ceiling,
        "live_on_this_box": sum(1 for r in local if not r["expired"]),
    }
    lines = [f"  {len(local)} sandbox(es) on this box "
             f"({payload['live_on_this_box']} live, ceiling {ceiling})"]
    for row in local:
        state = ("expired" if row["expired"]
                 else f"{row['seconds_remaining'] // 60}m left")
        sim = (f"pid {row['pid']}" + ("" if row["sim_running"] else " (dead)")
               if row["pid"] else "no sim")
        lines.append(f"  {row['site_id']:<22}{state:<12}{sim:<18}"
                     f"{row['console_url'] or ''}")
    exit_code = EXIT_OK
    if remote:
        owned = api is None
        api = api or SandboxAPI(base_url, key)
        try:
            payload["remote"] = api.list_sessions()
            lines.append(f"  backend reports {len(payload['remote'])} "
                         "session(s) (admin view, tokens never returned)")
        except SandboxError as exc:
            payload["remote_error"] = exc.message
            lines.append(f"  backend listing unavailable: {exc.message}")
            exit_code = EXIT_ERROR
        finally:
            if owned:
                api.close()
    _emit(payload, lines, json_output)
    return exit_code


def run_sandbox_drive(base_url: str, key: str, site: str, *,
                      token: str | None = None, robots: int = 6,
                      interval: float = 2.0, until: str | None = None,
                      ticks: int = 0, api: SandboxAPI | None = None,
                      stop: threading.Event | None = None) -> int:
    """``sandbox-drive`` — move one sandbox's fleet until its TTL runs out.

    Internal: ``sandbox-mint`` spawns this. It reads the demo token from
    :data:`DEMO_TOKEN_ENV` so the credential never appears in ``ps``.
    """
    token = token or os.environ.get(DEMO_TOKEN_ENV) or ""
    owned = api is None
    api = api or SandboxAPI(base_url, key)
    try:
        driver = SandboxDriver(api, site, token, robots=robots,
                               interval=interval, deadline=_parse_ts(until))
    except SandboxError as exc:
        print(f"yantraops: {exc.message}", file=sys.stderr)
        if owned:
            api.close()
        return EXIT_ERROR

    stop = stop or threading.Event()
    in_main = threading.current_thread() is threading.main_thread()
    old: dict[int, Any] = {}
    if in_main:
        def _handler(signum: int, frame: Any) -> None:
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old[sig] = signal.signal(sig, _handler)
            except ValueError:  # pragma: no cover - non-main thread
                pass
    try:
        driver.run(ticks=ticks, stop=stop)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        if owned:
            api.close()
    return EXIT_OK
