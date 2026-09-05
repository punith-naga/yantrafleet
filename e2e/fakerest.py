"""In-process fake PostgREST server for offline end-to-end tests.

A tiny stdlib ``http.server`` bound to ``127.0.0.1`` on an ephemeral port,
backed by in-memory dicts — one list of row-dicts per table. It speaks
exactly the subset of PostgREST that the YantraFleet components use:

* ``POST /rest/v1/{table}`` — bulk insert. With ``?on_conflict=col`` and a
  ``Prefer: resolution=merge-duplicates`` header it upserts (existing row
  is merged); with ``resolution=ignore-duplicates`` conflicting rows are
  skipped. Without ``on_conflict`` it is a plain append (auto ``id`` is
  assigned when missing, like a serial column).
* ``GET /rest/v1/{table}`` — horizontal filters ``eq. neq. in. lt. lte.
  gt. gte. is.``, plus ``order=col.asc|desc``, ``limit=N`` and a
  ``select=`` column projection (``*`` or a comma list).
* ``PATCH /rest/v1/{table}`` — same filters select the rows; the JSON
  body is merged into each matching row. Returns 204.
* ``POST /rest/v1/rpc/{fn}`` — the 0007 RPCs ``decide_command``,
  ``save_progress`` and ``issue_certificate``, the 0008 admin-settings
  RPCs ``admin_list_settings``/``admin_set_setting``, and every RPC added
  by migrations 0009-0016 (demo sandbox, share links, replay, utilization,
  explainable maintenance, inbound ops, certification, public status —
  see :attr:`FakePostgREST.RPCS` for the full list and
  ``docs/FEATURE-CONTRACTS.md`` for their exact contracts).

RBAC emulation (supabase/0007_rbac.sql) is **opt-in**:
``FakePostgREST(rbac=True, service_key="sk-test")``. In that mode the
handler parses ``Authorization: Bearer`` tokens of the *unsigned* test
form ``yf-test.<base64url json claims>.sig`` (build one with
:func:`make_test_jwt`) with claims ``{sub, email, role: 'authenticated',
yf_role: operator|engineer|manager|admin, site_id}`` and approximates the
0007 policies: anon/no-role reads come back empty, non-admin reads are
scoped to the claim's ``site_id``, operators may ack alerts and insert
their own pending commands, everything else needs the service key, and
command decisions go through the ``decide_command`` RPC (manager+).
The default mode (``rbac=False``) ignores auth entirely, exactly as
before — the RPCs still work there, minus the role checks.

Demo-sandbox emulation (supabase/0009_demo_sandbox.sql): a caller that
presents no ``Authorization`` bearer token but DOES send an
``x-yf-demo-token`` header matching a live session from
``demo_mint_session`` resolves to a ``demo`` identity pinned to that
session's ``DEMO-*`` site. Such a caller may read, insert and update rows
in :data:`DEMO_TABLES` **for that one site**, with the column-level UPDATE
limits in :data:`DEMO_UPDATE_COLUMNS`; every other table, every other site
and every DELETE is refused, exactly as the SQL policies do. Demo identity
is only consulted when there is no authenticated JWT, mirroring the fact
that 0009's policies are declared ``to anon``.

Everything stays on localhost sockets, so tests remain hermetic — no
Supabase, no DNS, no network egress.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

TABLES: tuple[str, ...] = (
    "robots", "alerts", "incidents", "commands", "fleet_meta", "robot_telemetry",
    "missions", "maintenance_findings",
    # 0012 / 0013: readable through PostgREST by authenticated role holders.
    "site_cost_settings", "maintenance_feedback",
)

#: The seven site-scoped fleet tables 0009's demo-sandbox policies cover.
#: A demo token may read/insert/update rows here — and NOWHERE else.
DEMO_TABLES: frozenset[str] = frozenset({
    "robots", "alerts", "incidents", "missions",
    "robot_telemetry", "maintenance_findings", "commands",
})

#: 0009 column-level UPDATE grants for anon inside a sandbox. A table listed
#: here may only be PATCHed with these columns (mirrors 0007's alerts.ack
#: discipline); a table not listed accepts any column.
DEMO_UPDATE_COLUMNS: dict[str, frozenset[str]] = {
    "alerts": frozenset({"ack"}),
    "commands": frozenset({"status", "decided_by", "decided_at", "note",
                           "executed_at"}),
}

#: 0009 demo site namespace (public.yf_is_demo_site).
DEMO_SITE_PREFIX = "DEMO-"

#: Tables that carry a ``site_id`` column with a server-side default
#: (supabase/0005_sites.sql). The fake mirrors that default so rows from
#: writers that do not stamp site_id still match site-filtered reads.
SITE_TABLES: frozenset[str] = frozenset(TABLES) - {"fleet_meta"}
DEFAULT_SITE_ID = "BLR-DC1"

Row = dict[str, Any]

#: 0007 role hierarchy — each role includes everything below it.
ROLE_RANK: dict[str, int] = {"operator": 1, "engineer": 2, "manager": 3, "admin": 4}

#: 0008_app_settings.sql's CHECK constraint / RPC allowlist — the 8 keys the
#: admin settings panel may rotate. Mirrors console/index.html's
#: APP_SETTING_KEYS and the SETTINGS_KEYS tuples in
#: copilot/sarathi/settings_sync.py / notifier/yantranotify/settings_sync.py.
APP_SETTING_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY", "SARATHI_TOKEN", "WEBHOOK_URL", "YANTRA_WEBHOOK_SECRET",
    "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO",
)


def _mask_setting(value: str | None) -> str | None:
    """Python mirror of ``public._yf_mask_setting`` (0008_app_settings.sql):
    last 4 chars only, never enough to reconstruct the secret."""
    if not value:
        return None
    if len(value) <= 4:
        return "•" * 8
    return "•" * 8 + value[-4:]

_JWT_PREFIX = "yf-test."


def make_test_jwt(email: str, yf_role: str, site: str = DEFAULT_SITE_ID) -> str:
    """Build an UNSIGNED test token for ``FakePostgREST(rbac=True)``.

    Shape: ``yf-test.<base64url json claims>.sig`` — deliberately not a real
    JWT (no HMAC), so it can never be mistaken for a production credential.
    Claims mirror what a Supabase access token + user_roles row provide.
    """
    claims = {
        "sub": str(uuid.uuid5(uuid.NAMESPACE_URL, f"yf-test-user:{email}")),
        "email": email,
        "role": "authenticated",
        "yf_role": yf_role,
        "site_id": site,
    }
    blob = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return f"{_JWT_PREFIX}{blob}.sig"


def _decode_test_jwt(token: str) -> dict[str, Any] | None:
    """Claims from a ``yf-test.…`` token, or None when unparsable."""
    if not token.startswith(_JWT_PREFIX):
        return None
    payload = token[len(_JWT_PREFIX):].split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


class _Identity:
    """Resolved caller identity: service / authenticated / anon."""

    __slots__ = ("kind", "sub", "email", "yf_role", "site_id")

    def __init__(self, kind: str, sub: str | None = None,
                 email: str | None = None, yf_role: str | None = None,
                 site_id: str = DEFAULT_SITE_ID) -> None:
        self.kind = kind              # 'service'|'authenticated'|'demo'|'anon'
        self.sub = sub
        self.email = email
        self.yf_role = yf_role        # validated against ROLE_RANK, else None
        self.site_id = site_id

    @property
    def rank(self) -> int:
        """Numeric role rank; 0 = no (or unknown) role — fails closed."""
        return ROLE_RANK.get(self.yf_role or "", 0)

    @property
    def is_admin(self) -> bool:
        return self.yf_role == "admin"

    @property
    def is_demo(self) -> bool:
        """0009: a live demo token, pinned to exactly one DEMO-* site."""
        return self.kind == "demo"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (or pass a datetime through) as tz-aware UTC.

    Rows in this fake carry timestamps as ISO strings, the way PostgREST
    returns them. Anything unparsable is None, and every caller treats
    None as "unknown", never as "now" — a silent now() would make the
    replay and utilization emulations quietly wrong."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


#: Shape guard shared by 0009's `yf_demo_token()` and 0010's
#: `share_resolve()`: opaque bearer tokens are hex only, 32..128 chars.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32,128}$")

#: Canonical robot statuses (0002) bucketed the way 0012 buckets them.
UTILIZATION_BUCKETS: dict[str, str] = {
    "active": "active",
    "idle": "idle", "paused": "idle",
    "charging": "charging",
    "fault": "faulted", "estop": "faulted", "degraded": "faulted",
}


def _bucket(status: Any) -> str:
    return UTILIZATION_BUCKETS.get(status or "", "other")


def _new_token() -> str:
    """64 hex chars, matching 0009/0010's two-UUIDs-of-entropy tokens."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _certificate_level(score: Any) -> str | None:
    """Python mirror of ``public.yf_certificate_level`` (0015)."""
    if not isinstance(score, (int, float)):
        return None
    if score >= 95:
        return "platinum"
    if score >= 85:
        return "gold"
    if score >= 75:
        return "silver"
    if score >= 60:
        return "bronze"
    return None


def _mask_identity(value: str | None) -> str | None:
    """Python mirror of ``public._yf_mask_identity`` (0014)."""
    if not value:
        return None
    if len(value) <= 4:
        return "•" * 6
    return "•" * 6 + value[-4:]


def _apply_write_triggers(table: str, row: Row) -> Row:
    """The BEFORE-trigger behaviour added by 0011/0012/0015.

    Writers never set these columns; the database fills them in, which is
    what makes replay history and throughput work with no writer change.
    Emulated here so offline tests see the same rows a real project does.
    """
    if table == "incidents":                       # 0011 yf_incidents_stamp
        row["updated_at"] = _now_iso()
        if row.get("state", "Open") == "Open":
            row["closed_at"] = None
            row["closed_at_estimated"] = False
        elif not row.get("closed_at"):
            row["closed_at"] = _now_iso()
            row["closed_at_estimated"] = False
    elif table == "missions":                      # 0012 yf_missions_stamp
        row["updated_at"] = _now_iso()
        if row.get("state") == "Done":
            row.setdefault("completed_at", None)
            if not row.get("completed_at"):
                row["completed_at"] = _now_iso()
        else:
            row["completed_at"] = None
    return row


def _apply_seed_backfill(table: str, row: Row) -> Row:
    """The ONE-SHOT backfill 0011/0012 run over rows that already existed.

    Distinct from :func:`_apply_write_triggers`, and deliberately so. The
    trigger fires on a *write* and stamps ``now()``; the backfill runs
    *once, at migration time*, over history nobody is writing any more, and
    derives the timestamp from the row's own data:

    * 0011 — ``closed_at = created_at + dur minutes`` for every incident
      whose ``state <> 'Open'``, flagged ``closed_at_estimated = true``
      because a duration is not a real close time.
    * 0012 — ``completed_at = created_at`` for every mission in state
      ``'Done'``.

    Rows handed to :class:`FakePostgREST`'s constructor are the fake's
    analogue of "rows that were already in the table when the migration
    ran", so they must go through this and NOT through the trigger. Without
    it a seeded ``state='Resolved'`` incident keeps ``closed_at = null`` and
    ``replay_state_at`` reports it as still open — an offline-only wrong
    answer that would not reproduce against a real project.
    """
    if table == "incidents":
        row.setdefault("state", "Open")
        row.setdefault("closed_at_estimated", False)
        if row["state"] != "Open" and not row.get("closed_at"):
            created = _ts(row.get("created_at"))
            if created is not None:
                mins = row.get("dur") or 0
                try:
                    mins = max(float(mins), 0.0)
                except (TypeError, ValueError):
                    mins = 0.0
                row["closed_at"] = _iso(created + timedelta(minutes=mins))
                row["closed_at_estimated"] = True
        elif row["state"] == "Open":
            row["closed_at"] = None
            row["closed_at_estimated"] = False
        if not row.get("updated_at"):
            row["updated_at"] = row.get("closed_at") or row.get("created_at")
    elif table == "missions":
        row.setdefault("state", "Queued")
        if row["state"] == "Done":
            if not row.get("completed_at"):
                row["completed_at"] = row.get("created_at")
        else:
            row["completed_at"] = None
        if not row.get("updated_at"):
            row["updated_at"] = row.get("completed_at") or row.get("created_at")
    return row


# --------------------------------------------------------------------------
# Filtering helpers
# --------------------------------------------------------------------------

def _coerce_pair(cell: Any, raw: str) -> tuple[Any, Any]:
    """Coerce the raw filter string to something comparable with ``cell``."""
    if isinstance(cell, bool):
        return cell, raw.lower() in ("true", "t", "1")
    if isinstance(cell, (int, float)):
        try:
            return float(cell), float(raw)
        except ValueError:
            return str(cell), raw
    return cell, raw


def _cmp(op: str) -> Callable[[Any, Any], bool]:
    return {
        "eq": lambda a, b: a == b,
        "neq": lambda a, b: a != b,
        "lt": lambda a, b: a is not None and a < b,
        "lte": lambda a, b: a is not None and a <= b,
        "gt": lambda a, b: a is not None and a > b,
        "gte": lambda a, b: a is not None and a >= b,
    }[op]


def _matches(row: Row, col: str, expr: str) -> bool:
    op, _, raw = expr.partition(".")
    cell = row.get(col)
    if op == "is":
        return (cell is None) == (raw == "null")
    if op == "in":
        values = [v.strip().strip('"') for v in raw.strip("()").split(",") if v.strip()]
        if isinstance(cell, bool) or not isinstance(cell, (int, float)):
            return str(cell) in values
        return any(_coerce_pair(cell, v)[0] == _coerce_pair(cell, v)[1] for v in values)
    a, b = _coerce_pair(cell, raw)
    return _cmp(op)(a, b)


_RESERVED = {"select", "order", "limit", "offset", "on_conflict", "apikey"}


def _apply_query(rows: list[Row], params: list[tuple[str, str]]) -> list[Row]:
    out = [dict(r) for r in rows]
    order: str | None = None
    limit: int | None = None
    select = "*"
    for key, value in params:
        if key == "select":
            select = value
        elif key == "order":
            order = value
        elif key == "limit":
            limit = int(value)
        elif key in _RESERVED:
            continue
        else:  # column filter
            out = [r for r in out if _matches(r, key, value)]
    if order:
        col, _, direction = order.partition(".")
        out.sort(key=lambda r: (r.get(col) is None, r.get(col)),
                 reverse=(direction == "desc"))
    if limit is not None:
        out = out[:limit]
    if select and select != "*":
        cols = [c.strip() for c in select.split(",") if c.strip()]
        out = [{c: r.get(c) for c in cols} for r in out]
    return out


# --------------------------------------------------------------------------
# Store + HTTP layer
# --------------------------------------------------------------------------

class FakePostgREST:
    """Owns the tables and the HTTP server thread."""

    def __init__(self, tables: dict[str, list[Row]] | None = None, *,
                 rbac: bool = False, service_key: str | None = None,
                 anon_key: str | None = None) -> None:
        #: rbac-0007 emulation switch. False (default) = demo-open mode:
        #: auth headers are ignored entirely, exactly as before 0007.
        self.rbac = rbac
        #: Full-bypass credential (the service_role key stand-in). Accepted
        #: as ``Authorization: Bearer <service_key>`` or ``apikey`` header.
        self.service_key = service_key
        #: The anon/publishable key stand-in. Callers presenting it (or any
        #: unrecognised credential — fail closed) resolve to anon.
        self.anon_key = anon_key
        #: academy_progress stand-in: {user email: {pack_id: row}} —
        #: written only via the save_progress RPC (mirrors 0007's revoke).
        self.progress: dict[str, dict[str, Row]] = {}
        #: certificates stand-in, written only via issue_certificate.
        self.certificates: list[Row] = []
        #: app_settings stand-in: {key: {"value","updated_at","updated_by"}} —
        #: written only via admin_set_setting (mirrors 0008's revoke-all-grants
        #: posture: this is deliberately NOT in self.tables, see module note
        #: on rpc_admin_list_settings/rpc_admin_set_setting below).
        self.settings: dict[str, Row] = {}
        #: 0009 demo_sessions stand-in: {token: session row}. No table
        #: route — a bare GET /rest/v1/demo_sessions must 404, mirroring
        #: the SQL's `revoke all` posture.
        self.demo_sessions: dict[str, Row] = {}
        #: 0009 demo_limits single row.
        self.demo_limits: Row = {
            "enabled": True, "ttl_minutes": 60, "max_live_sessions": 25,
            "max_seed_robots": 12, "updated_at": None, "updated_by": None,
        }
        #: 0010 share_links stand-in: {token: link row}. No table route.
        self.share_links: dict[str, Row] = {}
        #: 0014 channel_identities / inbound_actions. No table route (PII).
        self.channel_identities: list[Row] = []
        self.inbound_actions: list[Row] = []
        #: 0016 site_status_pages stand-in: {site_id: row}. No table route
        #: (so it cannot be used to enumerate which sites exist).
        self.status_pages: dict[str, Row] = {}
        #: 0007 user_roles stand-in: {(email, site_id): role}. Populated
        #: automatically from every yf-test JWT this fake sees, and
        #: explicitly via :meth:`set_user_role` — 0014's inbound RPCs need
        #: to look up the role of a user who is NOT the caller.
        self.user_roles: dict[tuple[str, str], str] = {}
        self._demo_serial = 0
        self.tables: dict[str, list[Row]] = {t: [] for t in TABLES}
        if tables:
            for name, rows in tables.items():
                seeded = [dict(r) for r in rows]
                if name in SITE_TABLES:
                    for r in seeded:  # column default (0005_sites.sql)
                        r.setdefault("site_id", DEFAULT_SITE_ID)
                for r in seeded:  # 0011/0012 one-shot backfill
                    _apply_seed_backfill(name, r)
                self.tables[name] = seeded
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []  # (method, path) audit log
        self._serial = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        #: Set by an orchestrator so a human who opens the backend URL in a
        #: browser gets bounced to the actual UI instead of a JSON 404.
        self.console_url: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Bind 127.0.0.1 on an ephemeral port; return the base URL."""
        store = self

        class Handler(_Handler):
            fake = store

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fakerest", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    # -- table operations (called with the lock held) ----------------------

    def next_id(self) -> int:
        self._serial += 1
        return self._serial

    def insert(self, table: str, rows: list[Row],
               on_conflict: str | None, resolution: str | None) -> None:
        existing = self.tables[table]
        for row in rows:
            row = dict(row)
            if table in SITE_TABLES and "site_id" not in row:
                row["site_id"] = DEFAULT_SITE_ID  # column default (0005)
            if table == "maintenance_findings":
                row.setdefault("factors", [])     # column default (0013)
            _apply_write_triggers(table, row)
            if on_conflict:
                key = row.get(on_conflict)
                hit = next((r for r in existing
                            if r.get(on_conflict) == key), None)
                if hit is not None:
                    if resolution == "merge-duplicates":
                        hit.update(row)
                    # ignore-duplicates (or unspecified): drop the new row
                    continue
            if "id" not in row:
                row["id"] = self.next_id()
            existing.append(row)

    def patch(self, table: str, params: list[tuple[str, str]], body: Row, *,
              row_ok: Callable[[Row], bool] | None = None) -> int:
        n = 0
        for row in self.tables[table]:
            if row_ok is not None and not row_ok(row):
                continue  # RLS USING clause stand-in (rbac mode only)
            if all(_matches(row, k, v) for k, v in params
                   if k not in _RESERVED and k not in ("order", "limit")):
                row.update(body)
                _apply_write_triggers(table, row)
                n += 1
        return n

    # -- rbac-0007 emulation ----------------------------------------------

    def identity(self, headers: Any) -> _Identity:
        """Resolve the caller from Authorization/apikey headers.

        service_key (Bearer or apikey) -> service; a parsable ``yf-test.``
        Bearer token with role='authenticated' -> authenticated (yf_role
        kept only when it is a known role — unknown roles fail closed to
        'authenticated with no role'); a live ``x-yf-demo-token`` header
        with NO authenticated JWT -> demo (0009's policies are declared
        ``to anon``, so a signed-in user is never a demo caller);
        everything else -> anon.
        """
        auth = headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
        apikey = (headers.get("apikey") or "").strip()
        if self.service_key and self.service_key in (bearer, apikey):
            return _Identity("service")
        claims = _decode_test_jwt(bearer)
        if claims and claims.get("role") == "authenticated":
            yf_role = claims.get("yf_role")
            email = claims.get("email")
            site = claims.get("site_id") or DEFAULT_SITE_ID
            if email and yf_role in ROLE_RANK:   # user_roles stand-in
                self.user_roles[(email, site)] = yf_role
            return _Identity(
                "authenticated",
                sub=claims.get("sub"),
                email=email,
                yf_role=yf_role if yf_role in ROLE_RANK else None,
                site_id=site,
            )
        site = self.demo_site(headers.get("x-yf-demo-token"))
        if site is not None:
            return _Identity("demo", site_id=site)
        return _Identity("anon")  # incl. anon_key and any garbage credential

    # -- 0009 demo-sandbox helpers ----------------------------------------

    def demo_site(self, token: str | None) -> str | None:
        """``public.yf_demo_site()``: the one site this token may touch.

        None for a missing / malformed / expired / reaped / unknown token —
        fail-closed, exactly like the SQL (where a NULL site_id compares as
        NULL and therefore never matches a row)."""
        token = (token or "").strip()
        if not _TOKEN_RE.match(token):
            return None                      # yf_demo_token()'s shape guard
        row = self.demo_sessions.get(token)
        if row is None or row.get("reaped_at"):
            return None
        if _ts(row.get("expires_at")) is None or _ts(row["expires_at"]) <= _now():
            return None
        return row.get("site_id")

    def can_read_site(self, ident: _Identity, site: str | None) -> bool:
        """``public.yf_can_read_site()``: operator+ at this site (admins
        global), or this request's own live demo sandbox."""
        if site is None:
            return False
        if ident.kind == "service":
            return True
        if ident.is_demo:
            return site == ident.site_id and site.startswith(DEMO_SITE_PREFIX)
        if ident.kind != "authenticated" or ident.rank < 1:
            return False
        return ident.is_admin or ident.site_id == site

    def has_role(self, ident: _Identity, minimum: str,
                 site: str | None = None) -> bool:
        """``public.yf_has_role()``: rank at site, admin at any site global."""
        if ident.kind == "service":
            return True
        if ident.kind != "authenticated":
            return False
        if ident.is_admin:
            return True
        if site is not None and ident.site_id != site:
            return False
        return ident.rank >= ROLE_RANK.get(minimum, 99)

    def rank_at(self, email: str | None, site: str) -> int:
        """``public.yf_rank_at()``: the rank of an ARBITRARY user, for the
        inbound RPCs (which act on behalf of somebody who is not the
        caller). Admin at any site counts as 4 everywhere, as in 0007."""
        if not email:
            return 0
        if any(r == "admin" for (e, _s), r in self.user_roles.items()
               if e == email):
            return 4
        return ROLE_RANK.get(self.user_roles.get((email, site), ""), 0)

    def set_user_role(self, email: str, role: str,
                      site: str = DEFAULT_SITE_ID) -> None:
        """Seed the user_roles stand-in directly (test helper)."""
        self.user_roles[(email, site)] = role

    def row_visible(self, table: str, row: Row, ident: _Identity) -> bool:
        """rbac_read stand-in: role at the row's site, admin = every site."""
        if ident.kind == "service":
            return True
        if ident.is_demo:
            # 0009: demo_sandbox_read, and only on the seven fleet tables.
            return (table in DEMO_TABLES
                    and row.get("site_id") == ident.site_id
                    and str(row.get("site_id", "")).startswith(DEMO_SITE_PREFIX))
        if ident.kind != "authenticated" or ident.rank < 1:
            return False  # anon / no role row -> RLS shows nothing
        if table not in SITE_TABLES:  # fleet_meta: any role holder
            return True
        return ident.is_admin or row.get("site_id", DEFAULT_SITE_ID) == ident.site_id

    # -- RPCs (POST /rest/v1/rpc/<fn>) — 0007's SECURITY DEFINER functions.
    #    Each returns (http_status, json_payload). Role checks apply only
    #    when rbac=True; the state machine (pending-only, unique codes)
    #    always applies, mirroring the SQL bodies.

    def rpc_decide_command(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        p_id, p_decision = args.get("p_id"), args.get("p_decision")
        p_note = args.get("p_note")
        if self.rbac and ident.kind == "anon":
            return 401, {"message": "decide_command: not authenticated — sign in first",
                         "code": "PGRST301"}
        if p_decision not in ("approved", "rejected"):
            return 400, {"message": f"decide_command: invalid decision \"{p_decision}\" — "
                                    "expected 'approved' or 'rejected'", "code": "P0001"}
        row = next((r for r in self.tables["commands"]
                    if str(r.get("id")) == str(p_id)), None)
        if row is None:
            return 400, {"message": f"decide_command: unknown command {p_id} — not found",
                         "code": "P0001"}
        site = row.get("site_id", DEFAULT_SITE_ID)
        if self.rbac and ident.kind != "service" and not (
                ident.is_admin or (ident.rank >= ROLE_RANK["manager"]
                                   and ident.site_id == site)):
            return 403, {"message": "decide_command: not authorized — requires manager "
                                    f"role (or higher) at site {site}", "code": "42501"}
        if row.get("status", "pending") != "pending":
            return 400, {"message": f"decide_command: command not pending — {p_id} is "
                                    f"already '{row.get('status')}'", "code": "P0001"}
        row["status"] = p_decision
        row["decided_by"] = ident.email or "service"
        row["decided_at"] = _now_iso()
        if p_note is not None:
            row["note"] = p_note
        return 200, dict(row)

    def rpc_save_progress(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        p_pack, p_data = args.get("p_pack"), args.get("p_data")
        if self.rbac and ident.kind == "anon":
            return 401, {"message": "save_progress: not authenticated — sign in first",
                         "code": "PGRST301"}
        if not p_pack or not str(p_pack).strip():
            return 400, {"message": "save_progress: p_pack (content pack id) is required",
                         "code": "P0001"}
        user = ident.email or "anon"
        row = {"user_id": ident.sub or user, "pack_id": p_pack,
               "data": p_data if p_data is not None else {},
               "updated_at": _now_iso()}
        self.progress.setdefault(user, {})[p_pack] = row  # upsert
        return 200, dict(row)

    def rpc_issue_certificate(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        p_track, p_score, p_code = (args.get("p_track"), args.get("p_score"),
                                    args.get("p_code"))
        if self.rbac and ident.kind == "anon":
            return 401, {"message": "issue_certificate: not authenticated — sign in first",
                         "code": "PGRST301"}
        if not p_track or not str(p_track).strip():
            return 400, {"message": "issue_certificate: p_track is required",
                         "code": "P0001"}
        if not isinstance(p_score, (int, float)) or not 0 <= p_score <= 100:
            return 400, {"message": "issue_certificate: p_score must be between 0 and "
                                    f"100 (got {p_score})", "code": "P0001"}
        if not p_code or not str(p_code).strip():
            return 400, {"message": "issue_certificate: p_code (verification code) is "
                                    "required", "code": "P0001"}
        if any(c.get("verification_code") == p_code for c in self.certificates):
            return 409, {"message": f"issue_certificate: verification code \"{p_code}\" "
                                    "already exists", "code": "23505"}
        row = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"yf-cert:{p_code}")),
               "user_id": ident.sub or ident.email or "anon",
               "track": p_track, "score": p_score,
               "verification_code": p_code, "issued_at": _now_iso()}
        self.certificates.append(row)
        return 200, dict(row)

    def rpc_admin_list_settings(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        """supabase/0008_app_settings.sql's admin_list_settings(): always
        returns all 8 known keys (configured or not) — gated the same way
        as the other admin-only RPCs (rbac mode only; see module docstring).
        Deliberately reads self.settings, never self.tables — a bare
        GET /rest/v1/app_settings must keep 404ing/not-routing, faithfully
        mirroring the real table's `revoke all` / no-policy posture."""
        if self.rbac:
            if ident.kind == "anon":
                return 401, {"message": "admin_list_settings: not authenticated "
                                        "— sign in first", "code": "PGRST301"}
            if ident.kind != "service" and not ident.is_admin:
                return 403, {"message": "admin_list_settings: requires admin role",
                             "code": "42501"}
        result = []
        for key in APP_SETTING_KEYS:
            row = self.settings.get(key)
            if row is None:
                result.append({"key": key, "configured": False,
                               "masked_value": None,
                               "updated_at": None, "updated_by": None})
            else:
                result.append({"key": key, "configured": True,
                               "masked_value": _mask_setting(row.get("value")),
                               "updated_at": row.get("updated_at"),
                               "updated_by": row.get("updated_by")})
        return 200, result

    def rpc_admin_set_setting(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        """supabase/0008_app_settings.sql's admin_set_setting(): p_value
        null/empty deletes the row (explicit "revert to the env var"
        action); otherwise upserts and returns the masked result."""
        p_key, p_value = args.get("p_key"), args.get("p_value")
        if self.rbac:
            if ident.kind == "anon":
                return 401, {"message": "admin_set_setting: not authenticated "
                                        "— sign in first", "code": "PGRST301"}
            if ident.kind != "service" and not ident.is_admin:
                return 403, {"message": "admin_set_setting: requires admin role",
                             "code": "42501"}
        if p_key not in APP_SETTING_KEYS:
            return 400, {"message": f"admin_set_setting: unknown setting key "
                                    f"\"{p_key}\"", "code": "P0001"}
        if p_value is None or len(str(p_value).strip()) == 0:
            self.settings.pop(p_key, None)
            return 200, {"key": p_key, "configured": False, "masked_value": None,
                        "updated_at": None, "updated_by": None}
        row = {"value": str(p_value), "updated_at": _now_iso(),
              "updated_by": ident.email or "service"}
        self.settings[p_key] = row
        return 200, {"key": p_key, "configured": True,
                    "masked_value": _mask_setting(row["value"]),
                    "updated_at": row["updated_at"], "updated_by": row["updated_by"]}

    # -- 0009-0016 RPCs ----------------------------------------------------
    #    Same convention as the 0007/0008 handlers above: each returns
    #    (http_status, json_payload); role checks apply only when rbac=True,
    #    while the state machines (expiry, pending-only, one-verdict-per-user)
    #    always apply, mirroring the SQL bodies. Every contract here is
    #    specified in docs/FEATURE-CONTRACTS.md.

    def _denied(self, fn: str, need: str) -> tuple[int, Any]:
        return 403, {"message": f"{fn}: requires {need}", "code": "42501"}

    def _unauth(self, fn: str) -> tuple[int, Any]:
        return 401, {"message": f"{fn}: not authenticated — sign in first",
                     "code": "PGRST301"}

    # ---- 0009 demo sandbox ----------------------------------------------

    def rpc_demo_mint_session(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        """demo_mint_session(): a throwaway site + bearer token, anon-callable.

        Unlike the SQL (which seeds randomised robots) the fake seeds a
        DETERMINISTIC starter fleet, so offline tests can assert on it."""
        lim = self.demo_limits
        if not lim.get("enabled", True):
            return 400, {"message": "demo_mint_session: the demo sandbox is "
                                    "disabled on this deployment", "code": "P0001"}
        self.rpc_demo_reap_expired(_Identity("service"), {})   # opportunistic
        live = sum(1 for r in self.demo_sessions.values()
                   if not r.get("reaped_at")
                   and (_ts(r.get("expires_at")) or _now()) > _now())
        if live >= lim["max_live_sessions"]:
            return 400, {"message": "demo_mint_session: too many live demo "
                                    f"sandboxes ({live} of "
                                    f"{lim['max_live_sessions']}) — try again "
                                    "in a few minutes", "code": "P0001"}
        ttl = args.get("p_ttl_minutes")
        ttl = lim["ttl_minutes"] if not isinstance(ttl, int) else \
            min(max(ttl, 5), lim["ttl_minutes"])
        seed = args.get("p_seed_robots")
        seed = 6 if not isinstance(seed, int) else \
            min(max(seed, 0), lim["max_seed_robots"])

        self._demo_serial += 1
        token = _new_token()
        site = f"{DEMO_SITE_PREFIX}{self._demo_serial:012X}"
        now = _now()
        row = {"token": token, "site_id": site, "created_at": _iso(now),
               "expires_at": _iso(now + timedelta(minutes=ttl)),
               "claimed": False, "claimed_at": None, "reaped_at": None,
               "origin": (args.get("p_origin") or None)}
        self.demo_sessions[token] = row

        statuses = ["active", "active", "idle", "charging", "active", "idle"]
        for i in range(1, seed + 1):
            status = statuses[(i - 1) % len(statuses)]
            self.tables["robots"].append({
                "id": f"{site}-R{i:02d}",
                "vendor": ["MiR", "OTTO", "Geek+", "Locus"][(i - 1) % 4],
                "status": status, "battery": 70.0, "pos": [i * 2.0, i * 1.5],
                "speed": 1.0 if status == "active" else 0,
                "task_kind": "transport" if status == "active" else None,
                "health": 95.0, "motor_temp": 45.0, "tasks_done": 0,
                "fault_msg": None, "site_id": site, "updated_at": _now_iso()})
        if seed > 0:
            self.insert("missions", [{
                "id": f"{site}-M01", "name": "Inbound pallet sweep",
                "robots": [f"{site}-R01"], "state": "Running", "prog": 42,
                "eta": "12m", "site_id": site}], None, None)
            self.insert("alerts", [{
                "id": f"{site}-A01", "sev": "warn",
                "msg": "Charge-bay contention: 2 robots queued for bay 3",
                "src": "fleet", "tlabel": f"{site}-R03", "ack": False,
                "site_id": site}], None, None)

        return 200, {"token": token, "site_id": site,
                     "created_at": row["created_at"],
                     "expires_at": row["expires_at"], "ttl_minutes": ttl,
                     "claimed": False, "seeded_robots": seed}

    def _demo_info(self, row: Row | None) -> Row:
        if row is None:
            return {"live": False, "reason": "unknown", "token": None,
                    "site_id": None, "created_at": None, "expires_at": None,
                    "claimed": False, "claimed_at": None,
                    "seconds_remaining": 0}
        expires = _ts(row.get("expires_at"))
        live = not row.get("reaped_at") and expires is not None and expires > _now()
        reason = ("reaped" if row.get("reaped_at")
                  else "expired" if not live else "ok")
        remaining = 0 if expires is None else max(
            0, int((expires - _now()).total_seconds()))
        return {"live": live, "reason": reason, "token": row["token"],
                "site_id": row["site_id"], "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "claimed": row.get("claimed", False),
                "claimed_at": row.get("claimed_at"),
                "seconds_remaining": remaining}

    def rpc_demo_claim_session(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        row = self.demo_sessions.get(args.get("p_token") or "")
        if row is None or row.get("reaped_at"):
            return 400, {"message": "demo_claim_session: unknown or "
                                    "already-reaped demo token", "code": "P0001"}
        expires = _ts(row.get("expires_at"))
        if expires is None or expires <= _now():
            return 400, {"message": "demo_claim_session: this demo sandbox "
                                    f"expired at {row.get('expires_at')}",
                         "code": "P0001"}
        if not row.get("claimed"):
            row["claimed"] = True
            row["claimed_at"] = _now_iso()
        return 200, self._demo_info(row)

    def rpc_demo_session_info(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        return 200, self._demo_info(self.demo_sessions.get(args.get("p_token") or ""))

    def _purge_demo_sites(self, sites: list[str]) -> Row:
        """`_yf_demo_purge_sites`: refuses outright to touch a real site."""
        for site in sites:
            if not str(site).startswith(DEMO_SITE_PREFIX):
                raise ValueError(f"_yf_demo_purge_sites: refusing to purge "
                                 f"non-demo site \"{site}\"")
        deleted, total = {}, 0
        for table in ("robot_telemetry", "maintenance_findings", "commands",
                      "alerts", "incidents", "missions", "robots"):
            rows = self.tables[table]
            keep = [r for r in rows if r.get("site_id") not in sites]
            deleted[table] = len(rows) - len(keep)
            total += deleted[table]
            self.tables[table] = keep
        return {"sites": list(sites), "deleted": deleted, "total": total}

    def rpc_demo_end_session(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        row = self.demo_sessions.get(args.get("p_token") or "")
        if row is None:
            return 400, {"message": "demo_end_session: unknown demo token",
                         "code": "P0001"}
        if row.get("reaped_at"):
            return 200, {"site_id": row["site_id"], "already_reaped": True,
                         "deleted": {}, "total": 0}
        purged = self._purge_demo_sites([row["site_id"]])
        row["reaped_at"] = _now_iso()
        row["expires_at"] = _now_iso()
        return 200, {"site_id": row["site_id"], "already_reaped": False,
                     "deleted": purged["deleted"], "total": purged["total"]}

    def rpc_demo_reap_expired(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        if self.rbac and ident.kind not in ("service", "authenticated"):
            return self._unauth("demo_reap_expired")
        grace = args.get("p_grace_minutes") or 0
        grace = max(int(grace) if isinstance(grace, (int, float)) else 0, 0)
        cutoff = _now() - timedelta(minutes=grace)
        sites, tokens = [], []
        for token, row in self.demo_sessions.items():
            expires = _ts(row.get("expires_at"))
            if not row.get("reaped_at") and expires is not None and expires < cutoff:
                sites.append(row["site_id"])
                tokens.append(token)
        if not sites:
            return 200, {"sessions_reaped": 0, "sites": [], "deleted": {},
                         "total": 0}
        purged = self._purge_demo_sites(sites)
        for token in tokens:
            self.demo_sessions[token]["reaped_at"] = _now_iso()
        return 200, {"sessions_reaped": len(sites), "sites": sites,
                     "deleted": purged["deleted"], "total": purged["total"]}

    def rpc_demo_limits_public(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        return 200, {"enabled": self.demo_limits["enabled"],
                     "ttl_minutes": self.demo_limits["ttl_minutes"]}

    def rpc_admin_list_demo_sessions(self, ident: _Identity,
                                     args: Row) -> tuple[int, Any]:
        if self.rbac and not self.has_role(ident, "admin"):
            return self._denied("admin_list_demo_sessions", "admin role")
        limit = args.get("p_limit") or 100
        out = []
        for row in self.demo_sessions.values():
            info = self._demo_info(row)
            out.append({k: row.get(k) for k in
                        ("site_id", "created_at", "expires_at", "claimed",
                         "claimed_at", "reaped_at", "origin")}
                       | {"live": info["live"]})   # token deliberately absent
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return 200, out[:max(int(limit), 1)]

    def rpc_admin_set_demo_limits(self, ident: _Identity,
                                  args: Row) -> tuple[int, Any]:
        if self.rbac and not self.has_role(ident, "admin"):
            return self._denied("admin_set_demo_limits", "admin role")
        for key, arg in (("enabled", "p_enabled"),
                         ("ttl_minutes", "p_ttl_minutes"),
                         ("max_live_sessions", "p_max_live_sessions"),
                         ("max_seed_robots", "p_max_seed_robots")):
            if args.get(arg) is not None:
                self.demo_limits[key] = args[arg]
        self.demo_limits["updated_at"] = _now_iso()
        self.demo_limits["updated_by"] = ident.email or "service"
        return 200, dict(self.demo_limits, id=1)

    # ---- 0010 share links ------------------------------------------------

    def rpc_share_mint_link(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        kind, target = args.get("p_kind"), args.get("p_target_id")
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and ident.kind == "anon":
            return self._unauth("share_mint_link")
        if self.rbac and not self.has_role(ident, "manager", site):
            return self._denied("share_mint_link",
                                f"manager role (or higher) at site {site}")
        if kind not in ("fleet", "incident"):
            return 400, {"message": "share_mint_link: p_kind must be 'fleet' "
                                    f"or 'incident' (got {kind})", "code": "P0001"}
        if kind == "incident":
            if not target:
                return 400, {"message": "share_mint_link: p_target_id "
                                        "(incident id) is required for "
                                        "kind='incident'", "code": "P0001"}
            if not any(i.get("id") == target and i.get("site_id") == site
                       for i in self.tables["incidents"]):
                return 400, {"message": f"share_mint_link: incident {target} "
                                        f"not found at site {site}",
                             "code": "P0001"}
        elif target is not None:
            return 400, {"message": "share_mint_link: p_target_id must be null "
                                    "for kind='fleet'", "code": "P0001"}
        ttl = args.get("p_ttl_hours")
        ttl = 168 if not isinstance(ttl, int) else min(max(ttl, 1), 24 * 90)
        token, now = _new_token(), _now()
        row = {"token": token, "kind": kind, "target_id": target,
               "site_id": site, "created_by": ident.sub,
               "created_email": ident.email,
               "label": (args.get("p_label") or None),
               "created_at": _iso(now),
               "expires_at": _iso(now + timedelta(hours=ttl)),
               "revoked_at": None, "revoked_by": None,
               "last_viewed_at": None, "view_count": 0}
        self.share_links[token] = row
        return 200, {"token": token, "kind": kind, "target_id": target,
                     "site_id": site, "label": row["label"],
                     "created_at": row["created_at"],
                     "expires_at": row["expires_at"], "ttl_hours": ttl,
                     "revoked_at": None, "view_count": 0}

    def rpc_share_resolve(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        """Read-only, kind-scoped. Never raises; never returns a command row
        (they carry operator emails) or any other site's data."""
        token = args.get("p_token") or ""
        link = self.share_links.get(token)
        if not _TOKEN_RE.match(token) or link is None:
            return 200, {"valid": False, "status": "not_found"}
        expires = _ts(link.get("expires_at"))
        status = ("revoked" if link.get("revoked_at")
                  else "expired" if expires is None or expires <= _now()
                  else "ok")
        if status != "ok":
            return 200, {"valid": False, "status": status,
                         "expires_at": link.get("expires_at")}
        link["view_count"] = link.get("view_count", 0) + 1
        link["last_viewed_at"] = _now_iso()
        site = link["site_id"]

        def pick(row: Row, cols: tuple[str, ...]) -> Row:
            return {c: row.get(c) for c in cols}

        robot_cols = ("id", "vendor", "status", "battery", "pos", "speed",
                      "task_kind", "health", "motor_temp", "tasks_done",
                      "fault_msg", "updated_at")
        if link["kind"] == "fleet":
            robots = sorted((r for r in self.tables["robots"]
                             if r.get("site_id") == site),
                            key=lambda r: str(r.get("id")))
            statuses = [r.get("status") for r in robots]
            alerts = sorted((a for a in self.tables["alerts"]
                             if a.get("site_id") == site),
                            key=lambda a: str(a.get("created_at") or ""),
                            reverse=True)[:50]
            incidents = sorted((i for i in self.tables["incidents"]
                                if i.get("site_id") == site
                                and i.get("state") == "Open"),
                               key=lambda i: str(i.get("created_at") or ""),
                               reverse=True)[:50]
            return 200, {
                "valid": True, "status": "ok", "kind": "fleet",
                "site_id": site, "label": link.get("label"),
                "expires_at": link["expires_at"], "generated_at": _now_iso(),
                "counts": {
                    "robots_total": len(robots),
                    "robots_active": statuses.count("active"),
                    "robots_idle": sum(1 for s in statuses if s in ("idle", "paused")),
                    "robots_charging": statuses.count("charging"),
                    "robots_faulted": sum(1 for s in statuses
                                          if s in ("fault", "estop", "degraded")),
                },
                "robots": [pick(r, robot_cols) for r in robots],
                "alerts": [pick(a, ("id", "sev", "msg", "src", "tlabel",
                                    "ack", "created_at")) for a in alerts],
                "open_incidents": [pick(i, ("id", "sev", "title", "src",
                                            "tlabel", "state", "created_at"))
                                   for i in incidents]}

        incident = next((i for i in self.tables["incidents"]
                         if i.get("id") == link["target_id"]
                         and i.get("site_id") == site), None)
        if incident is None:
            return 200, {"valid": False, "status": "target_missing"}
        tlabel = incident.get("tlabel")
        robot = next((r for r in self.tables["robots"]
                      if r.get("site_id") == site and r.get("id") == tlabel), None)
        findings = sorted((f for f in self.tables["maintenance_findings"]
                           if f.get("site_id") == site
                           and f.get("robot_id") == tlabel
                           and f.get("state") == "Open"),
                          key=lambda f: str(f.get("created_at") or ""),
                          reverse=True)[:20]
        return 200, {
            "valid": True, "status": "ok", "kind": "incident",
            "site_id": site, "label": link.get("label"),
            "expires_at": link["expires_at"], "generated_at": _now_iso(),
            "incident": pick(incident, ("id", "sev", "title", "src", "tlabel",
                                        "state", "impact", "rca", "fix", "dur",
                                        "created_at")),
            "robot": None if robot is None else pick(robot, robot_cols),
            "maintenance_findings": [
                pick(f, ("id", "robot_id", "component", "finding", "rul_days",
                         "confidence", "action", "state", "created_at"))
                for f in findings]}

    def rpc_share_revoke(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        link = self.share_links.get(args.get("p_token") or "")
        if self.rbac and ident.kind == "anon":
            return self._unauth("share_revoke")
        if link is None:
            return 400, {"message": "share_revoke: unknown share token",
                         "code": "P0001"}
        if self.rbac and not (self.has_role(ident, "manager", link["site_id"])
                              or (ident.sub and link.get("created_by") == ident.sub)):
            return self._denied("share_revoke", "manager role (or higher) at "
                                f"site {link['site_id']}, or being the link's "
                                "creator")
        if not link.get("revoked_at"):
            link["revoked_at"] = _now_iso()
            link["revoked_by"] = ident.email or "service"
        return 200, {"token": link["token"], "kind": link["kind"],
                     "site_id": link["site_id"],
                     "revoked_at": link["revoked_at"],
                     "revoked_by": link["revoked_by"]}

    def rpc_share_list_links(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.has_role(ident, "manager", site):
            return self._denied("share_list_links",
                                f"manager role (or higher) at site {site}")
        include_dead = bool(args.get("p_include_dead"))
        limit = max(int(args.get("p_limit") or 100), 1)
        out = []
        for link in self.share_links.values():
            if link["site_id"] != site:
                continue
            expires = _ts(link.get("expires_at"))
            live = (not link.get("revoked_at")
                    and expires is not None and expires > _now())
            if not include_dead and not live:
                continue
            out.append({k: link.get(k) for k in
                        ("token", "kind", "target_id", "site_id", "label",
                         "created_email", "created_at", "expires_at",
                         "revoked_at", "revoked_by", "last_viewed_at",
                         "view_count")} | {"live": live})
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return 200, out[:limit]

    # ---- 0011 replay -----------------------------------------------------

    def rpc_replay_time_range(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("replay_time_range",
                                f"operator role (or higher) at site {site}")
        tel = [_ts(r.get("ts")) for r in self.tables["robot_telemetry"]
               if r.get("site_id") == site]
        tel = [t for t in tel if t is not None]
        incs = [r for r in self.tables["incidents"] if r.get("site_id") == site]
        created = [t for t in (_ts(i.get("created_at")) for i in incs) if t]
        ended = [max(c, e) for c, e in
                 ((_ts(i.get("created_at")), _ts(i.get("closed_at")) or _ts(i.get("created_at")))
                  for i in incs) if c and e]
        robots = {r.get("robot_id") for r in self.tables["robot_telemetry"]
                  if r.get("site_id") == site}
        lo = [x for x in (min(tel) if tel else None, min(created) if created else None) if x]
        hi = [x for x in (max(tel) if tel else None, max(ended) if ended else None) if x]
        return 200, {
            "site_id": site,
            "from": _iso(min(lo)) if lo else None,
            "to": _iso(max(hi)) if hi else None,
            "telemetry_from": _iso(min(tel)) if tel else None,
            "telemetry_to": _iso(max(tel)) if tel else None,
            "incidents_from": _iso(min(created)) if created else None,
            "incidents_to": _iso(max(ended)) if ended else None,
            "sample_count": sum(1 for r in self.tables["robot_telemetry"]
                                if r.get("site_id") == site),
            "robot_count": len(robots),
            "incident_count": len(incs),
            "now": _now_iso(),
            "has_history": bool(tel or incs)}

    def rpc_replay_state_at(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("replay_state_at",
                                f"operator role (or higher) at site {site}")
        at = _ts(args.get("p_at")) or _now()
        max_age = args.get("p_max_age_seconds")
        max_age = 120 if not isinstance(max_age, int) else max(max_age, 1)
        limit = args.get("p_limit")
        limit = 200 if not isinstance(limit, int) else min(max(limit, 1), 1000)

        latest: dict[str, Row] = {}
        for row in self.tables["robot_telemetry"]:
            if row.get("site_id") != site:
                continue
            ts = _ts(row.get("ts"))
            if ts is None or ts > at:
                continue
            best = latest.get(row.get("robot_id"))
            if best is None or ts > _ts(best["ts"]):
                latest[row.get("robot_id")] = row
        robots = []
        for robot_id in sorted(latest):
            row = latest[robot_id]
            ts = _ts(row["ts"])
            age = int((at - ts).total_seconds())
            vendor = next((r.get("vendor") for r in self.tables["robots"]
                           if r.get("id") == robot_id and r.get("site_id") == site),
                          None)
            robots.append({"robot_id": robot_id, "ts": row.get("ts"),
                           "pos": row.get("pos"), "battery": row.get("battery"),
                           "speed": row.get("speed"),
                           "motor_temp": row.get("motor_temp"),
                           "status": row.get("status"),
                           "stale": age > max_age, "age_seconds": age,
                           "vendor": vendor})
        robots = robots[:limit]

        incidents = []
        for row in self.tables["incidents"]:
            if row.get("site_id") != site:
                continue
            created = _ts(row.get("created_at"))
            closed = _ts(row.get("closed_at"))
            if created is None or created > at:
                continue
            if closed is not None and closed <= at:
                continue
            incidents.append({k: row.get(k) for k in
                              ("id", "sev", "title", "src", "tlabel",
                               "created_at", "closed_at",
                               "closed_at_estimated", "impact")})
        incidents.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        incidents = incidents[:limit]

        alerts = [{"id": a.get("id"), "sev": a.get("sev"), "msg": a.get("msg"),
                   "src": a.get("src"), "tlabel": a.get("tlabel"),
                   "created_at": a.get("created_at"), "ack_now": a.get("ack")}
                  for a in self.tables["alerts"]
                  if a.get("site_id") == site
                  and (_ts(a.get("created_at")) or _now()) <= at]
        alerts.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        alerts = alerts[:min(limit, 100)]

        commands = []
        for c in self.tables["commands"]:
            if c.get("site_id") != site:
                continue
            created = _ts(c.get("created_at"))
            if created is not None and created > at:
                continue
            decided = _ts(c.get("decided_at"))
            status_at = ("pending" if decided is None or decided > at
                         else c.get("status"))
            commands.append({"id": c.get("id"), "robot_id": c.get("robot_id"),
                             "cmd": c.get("cmd"),
                             "requested_by": c.get("requested_by"),
                             "created_at": c.get("created_at"),
                             "status_at": status_at,
                             "status_now": c.get("status")})
        commands.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        commands = commands[:min(limit, 100)]

        return 200, {
            "site_id": site, "at": _iso(at), "max_age_seconds": max_age,
            "generated_at": _now_iso(),
            "counts": {"robots": len(robots),
                       "robots_stale": sum(1 for r in robots if r["stale"]),
                       "open_incidents": len(incidents),
                       "alerts": len(alerts), "commands": len(commands)},
            "robots": robots, "open_incidents": incidents,
            "alerts": alerts, "commands": commands}

    def rpc_replay_robot_track(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("replay_robot_track",
                                f"operator role (or higher) at site {site}")
        robot = args.get("p_robot")
        if not robot:
            return 400, {"message": "replay_robot_track: p_robot is required",
                         "code": "P0001"}
        limit = args.get("p_limit")
        limit = 2000 if not isinstance(limit, int) else min(max(limit, 1), 20000)
        lo, hi = _ts(args.get("p_from")), _ts(args.get("p_to")) or _now()
        points = []
        for row in self.tables["robot_telemetry"]:
            if row.get("site_id") != site or row.get("robot_id") != robot:
                continue
            ts = _ts(row.get("ts"))
            if ts is None or (lo is not None and ts < lo) or ts > hi:
                continue
            points.append({k: row.get(k) for k in
                           ("ts", "pos", "battery", "speed", "motor_temp",
                            "status")})
        points.sort(key=lambda r: str(r.get("ts") or ""))
        points = points[:limit]
        return 200, {"site_id": site, "robot_id": robot,
                     "from": _iso(lo), "to": _iso(hi),
                     "point_count": len(points),
                     "truncated": len(points) >= limit, "points": points}

    # ---- 0012 utilization ------------------------------------------------

    def _spans(self, site: str, lo: datetime, hi: datetime,
               gap: int) -> list[tuple[str, Any, float, datetime]]:
        """(robot_id, status, seconds, ts) with the gap cap 0012 applies."""
        by_robot: dict[str, list[Row]] = {}
        for row in self.tables["robot_telemetry"]:
            if row.get("site_id") != site:
                continue
            ts = _ts(row.get("ts"))
            if ts is None or ts < lo or ts > hi:
                continue
            by_robot.setdefault(row.get("robot_id"), []).append(row)
        out = []
        for robot_id, rows in by_robot.items():
            rows.sort(key=lambda r: _ts(r["ts"]))
            for i, row in enumerate(rows):
                ts = _ts(row["ts"])
                nxt = _ts(rows[i + 1]["ts"]) if i + 1 < len(rows) else hi
                secs = min((nxt - ts).total_seconds(), float(gap))
                out.append((robot_id, row.get("status"), max(secs, 0.0), ts))
        return out

    def rpc_get_cost_settings(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("get_cost_settings",
                                f"operator role (or higher) at site {site}")
        row = next((r for r in self.tables["site_cost_settings"]
                    if r.get("site_id") == site), None)
        keys = ("currency", "robot_hour_cost", "operator_hour_cost",
                "target_utilization_pct", "shift_hours_per_day", "notes",
                "updated_at", "updated_by")
        if row is None:
            return 200, {"site_id": site, "configured": False,
                         **{k: None for k in keys}}
        return 200, {"site_id": site,
                     "configured": (row.get("robot_hour_cost") is not None
                                    or row.get("operator_hour_cost") is not None),
                     **{k: row.get(k) for k in keys}}

    def rpc_admin_set_cost_settings(self, ident: _Identity,
                                    args: Row) -> tuple[int, Any]:
        site = args.get("p_site")
        if not site:
            return 400, {"message": "admin_set_cost_settings: p_site is required",
                         "code": "P0001"}
        if self.rbac and not self.has_role(ident, "admin", site):
            return self._denied("admin_set_cost_settings", "admin role")
        row = next((r for r in self.tables["site_cost_settings"]
                    if r.get("site_id") == site), None)
        if row is None:
            row = {"site_id": site}
            self.tables["site_cost_settings"].append(row)
        for key, arg in (("currency", "p_currency"),
                         ("robot_hour_cost", "p_robot_hour_cost"),
                         ("operator_hour_cost", "p_operator_hour_cost"),
                         ("target_utilization_pct", "p_target_utilization_pct"),
                         ("shift_hours_per_day", "p_shift_hours_per_day"),
                         ("notes", "p_notes")):
            if args.get(arg) is not None:      # null = leave as is
                row[key] = args[arg]
            row.setdefault(key, None)
        row["updated_at"] = _now_iso()
        row["updated_by"] = ident.email or "service"
        return 200, dict(row, configured=True)

    def rpc_utilization_rollup(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("utilization_rollup",
                                f"operator role (or higher) at site {site}")
        hi = _ts(args.get("p_to")) or _now()
        lo = _ts(args.get("p_from")) or (hi - timedelta(hours=24))
        gap = args.get("p_max_gap_seconds")
        gap = 60 if not isinstance(gap, int) else min(max(gap, 1), 3600)
        if lo >= hi:
            return 400, {"message": f"utilization_rollup: p_from ({_iso(lo)}) "
                                    f"must be before p_to ({_iso(hi)})",
                         "code": "P0001"}
        window = (hi - lo).total_seconds()

        spans = self._spans(site, lo, hi, gap)
        per: dict[str, Row] = {}
        for robot_id, status, secs, ts in spans:
            r = per.setdefault(robot_id, {
                "robot_id": robot_id, "active_seconds": 0.0,
                "idle_seconds": 0.0, "charging_seconds": 0.0,
                "faulted_seconds": 0.0, "other_seconds": 0.0,
                "covered_seconds": 0.0, "samples": 0,
                "first_ts": None, "last_ts": None, "by_status": {}})
            r[f"{_bucket(status)}_seconds"] += secs
            r["covered_seconds"] += secs
            r["samples"] += 1
            key = status or "unknown"
            r["by_status"][key] = round(r["by_status"].get(key, 0.0) + secs, 1)
            iso = _iso(ts)
            if r["first_ts"] is None or iso < r["first_ts"]:
                r["first_ts"] = iso
            if r["last_ts"] is None or iso > r["last_ts"]:
                r["last_ts"] = iso

        # tasks_done delta: max - min of the non-null samples in the window.
        deltas: dict[str, int] = {}
        for row in self.tables["robot_telemetry"]:
            if row.get("site_id") != site or row.get("tasks_done") is None:
                continue
            ts = _ts(row.get("ts"))
            if ts is None or ts < lo or ts > hi:
                continue
            seen = deltas.setdefault(row["robot_id"], [row["tasks_done"],
                                                       row["tasks_done"]])
            seen[0] = min(seen[0], row["tasks_done"])
            seen[1] = max(seen[1], row["tasks_done"])

        robots = []
        for robot_id in sorted(per):
            r = per[robot_id]
            covered = r["covered_seconds"]
            span = deltas.get(robot_id)
            robots.append({
                "robot_id": robot_id,
                **{k: round(r[k], 1) for k in
                   ("active_seconds", "idle_seconds", "charging_seconds",
                    "faulted_seconds", "other_seconds", "covered_seconds")},
                "utilization_pct": (round(100 * r["active_seconds"] / covered, 2)
                                    if covered > 0 else None),
                "coverage_pct": (round(100 * covered / window, 2)
                                 if window > 0 else None),
                "samples": r["samples"], "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "tasks_done_delta": None if span is None else span[1] - span[0],
                "by_status": r["by_status"]})

        def total(key: str) -> float:
            return sum(r[key] for r in robots)

        covered_all = total("covered_seconds")
        fleet = {
            "robot_count": len(robots),
            **{k: round(total(k), 1) for k in
               ("active_seconds", "idle_seconds", "charging_seconds",
                "faulted_seconds", "other_seconds", "covered_seconds")},
            **{f"{b}_robot_hours": round(total(f"{b}_seconds") / 3600, 3)
               for b in ("active", "idle", "charging", "faulted", "covered")},
            "utilization_pct": (round(100 * total("active_seconds") / covered_all, 2)
                                if covered_all > 0 else None),
            "coverage_pct": (round(100 * covered_all / (window * len(robots)), 2)
                             if window > 0 and robots else None)}

        completed = sum(1 for m in self.tables["missions"]
                        if m.get("site_id") == site and m.get("completed_at")
                        and lo <= (_ts(m["completed_at"]) or hi) <= hi)
        task_deltas = [r["tasks_done_delta"] for r in robots
                       if r["tasks_done_delta"] is not None]
        throughput = {
            "completed_missions": completed,
            "tasks_done_delta": sum(task_deltas) if task_deltas else None,
            "tasks_done_total": sum(r.get("tasks_done") or 0
                                    for r in self.tables["robots"]
                                    if r.get("site_id") == site),
            "tasks_per_hour": (round(completed * 3600.0 / window, 3)
                               if window > 0 else None)}

        _, cost = self.rpc_get_cost_settings(_Identity("service"),
                                             {"p_site": site})
        return 200, {"site_id": site, "from": _iso(lo), "to": _iso(hi),
                     "window_seconds": round(window, 1), "max_gap_seconds": gap,
                     "generated_at": _now_iso(), "robots": robots,
                     "fleet": fleet, "throughput": throughput, "cost": cost}

    def rpc_utilization_series(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("utilization_series",
                                f"operator role (or higher) at site {site}")
        bucket = str(args.get("p_bucket") or "day").lower()
        if bucket not in ("hour", "day", "week"):
            return 400, {"message": "utilization_series: p_bucket must be "
                                    f"'hour', 'day' or 'week' (got {bucket})",
                         "code": "P0001"}
        hi = _ts(args.get("p_to")) or _now()
        lo = _ts(args.get("p_from")) or (hi - timedelta(days=7))
        gap = args.get("p_max_gap_seconds")
        gap = 60 if not isinstance(gap, int) else min(max(gap, 1), 3600)
        if lo >= hi:
            return 400, {"message": f"utilization_series: p_from ({_iso(lo)}) "
                                    f"must be before p_to ({_iso(hi)})",
                         "code": "P0001"}

        def trunc(ts: datetime) -> datetime:
            if bucket == "hour":
                return ts.replace(minute=0, second=0, microsecond=0)
            day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket == "day":
                return day
            return day - timedelta(days=day.weekday())      # date_trunc('week')

        buckets: dict[datetime, Row] = {}
        for _robot, status, secs, ts in self._spans(site, lo, hi, gap):
            key = trunc(ts)
            b = buckets.setdefault(key, {
                "bucket": _iso(key), "active_seconds": 0.0, "idle_seconds": 0.0,
                "charging_seconds": 0.0, "faulted_seconds": 0.0,
                "covered_seconds": 0.0})
            name = _bucket(status)
            if name != "other":
                b[f"{name}_seconds"] += secs
            b["covered_seconds"] += secs
        out = []
        for key in sorted(buckets):
            b = buckets[key]
            covered = b["covered_seconds"]
            out.append({"bucket": b["bucket"],
                        **{k: round(b[k], 1) for k in
                           ("active_seconds", "idle_seconds",
                            "charging_seconds", "faulted_seconds",
                            "covered_seconds")},
                        "utilization_pct": (round(100 * b["active_seconds"] / covered, 2)
                                            if covered > 0 else None)})
        return 200, {"site_id": site, "from": _iso(lo), "to": _iso(hi),
                     "bucket": bucket, "max_gap_seconds": gap,
                     "generated_at": _now_iso(), "buckets": out}

    # ---- 0013 explainable maintenance ------------------------------------

    @staticmethod
    def factors_problem(factors: Any) -> str | None:
        """Python mirror of ``public.yf_factors_problem`` (0013)."""
        if factors is None:
            return None
        if not isinstance(factors, list):
            return "factors must be a JSON array"
        for i, el in enumerate(factors, start=1):
            if not isinstance(el, dict):
                return f"factor {i} is not an object"
            if not str(el.get("factor") or "").strip():
                return (f"factor {i} is missing \"factor\" "
                        "(a stable machine key)")
            if el.get("weight") is None:
                return f"factor \"{el.get('factor')}\" is missing \"weight\""
            try:
                float(el["weight"])
            except (TypeError, ValueError):
                return (f"factor \"{el.get('factor')}\" has a non-numeric "
                        "weight")
        return None

    def rpc_maintenance_explain(self, ident: _Identity,
                                args: Row) -> tuple[int, Any]:
        finding_id = args.get("p_finding_id")
        if not finding_id:
            return 400, {"message": "maintenance_explain: p_finding_id is "
                                    "required", "code": "P0001"}
        f = next((r for r in self.tables["maintenance_findings"]
                  if r.get("id") == finding_id), None)
        if f is None:
            return 400, {"message": f"maintenance_explain: finding "
                                    f"{finding_id} not found", "code": "P0001"}
        site = f.get("site_id", DEFAULT_SITE_ID)
        if self.rbac and not self.can_read_site(ident, site):
            return self._denied("maintenance_explain",
                                f"operator role (or higher) at site {site}")
        raw = f.get("factors") or []
        problem = self.factors_problem(raw)
        factors = []
        if problem is None:
            total = sum(abs(float(e["weight"])) for e in raw) or None
            for el in raw:
                weight = float(el["weight"])
                factors.append({
                    "factor": el.get("factor"),
                    "label": el.get("label") or el.get("factor"),
                    "weight": weight,
                    "weight_pct": (round(100 * abs(weight) / total, 1)
                                   if total else None),
                    "value": None if el.get("value") is None else str(el["value"]),
                    "unit": el.get("unit"), "direction": el.get("direction"),
                    "threshold": (None if el.get("threshold") is None
                                  else str(el["threshold"])),
                    "window": el.get("window"), "detail": el.get("detail")})
            factors.sort(key=lambda x: -x["weight"])
        feedback = sorted(
            ({k: r.get(k) for k in
              ("verdict", "actual_fault", "action_taken", "parts_replaced",
               "downtime_minutes", "note", "submitted_by", "created_at",
               "updated_at")}
             for r in self.tables["maintenance_feedback"]
             if r.get("finding_id") == finding_id),
            key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return 200, {
            **{k: f.get(k) for k in
               ("id", "robot_id", "site_id", "component", "finding",
                "rul_days", "confidence", "score", "severity", "action",
                "state", "model_version", "created_at", "cleared_at")},
            "factors": factors, "factors_problem": problem,
            "feedback": feedback, "feedback_count": len(feedback)}

    def rpc_maintenance_submit_feedback(self, ident: _Identity,
                                        args: Row) -> tuple[int, Any]:
        if self.rbac and ident.kind == "anon":
            return self._unauth("maintenance_submit_feedback")
        verdict = args.get("p_verdict")
        if verdict not in ("correct", "incorrect", "unclear"):
            return 400, {"message": "maintenance_submit_feedback: p_verdict "
                                    "must be 'correct', 'incorrect' or "
                                    f"'unclear' (got {verdict})",
                         "code": "P0001"}
        finding_id = args.get("p_finding_id")
        f = next((r for r in self.tables["maintenance_findings"]
                  if r.get("id") == finding_id), None)
        if f is None:
            return 400, {"message": f"maintenance_submit_feedback: finding "
                                    f"{finding_id} not found", "code": "P0001"}
        site = f.get("site_id", DEFAULT_SITE_ID)
        if self.rbac and not self.has_role(ident, "engineer", site):
            return self._denied("maintenance_submit_feedback",
                                f"engineer role (or higher) at site {site}")
        downtime = args.get("p_downtime_minutes")
        if isinstance(downtime, (int, float)) and downtime < 0:
            return 400, {"message": "maintenance_submit_feedback: "
                                    "p_downtime_minutes must be >= 0",
                         "code": "P0001"}
        uid = ident.sub or ident.email or "service"
        row = next((r for r in self.tables["maintenance_feedback"]
                    if r.get("finding_id") == finding_id
                    and r.get("submitted_by_uid") == uid), None)
        if row is None:
            row = {"id": str(uuid.uuid4()), "finding_id": finding_id,
                   "site_id": site, "submitted_by": ident.email or "service",
                   "submitted_by_uid": uid, "created_at": _now_iso()}
            self.tables["maintenance_feedback"].append(row)
        row.update({"verdict": verdict,
                    "actual_fault": args.get("p_actual_fault"),
                    "action_taken": args.get("p_action_taken"),
                    "parts_replaced": args.get("p_parts_replaced"),
                    "downtime_minutes": downtime,
                    "note": args.get("p_note"),
                    "updated_at": _now_iso()})
        return 200, {k: row.get(k) for k in
                     ("id", "finding_id", "site_id", "verdict", "actual_fault",
                      "action_taken", "parts_replaced", "downtime_minutes",
                      "note", "submitted_by", "created_at", "updated_at")}

    def rpc_maintenance_accuracy(self, ident: _Identity,
                                 args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.has_role(ident, "engineer", site):
            return self._denied("maintenance_accuracy",
                                f"engineer role (or higher) at site {site}")
        hi = _ts(args.get("p_to")) or _now()
        lo = _ts(args.get("p_from")) or (hi - timedelta(days=90))
        findings = {f["id"]: f for f in self.tables["maintenance_findings"]
                    if f.get("id") is not None
                    and f.get("site_id") == site
                    and lo <= (_ts(f.get("created_at")) or hi) <= hi}
        scored = [(findings[r["finding_id"]], r)
                  for r in self.tables["maintenance_feedback"]
                  if r.get("finding_id") in findings]

        def precision(rows: list[tuple[Row, Row]]) -> float | None:
            decided = [v for _f, v in rows if v["verdict"] in ("correct", "incorrect")]
            if not decided:
                return None
            correct = sum(1 for v in decided if v["verdict"] == "correct")
            return round(100.0 * correct / len(decided), 2)

        def mean(values: list[float]) -> float | None:
            return round(sum(values) / len(values), 4) if values else None

        overall = {
            "total": len(scored),
            "correct": sum(1 for _f, v in scored if v["verdict"] == "correct"),
            "incorrect": sum(1 for _f, v in scored if v["verdict"] == "incorrect"),
            "unclear": sum(1 for _f, v in scored if v["verdict"] == "unclear"),
            "precision": precision(scored),
            "mean_confidence_correct": mean(
                [float(f["confidence"]) for f, v in scored
                 if v["verdict"] == "correct" and f.get("confidence") is not None]),
            "mean_confidence_incorrect": mean(
                [float(f["confidence"]) for f, v in scored
                 if v["verdict"] == "incorrect" and f.get("confidence") is not None])}

        by_component: dict[str, list[tuple[Row, Row]]] = {}
        for f, v in scored:
            by_component.setdefault(f.get("component"), []).append((f, v))
        components = [{
            "component": name,
            "total": len(rows),
            "correct": sum(1 for _f, v in rows if v["verdict"] == "correct"),
            "incorrect": sum(1 for _f, v in rows if v["verdict"] == "incorrect"),
            "unclear": sum(1 for _f, v in rows if v["verdict"] == "unclear"),
            "precision": precision(rows)}
            for name, rows in by_component.items()]
        components.sort(key=lambda r: -r["total"])

        by_factor: dict[str, list[tuple[float, str]]] = {}
        for f, v in scored:
            raw = f.get("factors") or []
            if self.factors_problem(raw) is not None:
                continue
            for el in raw:
                by_factor.setdefault(str(el["factor"]), []).append(
                    (float(el["weight"]), v["verdict"]))
        factors = []
        for name, entries in by_factor.items():
            decided = [e for e in entries if e[1] in ("correct", "incorrect")]
            correct = sum(1 for e in decided if e[1] == "correct")
            factors.append({
                "factor": name, "appearances": len(entries),
                "on_correct": sum(1 for e in entries if e[1] == "correct"),
                "on_incorrect": sum(1 for e in entries if e[1] == "incorrect"),
                "mean_weight_correct": mean([w for w, k in entries if k == "correct"]),
                "mean_weight_incorrect": mean([w for w, k in entries if k == "incorrect"]),
                "precision": (round(100.0 * correct / len(decided), 2)
                              if decided else None)})
        factors.sort(key=lambda r: -r["appearances"])

        return 200, {"site_id": site, "from": _iso(lo), "to": _iso(hi),
                     "generated_at": _now_iso(), "overall": overall,
                     "by_component": components, "by_factor": factors}

    # ---- 0014 inbound ops ------------------------------------------------

    INBOUND_CHANNELS = ("whatsapp", "sms", "telegram", "email", "voice")
    INBOUND_ACTIONS = ("ack_alert", "assign_incident", "approve_command",
                       "reject_command", "status")

    def rpc_inbound_map_identity(self, ident: _Identity,
                                 args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.has_role(ident, "admin", site):
            return self._denied("inbound_map_identity", "admin role")
        channel = args.get("p_channel")
        if channel not in self.INBOUND_CHANNELS:
            return 400, {"message": f"inbound_map_identity: unknown channel "
                                    f"\"{channel}\"", "code": "P0001"}
        external = str(args.get("p_external_id") or "").strip()
        if not external:
            return 400, {"message": "inbound_map_identity: p_external_id is "
                                    "required", "code": "P0001"}
        email = str(args.get("p_user_email") or "").strip()
        if not email:
            return 400, {"message": f"inbound_map_identity: no auth user with "
                                    f"email {email} — they must sign in once "
                                    "first", "code": "P0001"}
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"yf-test-user:{email}"))
        row = next((r for r in self.channel_identities
                    if r["channel"] == channel and r["external_id"] == external),
                   None)
        if row is None:
            row = {"id": str(uuid.uuid4()), "channel": channel,
                   "external_id": external, "created_at": _now_iso()}
            self.channel_identities.append(row)
        row.update({"user_id": uid, "user_email": email, "site_id": site,
                    "display_name": (args.get("p_display_name") or
                                     row.get("display_name")),
                    "verified_at": _now_iso(), "revoked_at": None,
                    "created_by": ident.email or "service"})
        return 200, {k: row.get(k) for k in
                     ("id", "channel", "external_id", "user_id", "site_id",
                      "display_name", "verified_at", "revoked_at",
                      "created_at", "created_by")}

    def rpc_inbound_unmap_identity(self, ident: _Identity,
                                   args: Row) -> tuple[int, Any]:
        channel = args.get("p_channel")
        external = str(args.get("p_external_id") or "").strip()
        row = next((r for r in self.channel_identities
                    if r["channel"] == channel and r["external_id"] == external),
                   None)
        if row is None:
            return 400, {"message": f"inbound_unmap_identity: no mapping for "
                                    f"{external} on {channel}", "code": "P0001"}
        if self.rbac and not self.has_role(ident, "admin", row["site_id"]):
            return self._denied("inbound_unmap_identity", "admin role")
        row["revoked_at"] = _now_iso()          # revoke, never delete
        return 200, {"id": row["id"], "channel": row["channel"],
                     "external_id": row["external_id"],
                     "revoked_at": row["revoked_at"]}

    def rpc_inbound_list_identities(self, ident: _Identity,
                                    args: Row) -> tuple[int, Any]:
        site = args.get("p_site")
        if self.rbac and not self.has_role(ident, "admin", site or DEFAULT_SITE_ID):
            return self._denied("inbound_list_identities", "admin role")
        out = [{**{k: r.get(k) for k in
                   ("id", "channel", "external_id", "user_id", "site_id",
                    "display_name", "verified_at", "revoked_at", "created_at")},
                "rank_at_site": self.rank_at(r.get("user_email"), r["site_id"]),
                "active": not r.get("revoked_at")}
               for r in self.channel_identities
               if site is None or r["site_id"] == site]
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return 200, out

    def _inbound_lookup(self, channel: Any, external: Any) -> Row | None:
        key = str(external or "").strip()
        return next((r for r in self.channel_identities
                     if r["channel"] == channel and r["external_id"] == key
                     and not r.get("revoked_at")), None)

    def rpc_inbound_resolve_sender(self, ident: _Identity,
                                   args: Row) -> tuple[int, Any]:
        if self.rbac and ident.kind != "service":
            return 403, {"message": "inbound_resolve_sender: service_role only",
                         "code": "42501"}
        row = self._inbound_lookup(args.get("p_channel"), args.get("p_external_id"))
        if row is None:
            return 200, {"mapped": False, "channel": args.get("p_channel"),
                         "user_id": None, "site_id": None,
                         "display_name": None, "rank": 0}
        return 200, {"mapped": True, "channel": row["channel"],
                     "user_id": row["user_id"], "site_id": row["site_id"],
                     "display_name": (row.get("display_name")
                                      or _mask_identity(row["external_id"])),
                     "rank": self.rank_at(row.get("user_email"), row["site_id"])}

    def rpc_inbound_perform(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        """The single inbound entry point. service_role only; NEVER raises for
        an authorisation failure — the refusal is returned AND audited."""
        if self.rbac and ident.kind != "service":
            return 403, {"message": "inbound_perform: service_role only — an "
                                    "inbound message must never be replayable "
                                    "from a browser session", "code": "42501"}
        channel = args.get("p_channel")
        external = str(args.get("p_external_id") or "").strip()
        action = args.get("p_action")
        if action not in self.INBOUND_ACTIONS:
            action = "unknown"
        target = args.get("p_target_id")
        note = args.get("p_note")
        message_id = (str(args.get("p_message_id") or "").strip() or None)

        if message_id:
            prior = next((a for a in self.inbound_actions
                          if a.get("message_id") == message_id), None)
            if prior is not None:
                return 200, {"ok": prior["result"] == "ok",
                             "result": prior["result"], "action": prior["action"],
                             "target_id": prior["target_id"],
                             "site_id": prior["site_id"],
                             "mapped_as": prior["mapped_as"],
                             # neither is persisted — a replay reports null
                             # for both rather than omitting the keys, so the
                             # envelope's key set never varies by outcome
                             "rank": None,
                             "detail": prior["detail"],
                             "payload": None,
                             "log_id": prior["id"], "replayed": True}

        mapping = self._inbound_lookup(channel, external)
        result, detail, payload = "error", None, None
        uid = site = name = None
        rank = 0
        if mapping is None:
            result = "unmapped"
            detail = "sender is not mapped to a platform user"
        else:
            uid, site = mapping["user_id"], mapping["site_id"]
            name = mapping.get("display_name") or _mask_identity(external)
            rank = self.rank_at(mapping.get("user_email"), site)
            if action == "unknown":
                result, detail = "invalid", (
                    f"unrecognised action \"{args.get('p_action')}\"")
            elif rank < 1:
                result, detail = "denied", f"{name} holds no role at site {site}"
            elif action == "status":
                robots = [r for r in self.tables["robots"]
                          if r.get("site_id") == site]
                payload = {
                    "site_id": site, "robots_total": len(robots),
                    "robots_active": sum(1 for r in robots
                                         if r.get("status") == "active"),
                    "robots_faulted": sum(1 for r in robots if r.get("status")
                                          in ("fault", "estop", "degraded")),
                    "open_incidents": sum(1 for i in self.tables["incidents"]
                                          if i.get("site_id") == site
                                          and i.get("state") == "Open"),
                    "unacked_alerts": sum(1 for a in self.tables["alerts"]
                                          if a.get("site_id") == site
                                          and not a.get("ack")),
                    "pending_commands": sum(1 for c in self.tables["commands"]
                                            if c.get("site_id") == site
                                            and c.get("status") == "pending")}
                result, detail = "ok", "status snapshot"
            elif action == "ack_alert":
                alert = next((a for a in self.tables["alerts"]
                              if a.get("id") == target and a.get("site_id") == site),
                             None)
                if alert is None:
                    result, detail = "not_found", f"no alert {target} at site {site}"
                else:
                    alert["ack"] = True
                    result = "ok"
                    detail = f"alert {alert['id']} acknowledged by {name}"
                    payload = {"id": alert["id"], "sev": alert.get("sev"),
                               "msg": alert.get("msg"), "ack": True}
            elif action == "assign_incident":
                inc = next((i for i in self.tables["incidents"]
                            if i.get("id") == target and i.get("site_id") == site),
                           None)
                if inc is None:
                    result, detail = "not_found", (
                        f"no incident {target} at site {site}")
                elif note and str(note).strip():
                    if rank < ROLE_RANK["manager"]:
                        result, detail = "denied", (
                            f"{name} may self-assign, but assigning another "
                            f"person requires manager role at site {site}")
                    else:
                        inc.update({"assignee": str(note).strip(),
                                    "assignee_uid": None,
                                    "assigned_at": _now_iso()})
                        result = "ok"
                        detail = (f"incident {inc['id']} assigned to "
                                  f"{str(note).strip()} by {name}")
                else:
                    inc.update({"assignee": name, "assignee_uid": uid,
                                "assigned_at": _now_iso()})
                    result = "ok"
                    detail = f"incident {inc['id']} self-assigned by {name}"
                if result == "ok" and inc is not None:
                    payload = {"id": inc["id"], "title": inc.get("title"),
                               "sev": inc.get("sev"), "state": inc.get("state")}
            else:   # approve_command / reject_command
                if rank < ROLE_RANK["manager"]:
                    result, detail = "denied", (
                        f"{name} (rank {rank}) may not decide commands — "
                        f"manager role required at site {site}")
                else:
                    cmd = next((c for c in self.tables["commands"]
                                if str(c.get("id")) == str(target)
                                and c.get("site_id") == site), None)
                    if cmd is None:
                        result, detail = "not_found", (
                            f"no command {target} at site {site}")
                    elif cmd.get("status", "pending") != "pending":
                        result, detail = "invalid", (
                            f"command {cmd['id']} is already "
                            f"'{cmd.get('status')}' — only pending commands "
                            "can be decided")
                    else:
                        cmd["status"] = ("approved" if action == "approve_command"
                                         else "rejected")
                        cmd["decided_by"] = f"{channel}:{name}"
                        cmd["decided_at"] = _now_iso()
                        if note and str(note).strip():
                            cmd["note"] = str(note).strip()
                        result = "ok"
                        detail = (f"command {cmd['id']} {cmd['status']} by "
                                  f"{name} via {channel}")
                        payload = {"id": cmd["id"], "robot_id": cmd.get("robot_id"),
                                   "cmd": cmd.get("cmd"), "status": cmd["status"]}

        log = {"id": str(uuid.uuid4()), "channel": channel or "unknown",
               "external_id": external, "user_id": uid, "mapped_as": name,
               "site_id": site, "action": action, "target_id": target,
               "raw_text": args.get("p_raw_text"), "result": result,
               "detail": detail, "message_id": message_id,
               "received_at": _now_iso(), "processed_at": _now_iso()}
        self.inbound_actions.append(log)
        return 200, {"ok": result == "ok", "result": result, "action": action,
                     "target_id": target, "site_id": site, "mapped_as": name,
                     "rank": rank, "detail": detail, "payload": payload,
                     "log_id": log["id"], "replayed": False}

    def rpc_inbound_recent_actions(self, ident: _Identity,
                                   args: Row) -> tuple[int, Any]:
        site = args.get("p_site") or DEFAULT_SITE_ID
        if self.rbac and not self.has_role(ident, "manager", site):
            return self._denied("inbound_recent_actions",
                                f"manager role (or higher) at site {site}")
        limit = max(int(args.get("p_limit") or 50), 1)
        out = [{"id": a["id"], "channel": a["channel"],
                "external_id_masked": _mask_identity(a["external_id"]),
                "mapped_as": a["mapped_as"], "site_id": a["site_id"],
                "action": a["action"], "target_id": a["target_id"],
                "result": a["result"], "detail": a["detail"],
                "received_at": a["received_at"],
                "processed_at": a["processed_at"]}
               for a in self.inbound_actions
               if a["site_id"] == site or a["site_id"] is None]
        out.sort(key=lambda r: str(r.get("received_at") or ""), reverse=True)
        return 200, out[:limit]

    # ---- 0015 certification ----------------------------------------------

    def rpc_issue_certificate_v2(self, ident: _Identity,
                                 args: Row) -> tuple[int, Any]:
        if self.rbac and ident.kind == "anon":
            return self._unauth("issue_certificate_v2")
        track, score = args.get("p_track"), args.get("p_score")
        if not track or not str(track).strip():
            return 400, {"message": "issue_certificate_v2: p_track is required",
                         "code": "P0001"}
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            return 400, {"message": "issue_certificate_v2: p_score must be "
                                    f"between 0 and 100 (got {score})",
                         "code": "P0001"}
        level = args.get("p_level")
        if level is not None and level not in ("bronze", "silver", "gold",
                                               "platinum"):
            return 400, {"message": "issue_certificate_v2: p_level must be "
                                    f"bronze/silver/gold/platinum (got {level})",
                         "code": "P0001"}
        code = (args.get("p_code") or "").strip() or None
        if code and any(c["verification_code"] == code for c in self.certificates):
            return 409, {"message": f"issue_certificate_v2: verification code "
                                    f"\"{code}\" already exists", "code": "23505"}
        row = self._new_certificate(ident, track, score, code, level,
                                    args.get("p_holder_name"),
                                    args.get("p_issued_for"),
                                    args.get("p_site"), args.get("p_expires_at"))
        return 200, {k: row.get(k) for k in
                     ("id", "track", "score", "level", "holder_name",
                      "issued_for", "verification_code", "issued_at",
                      "expires_at")}

    def _new_certificate(self, ident: _Identity, track: Any, score: Any,
                         code: str | None, level: str | None = None,
                         holder: str | None = None, issued_for: str | None = None,
                         site: str | None = None,
                         expires_at: str | None = None) -> Row:
        """The `yf_certificates_fill` BEFORE trigger (0015): whatever the
        caller left out, the database supplies."""
        if not code:
            slug = "".join(ch for ch in str(track or "") if ch.isalnum()).upper()
            code = f"YF-{slug or 'GEN'}-{uuid.uuid4().hex[:12].upper()}"
        if not holder:
            holder = ((ident.email or "").split("@")[0]
                      or "YantraFleet operator")
        row = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"yf-cert:{code}")),
               "user_id": ident.sub or ident.email or "anon",
               "track": track, "score": score,
               "verification_code": code,
               "level": level or _certificate_level(score),
               "holder_name": holder, "issued_for": issued_for or track,
               "site_id": site, "issued_at": _now_iso(),
               "expires_at": expires_at, "revoked_at": None,
               "revoked_by": None, "revoke_reason": None}
        self.certificates.append(row)
        return row

    def rpc_certificate_verify(self, ident: _Identity,
                               args: Row) -> tuple[int, Any]:
        """THE PUBLIC ENDPOINT. Four facts plus a verdict, and nothing else —
        no email, no score, no site, no user id. Never raises."""
        code = str(args.get("p_code") or "").strip()
        if not code:
            return 200, {"valid": False, "status": "not_found"}
        row = next((c for c in self.certificates
                    if c.get("verification_code") == code), None)
        if row is None:
            return 200, {"valid": False, "status": "not_found"}
        expires = _ts(row.get("expires_at"))
        status = ("revoked" if row.get("revoked_at")
                  else "expired" if expires is not None and expires <= _now()
                  else "valid")
        return 200, {"valid": status == "valid", "status": status,
                     "verification_code": row["verification_code"],
                     "holder_name": row.get("holder_name"),
                     "track": row.get("issued_for") or row.get("track"),
                     "level": row.get("level"),
                     "issued_at": row.get("issued_at"),
                     "expires_at": row.get("expires_at")}

    def rpc_my_certificates(self, ident: _Identity, args: Row) -> tuple[int, Any]:
        if self.rbac and ident.kind == "anon":
            return self._unauth("my_certificates")
        me = ident.sub or ident.email or "anon"
        out = [{k: c.get(k) for k in
                ("id", "track", "issued_for", "score", "level", "holder_name",
                 "verification_code", "issued_at", "expires_at", "revoked_at")}
               for c in self.certificates if c.get("user_id") == me]
        out.sort(key=lambda r: str(r.get("issued_at") or ""), reverse=True)
        return 200, out

    def rpc_certificate_revoke(self, ident: _Identity,
                               args: Row) -> tuple[int, Any]:
        if self.rbac and not self.has_role(ident, "admin"):
            return self._denied("certificate_revoke", "admin role")
        code = str(args.get("p_code") or "").strip()
        row = next((c for c in self.certificates
                    if c.get("verification_code") == code), None)
        if row is None:
            return 400, {"message": f"certificate_revoke: no certificate with "
                                    f"code {code}", "code": "P0001"}
        if not row.get("revoked_at"):
            row["revoked_at"] = _now_iso()
            row["revoked_by"] = ident.email or "service"
            row["revoke_reason"] = (args.get("p_reason") or None)
        return 200, {"verification_code": row["verification_code"],
                     "holder_name": row.get("holder_name"),
                     "track": row.get("track"),
                     "revoked_at": row["revoked_at"],
                     "revoked_by": row["revoked_by"],
                     "revoke_reason": row.get("revoke_reason")}

    # ---- 0016 public status page -----------------------------------------

    def rpc_public_fleet_status(self, ident: _Identity,
                                args: Row) -> tuple[int, Any]:
        """anon-callable; counts and percentages only. A disabled site and a
        site that never existed return the IDENTICAL shape, so this can never
        be used to enumerate a customer's sites."""
        site = str(args.get("p_site") or "").strip()
        cfg = self.status_pages.get(site)
        if not site or cfg is None or not cfg.get("enabled"):
            return 200, {"site_id": site or None, "enabled": False,
                         "status": "not_published"}
        grace = cfg.get("online_grace_seconds", 300)
        cutoff = _now() - timedelta(seconds=grace)
        robots = [r for r in self.tables["robots"] if r.get("site_id") == site]
        statuses = [r.get("status") for r in robots]
        online = sum(1 for r in robots
                     if (_ts(r.get("updated_at")) or _now()) > cutoff)
        last = max((r.get("updated_at") for r in robots
                    if r.get("updated_at")), default=None)

        incidents = alerts = by_sev = None
        if cfg.get("show_incidents", True):
            open_incs = [i for i in self.tables["incidents"]
                         if i.get("site_id") == site and i.get("state") == "Open"]
            incidents = len(open_incs)
            by_sev = {}
            for i in open_incs:
                by_sev[i.get("sev")] = by_sev.get(i.get("sev"), 0) + 1
            alerts = sum(1 for a in self.tables["alerts"]
                         if a.get("site_id") == site and not a.get("ack"))
        throughput = None
        if cfg.get("show_throughput", True):
            midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
            throughput = sum(1 for m in self.tables["missions"]
                             if m.get("site_id") == site and m.get("completed_at")
                             and (_ts(m["completed_at"]) or midnight) >= midnight)

        spans = self._spans(site, _now() - timedelta(hours=24), _now(), 60)
        covered = sum(s for _r, _st, s, _t in spans)
        up = sum(s for _r, st, s, _t in spans
                 if st not in ("fault", "estop", "degraded"))
        return 200, {
            "site_id": site, "enabled": True, "status": "published",
            "display_name": cfg.get("display_name") or site,
            "blurb": cfg.get("blurb"), "generated_at": _now_iso(),
            "robots_total": len(robots), "robots_online": online,
            "robots_online_pct": (round(100.0 * online / len(robots), 1)
                                  if robots else None),
            "robots_active": statuses.count("active"),
            "robots_charging": statuses.count("charging"),
            "robots_faulted": sum(1 for s in statuses
                                  if s in ("fault", "estop", "degraded")),
            "uptime_pct_24h": (round(100 * up / covered, 2)
                               if covered > 0 else None),
            "uptime_covered_seconds": round(covered),
            "active_incidents": incidents,
            "active_incidents_by_sev": by_sev,
            "unacked_alerts": alerts, "throughput_today": throughput,
            "last_robot_update": last}

    def rpc_admin_set_status_page(self, ident: _Identity,
                                  args: Row) -> tuple[int, Any]:
        site = str(args.get("p_site") or "").strip()
        if not site:
            return 400, {"message": "admin_set_status_page: p_site is required",
                         "code": "P0001"}
        if self.rbac and not self.has_role(ident, "admin", site):
            return self._denied("admin_set_status_page", "admin role")
        cfg = self.status_pages.setdefault(site, {
            "site_id": site, "enabled": False, "display_name": None,
            "blurb": None, "show_throughput": True, "show_incidents": True,
            "online_grace_seconds": 300})
        for key, arg in (("enabled", "p_enabled"),
                         ("display_name", "p_display_name"),
                         ("blurb", "p_blurb"),
                         ("show_throughput", "p_show_throughput"),
                         ("show_incidents", "p_show_incidents"),
                         ("online_grace_seconds", "p_online_grace_seconds")):
            if args.get(arg) is not None:
                cfg[key] = args[arg]
        cfg["updated_at"] = _now_iso()
        cfg["updated_by"] = ident.email or "service"
        return 200, dict(cfg)

    def rpc_admin_list_status_pages(self, ident: _Identity,
                                    args: Row) -> tuple[int, Any]:
        if self.rbac and not self.has_role(ident, "admin"):
            return self._denied("admin_list_status_pages", "admin role")
        return 200, sorted((dict(c) for c in self.status_pages.values()),
                           key=lambda r: str(r.get("site_id")))

    RPCS: dict[str, str] = {
        # 0007 / 0008
        "decide_command": "rpc_decide_command",
        "save_progress": "rpc_save_progress",
        "issue_certificate": "rpc_issue_certificate",
        "admin_list_settings": "rpc_admin_list_settings",
        "admin_set_setting": "rpc_admin_set_setting",
        # 0009 demo sandbox
        "demo_mint_session": "rpc_demo_mint_session",
        "demo_claim_session": "rpc_demo_claim_session",
        "demo_session_info": "rpc_demo_session_info",
        "demo_end_session": "rpc_demo_end_session",
        "demo_reap_expired": "rpc_demo_reap_expired",
        "demo_limits_public": "rpc_demo_limits_public",
        "admin_list_demo_sessions": "rpc_admin_list_demo_sessions",
        "admin_set_demo_limits": "rpc_admin_set_demo_limits",
        # 0010 share links
        "share_mint_link": "rpc_share_mint_link",
        "share_resolve": "rpc_share_resolve",
        "share_revoke": "rpc_share_revoke",
        "share_list_links": "rpc_share_list_links",
        # 0011 replay
        "replay_time_range": "rpc_replay_time_range",
        "replay_state_at": "rpc_replay_state_at",
        "replay_robot_track": "rpc_replay_robot_track",
        # 0012 utilization
        "get_cost_settings": "rpc_get_cost_settings",
        "admin_set_cost_settings": "rpc_admin_set_cost_settings",
        "utilization_rollup": "rpc_utilization_rollup",
        "utilization_series": "rpc_utilization_series",
        # 0013 explainable maintenance
        "maintenance_explain": "rpc_maintenance_explain",
        "maintenance_submit_feedback": "rpc_maintenance_submit_feedback",
        "maintenance_accuracy": "rpc_maintenance_accuracy",
        # 0014 inbound ops
        "inbound_map_identity": "rpc_inbound_map_identity",
        "inbound_unmap_identity": "rpc_inbound_unmap_identity",
        "inbound_list_identities": "rpc_inbound_list_identities",
        "inbound_resolve_sender": "rpc_inbound_resolve_sender",
        "inbound_perform": "rpc_inbound_perform",
        "inbound_recent_actions": "rpc_inbound_recent_actions",
        # 0015 certification
        "issue_certificate_v2": "rpc_issue_certificate_v2",
        "certificate_verify": "rpc_certificate_verify",
        "my_certificates": "rpc_my_certificates",
        "certificate_revoke": "rpc_certificate_revoke",
        # 0016 public status page
        "public_fleet_status": "rpc_public_fleet_status",
        "admin_set_status_page": "rpc_admin_set_status_page",
        "admin_list_status_pages": "rpc_admin_list_status_pages",
    }


class _Handler(BaseHTTPRequestHandler):
    fake: FakePostgREST  # set by subclass in FakePostgREST.start

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass

    def _route(self) -> tuple[str | None, list[tuple[str, str]]]:
        parts = urlsplit(self.path)
        params = parse_qsl(parts.query, keep_blank_values=True)
        prefix, _, table = parts.path.rpartition("/")
        if prefix != "/rest/v1" or table not in self.fake.tables:
            return None, params
        return table, params

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def _reply(self, code: int, payload: Any | None = None) -> None:
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _prefer_resolution(self) -> str | None:
        prefer = self.headers.get("Prefer", "")
        for part in prefer.replace(" ", "").split(","):
            if part.startswith("resolution="):
                return part.split("=", 1)[1]
        return None

    def _rpc_name(self) -> str | None:
        """The function name for a ``/rest/v1/rpc/<fn>`` path, else None."""
        path = urlsplit(self.path).path
        prefix, _, fn = path.rpartition("/")
        return fn if prefix == "/rest/v1/rpc" and fn else None

    def _write_denied(self, ident: _Identity, what: str) -> None:
        """The common anon-401 / no-role-403 write rejection (rbac mode)."""
        if ident.kind == "anon":
            self._reply(401, {"message": f"{what}: no authenticated session — "
                                         "anon key has no access in RBAC mode",
                              "code": "PGRST301"})
        else:
            self._reply(403, {"message": f"{what}: permission denied — no role "
                                         "grants this write", "code": "42501"})

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        table, params = self._route()
        if table is None:
            # A browser landed on the DATA backend by mistake.
            bare = self.path.split("?", 1)[0]
            if bare in ("/", "/index.html", "/console", "/ui"):
                if self.fake.console_url:
                    self.send_response(302)
                    self.send_header("Location", self.fake.console_url)
                    self.end_headers()
                    return None
                return self._reply(200, {
                    "service": "yantrafleet data backend (fake PostgREST)",
                    "hint": "this is the API, not the UI — open the console "
                            "URL from the READY line of `yantraops up`",
                })
            return self._reply(404, {"message": f"unknown route {self.path}"})
        with self.fake.lock:
            self.fake.requests.append(("GET", self.path))
            source = self.fake.tables[table]
            if self.fake.rbac:
                # RLS stand-in: invisible rows simply do not exist — anon
                # and role-less callers get 200 [] just like real RLS.
                ident = self.fake.identity(self.headers)
                source = [r for r in source
                          if self.fake.row_visible(table, r, ident)]
            rows = _apply_query(source, params)
        self._reply(200, rows)

    def do_POST(self) -> None:  # noqa: N802
        fn = self._rpc_name()
        if fn is not None:
            return self._do_rpc(fn)
        table, params = self._route()
        if table is None:
            return self._reply(404, {"message": f"unknown route {self.path}"})
        body = self._body()
        rows = body if isinstance(body, list) else [body or {}]
        on_conflict = next((v for k, v in params if k == "on_conflict"), None)
        if self.fake.rbac:
            ident = self.fake.identity(self.headers)
            if ident.kind != "service":
                err = self._insert_denied(table, rows, ident)
                if err is not None:
                    return err
        with self.fake.lock:
            self.fake.requests.append(("POST", self.path))
            self.fake.insert(table, rows, on_conflict, self._prefer_resolution())
        self._reply(201)

    def _insert_denied(self, table: str, rows: list[Row],
                       ident: _Identity) -> None | Any:
        """rbac gate for non-service INSERTs. None = allowed; otherwise the
        rejection has been written and its (truthy) sentinel is returned.

        Mirrors 0007: only ``rbac_commands_insert`` exists — an operator+
        may insert a *pending* command as themselves at their own site;
        every other table has no insert policy at all. Plus 0009's
        ``demo_sandbox_insert``: a live demo token may insert into the
        seven fleet tables, pinned to its own DEMO-* site.
        """
        what = f"insert into {table}"
        if ident.is_demo:
            if table not in DEMO_TABLES:
                return self._reply(403, {
                    "message": f"{what}: permission denied — a demo token "
                               "reaches only the fleet tables of its own "
                               "sandbox", "code": "42501"}) or True
            for row in rows:
                if row.get("site_id", DEFAULT_SITE_ID) != ident.site_id:
                    return self._reply(403, {
                        "message": "new row violates row-level security "
                                   f"policy for table \"{table}\" — a demo "
                                   "token may only write its own sandbox "
                                   f"site {ident.site_id}",
                        "code": "42501"}) or True
            return None
        if ident.kind == "anon" or ident.rank < 1:
            return self._write_denied(ident, what) or True
        if table != "commands":
            return self._reply(403, {
                "message": f"{what}: permission denied — writers must use the "
                           "service key in RBAC mode", "code": "42501"}) or True
        for row in rows:  # WITH CHECK, evaluated after column defaults
            site = row.get("site_id", DEFAULT_SITE_ID)
            ok = (row.get("status", "pending") == "pending"
                  and row.get("requested_by") == ident.email
                  and row.get("decided_by") is None
                  and (ident.is_admin or site == ident.site_id))
            if not ok:
                return self._reply(403, {
                    "message": "new row violates row-level security policy "
                               "for table \"commands\" — only a pending command "
                               "requested_by yourself at your own site",
                    "code": "42501"}) or True
        return None

    def do_PATCH(self) -> None:  # noqa: N802
        table, params = self._route()
        if table is None:
            return self._reply(404, {"message": f"unknown route {self.path}"})
        body = self._body() or {}
        row_ok: Callable[[Row], bool] | None = None
        if self.fake.rbac:
            ident = self.fake.identity(self.headers)
            if ident.is_demo:
                # 0009 demo_sandbox_update + the column-level grants.
                what = f"update {table}"
                if table not in DEMO_TABLES:
                    return self._reply(403, {
                        "message": f"{what}: permission denied — a demo token "
                                   "reaches only the fleet tables of its own "
                                   "sandbox", "code": "42501"})
                allowed = DEMO_UPDATE_COLUMNS.get(table)
                if allowed is not None and not set(body) <= allowed:
                    return self._reply(403, {
                        "message": f"{what}: permission denied — a demo token "
                                   f"may only update {sorted(allowed)} on "
                                   f"{table}", "code": "42501"})
                if body.get("site_id", ident.site_id) != ident.site_id:
                    return self._reply(403, {
                        "message": "new row violates row-level security policy "
                                   f"for table \"{table}\" — a demo token may "
                                   "not move a row to another site",
                        "code": "42501"})
                demo_site = ident.site_id
                row_ok = (lambda r: r.get("site_id") == demo_site)
                with self.fake.lock:
                    self.fake.requests.append(("PATCH", self.path))
                    self.fake.patch(table, params, body, row_ok=row_ok)
                return self._reply(204)
            if ident.kind != "service":
                what = f"update {table}"
                if ident.kind == "anon" or ident.rank < 1:
                    return self._write_denied(ident, what)
                # 0007: the ONLY authenticated UPDATE path is alerts.ack
                # (rbac_alerts_ack policy + column-level grant on `ack`).
                if table != "alerts" or set(body) != {"ack"}:
                    return self._reply(403, {
                        "message": f"{what}: permission denied — authenticated "
                                   "may only update alerts.ack (use "
                                   "decide_command for commands)",
                        "code": "42501"})
                site_ok = ident.is_admin
                claim_site = ident.site_id
                row_ok = (None if site_ok else
                          lambda r: r.get("site_id", DEFAULT_SITE_ID) == claim_site)
        with self.fake.lock:
            self.fake.requests.append(("PATCH", self.path))
            self.fake.patch(table, params, body, row_ok=row_ok)
        self._reply(204)

    def _do_rpc(self, fn: str) -> None:
        """POST /rest/v1/rpc/<fn> — PostgREST-style function call."""
        args = self._body() or {}
        if not isinstance(args, dict):
            return self._reply(400, {"message": f"{fn}: arguments must be a "
                                                "JSON object", "code": "P0001"})
        method = self.fake.RPCS.get(fn)
        if method is None:
            return self._reply(404, {
                "message": f"Could not find the function public.{fn} in the "
                           "schema cache", "code": "PGRST202"})
        ident = self.fake.identity(self.headers)
        try:
            with self.fake.lock:
                self.fake.requests.append(("POST", self.path))
                status, payload = getattr(self.fake, method)(ident, args)
        except Exception as exc:  # noqa: BLE001 - see below
            # A handler that raises would otherwise propagate out of
            # BaseHTTPRequestHandler, drop the socket, and reach the caller
            # as an opaque ``RemoteProtocolError: Server disconnected`` with
            # the real traceback buried in stderr. Real PostgREST answers a
            # failing function with a JSON error, so do that: the offending
            # RPC and exception are named in the body where the failing
            # assertion can print them.
            return self._reply(500, {
                "message": f"{fn}: fake handler raised "
                           f"{type(exc).__name__}: {exc}",
                "hint": "This is a bug in e2e/fakerest.py, or a seeded row "
                        "missing a column the real schema declares NOT NULL "
                        "(see docs/FEATURE-CONTRACTS.md).",
                "code": "XX000"})
        self._reply(status, payload)
