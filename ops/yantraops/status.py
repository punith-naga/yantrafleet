"""`yantraops status` — read the state file, ping every port, print a table."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _http_ok(client: httpx.Client, url: str) -> tuple[bool, str]:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return False, f"unreachable ({type(exc).__name__})"
    return resp.status_code < 500, f"HTTP {resp.status_code}"


def run_status(state_file: Path) -> int:
    if not state_file.is_file():
        print(f"yantraops: no state file at {state_file} — is the stack running?")
        return 1

    state: dict[str, Any] = json.loads(state_file.read_text())
    base_url = state["base_url"]
    rows: list[tuple[str, str, str, str]] = []  # name, pid, endpoint, status
    all_ok = True

    with httpx.Client(timeout=3.0) as client:
        # Backend first (fakerest in loopback mode, Supabase otherwise).
        ok, detail = _http_ok(
            client, f"{base_url}/rest/v1/robots?limit=1&apikey={state['key']}")
        if ok:
            try:
                n = len(client.get(f"{base_url}/rest/v1/robots",
                                   params={"apikey": state["key"]}).json())
                detail = f"ok ({n} robots)"
            except (httpx.HTTPError, ValueError):
                pass
        all_ok &= ok
        rows.append(("backend", "-", base_url, detail))

        for svc in state["services"]:
            name, pid, url = svc["name"], svc["pid"], svc.get("url")
            alive = _pid_alive(pid)
            if url:
                probe = f"{url.rstrip('/')}/health" if name == "sarathi" else url
                ok, detail = _http_ok(client, probe)
                ok = ok and alive
            else:
                ok, detail = alive, ("running" if alive else "not running")
            all_ok &= ok
            rows.append((name, str(pid), url or "-", detail))

    w_name = max(len(r[0]) for r in rows) + 2
    w_pid = max(len(r[1]) for r in rows) + 2
    w_url = max(len(r[2]) for r in rows) + 2
    print(f"{'SERVICE':<{w_name}}{'PID':<{w_pid}}{'ENDPOINT':<{w_url}}STATUS")
    for name, pid, url, detail in rows:
        print(f"{name:<{w_name}}{pid:<{w_pid}}{url:<{w_url}}{detail}")
    print(f"\nconsole: {state['console_url']}")
    if state.get("academy_url"):
        print(f"academy: {state['academy_url']}")
    return 0 if all_ok else 1
