"""Live settings sync: poll ``public.app_settings``, env var as fallback.

Same shape as ``copilot/sarathi/settings_sync.py`` (deliberately
duplicated, not shared — see the design doc's rationale for why
``core/yantracore`` doesn't grow an HTTP client for two call sites),
minus the ``os.environ`` mirroring: nothing in notifier reads
``WEBHOOK_URL``/``YANTRA_WEBHOOK_SECRET``/``TWILIO_*`` from the
environment except our own explicit code (no third-party lib reads them
the way litellm reads ``GEMINI_API_KEY``).

Design principle: the env var stays the fallback default forever. A
deployment that never touches the admin settings panel — including a
project that hasn't even run ``supabase/0008_app_settings.sql`` —
behaves byte-for-byte like today: ``_overrides`` simply stays empty, and
every poll failure (network, 404, RLS, malformed payload) is caught and
logged at debug rather than raised.

No background thread here: notifier already runs its own poll loop in
``__main__.run()``, so ``maybe_poll()`` just self-throttles and is called
once per loop tick.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

log = logging.getLogger("yantranotify")

SETTINGS_KEYS = (
    "WEBHOOK_URL",
    "YANTRA_WEBHOOK_SECRET",
    "TWILIO_SID",
    "TWILIO_TOKEN",
    "TWILIO_FROM",
    "TWILIO_TO",
)
DEFAULT_POLL_INTERVAL_S = 30.0


class SystemClock:
    """Real time. Tests inject a fake with the same one method."""

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class SettingsSync:
    """Poll ``public.app_settings`` for a fixed set of keys.

    ``.get(key)`` is the read seam used everywhere else: table override
    (if any) else ``os.environ.get(key)`` else ``None``.
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        keys: Iterable[str] = SETTINGS_KEYS,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        client=None,
        clock=None,
    ) -> None:
        self._keys = tuple(keys)
        self._interval_s = interval_s
        self._clock = clock or SystemClock()
        self._overrides: dict[str, str] = {}
        self._last_poll: float | None = None
        # Full absolute URL per request (like AlertSource in source.py),
        # not a client-level base_url — keeps an injected test client
        # (built with no base_url, e.g. FakeRest.client()) working
        # identically to the real thing.
        self._rest = f"{supabase_url.rstrip('/')}/rest/v1"
        self._key = supabase_key
        self._own_client = client is None
        self._client = client or self._new_client()

    @staticmethod
    def _new_client():
        import httpx  # local import keeps offline installs light

        return httpx.Client(timeout=10.0)

    # -- reads -----------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._overrides.get(key) or os.environ.get(key) or None

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> None:
        """Unconditional fetch; never raises.

        Any failure — network, 404 because 0008 isn't applied yet,
        RLS/table missing, timeout, malformed payload — is caught and
        logged at debug; ``_overrides`` is left exactly as it was. This is
        exactly how "a deployment with nothing configured behaves like
        today" is satisfied.
        """
        self._last_poll = self._clock.monotonic()
        try:
            key_list = ",".join(self._keys)
            resp = self._client.get(
                f"{self._rest}/app_settings",
                params=[("select", "key,value"), ("key", f"in.({key_list})")],
                headers={
                    "apikey": self._key,
                    "Authorization": f"Bearer {self._key}",
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"app_settings poll: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            rows = resp.json()
            if not isinstance(rows, list):
                raise ValueError(f"unexpected app_settings payload: {rows!r}")
            overrides = {
                row["key"]: row["value"]
                for row in rows
                if isinstance(row, dict)
                and row.get("key") in self._keys
                and row.get("value")
            }
        except Exception as exc:  # network, 404, RLS, malformed — degrade quietly
            log.debug("settings poll failed (using env fallback): %s", exc)
            return
        self._overrides = overrides

    def maybe_poll(self) -> None:
        """Poll only if ``interval_s`` has elapsed since the last poll.

        Called once per notifier poll-loop tick — cheap to call every
        time; self-throttled so it doesn't add a request per tick.
        """
        now = self._clock.monotonic()
        if self._last_poll is None or (now - self._last_poll) >= self._interval_s:
            self.poll_once()

    def close(self) -> None:
        self._client.close()
