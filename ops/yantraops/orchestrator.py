"""FleetStack: start/stop the whole YantraFleet demo as child processes.

Loopback mode (the default) starts the in-process fake PostgREST from
``e2e/fakerest.py`` first, then points every child at it via the
``SUPABASE_URL`` / ``SUPABASE_KEY`` environment (and the sim's ``--url`` /
``--key`` CLI flags), so the full demo runs with zero cloud dependencies.

Children (start order; shutdown is the reverse):

1. ``yantrasim --supabase``      — the 10-AMR fleet simulator
2. ``yantradetect --interval N`` — incident detector
3. ``yantranotify --dry-run``    — notifier (prints instead of sending)
4. ``uvicorn sarathi.app:app``   — the copilot API (skipped by --no-copilot)
5. ``http.server``               — static console on an ephemeral port

Every port is ephemeral; the startup banner prints the console URL with
``?supa=...&key=...`` query params so the browser talks to the same backend.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_STATE_FILE = Path(os.environ.get(
    "YANTRAOPS_STATE", "~/.yantraops-state.json")).expanduser()

LOOPBACK_KEY = "yantraops-loopback-key"  # fakerest ignores auth entirely

# Wall-clock seconds to wait for a child after terminate() before kill().
TERM_GRACE_S = 8.0


# --------------------------------------------------------------------------
# Repo / fakerest discovery
# --------------------------------------------------------------------------

def repo_root() -> Path:
    """Locate the yantrafleet checkout (env YANTRAFLEET_ROOT, or walk up)."""
    env = os.environ.get("YANTRAFLEET_ROOT")
    candidates = [Path(env)] if env else []
    candidates += list(Path(__file__).resolve().parents)
    for cand in candidates:
        if (cand / "e2e" / "fakerest.py").is_file() and (cand / "console").is_dir():
            return cand
    raise RuntimeError(
        "cannot find the yantrafleet repo (e2e/fakerest.py + console/); "
        "set YANTRAFLEET_ROOT to the checkout directory")


def load_fakerest(root: Path) -> Any:
    """Import e2e/fakerest.py by path and return the module."""
    path = root / "e2e" / "fakerest.py"
    spec = importlib.util.spec_from_file_location("yantraops_fakerest", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port() -> int:
    """Ask the OS for an ephemeral 127.0.0.1 port and release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def resolve_supabase(url: str | None, key: str | None) -> tuple[str, str]:
    """Cloud-mode config: flag > env > yantrasim's embedded default."""
    try:
        from yantrasim.transports.supabase import resolve_config
        return resolve_config(url, key)
    except ImportError:  # pragma: no cover - yantrasim is a hard dep in practice
        resolved_url = url or os.environ.get("SUPABASE_URL")
        resolved_key = key or os.environ.get("SUPABASE_KEY")
        if not (resolved_url and resolved_key):
            raise RuntimeError(
                "--supabase mode needs --url/--key or SUPABASE_URL/SUPABASE_KEY")
        return resolved_url.rstrip("/"), resolved_key


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------

@dataclass
class Service:
    """One managed child process."""

    name: str
    proc: subprocess.Popen
    port: int | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pid": self.proc.pid,
                "port": self.port, "url": self.url}


@dataclass
class StackInfo:
    """What ``FleetStack.start()`` hands back (and writes to the state file)."""

    mode: str
    base_url: str
    key: str
    console_url: str
    copilot_url: str | None
    services: list[dict[str, Any]] = field(default_factory=list)


class FleetStack:
    """Owns the fake backend (loopback) and all child processes."""

    def __init__(
        self,
        loopback: bool = True,
        copilot: bool = True,
        url: str | None = None,
        key: str | None = None,
        sim_interval: float = 2.0,
        detect_interval: float = 5.0,
        notify_interval: float = 10.0,
        state_file: Path | str | None = None,
        quiet: bool = False,
        verbose: bool = False,
        open_browser: bool = True,
    ) -> None:
        self.loopback = loopback
        self.copilot = copilot
        self.url = url
        self.key = key
        self.sim_interval = sim_interval
        self.detect_interval = detect_interval
        self.notify_interval = notify_interval
        self.state_file = Path(state_file) if state_file else DEFAULT_STATE_FILE
        self.quiet = quiet
        self.verbose = verbose
        self.open_browser = open_browser

        self.root = repo_root()
        self.fake: Any = None            # FakePostgREST instance in loopback mode
        self.services: list[Service] = []
        self.info: StackInfo | None = None
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> StackInfo:
        """Start the backend first, then every child; return the ports/URLs."""
        if self.loopback:
            fakerest = load_fakerest(self.root)
            self.fake = fakerest.FakePostgREST()
            base_url = self.fake.start()      # binds 127.0.0.1 ephemeral port
            key = LOOPBACK_KEY
        else:
            base_url, key = resolve_supabase(self.url, self.key)

        env = dict(os.environ)
        env["SUPABASE_URL"] = base_url
        env["SUPABASE_KEY"] = key
        # v0.6.1: keep child logs calm so the banner/READY line stays visible;
        # `up --verbose` restores full INFO streams.
        env.setdefault("YANTRA_LOG_LEVEL", "DEBUG" if self.verbose else "WARNING")

        out = subprocess.DEVNULL if self.quiet else None
        py = sys.executable

        def spawn(name: str, cmd: list[str], *, cwd: Path | None = None,
                  extra_env: dict[str, str] | None = None,
                  port: int | None = None, url: str | None = None) -> Service:
            child_env = dict(env, **(extra_env or {}))
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=child_env, stdout=out, stderr=out)
            svc = Service(name=name, proc=proc, port=port, url=url)
            self.services.append(svc)
            return svc

        try:
            spawn("yantrasim", [
                py, "-m", "yantrasim", "--supabase",
                "--url", base_url, "--key", key,
                "--interval", str(self.sim_interval),
            ])
            spawn("yantradetect", [
                py, "-m", "yantradetect", "--interval", str(self.detect_interval),
            ])
            spawn("yantranotify", [
                py, "-m", "yantranotify", "--dry-run",
                "--interval", str(self.notify_interval),
            ])

            copilot_url: str | None = None
            if self.copilot:
                port = free_port()
                copilot_url = f"http://127.0.0.1:{port}"
                copilot_dir = self.root / "copilot"
                spawn("sarathi", [
                    py, "-m", "uvicorn", "sarathi.app:app",
                    "--host", "127.0.0.1", "--port", str(port),
                    "--log-level", "warning",
                ], cwd=copilot_dir,
                   extra_env={"PYTHONPATH": str(copilot_dir)},
                   port=port, url=copilot_url)

            console_port = free_port()
            console_url = (
                f"http://127.0.0.1:{console_port}/index.html"
                f"?supa={quote(base_url, safe='')}&key={quote(key, safe='')}"
            )
            spawn("console", [
                py, "-m", "http.server", str(console_port),
                "--bind", "127.0.0.1",
                "--directory", str(self.root / "console"),
            ], port=console_port, url=f"http://127.0.0.1:{console_port}/")
        except Exception:
            self.stop()
            raise

        if self.fake is not None:
            self.fake.console_url = console_url  # browser-mistake redirect
        self.info = StackInfo(
            mode="loopback" if self.loopback else "supabase",
            base_url=base_url, key=key,
            console_url=console_url, copilot_url=copilot_url,
            services=[s.as_dict() for s in self.services],
        )
        self._write_state()
        self._t0 = time.time()
        return self.info

    def stop(self) -> None:
        """Terminate children in reverse start order; kill stragglers."""
        if self._stopping:
            return
        self._stopping = True
        for svc in reversed(self.services):
            if svc.proc.poll() is None:
                svc.proc.terminate()
        deadline = time.monotonic() + TERM_GRACE_S
        for svc in reversed(self.services):
            remaining = max(deadline - time.monotonic(), 0.1)
            try:
                svc.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                svc.proc.kill()
                svc.proc.wait(timeout=5)
        if self.fake is not None:
            self.fake.stop()
            self.fake = None
        try:
            self.state_file.unlink()
        except OSError:
            pass
        self._stopping = False

    def run(self, duration: float | None = None) -> int:
        """Block until Ctrl-C/SIGTERM (or ``duration`` seconds), then stop."""
        stop_event = threading.Event()

        def _handler(signum: int, frame: Any) -> None:
            self._say(f"\nyantraops: received {signal.Signals(signum).name}, "
                      "shutting down...")
            stop_event.set()

        in_main = threading.current_thread() is threading.main_thread()
        old: dict[int, Any] = {}
        if in_main:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, _handler)
        deadline = time.monotonic() + duration if duration is not None else None
        try:
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    self._say(f"yantraops: --duration reached, shutting down...")
                    break
                self._reap()
                stop_event.wait(0.5)
        finally:
            if in_main:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
            self.stop()
        return 0

    # -- helpers -----------------------------------------------------------

    def _reap(self) -> None:
        """Warn (once) about children that died while we are running."""
        for svc in self.services:
            code = svc.proc.poll()
            if code is not None and not getattr(svc, "_reported", False):
                svc._reported = True  # type: ignore[attr-defined]
                self._say(f"yantraops: WARNING service '{svc.name}' exited "
                          f"with code {code}")

    def _write_state(self) -> None:
        assert self.info is not None
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "started_at": time.time(),
            "mode": self.info.mode,
            "base_url": self.info.base_url,
            "key": self.info.key,
            "console_url": self.info.console_url,
            "copilot_url": self.info.copilot_url,
            "services": self.info.services,
        }, indent=2))

    def wait_ready(self, timeout_s: float = 45.0) -> float:
        """Block until the fleet is actually flowing; return seconds taken.

        Ready means: robots rows exist in the backend AND (when enabled)
        sarathi /health answers. Prints a final READY line with the console
        URL so it is the last thing on screen even above child logs.
        """
        import httpx as _hx
        assert self.info is not None
        started = getattr(self, "_t0", time.time())
        deadline = started + timeout_s
        headers = {"apikey": self.info.key,
                   "Authorization": f"Bearer {self.info.key}"}
        robots_ok = False
        sarathi_ok = self.info.copilot_url is None
        with _hx.Client(timeout=2.0) as c:
            while time.time() < deadline and not (robots_ok and sarathi_ok):
                if not robots_ok:
                    try:
                        r = c.get(f"{self.info.base_url}/rest/v1/robots",
                                  params={"select": "id", "limit": "1"},
                                  headers=headers)
                        robots_ok = r.status_code == 200 and bool(r.json())
                    except Exception:
                        pass
                if not sarathi_ok:
                    try:
                        sarathi_ok = c.get(
                            f"{self.info.copilot_url}/health").status_code == 200
                    except Exception:
                        pass
                if not (robots_ok and sarathi_ok):
                    time.sleep(0.4)
        took = time.time() - started
        state = "READY" if (robots_ok and sarathi_ok) else "PARTIAL (still warming up)"
        print(f"\n  \u2714 {state} in {took:.1f}s \u2014 open:  "
              f"{self.info.console_url}\n", flush=True)
        if self.open_browser and robots_ok:
            try:  # best-effort; headless/CI environments just skip
                import webbrowser
                webbrowser.open(self.info.console_url)
            except Exception:
                pass
        return took

    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    def banner(self) -> str:
        """The startup banner (also returned so tests can assert on it)."""
        assert self.info is not None
        i = self.info
        key_label = i.key if len(i.key) <= 24 else i.key[:24] + "..."
        width = max([len("backend")] + [len(s.name) for s in self.services]) + 2
        lines = [
            "=" * 72,
            f"YantraFleet up — mode: {i.mode}",
            "=" * 72,
            f"  {'backend':<{width}}{i.base_url}  (data API \u2014 not the UI; key: {key_label})",
        ]
        for svc in self.services:
            where = svc.url or "-"
            lines.append(f"  {svc.name:<{width}}pid {svc.proc.pid:<8}{where}")
        if i.copilot_url:
            lines.append(f"  {'copilot':<{width}}POST {i.copilot_url}/ask")
        lines += [
            "-" * 72,
            f"  {'console':<{width}}{i.console_url}",
            "-" * 72,
            "  Ctrl-C to stop.",
        ]
        return "\n".join(lines)
