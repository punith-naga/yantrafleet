"""Transport protocol + a stdout transport for --dry-run and debugging."""
from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from ..sim import TickOutput


@runtime_checkable
class Transport(Protocol):
    """Anything that can publish one tick's output."""

    def publish(self, out: TickOutput) -> None:  # pragma: no cover - protocol
        ...

    def close(self) -> None:  # pragma: no cover - protocol
        ...


class StdoutTransport:
    """Prints a compact per-tick summary; full JSON with verbose=True."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def publish(self, out: TickOutput) -> None:
        if self.verbose:
            print(json.dumps({"tick": out.tick, "states": out.states,
                              "events": [e.__dict__ for e in out.events]}, indent=2))
            return
        parts = []
        for s in out.states:
            batt = s["batteryState"]["batteryCharge"]
            err = "!" if s["errors"] else ""
            parts.append(f"{s['serialNumber']}:{batt:.0f}%{err}")
        print(f"[tick {out.tick:4d}] " + " ".join(parts))
        for e in out.events:
            print(f"           event {e.kind} ({e.sev}): {e.msg}")

    def close(self) -> None:
        pass
