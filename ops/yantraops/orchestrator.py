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
5. ``http.server``               — one static server on an ephemeral port,
   serving a tiny generated docroot that exposes the console at ``/``,
   the training app at ``/academy/`` and the docs site at ``/docs/``

MQTT mode (``--mqtt``) swaps the robot-data path for the real VDA 5050
wire: an MQTT broker (embedded amqtt on an ephemeral port, or an external
``--broker host:port``), ``yantrasim --mqtt`` publishing per-vehicle VDA
topics to it (skipped by ``--no-sim`` when real robots publish), and
``python -m yantrabridge --mqtt-host ...`` subscribing and writing the
same Supabase rows the sim would have written. Detector/notifier/copilot/
console are unchanged, and so is the readiness probe (robots rows).

Every port is ephemeral; the startup banner prints the console URL with
``?supa=...&key=...`` query params so the browser talks to the same backend.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:  # single source of the release version: core/yantracore/version.py
    from yantracore import __version__ as YF_VERSION
except Exception:  # yantracore not installed — standalone ops checkout
    YF_VERSION = "dev"

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


def _place(src: Path, dst: Path) -> None:
    """Symlink ``src`` at ``dst``; copy when symlinks are unavailable.

    Linux/macOS always allow symlinks; Windows needs a privilege, so the
    fallback copies (dirs recursively) to keep the docroot working there.
    """
    try:
        dst.symlink_to(src.resolve(), target_is_directory=src.is_dir())
    except OSError:
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def build_docroot(root: Path) -> Path:
    """Assemble the static server's docroot in a fresh temp directory.

    ONE ``http.server`` child exposes the three static apps side by side:

    * ``/``          — every top-level entry of ``console/`` (so the
      console's ``/index.html`` URL keeps working exactly as before)
    * ``/academy/``  — the training app, when ``academy/`` exists
    * ``/docs/``     — the docs site, when ``docs/`` exists

    The caller owns the returned directory and removes it on stop().
    """
    docroot = Path(tempfile.mkdtemp(prefix="yantraops-www-"))
    for entry in sorted((root / "console").iterdir()):
        if entry.name.startswith("."):
            continue                       # .pytest_cache and friends
        _place(entry, docroot / entry.name)
    for name in ("academy", "docs"):
        src = root / name
        if src.is_dir():
            _place(src, docroot / name)
    return docroot


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


def preflight_supabase_schema(
    base_url: str, key: str, transport: Any = None,
) -> tuple[str, str]:
    """Probe ``{url}/rest/v1/robots?limit=1`` before spawning any children.

    Returns ``(status, detail)`` where status is one of:

    * ``"ok"``             — robots table answered 200
    * ``"schema-missing"`` — 404-ish / PostgREST "relation does not exist"
    * ``"auth"``           — 401/403 (bad or missing key)
    * ``"unreachable"``    — network-level failure
    * ``"unknown"``        — anything else

    ``transport`` is handed to ``httpx.Client`` so tests can inject a
    ``httpx.MockTransport`` — no network needed.
    """
    import httpx
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client:
            resp = client.get(f"{base_url}/rest/v1/robots",
                              params={"limit": "1"}, headers=headers)
    except Exception as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200:
        return "ok", "robots table found"
    body = resp.text[:300]
    table_missing = (
        resp.status_code == 404
        or "PGRST205" in body          # PostgREST: table not in schema cache
        or "42P01" in body             # Postgres: undefined_table
        or "does not exist" in body
    )
    if table_missing:
        return "schema-missing", f"HTTP {resp.status_code}: {body}"
    if resp.status_code in (401, 403):
        return "auth", f"HTTP {resp.status_code}: {body}"
    return "unknown", f"HTTP {resp.status_code}: {body}"


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
    academy_url: str | None = None    # /academy/ on the static server, if present
    services: list[dict[str, Any]] = field(default_factory=list)
    mqtt_url: str | None = None       # mqtt://host:port when --mqtt is active
    mqtt_embedded: bool = False       # True when yantraops owns the broker


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
        mqtt: bool = False,
        mqtt_broker: str | None = None,
        sim: bool = True,
        site: str | None = None,
    ) -> None:
        self.loopback = loopback
        self.copilot = copilot
        self.mqtt = mqtt
        self.mqtt_broker = mqtt_broker  # external "host[:port]", None=embedded
        self.sim = sim
        # Site stamped on bridge-written rows and used to scope the command
        # gate; flag > env > default demo site.
        self.site = site or os.environ.get("YANTRA_SITE_ID") or "BLR-DC1"
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
        self.docroot: Path | None = None  # generated static docroot (temp dir)
        self.fake: Any = None            # FakePostgREST instance in loopback mode
        self.broker: Any = None          # EmbeddedBroker when --mqtt w/o --broker
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

        # MQTT wire (broker first — the sim and the bridge connect to it).
        mqtt_host: str | None = None
        mqtt_port: int | None = None
        try:
            if self.mqtt:
                from .broker import EmbeddedBroker, parse_broker
                if self.mqtt_broker:
                    mqtt_host, mqtt_port = parse_broker(self.mqtt_broker)
                else:
                    self.broker = EmbeddedBroker()
                    mqtt_host, mqtt_port = self.broker.start()

            if self.sim:
                if self.mqtt:
                    spawn("yantrasim", [
                        py, "-m", "yantrasim", "--mqtt",
                        "--broker", str(mqtt_host), "--port", str(mqtt_port),
                        "--interval", str(self.sim_interval),
                    ])
                else:
                    spawn("yantrasim", [
                        py, "-m", "yantrasim", "--supabase",
                        "--url", base_url, "--key", key,
                        "--interval", str(self.sim_interval),
                    ])
            if self.mqtt:
                spawn("yantrabridge", [
                    py, "-m", "yantrabridge",
                    "--mqtt-host", str(mqtt_host),
                    "--mqtt-port", str(mqtt_port),
                    "--supabase-url", base_url, "--supabase-key", key,
                    "--commands",
                    "--site", self.site,
                ], url=f"mqtt://{mqtt_host}:{mqtt_port}")
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
            self.docroot = build_docroot(self.root)
            # Same backend params for every static app (the pages read
            # ?supa/?key/?site via URLSearchParams).
            params = (
                f"?supa={quote(base_url, safe='')}&key={quote(key, safe='')}"
                f"&site={quote(self.site, safe='')}"
            )
            console_url = f"http://127.0.0.1:{console_port}/index.html{params}"
            academy_url = (
                f"http://127.0.0.1:{console_port}/academy/index.html{params}"
                if (self.docroot / "academy" / "index.html").is_file()
                else None
            )
            spawn("console", [
                py, "-m", "http.server", str(console_port),
                "--bind", "127.0.0.1",
                "--directory", str(self.docroot),
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
            academy_url=academy_url,
            services=[s.as_dict() for s in self.services],
            mqtt_url=(f"mqtt://{mqtt_host}:{mqtt_port}" if self.mqtt else None),
            mqtt_embedded=self.broker is not None,
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
        if self.broker is not None:  # after the clients, before the backend
            self.broker.stop()
            self.broker = None
        if self.fake is not None:
            self.fake.stop()
            self.fake = None
        if self.docroot is not None:
            shutil.rmtree(self.docroot, ignore_errors=True)
            self.docroot = None
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
            "academy_url": self.info.academy_url,
            "copilot_url": self.info.copilot_url,
            "mqtt_url": self.info.mqtt_url,
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
            f"YantraFleet {YF_VERSION} up — mode: {i.mode}",
            "=" * 72,
            f"  {'backend':<{width}}{i.base_url}  (data API \u2014 not the UI; key: {key_label})",
        ]
        if i.mqtt_url:
            kind = "embedded" if i.mqtt_embedded else "external"
            lines.append(f"  {'broker':<{width}}{i.mqtt_url}  ({kind})")
            lines.append(f"  {'':<{width}}MQTT: VDA 5050 wire active")
        for svc in self.services:
            where = svc.url or "-"
            lines.append(f"  {svc.name:<{width}}pid {svc.proc.pid:<8}{where}")
        if i.copilot_url:
            lines.append(f"  {'copilot':<{width}}POST {i.copilot_url}/ask")
        lines += [
            "-" * 72,
            f"  {'console':<{width}}{i.console_url}",
        ]
        if i.academy_url:
            lines.append(f"  {'academy':<{width}}{i.academy_url}")
        lines += [
            "-" * 72,
            "  Ctrl-C to stop.",
        ]
        return "\n".join(lines)
