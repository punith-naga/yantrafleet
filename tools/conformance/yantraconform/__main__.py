"""CLI: ``yantra-conform run|checks|render`` (also ``python -m yantraconform``).

    yantra-conform run --broker mqtt://localhost:1883
    yantra-conform run --broker mqtts://user@fleet.example:8883 --password ... \\
        --manufacturer acme --serial AGV_01 --json out.json --html out.html
    yantra-conform run --broker localhost --passive        # observe only
    yantra-conform checks --json                           # the rule set
    yantra-conform render out.json -o out.html             # re-render a report

Exit codes: 0 = the run completed (and cleared ``--fail-under`` if given),
1 = the score was below ``--fail-under``, 2 = the run could not be performed
(no broker, no vehicles, bad arguments).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import checks, spec
from .model import TOOL_NAME, TOOL_VERSION
from .report_html import render_html
from .runner import RunOptions, run_conformance
from .session import PAHO_AVAILABLE, PahoSession, parse_broker

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_CANNOT_RUN = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yantra-conform",
        description="Free VDA 5050 v2.1 conformance tester: point it at an "
                    "MQTT broker and it grades what your AGVs actually do.")
    p.add_argument("--version", action="version",
                   version=f"{TOOL_NAME} {TOOL_VERSION} "
                           f"(VDA 5050 {spec.TARGET_VERSION})")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the conformance battery")
    run.add_argument("--broker", default="localhost",
                     metavar="URL",
                     help="mqtt://host:1883, mqtts://host:8883, or host[:port] "
                          "(default: localhost:1883)")
    run.add_argument("--username", default=None, help="MQTT username")
    run.add_argument("--password", default=None, help="MQTT password")
    run.add_argument("--interface", default=spec.DEFAULT_INTERFACE,
                     help=f"interfaceName topic segment (default: "
                          f"{spec.DEFAULT_INTERFACE})")
    run.add_argument("--major", default=spec.DEFAULT_MAJOR_SEGMENT,
                     help=f"majorVersion topic segment (default: "
                          f"{spec.DEFAULT_MAJOR_SEGMENT})")
    run.add_argument("--manufacturer", default=None,
                     help="test only this manufacturer (default: all)")
    run.add_argument("--serial", default=None,
                     help="test only this serialNumber (default: all)")
    run.add_argument("--node", default=None,
                     help="nodeId to use for the order probes (default: "
                          "discovered from the vehicle's own state)")
    run.add_argument("--discover-seconds", type=float, default=5.0,
                     help="how long to wait for vehicles to announce (5)")
    run.add_argument("--observe-seconds", type=float, default=8.0,
                     help="passive observation window (8)")
    run.add_argument("--settle-seconds", type=float, default=2.0,
                     help="wait after each active probe (2)")
    run.add_argument("--max-robots", type=int, default=5,
                     help="cap how many discovered vehicles are tested (5)")
    run.add_argument("--passive", action="store_true",
                     help="observe only: publish nothing to the vehicles "
                          "(safe against a live fleet; order, instantAction "
                          "and actionState checks are then reported n/a)")
    run.add_argument("--json", dest="json_out", default=None, metavar="PATH",
                     help="write the machine-readable report here ('-' = stdout)")
    run.add_argument("--html", dest="html_out", default=None, metavar="PATH",
                     help="write the self-contained HTML report here")
    run.add_argument("--title", default=None,
                     help="heading for the HTML report")
    run.add_argument("--fail-under", type=float, default=None, metavar="SCORE",
                     help="exit 1 when the overall score is below SCORE "
                          "(for CI)")
    run.add_argument("--quiet", action="store_true",
                     help="suppress progress output")

    ck = sub.add_parser("checks", help="print the check catalogue and exit")
    ck.add_argument("--json", dest="as_json", action="store_true",
                    help="emit the catalogue as JSON")

    rn = sub.add_parser("render", help="re-render a saved JSON report as HTML")
    rn.add_argument("report", help="path to a JSON report ('-' = stdin)")
    rn.add_argument("-o", "--out", default="-",
                    help="output path ('-' = stdout)")
    rn.add_argument("--title", default=None, help="heading for the report")
    return p


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_checks(args: argparse.Namespace) -> int:
    catalogue = checks.catalogue()
    if args.as_json:
        print(json.dumps({"tool": TOOL_NAME, "tool_version": TOOL_VERSION,
                          "spec": f"VDA 5050 {spec.TARGET_VERSION}",
                          "checks": catalogue}, indent=2))
        return EXIT_OK
    print(f"{TOOL_NAME} {TOOL_VERSION} — {len(catalogue)} checks "
          f"against VDA 5050 {spec.TARGET_VERSION}\n")
    category = None
    for c in catalogue:
        if c["category"] != category:
            category = c["category"]
            print(f"[{category}]")
        print(f"  {c['id']:<38} {c['severity']:<9} {c['spec_ref']}")
        print(f"    {c['expected']}")
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.report == "-" else \
        Path(args.report).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except ValueError as exc:
        print(f"yantra-conform: {args.report} is not valid JSON: {exc}",
              file=sys.stderr)
        return EXIT_CANNOT_RUN
    if "summary" not in document or "robots" not in document:
        print("yantra-conform: that JSON is not a conformance report "
              "(no 'summary'/'robots')", file=sys.stderr)
        return EXIT_CANNOT_RUN
    html = render_html(document, title=args.title)
    if args.out == "-":
        sys.stdout.write(html)
    else:
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"wrote {args.out}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    if not PAHO_AVAILABLE:
        print("yantra-conform: paho-mqtt is not installed. Run\n"
              "    pip install 'yantraconform[mqtt]'\n"
              "(or pip install paho-mqtt) to talk to a broker.",
              file=sys.stderr)
        return EXIT_CANNOT_RUN

    target = parse_broker(args.broker, args.username, args.password)
    options = RunOptions(
        interface=args.interface, major=args.major,
        manufacturer=args.manufacturer, serial=args.serial, node=args.node,
        discover_seconds=args.discover_seconds,
        observe_seconds=args.observe_seconds,
        settle_seconds=args.settle_seconds,
        active=not args.passive, max_robots=args.max_robots,
    )

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    progress(f"connecting to {target} ...")
    try:
        session = PahoSession(target)
    except (OSError, TimeoutError, ConnectionError) as exc:
        print(f"yantra-conform: cannot reach {target}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    try:
        report = run_conformance(session, options, progress=progress,
                                 broker_label=str(target))
    finally:
        session.close()

    document = report.to_dict()
    return _emit(args, document, quiet=args.quiet)


def _emit(args: argparse.Namespace, document: dict, quiet: bool) -> int:
    if args.json_out == "-":
        print(json.dumps(document, indent=2))
    elif args.json_out:
        Path(args.json_out).write_text(
            json.dumps(document, indent=2), encoding="utf-8")
    if args.html_out:
        Path(args.html_out).write_text(
            render_html(document, title=args.title), encoding="utf-8")

    summary = document["summary"]
    if not quiet:
        _print_summary(document)
    if args.json_out and args.json_out != "-" and not quiet:
        print(f"JSON report: {args.json_out}")
    if args.html_out and not quiet:
        print(f"HTML report: {args.html_out}")

    if not document["robots"]:
        return EXIT_CANNOT_RUN
    if args.fail_under is not None and summary["score"] < args.fail_under:
        return EXIT_BELOW_THRESHOLD
    return EXIT_OK


def _print_summary(document: dict) -> None:
    summary = document["summary"]
    counts = summary["counts"]
    print()
    for warning in document.get("warnings") or []:
        print(f"! {warning}")
    for robot in document["robots"]:
        rc = robot["counts"]
        print(f"{robot['grade']}  {robot['score']:>5}  {robot['topic_prefix']}"
              f"   ({rc['pass']} pass / {rc['fail']} fail / {rc['warn']} warn "
              f"/ {rc['skip']} n-a)")
        for chk in robot["checks"]:
            if chk["status"] in ("fail", "warn"):
                print(f"       {chk['status'].upper():<4} {chk['id']:<38} "
                      f"{chk['observed']}")
                if chk["remediation"]:
                    print(f"            fix: {chk['remediation']}")
    print(f"\nOVERALL {summary['grade']}  score {summary['score']}/100  "
          f"({counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn, "
          f"{counts['skip']} n/a)")
    if summary["grade_capped_by"]:
        print("grade capped by: " + ", ".join(summary["grade_capped_by"]))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "checks":
        return cmd_checks(args)
    if args.command == "render":
        return cmd_render(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
