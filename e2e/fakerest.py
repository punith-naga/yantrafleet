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
  ``save_progress`` and ``issue_certificate``, plus the 0008 admin-settings
  RPCs ``admin_list_settings``/``admin_set_setting`` (see below).

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

Everything stays on localhost sockets, so tests remain hermetic — no
Supabase, no DNS, no network egress.
"""
from __future__ import annotations

import base64
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

TABLES: tuple[str, ...] = (
    "robots", "alerts", "incidents", "commands", "fleet_meta", "robot_telemetry",
    "missions", "maintenance_findings",
)

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
        self.kind = kind              # 'service' | 'authenticated' | 'anon'
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.tables: dict[str, list[Row]] = {t: [] for t in TABLES}
        if tables:
            for name, rows in tables.items():
                seeded = [dict(r) for r in rows]
                if name in SITE_TABLES:
                    for r in seeded:  # column default (0005_sites.sql)
                        r.setdefault("site_id", DEFAULT_SITE_ID)
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
                n += 1
        return n

    # -- rbac-0007 emulation ----------------------------------------------

    def identity(self, headers: Any) -> _Identity:
        """Resolve the caller from Authorization/apikey headers.

        service_key (Bearer or apikey) -> service; a parsable ``yf-test.``
        Bearer token with role='authenticated' -> authenticated (yf_role
        kept only when it is a known role — unknown roles fail closed to
        'authenticated with no role'); everything else -> anon.
        """
        auth = headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
        apikey = (headers.get("apikey") or "").strip()
        if self.service_key and self.service_key in (bearer, apikey):
            return _Identity("service")
        claims = _decode_test_jwt(bearer)
        if claims and claims.get("role") == "authenticated":
            yf_role = claims.get("yf_role")
            return _Identity(
                "authenticated",
                sub=claims.get("sub"),
                email=claims.get("email"),
                yf_role=yf_role if yf_role in ROLE_RANK else None,
                site_id=claims.get("site_id") or DEFAULT_SITE_ID,
            )
        return _Identity("anon")  # incl. anon_key and any garbage credential

    def row_visible(self, table: str, row: Row, ident: _Identity) -> bool:
        """rbac_read stand-in: role at the row's site, admin = every site."""
        if ident.kind == "service":
            return True
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

    RPCS: dict[str, str] = {
        "decide_command": "rpc_decide_command",
        "save_progress": "rpc_save_progress",
        "issue_certificate": "rpc_issue_certificate",
        "admin_list_settings": "rpc_admin_list_settings",
        "admin_set_setting": "rpc_admin_set_setting",
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
        every other table has no insert policy at all.
        """
        what = f"insert into {table}"
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
        with self.fake.lock:
            self.fake.requests.append(("POST", self.path))
            status, payload = getattr(self.fake, method)(ident, args)
        self._reply(status, payload)
