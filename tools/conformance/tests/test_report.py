"""The report and the CLI: what a stranger actually walks away with.

These reports get forwarded. An integrator runs the tool, gets one HTML file,
and mails it to a vendor's firmware lead who has never heard of any of this. So
the tests below are about the artefact, not the engine: is it self-contained
(no CDN, no build step, no missing stylesheet), does every finding carry the
four things somebody needs to act -- spec clause, severity, observed-vs-expected
and a plain-English fix -- and does it say where it came from.
"""
from __future__ import annotations

import json
import re
from html import escape

import pytest

from fake_robots import Defects, FakeRobot
from harness import run_against
from yantraconform import checks
from yantraconform.loopback import LoopbackBroker
from yantraconform.report_html import HOME_URL, TOOL_URL, render_html


@pytest.fixture(scope="module")
def document():
    broker = LoopbackBroker()
    robot = FakeRobot(broker, publish_visualization=True,
                      defects=Defects(header_id_frozen=True, bad_estop=True,
                                      no_lastwill=True,
                                      battery_as_fraction=True,
                                      accept_stale_order=True))
    return run_against(lambda ticks: robot.tick(ticks), broker)


@pytest.fixture(scope="module")
def html(document):
    return render_html(document)


# ---------------------------------------------------------------------------
# self-contained
# ---------------------------------------------------------------------------

def test_the_report_is_one_self_contained_file(html):
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    # No external anything: a report that needs the network is not a report
    # you can attach to a procurement email.
    assert "<link" not in html.lower()
    assert not re.search(r"<script[^>]+src=", html, re.I)
    assert not re.search(r"<img[^>]+src=", html, re.I)
    assert "cdn" not in html.lower()
    assert "@import" not in html
    # the only URLs are the ones we deliberately put in the footer
    urls = set(re.findall(r'href="(https?://[^"]+)"', html))
    assert urls <= {HOME_URL, TOOL_URL}, urls


def test_the_report_carries_its_own_styling(html):
    assert "<style>" in html
    # the marketing site's tokens, inlined rather than imported
    for token in ("--accent:#2563c9", "--ink:#1c1b17", "--bg:#fbfaf7"):
        assert token in html, token


def test_the_report_says_where_it_came_from(html):
    """It gets forwarded to people who have never heard of the tool."""
    assert "Yantrika" in html
    assert "yantrika.ai" in html
    assert TOOL_URL in html
    assert "yantra-conform" in html
    assert "VDA 5050 2.1.0" in html


def test_the_report_prints_sensibly(html):
    """Findings are read in PDF form as often as in a browser."""
    assert "@media print" in html
    assert "break-inside:avoid" in html


# ---------------------------------------------------------------------------
# actionable findings
# ---------------------------------------------------------------------------

def test_every_finding_is_actionable(document, html):
    findings = [c for r in document["robots"] for c in r["checks"]
                if c["status"] in ("fail", "warn")]
    assert len(findings) >= 4
    for finding in findings:
        # escaped, because that is how it reaches the page -- an assertion on
        # the raw text would pass only for findings with no punctuation in them
        assert escape(finding["spec_ref"], quote=True) in html
        assert escape(finding["observed"][:60], quote=True) in html
        assert escape(finding["expected"][:60], quote=True) in html
        assert escape(finding["remediation"][:60], quote=True) in html
    assert "How to fix" in html


def test_the_grade_explains_itself(document, html):
    assert document["summary"]["grade_capped_by"]
    assert "Grade capped at" in html
    for check_id in document["summary"]["grade_capped_by"]:
        assert check_id in html


def test_the_report_is_escaped_against_a_hostile_payload():
    """Topic and payload content comes off somebody else's wire."""
    hostile = "</script><script>alert(1)</script>"
    document = {
        "schema_version": "1.0", "tool": "yantra-conform",
        "tool_version": "0.1.0", "spec": "VDA 5050 2.1.0",
        "generated_at": "2026-01-01T00:00:00.000Z", "broker": hostile,
        "options": {}, "warnings": [hostile],
        "summary": {"robots": 1, "score": 0.0, "grade": "F",
                    "grade_capped_by": [],
                    "counts": {"pass": 0, "fail": 1, "warn": 0, "skip": 0},
                    "by_category": {"state": {"pass": 0, "fail": 1, "warn": 0,
                                              "skip": 0}},
                    "failed_checks": []},
        "robots": [{
            "manufacturer": hostile, "serial": hostile, "interface": "uagv",
            "major_version": "v2", "topic_prefix": hostile,
            "version_reported": hostile, "score": 0.0, "grade": "F",
            "grade_capped_by": [],
            "counts": {"pass": 0, "fail": 1, "warn": 0, "skip": 0},
            "message_counts": {"state": 1}, "topics_seen": [hostile],
            "notes": [hostile],
            "checks": [{"id": "state.published", "title": hostile,
                        "category": "state", "severity": "critical",
                        "spec_ref": hostile, "status": "fail",
                        "expected": hostile, "observed": hostile,
                        "remediation": hostile, "detail": {"x": hostile}}],
        }],
    }
    out = render_html(document)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    # exactly the one script block we ship
    assert out.count("<script>") == 1


def test_a_clean_report_renders_without_a_findings_section_breaking():
    broker = LoopbackBroker()
    robot = FakeRobot(broker, publish_visualization=True)
    document = run_against(lambda ticks: robot.tick(ticks), broker)
    out = render_html(document)
    assert 'class="grade A"' in out
    assert f'{document["summary"]["counts"]["pass"]} pass' in out
    # No finding blocks at all. Asserting on the string "FAIL" would be wrong:
    # it legitimately appears inside the actionStatus vocabulary that several
    # checks quote in their "expected" line.
    # (the CSS legitimately mentions both selectors, so match the element)
    assert '<details class="chk" data-status="fail">' not in out
    assert '<details class="chk" data-status="warn">' not in out
    assert '<span class="badge fail">' not in out
    assert out.count('<details class="chk" data-status="pass">') == \
        document["summary"]["counts"]["pass"]


def test_an_empty_run_still_renders():
    """Nobody home on the broker: the report must say so, not crash."""
    from yantraconform.model import Report

    report = Report(broker="mqtt://nowhere:1883",
                    generated_at="2026-01-01T00:00:00.000Z")
    report.warnings.append("no vehicles discovered")
    out = render_html(report.to_dict())
    assert "no vehicles discovered" in out
    assert out.startswith("<!doctype html>")


# ---------------------------------------------------------------------------
# the CLI, without a broker anywhere
# ---------------------------------------------------------------------------

def test_cli_checks_lists_the_whole_rule_set(capsys):
    from yantraconform.__main__ import main

    assert main(["checks"]) == 0
    out = capsys.readouterr().out
    for spec_check in checks.CATALOGUE:
        assert spec_check.id in out
    assert f"{len(checks.CATALOGUE)} checks" in out


def test_cli_checks_json_is_a_usable_rule_set(capsys):
    from yantraconform.__main__ import main

    assert main(["checks", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["checks"]) == len(checks.CATALOGUE)
    for entry in payload["checks"]:
        assert set(entry) == {"id", "title", "category", "severity",
                              "spec_ref", "expected", "remediation"}
        assert entry["severity"] in ("critical", "major", "minor", "info")
        assert entry["spec_ref"].startswith("VDA 5050")
        # this is the copy a marketing page renders as "what we test"
        assert len(entry["expected"]) > 20
        assert len(entry["remediation"]) > 20


def test_cli_render_round_trips_a_saved_report(tmp_path, document):
    from yantraconform.__main__ import main

    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(document), encoding="utf-8")
    out_path = tmp_path / "report.html"
    assert main(["render", str(report_path), "-o", str(out_path)]) == 0
    assert out_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_cli_render_rejects_junk(tmp_path, capsys):
    from yantraconform.__main__ import main

    path = tmp_path / "junk.json"
    path.write_text("not json", encoding="utf-8")
    assert main(["render", str(path)]) == 2
    assert "not valid JSON" in capsys.readouterr().err

    path.write_text('{"hello": "world"}', encoding="utf-8")
    assert main(["render", str(path)]) == 2
    assert "not a conformance report" in capsys.readouterr().err


def test_cli_version_and_help_work(capsys):
    from yantraconform.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "yantra-conform" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["run", "checks", "render", "replay"])
def test_every_documented_subcommand_exists(command):
    from yantraconform.__main__ import build_parser

    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    assert any(command in a.choices for a in actions), command


def test_the_json_report_shape_is_stable(document):
    """The contract a web layer or a CI job reads."""
    assert document["schema_version"] == "1.0"
    assert set(document) == {
        "schema_version", "tool", "tool_version", "spec", "generated_at",
        "broker", "options", "summary", "warnings", "robots"}
    assert set(document["summary"]) == {
        "robots", "score", "grade", "grade_capped_by", "counts",
        "by_category", "failed_checks"}
    robot = document["robots"][0]
    assert set(robot) == {
        "manufacturer", "serial", "interface", "major_version",
        "topic_prefix", "version_reported", "score", "grade",
        "grade_capped_by", "counts", "message_counts", "topics_seen",
        "notes", "checks"}
    assert set(robot["checks"][0]) == {
        "id", "title", "category", "severity", "spec_ref", "status",
        "expected", "observed", "remediation", "detail"}
    assert document["summary"]["failed_checks"] == sorted(
        {c["id"] for r in document["robots"] for c in r["checks"]
         if c["status"] == "fail"})
