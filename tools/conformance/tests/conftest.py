"""Make the package importable straight from a checkout.

``yantraconform`` is normally pip-installed (``pip install -e tools/conformance``),
but the suite must also run against a bare clone, so the package root goes on
``sys.path`` when the import fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
