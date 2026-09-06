"""Live settings sync: poll ``public.app_settings``, env var as fallback.

Same shape as ``copilot/sarathi/settings_sync.py``, minus the
``os.environ`` mirroring: nothing in notifier reads
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

The actual HTTP fetch is delegated to
``yantracore.runtime_config.TablePoller`` — the same mechanism
``copilot/sarathi/settings_sync.py`` and every ``app_config`` (non-secret
runtime tunables) call site use. This class only adds what's specific to
*this* table: the fixed ``SETTINGS_KEYS`` tuple and the plain
``.get()``/``._overrides`` shape the rest of notifier already expects.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

from yantracore.runtime_config import SystemClock, TablePoller

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
        self._overrides: dict[str, str] = {}
        self._poller = TablePoller(
            supabase_url, supabase_key, table="app_settings",
            keys=self._keys, interval_s=interval_s, client=client,
            clock=clock or SystemClock(),
        )

    # -- reads -----------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._overrides.get(key) or os.environ.get(key) or None

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> None:
        """Unconditional fetch (delegated to the shared TablePoller);
        never raises. See ``TablePoller.poll_once`` for the degrade-
        quietly behaviour on any failure."""
        self._overrides = self._poller.poll_once()

    def maybe_poll(self) -> None:
        """Poll only if ``interval_s`` has elapsed since the last poll.

        Called once per notifier poll-loop tick — cheap to call every
        time; self-throttled so it doesn't add a request per tick.
        """
        self._overrides = self._poller.maybe_poll()

    def close(self) -> None:
        self._poller.close()
