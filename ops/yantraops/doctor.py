"""``yantraops doctor`` — environment preflight with PASS/WARN/FAIL lines.

Every check is a small function with its dependencies injectable (importer,
socket allocator, HTTP getter, repo root), so the whole thing is
unit-testable offline with monkeypatched fakes.  Nothing third-party is
imported at module level — doctor must still run (and diagnose!) when httpx
or fastapi are missing.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# yantra package -> directory in the repo you `pip install -e` from
REPO_PACKAGES: dict[str, str] = {
    "yantracore": "core",
    "yantrasim": "sim",
    "yantrabridge": "connector",
    "yantradetect": "detector",
    "yantranotify": "notifier",
}

PYPI_PACKAGES = ("httpx", "fastapi", "uvicorn")


@dataclass
class Check:
    status: str          # PASS | WARN | FAIL
    name: str
    detail: str
    fix: str | None = None   # exact command that repairs a FAIL


def _find_spec(name: str) -> Any:
    """Default importer: importlib spec lookup (never raises for a plain name)."""
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


# --------------------------------------------------------------------------
# Individual checks (all dependencies injectable)
# --------------------------------------------------------------------------

def check_python(version: tuple[int, int, int] | None = None) -> Check:
    v = version or sys.version_info[:3]
    text = ".".join(str(p) for p in v[:3])
    if v >= (3, 10):
        return Check(PASS, "python", f"{text} (>= 3.10 required)")
    return Check(FAIL, "python", f"{text} is too old (>= 3.10 required)",
                 fix="install Python 3.10+ from https://www.python.org/downloads/")


def check_repo_package(
    name: str,
    importer: Callable[[str], Any] | None = None,
) -> Check:
    """One yantra* package importable from this interpreter."""
    spec = (importer or _find_spec)(name)
    subdir = REPO_PACKAGES.get(name, name)
    if spec is not None:
        return Check(PASS, name, "importable")
    return Check(FAIL, name, "not importable from this Python",
                 fix=f"pip install -e {subdir}/")


def check_pypi_package(
    name: str,
    importer: Callable[[str], Any] | None = None,
) -> Check:
    spec = (importer or _find_spec)(name)
    if spec is not None:
        return Check(PASS, name, "installed")
    return Check(FAIL, name, "not installed",
                 fix=f"pip install {name}")


def check_sarathi(root: Path) -> Check:
    """sarathi is loaded via path (copilot/ on PYTHONPATH), not pip-installed."""
    app = root / "copilot" / "sarathi" / "app.py"
    if app.is_file():
        return Check(PASS, "sarathi",
                     f"found at {app.parent} (loaded via path by `up`)")
    return Check(FAIL, "sarathi", f"copilot/sarathi/app.py not found under {root}",
                 fix="re-clone the repo, or set YANTRAFLEET_ROOT to the checkout")


def check_console(root: Path) -> Check:
    index = root / "console" / "index.html"
    if index.is_file():
        return Check(PASS, "console", f"{index} exists")
    return Check(FAIL, "console", f"{index} not found",
                 fix="re-clone the repo, or set YANTRAFLEET_ROOT to the checkout")


def check_supabase(
    env: Mapping[str, str] | None = None,
    http_get: Callable[[str, dict[str, str]], int] | None = None,
) -> Check:
    """Reachability of SUPABASE_URL, if set. WARN (not FAIL) when blocked:
    corporate proxies/firewalls commonly block it and loopback mode still works.
    """
    e = env if env is not None else os.environ
    url = (e.get("SUPABASE_URL") or "").rstrip("/")
    if not url:
        return Check(PASS, "supabase",
                     "SUPABASE_URL not set — fine, loopback mode needs no cloud")
    key = e.get("SUPABASE_KEY", "")
    getter = http_get or _default_http_get
    probe = f"{url}/rest/v1/"
    try:
        status = getter(probe, {"apikey": key, "Authorization": f"Bearer {key}"})
    except Exception as exc:
        return Check(WARN, "supabase",
                     f"{url} unreachable ({type(exc).__name__}) — network may be "
                     "blocked; `up --loopback` still works")
    if status < 500:
        return Check(PASS, "supabase", f"{url} reachable (HTTP {status})")
    return Check(WARN, "supabase", f"{url} answered HTTP {status}")


def _default_http_get(url: str, headers: dict[str, str]) -> int:
    import httpx  # lazy: doctor must run when httpx is missing
    with httpx.Client(timeout=3.0) as client:
        return client.get(url, headers=headers).status_code


def check_free_port(port_fn: Callable[[], int] | None = None) -> Check:
    if port_fn is None:
        from .orchestrator import free_port as port_fn  # stdlib only
    try:
        port = port_fn()
    except OSError as exc:
        return Check(FAIL, "free-port",
                     f"cannot bind an ephemeral 127.0.0.1 port ({exc})",
                     fix="check firewall/antivirus settings for 127.0.0.1 binds")
    return Check(PASS, "free-port", f"ephemeral port allocation works (got {port})")


# --------------------------------------------------------------------------
# Assembly / CLI
# --------------------------------------------------------------------------

def gather_checks(
    *,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
    importer: Callable[[str], Any] | None = None,
    port_fn: Callable[[], int] | None = None,
    http_get: Callable[[str, dict[str, str]], int] | None = None,
) -> list[Check]:
    if root is None:
        try:
            from .orchestrator import repo_root
            root = repo_root()
        except RuntimeError:
            root = Path.cwd()  # checks below will FAIL with a clear fix
    checks = [check_python()]
    for name in REPO_PACKAGES:
        checks.append(check_repo_package(name, importer))
    checks.append(check_sarathi(root))
    for name in PYPI_PACKAGES:
        checks.append(check_pypi_package(name, importer))
    checks.append(check_console(root))
    checks.append(check_supabase(env, http_get))
    checks.append(check_free_port(port_fn))
    return checks


def format_report(checks: list[Check]) -> tuple[str, int]:
    """Render the PASS/WARN/FAIL lines + one-line verdict; return (text, exit)."""
    lines = ["yantraops doctor — environment preflight", ""]
    for c in checks:
        lines.append(f"  [{c.status}] {c.name:<12} {c.detail}")
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    lines.append("")
    if not fails:
        verdict = "all checks passed" if not warns else \
            f"OK with {len(warns)} warning(s)"
        lines.append(f"verdict: {verdict} — next, run:  "
                     "python -m yantraops up --loopback")
        code = 0
    else:
        fixes = []
        for c in fails:
            if c.fix and c.fix not in fixes:
                fixes.append(c.fix)
        # collapse multiple `pip install X` fixes into one command line
        pips = [f[len("pip install "):] for f in fixes
                if f.startswith("pip install ")]
        other = [f for f in fixes if not f.startswith("pip install ")]
        next_cmds = ([f"pip install {' '.join(pips)}"] if pips else []) + other
        lines.append(f"verdict: {len(fails)} check(s) FAILED — fix with:")
        for cmd in next_cmds:
            lines.append(f"  {cmd}")
        code = 1
    return "\n".join(lines), code


def run_doctor(**kwargs: Any) -> int:
    """Entry point for ``python -m yantraops doctor``. Returns an exit code."""
    checks = gather_checks(**kwargs)
    text, code = format_report(checks)
    print(text)
    return code
