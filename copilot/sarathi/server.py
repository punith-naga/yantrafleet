"""Uvicorn entry point alias.

Run:  py -m uvicorn sarathi.server:app --port 8001   (Windows)
      uvicorn sarathi.server:app --port 8001         (POSIX)

``sarathi.app`` owns the app factory; this module only re-exports the
default instance so both module paths work.
"""
from __future__ import annotations

from .app import app, create_app

__all__ = ["app", "create_app"]
