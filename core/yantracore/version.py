"""Single source of the Yantrika RELEASE version.

This is the fleet-wide release number (the git tag / CHANGELOG version),
distinct from the per-package ``pyproject.toml`` versions. Release commits
bump it here; the console and academy each carry a hardcoded ``YF_VERSION``
constant at the top of their script that must be kept in sync with this
value (documented at each constant).
"""
__version__ = "0.12.1"
