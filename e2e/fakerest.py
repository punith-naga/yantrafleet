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

Everything stays on localhost sockets, so tests remain hermetic — no
Supabase, no DNS, no network egress.
"""
from __future__ import annotations

import json
import threading
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

    def __init__(self, tables: dict[str, list[Row]] | None = None) -> None:
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

    def patch(self, table: str, params: list[tuple[str, str]], body: Row) -> int:
        n = 0
        for row in self.tables[table]:
            if all(_matches(row, k, v) for k, v in params
                   if k not in _RESERVED and k not in ("order", "limit")):
                row.update(body)
                n += 1
        return n


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
            rows = _apply_query(self.fake.tables[table], params)
        self._reply(200, rows)

    def do_POST(self) -> None:  # noqa: N802
        table, params = self._route()
        if table is None:
            return self._reply(404, {"message": f"unknown route {self.path}"})
        body = self._body()
        rows = body if isinstance(body, list) else [body or {}]
        on_conflict = next((v for k, v in params if k == "on_conflict"), None)
        with self.fake.lock:
            self.fake.requests.append(("POST", self.path))
            self.fake.insert(table, rows, on_conflict, self._prefer_resolution())
        self._reply(201)

    def do_PATCH(self) -> None:  # noqa: N802
        table, params = self._route()
        if table is None:
            return self._reply(404, {"message": f"unknown route {self.path}"})
        body = self._body() or {}
        with self.fake.lock:
            self.fake.requests.append(("PATCH", self.path))
            self.fake.patch(table, params, body)
        self._reply(204)
