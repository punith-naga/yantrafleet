"""Site identity for multi-site deployments (v0.5.x groundwork).

Every YantraFleet writer stamps its rows with a ``site_id`` so one
Supabase project can host several facilities. The id comes from the
``YANTRA_SITE_ID`` environment variable and falls back to ``BLR-DC1``
(the original deployment), matching the column default added in
``supabase/0005_sites.sql`` — so a process that never heard of sites
still produces rows attributed to BLR-DC1.

Readers must treat ``site_id`` as optional on rows they consume
(``row.get("site_id")``, never ``row["site_id"]``): rows written by
pre-0005 components, or fakes without the column, have no value.
"""
from __future__ import annotations

import os

__all__ = ["DEFAULT_SITE_ID", "SITE_ID", "site_id"]

DEFAULT_SITE_ID = "BLR-DC1"


def site_id() -> str:
    """This process's site id: env ``YANTRA_SITE_ID`` or ``BLR-DC1``.

    Reads the environment on every call (cheap, and lets tests
    monkeypatch the variable without reimporting the module). Blank or
    whitespace-only values fall back to the default.
    """
    return os.environ.get("YANTRA_SITE_ID", "").strip() or DEFAULT_SITE_ID


#: Convenience snapshot taken at import time. Prefer :func:`site_id` in
#: code paths that must honor a late environment change.
SITE_ID: str = site_id()
