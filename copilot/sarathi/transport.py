"""HTTP transport layer for PostgREST reads.

The tools never talk to httpx directly — they go through a small ``Transport``
interface so the backend is swappable:

* ``SupabaseTransport`` — real httpx client against ``{SUPABASE_URL}/rest/v1``.
* ``StaticTransport``   — in-memory table snapshot that interprets a useful
  subset of PostgREST query params (eq/neq/lt/lte/gt/gte/is, order, limit).
  Used by the offline test suite and handy for demos without network.

Params are passed as a list of ``(key, value)`` tuples because PostgREST
allows repeating a column key (e.g. two range filters on ``battery``).
"""
from __future__ import annotations

import abc
from typing import Any

Params = list[tuple[str, str]]


class TransportError(RuntimeError):
    """Raised when the data backend is unreachable or returns an error."""


class Transport(abc.ABC):
    """Read-only access to PostgREST-style tables."""

    @abc.abstractmethod
    def get(self, table: str, params: Params) -> list[dict[str, Any]]:
        """Return rows of ``table`` filtered by PostgREST query params."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial default
        """Release any underlying resources."""


class SupabaseTransport(Transport):
    """Real transport: GET {url}/rest/v1/{table} with the anon key."""

    def __init__(self, url: str, key: str, timeout_s: float = 10.0) -> None:
        import httpx  # local import keeps offline installs light

        self._client = httpx.Client(
            base_url=f"{url.rstrip('/')}/rest/v1",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=timeout_s,
        )

    def get(self, table: str, params: Params) -> list[dict[str, Any]]:
        import httpx

        try:
            resp = self._client.get(f"/{table}", params=params)
        except httpx.HTTPError as exc:  # DNS, timeout, refused, TLS, ...
            raise TransportError(f"supabase unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"supabase error {resp.status_code} on {table}: {resp.text[:200]}"
            )
        data = resp.json()
        if not isinstance(data, list):  # PostgREST returns a JSON array
            raise TransportError(f"unexpected payload for {table}")
        return data

    def probe(self, timeout_s: float = 3.0) -> None:
        """Cheap reachability check: fleet_meta, one id, short timeout.

        Raises TransportError when the backend is unreachable or errors.
        Used by the /health endpoint so a hung backend can't stall it for
        the full data-path timeout.
        """
        import httpx

        try:
            resp = self._client.get(
                "/fleet_meta",
                params=[("select", "id"), ("limit", "1")],
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"supabase unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"supabase error {resp.status_code} on fleet_meta probe"
            )

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# In-memory transport (tests / offline demos)
# --------------------------------------------------------------------------

_OPS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "lt": lambda a, b: a is not None and a < b,
    "lte": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "gte": lambda a, b: a is not None and a >= b,
}


def _coerce(raw: str, sample: Any) -> Any:
    """Coerce a filter's string value to the type of the column sample."""
    if isinstance(sample, bool):
        return raw.lower() in ("true", "t", "1")
    if isinstance(sample, (int, float)) and not isinstance(sample, bool):
        try:
            return type(sample)(float(raw)) if isinstance(sample, int) else float(raw)
        except ValueError:
            return raw
    return raw


class StaticTransport(Transport):
    """Serve fixed table snapshots, honouring basic PostgREST params."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def get(self, table: str, params: Params) -> list[dict[str, Any]]:
        if table not in self._tables:
            raise TransportError(f"unknown table: {table}")
        rows = [dict(r) for r in self._tables[table]]
        order: str | None = None
        limit: int | None = None
        for key, value in params:
            if key == "select":
                continue  # always serve full rows; harmless for our tools
            if key == "order":
                order = value
                continue
            if key == "limit":
                limit = int(value)
                continue
            # Column filter: "op.value", e.g. status=eq.fault, battery=lt.20
            op, _, raw = value.partition(".")
            if op == "is":
                want_null = raw == "null"
                rows = [r for r in rows if (r.get(key) is None) == want_null]
                continue
            fn = _OPS.get(op)
            if fn is None:
                raise TransportError(f"unsupported filter op: {value}")
            sample = next((r[key] for r in rows if r.get(key) is not None), "")
            typed = _coerce(raw, sample)
            rows = [r for r in rows if fn(r.get(key), typed)]
        if order:
            col, _, direction = order.partition(".")
            rows.sort(
                key=lambda r: (r.get(col) is None, r.get(col)),
                reverse=(direction == "desc"),
            )
        if limit is not None:
            rows = rows[:limit]
        return rows


class FailingTransport(Transport):
    """Always raises — simulates Supabase being down (for tests)."""

    def get(self, table: str, params: Params) -> list[dict[str, Any]]:
        raise TransportError("backend down (simulated)")
