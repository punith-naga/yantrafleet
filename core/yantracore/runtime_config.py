"""Shared runtime-config poller: poll a PostgREST table on an interval.

Extracted from ``copilot/sarathi/settings_sync.py`` and
``notifier/yantranotify/settings_sync.py``, which started life as two
near-identical ``SettingsSync`` classes polling ``public.app_settings``
(see their module docstrings for the original "deliberately duplicated,
not shared" rationale). Once a THIRD table (``public.app_config``, the
non-secret runtime tunables — see ``supabase/0018_app_config.sql``)
needed the exact same poll-a-table-of-key/value-rows mechanism, across
FIVE more call sites (site id, sarathi's battery threshold, detector,
notifier, sim, connector), duplicating it again stopped making sense —
hence this module.

:class:`TablePoller` is the mechanical core only: it fetches
``select=key,value&key=in.(...)`` from one table, keeps the latest
snapshot as a plain ``dict[str, str]``, and exposes it via ``.get()``.
It does **not** decide precedence against a CLI flag or env var, does
**not** mirror anything into ``os.environ``, and does **not** know what
"changed" should trigger (a reconnect, a re-read, nothing) — those are
all caller decisions. ``copilot/sarathi/settings_sync.SettingsSync`` and
``notifier/yantranotify/settings_sync.SettingsSync`` now compose one of
these internally for the actual HTTP fetch, while keeping their own
public API (``.get()``, ``._overrides``, os.environ mirroring, thread
lifecycle) unchanged for backwards compatibility with existing callers
and tests. Every other new call site (site id, app_config-backed
tunables) uses :class:`TablePoller` directly.

Design principle carried over from both original modules: any failure —
network, 404 because the migration hasn't been applied yet, RLS,
timeout, malformed payload — is caught and logged at debug; the poller
degrades quietly to "keep whatever we last had" rather than raising.
This is what lets a deployment that has never run the relevant
migration (or has no real Supabase at all, e.g. loopback/demo mode)
behave exactly as if this module didn't exist.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable

log = logging.getLogger("yantracore.runtime_config")


class SystemClock:
    """Real wall-clock time. Tests inject a fake with the same one method."""

    def monotonic(self) -> float:
        import time

        return time.monotonic()


def coerce_float(raw: str | None, default: float) -> float:
    """Best-effort ``float(raw)``; ``None``/blank/unparseable -> ``default``."""
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def coerce_int(raw: str | None, default: int) -> int:
    """Best-effort ``int(float(raw))``; ``None``/blank/unparseable -> ``default``.

    Parses through ``float`` first so a value like ``"2.0"`` (easy to type
    by mistake in a UI meant for a whole number) still resolves instead of
    silently falling back to ``default``.
    """
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def resolve_value(explicit, poller: "TablePoller | None", key: str, default,
                  coerce=None):
    """Effective config value, in the precedence every wired service uses:

        explicit CLI flag/argument (if not ``None``)
        > live value from ``poller`` for ``key`` (if present and, when
          ``coerce`` is given, parseable)
        > ``default`` (the service's own hardcoded fallback)

    ``coerce`` is one of :func:`coerce_float`/:func:`coerce_int` for
    numeric settings, or ``None`` to keep the raw string as-is (site id,
    mqtt host/topic/user/pass).
    """
    if explicit is not None:
        return explicit
    if poller is not None:
        raw = poller.get(key)
        if raw is not None and raw != "":
            return coerce(raw, default) if coerce is not None else raw
    return default


class TablePoller:
    """Poll one PostgREST table for a fixed set of keys on an interval.

    ``poll_once()``/``maybe_poll()`` let a caller embed polling in its own
    loop (mirrors ``notifier``'s pre-existing self-throttle pattern);
    ``start()``/``stop()`` run it on a dedicated background thread instead
    (mirrors ``sarathi``'s pre-existing pattern). Both styles are exposed
    because different callers already had different lifecycles — nothing
    stops a caller from using ``start()``/``stop()`` even though the
    thread body just calls the same ``poll_once()``.
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        table: str,
        keys: Iterable[str],
        interval_s: float = 30.0,
        client=None,
        clock=None,
    ) -> None:
        self._keys = tuple(keys)
        self.table = table
        self._interval_s = interval_s
        self._clock = clock or SystemClock()
        self._values: dict[str, str] = {}
        self._last_poll: float | None = None
        # Full absolute URL per request (not a client-level base_url) so an
        # injected test client built with no base_url (httpx.MockTransport,
        # a FakeRest double, ...) works identically to the real thing.
        self._rest = f"{supabase_url.rstrip('/')}/rest/v1"
        self._key = supabase_key
        self._own_client = client is None
        self._client = client or self._new_client()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @staticmethod
    def _new_client():
        import httpx  # local import keeps offline installs light

        return httpx.Client(timeout=10.0)

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def snapshot(self) -> dict[str, str]:
        """A copy of every currently-known key/value pair."""
        return dict(self._values)

    # -- polling ---------------------------------------------------------------

    def poll_once(self) -> dict[str, str]:
        """Unconditional fetch; never raises.

        Any failure — network, 404 because the migration isn't applied
        yet, RLS/table missing, timeout, malformed payload — is caught
        and logged at debug; the previous snapshot is kept exactly as it
        was. Returns the (possibly unchanged) snapshot after the attempt.
        """
        self._last_poll = self._clock.monotonic()
        try:
            key_list = ",".join(self._keys)
            resp = self._client.get(
                f"{self._rest}/{self.table}",
                params=[("select", "key,value"), ("key", f"in.({key_list})")],
                headers={"apikey": self._key, "Authorization": f"Bearer {self._key}"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"{self.table} poll: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            rows = resp.json()
            if not isinstance(rows, list):
                raise ValueError(f"unexpected {self.table} payload: {rows!r}")
            values = {
                row["key"]: row["value"]
                for row in rows
                if isinstance(row, dict)
                and row.get("key") in self._keys
                and row.get("value") not in (None, "")
            }
        except Exception as exc:  # network, 404, RLS, malformed — degrade quietly
            log.debug("%s poll failed (keeping previous values): %s", self.table, exc)
            return dict(self._values)
        self._values = values
        return dict(self._values)

    def maybe_poll(self) -> dict[str, str]:
        """Poll only if ``interval_s`` has elapsed since the last poll.

        Cheap to call every tick of an existing loop; self-throttled so
        it doesn't add a request per tick (notifier's pattern).
        """
        now = self._clock.monotonic()
        if self._last_poll is None or (now - self._last_poll) >= self._interval_s:
            return self.poll_once()
        return dict(self._values)

    # -- lifecycle: optional dedicated background thread ----------------------

    def start(self) -> None:
        """One blocking ``poll_once()``, then a daemon thread every
        ``interval_s``. Safe to call more than once (no-op after the
        first)."""
        self.poll_once()
        if self._thread is not None:
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(self._interval_s):
                self.poll_once()

        self._thread = threading.Thread(
            target=_loop, name=f"{self.table}-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 1.0)
            self._thread = None
        if self._own_client:
            self._client.close()

    def close(self) -> None:
        """Alias for callers (like notifier) that never call ``start()``
        and so have no thread to join."""
        if self._own_client:
            self._client.close()
