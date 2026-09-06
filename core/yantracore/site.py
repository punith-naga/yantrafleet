"""Site identity for multi-site deployments (v0.5.x groundwork).

Every Yantrika writer stamps its rows with a ``site_id`` so one
Supabase project can host several facilities. The id comes from (in
order): a live override synced from ``public.app_config`` (v0.18, see
:class:`SiteSync`/:func:`start_site_sync`), the ``YANTRA_SITE_ID``
environment variable, then ``BLR-DC1`` (the original deployment) —
matching the column default added in ``supabase/0005_sites.sql`` — so a
process that never heard of sites or the config panel still produces
rows attributed to BLR-DC1.

Readers must treat ``site_id`` as optional on rows they consume
(``row.get("site_id")``, never ``row["site_id"]``): rows written by
pre-0005 components, or fakes without the column, have no value.

Live sync (:class:`SiteSync`) is opt-in: a process that never starts one
(or starts one against a backend that has no ``app_config`` table yet —
e.g. loopback/demo mode, or a project that hasn't run
``supabase/0018_app_config.sql``) behaves exactly as before this module
gained the sync — ``site_id()`` degrades to the env-var-or-default
behaviour, byte for byte.

Design note (v0.18.1): this used to be a single bare module-level
poller. Wiring :func:`start_site_sync` into detector/sim/notifier's own
startup surfaced a real test-isolation bug: those services' test suites
call ``run()``/``main()`` many times per process, each against its own
fake backend, and a bare "first call wins, later calls are a no-op"
singleton would leak the FIRST test's site id into every later test in
the same pytest session. :class:`SiteSync` is now a plain object a
caller (a service's entrypoint, or a test) owns outright — independent
of any other instance — and ``start_site_sync``/``stop_site_sync``/
``site_id`` are a thin convenience that track whichever ``SiteSync`` is
currently "active" for this process (exactly one at a time, matching how
exactly one process runs each service in production). Calling
``.start()`` on a fresh instance always takes over as the active one
(cleanly stopping whatever was active before), so a test can create,
use, and tear down its own instance with no risk of inheriting another
test's state.
"""
from __future__ import annotations

import os
import threading

__all__ = [
    "DEFAULT_SITE_ID",
    "SITE_ID",
    "SiteSync",
    "site_id",
    "start_site_sync",
    "stop_site_sync",
]

DEFAULT_SITE_ID = "BLR-DC1"

#: The SiteSync instance (if any) currently backing site_id()'s live
#: override — set/cleared by SiteSync.start()/.stop(), see module
#: docstring. Exactly one at a time; None means "no live override".
_active: "SiteSync | None" = None
_lock = threading.RLock()


class SiteSync:
    """One live ``YANTRA_SITE_ID`` poller, owned by whoever constructs it.

    Each instance wraps its own ``yantracore.runtime_config.TablePoller``
    with its own backend URL/key/client — nothing here is shared between
    instances. :meth:`start` polls once, starts a background thread, and
    becomes the process's active instance (see module-level
    :func:`site_id`); :meth:`stop` tears the thread/client down and, if
    this instance was active, clears the override. Safe to construct,
    start, and stop as many independent instances as needed — e.g. one
    per test — with no cross-contamination between them.
    """

    def __init__(self, supabase_url: str, supabase_key: str, *,
                 interval_s: float = 30.0, client=None) -> None:
        from .runtime_config import TablePoller

        self._poller = TablePoller(
            supabase_url, supabase_key, table="app_config",
            keys=("YANTRA_SITE_ID",), interval_s=interval_s, client=client,
        )

    def site_id(self) -> str:
        """This instance's resolved site id: its own live poll (if any)
        > env ``YANTRA_SITE_ID`` > ``BLR-DC1``."""
        override = self._poller.get("YANTRA_SITE_ID")
        if override and override.strip():
            return override.strip()
        return os.environ.get("YANTRA_SITE_ID", "").strip() or DEFAULT_SITE_ID

    def start(self) -> "SiteSync":
        """Poll once, start the background thread, and become the
        process's active instance — replacing (and stopping) whatever
        was active before, if anything. Returns ``self``."""
        global _active
        self._poller.start()
        with _lock:
            previous = _active if _active is not self else None
            _active = self
        if previous is not None:
            previous.stop()
        return self

    def stop(self) -> None:
        """Stop this instance's poller and, if it was the active one,
        clear the process's override (``site_id()`` falls back to
        env/default). Safe to call on an instance that was never
        started, or already stopped."""
        global _active
        self._poller.stop()
        with _lock:
            if _active is self:
                _active = None


def site_id() -> str:
    """This process's site id.

    Precedence: the active :class:`SiteSync`'s live ``app_config``
    override (if :func:`start_site_sync` — or a service's own
    ``SiteSync`` — has been started and has a value) > env
    ``YANTRA_SITE_ID`` > ``BLR-DC1``. Reads live state on every call
    (cheap, and lets tests swap the active instance without reimporting
    the module). Blank or whitespace-only values fall back to the next
    source in the precedence.
    """
    with _lock:
        active = _active
    if active is not None:
        return active.site_id()
    return os.environ.get("YANTRA_SITE_ID", "").strip() or DEFAULT_SITE_ID


def start_site_sync(supabase_url: str, supabase_key: str, *,
                     interval_s: float = 30.0, client=None) -> SiteSync:
    """Start (idempotently) a background poll of ``app_config`` for
    ``YANTRA_SITE_ID``, so :func:`site_id` picks up an admin-console edit
    within ``interval_s`` seconds, no restart needed.

    Idempotent for repeat calls while an instance is already active — the
    existing active :class:`SiteSync` is returned unchanged (this is
    what earlier versions of this function did with its bare module
    poller, and callers/tests still rely on that). To force a fresh
    instance (e.g. a new fake backend in a test), call
    :func:`stop_site_sync` first, or construct and ``.start()`` a
    :class:`SiteSync` directly — its ``.start()`` always takes over.

    Safe to call against a backend with no ``app_config`` table (poll
    failures degrade quietly — see
    ``yantracore.runtime_config.TablePoller``). Each service process
    that consumes :func:`site_id` and wants it live (detector, notifier,
    sim) calls this once at startup with its own resolved Supabase
    URL/key; there is no cross-process shared state, matching how
    ``SettingsSync`` already works (each process polls independently).
    """
    with _lock:
        if _active is not None:
            return _active
    return SiteSync(supabase_url, supabase_key, interval_s=interval_s,
                     client=client).start()


def stop_site_sync() -> None:
    """Stop the process's active :class:`SiteSync`, if any. Mainly for
    tests that need a clean slate between cases."""
    with _lock:
        instance = _active
    if instance is not None:
        instance.stop()


#: Convenience snapshot taken at import time. Prefer :func:`site_id` in
#: code paths that must honor a late environment/config change.
SITE_ID: str = site_id()
