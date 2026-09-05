"""yantraconform — a free VDA 5050 v2.1 conformance tester.

Library use::

    from yantraconform import RunOptions, run_conformance
    from yantraconform.session import PahoSession, parse_broker

    session = PahoSession(parse_broker("mqtt://broker.example:1883"))
    report = run_conformance(session, RunOptions(observe_seconds=10))
    document = report.to_dict()          # the machine-readable report
    print(document["summary"]["grade"])  # A .. F

CLI use::

    yantra-conform run --broker mqtt://localhost:1883 --html report.html
    yantra-conform run --broker localhost --passive     # publish nothing
    yantra-conform run --broker localhost --capture capture.json
    yantra-conform replay capture.json --html report.html   # offline re-grade
    yantra-conform checks --json          # the rule set, without a broker
    yantra-conform render report.json -o report.html

Only ``run`` needs a broker (and therefore paho); ``replay``, ``render`` and
``checks`` are pure standard library, which is what lets someone grade a
captured session on a machine that can reach neither the fleet nor the
internet.
"""
from __future__ import annotations

from .model import Report, RobotReport, CheckResult, SCHEMA_VERSION, TOOL_VERSION
from .runner import ConformanceRunner, RunOptions, run_conformance

__version__ = TOOL_VERSION

__all__ = [
    "CheckResult",
    "ConformanceRunner",
    "Report",
    "RobotReport",
    "RunOptions",
    "SCHEMA_VERSION",
    "__version__",
    "run_conformance",
]
