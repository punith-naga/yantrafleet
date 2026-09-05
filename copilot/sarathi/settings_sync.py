"""Live settings sync: poll ``public.app_settings``, env var as fallback.

Design principle: the env var stays the fallback default forever. A
deployment that never touches the admin settings panel — including every
existing test — behaves byte-for-byte like today: ``_overrides`` simply
stays empty, including against a project that has not even run
``supabase/0008_app_settings.sql`` (the poll fails, is logged at debug,
and ``_overrides`` is left exactly as it was).

Construction never touches the network — it only stores config and builds
an inert ``httpx.Client`` (constructing a client performs no I/O). Only
``.start()`` spawns the background thread and does the first blocking
fetch. This is what lets every existing copilot test keep constructing
apps/services without a ``settings_sync`` at all: :func:`sarathi.app.create_app`
gives them a real-but-never-started ``SettingsSync`` whose ``.get(key)``
degrades to a plain ``os.environ.get(key)`` forever.

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
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable

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
        self._own_client = client is None
        if client is not None:
            self._client = client
        else:
            import httpx  # local import keeps offline installs light

            self._client = httpx.Client(
                base_url=f"{supabase_url.rstrip('/')}/rest/v1",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._overrides.get(key) or os.environ.get(key) or None

    # -- polling ---------------------------------------------------------------

    def poll_once(self) -> None:
        """Unconditional fetch; never raises.

        Any failure — network, 404 because 0008 isn't applied yet,
        RLS/table missing, timeout, malformed payload — is caught and
        logged at debug; ``_overrides`` is left exactly as it was. This is
        exactly how "a deployment with nothing configured behaves like
        today" is satisfied.
        """
        try:
            key_list = ",".join(self._keys)
            resp = self._client.get(
                "/app_settings",
                params=[("select", "key,value"), ("key", f"in.({key_list})")],
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
        if self._own_client:
            self._client.close()
