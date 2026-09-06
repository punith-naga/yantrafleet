"""Live settings sync: poll ``public.app_settings``, env var as fallback.

Design principle: the env var stays the fallback default forever. A
deployment that never touches the admin settings panel — including
every existing test — behaves byte-for-byte like today: ``_overrides``
simply stays empty, including against a project that has not even run
``supabase/0008_app_settings.sql`` (the poll fails, is logged at debug,
and ``_overrides`` is left exactly as it was).

Construction never touches the network — it only stores config and, when
no client is injected, builds one via ``yantracore.runtime_config.TablePoller``
(constructing a client performs no I/O). Only ``.start()`` spawns the
background thread and does the first blocking fetch. This is what lets
every existing copilot test keep constructing apps/services without a
``settings_sync`` at all: :func:`sarathi.app.create_app` gives them a
real-but-never-started ``SettingsSync`` whose ``.get(key)`` degrades to a
plain ``os.environ.get(key)`` forever.

``os.environ`` mirroring (``GEMINI_API_KEY``-specific reason): ``litellm``
reads its provider API key straight out of ``os.environ`` inside
``litellm.completion(...)`` — ``sarathi/llm.py`` never passes ``api_key=``
explicitly. So detecting "a key is configured" in our own code is not
enough; the live value has to actually land in
``os.environ["GEMINI_API_KEY"]`` for the LLM tier to authenticate. Each
poll recomputes every tracked env var from an immutable snapshot taken at
construction time (never incrementally overwritten), so clearing a panel
value correctly restores the original deploy-time env var instead of
leaving a stale override behind.

The actual HTTP fetch (build the PostgREST querystring, parse the JSON,
degrade quietly on any failure) is NOT reimplemented here — it is
``yantracore.runtime_config.TablePoller``, the same mechanism
``notifier/yantranotify/settings_sync.py`` and every ``app_config``
(non-secret runtime tunables) call site use. This class only adds what's
specific to *this* table: the fixed ``SETTINGS_KEYS`` tuple, the
``os.environ`` mirroring, and its own dedicated background thread (kept
as its own attribute, not delegated, so ``.start()``/``.stop()`` can run
the mirroring step after every poll — see ``poll_once()`` below).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable

from yantracore.runtime_config import TablePoller

log = logging.getLogger("sarathi")

SETTINGS_KEYS = ("GEMINI_API_KEY", "SARATHI_TOKEN")
DEFAULT_POLL_INTERVAL_S = 30.0


class SettingsSync:
    """Poll ``public.app_settings`` for a fixed set of keys.

    ``.get(key)`` is the read seam used everywhere else in the service:
    table override (if any) else ``os.environ.get(key)`` else ``None``.
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        keys: Iterable[str] = SETTINGS_KEYS,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        client=None,
        mirror_to_environ: bool = True,
    ) -> None:
        self._keys = tuple(keys)
        self._interval_s = interval_s
        self._mirror_to_environ = mirror_to_environ
        # Immutable snapshot of the bootstrap env, taken once. See module
        # docstring — polls recompute os.environ from this every time,
        # they never mutate it incrementally.
        self._env_snapshot: dict[str, str | None] = {
            k: os.environ.get(k) for k in self._keys
        }
        self._overrides: dict[str, str] = {}
        self._poller = TablePoller(
            supabase_url, supabase_key, table="app_settings",
            keys=self._keys, interval_s=interval_s, client=client,
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._overrides.get(key) or os.environ.get(key) or None

    # -- polling ---------------------------------------------------------------

    def poll_once(self) -> None:
        """Unconditional fetch (delegated to the shared TablePoller);
        never raises. See ``TablePoller.poll_once`` for the degrade-
        quietly behaviour on any failure."""
        self._overrides = self._poller.poll_once()
        if self._mirror_to_environ:
            self._sync_environ()

    def _sync_environ(self) -> None:
        """Recompute every tracked ``os.environ[k]`` from the immutable
        snapshot, never incrementally — see module docstring."""
        for k in self._keys:
            value = self._overrides.get(k) or self._env_snapshot.get(k)
            if value:
                os.environ[k] = value
            else:
                os.environ.pop(k, None)

    # -- lifecycle ---------------------------------------------------------------

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
            target=_loop, name="sarathi-settings-sync", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 1.0)
            self._thread = None
        self._poller.close()
