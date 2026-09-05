"""Capture and replay: can somebody grade a vehicle they cannot reach?

The promise of a capture file is exact: replaying it must produce the SAME
report the live run produced, or it is a lossy summary pretending to be
evidence. So the central test here does not check that replay "works" -- it
checks that every check id, every status, and every observed-vs-expected line
comes back identical after a JSON round trip.
"""
from __future__ import annotations

import json

import pytest

from fake_robots import Defects, FakeRobot
from harness import fast_options
from yantraconform import capture
from yantraconform.loopback import LoopbackBroker, LoopbackSession
from yantraconform.runner import ConformanceRunner


def run_and_capture(defects: Defects | None = None, **robot_kwargs):
    """Run a live battery with recording on; return (live doc, capture doc)."""
    broker = LoopbackBroker()
    robot_kwargs.setdefault("publish_visualization", True)
    robot = FakeRobot(broker, defects=defects or Defects(), **robot_kwargs)
    session = LoopbackSession(broker)
    runner = ConformanceRunner(
        session=session,
        options=fast_options(capture=True),
        sleep_fn=lambda seconds: robot.tick(max(1, int(seconds))),
        broker_label="loopback://in-process")
    try:
        live = runner.run().to_dict()
    finally:
        session.close()
    return live, runner.capture_document()


def _roundtrip(capture_document: dict) -> dict:
    """Through actual JSON, because that is how the file travels."""
    return capture.replay(json.loads(json.dumps(capture_document))).to_dict()


# ---------------------------------------------------------------------------

def test_replay_reproduces_the_live_report_exactly():
    live, captured = run_and_capture(
        Defects(header_id_frozen=True, bad_estop=True, no_lastwill=True))
    replayed = _roundtrip(captured)

    assert len(replayed["robots"]) == len(live["robots"])
    for live_robot, replayed_robot in zip(live["robots"], replayed["robots"]):
        assert replayed_robot["serial"] == live_robot["serial"]
        assert replayed_robot["score"] == live_robot["score"]
        assert replayed_robot["grade"] == live_robot["grade"]
        assert replayed_robot["counts"] == live_robot["counts"]
        # The findings themselves, not just the totals.
        assert ([(c["id"], c["status"], c["observed"])
                 for c in replayed_robot["checks"]]
                == [(c["id"], c["status"], c["observed"])
                    for c in live_robot["checks"]])
    assert replayed["summary"]["score"] == live["summary"]["score"]
    assert replayed["summary"]["grade"] == live["summary"]["grade"]
    assert (replayed["summary"]["grade_capped_by"]
            == live["summary"]["grade_capped_by"])


def test_replay_preserves_the_active_probe_findings():
    """The hard half: order/instantAction verdicts survive a replay.

    Passive evidence replays trivially. The active-probe checks depend on
    knowing WHICH message the tester sent and WHEN, relative to the state
    stream -- if that were dropped from the capture they would all silently
    turn into 'skip', and the replayed report would look clean.
    """
    live, captured = run_and_capture(
        Defects(accept_stale_order=True, silent_unknown_action=True))
    replayed = _roundtrip(captured)
    statuses = {c["id"]: c["status"] for c in replayed["robots"][0]["checks"]}
    assert statuses["order.reject_stale"] == "fail"
    assert statuses["instant.unknown_rejected"] == "fail"
    assert statuses["order.accept_new"] == "pass"
    assert statuses["instant.cancelOrder"] == "pass"
    # and nothing quietly became unevaluated
    live_skips = {c["id"] for c in live["robots"][0]["checks"]
                  if c["status"] == "skip"}
    replayed_skips = {cid for cid, s in statuses.items() if s == "skip"}
    assert replayed_skips == live_skips


def test_a_compliant_vehicle_still_scores_a_hundred_after_replay():
    live, captured = run_and_capture()
    replayed = _roundtrip(captured)
    assert live["summary"]["score"] == 100.0
    assert replayed["summary"]["score"] == 100.0
    assert replayed["summary"]["grade"] == "A"


def test_the_capture_is_plain_reviewable_json():
    """A vendor has to be able to read what they are about to send out."""
    _, captured = run_and_capture()
    assert captured["kind"] == capture.CAPTURE_KIND
    assert captured["messages"], "a capture with no messages proves nothing"
    first = captured["messages"][0]
    assert set(first) == {"t", "topic", "payload", "qos", "retain"}
    # payloads are the literal bytes off the wire, not a re-serialization
    assert isinstance(first["payload"], str)
    json.loads(first["payload"])
    assert all(m["topic"].count("/") == 4 for m in captured["messages"])
    # nothing but the session: no credentials, no host secrets
    assert "password" not in json.dumps(captured).lower()


def test_replay_says_the_vehicle_was_not_contacted():
    """An offline grade must never be mistaken for a live one."""
    _, captured = run_and_capture()
    replayed = _roundtrip(captured)
    assert any("was not contacted" in w for w in replayed["warnings"]), \
        replayed["warnings"]
    assert any("captured session" in n
               for n in replayed["robots"][0]["notes"])


def test_replay_rejects_a_scored_report():
    """`replay report.json` must say so, not produce an empty result."""
    live, _ = run_and_capture()
    with pytest.raises(ValueError, match="not a yantra-conform capture"):
        capture.replay(live)


def test_replay_rejects_a_future_capture_schema():
    _, captured = run_and_capture()
    captured["schema_version"] = "9.0"
    with pytest.raises(ValueError, match="not.*readable"):
        capture.replay(captured)


def test_nothing_is_recorded_unless_capture_was_asked_for():
    """Recording every message on a long passive run would be a memory leak."""
    broker = LoopbackBroker()
    robot = FakeRobot(broker)
    session = LoopbackSession(broker)
    runner = ConformanceRunner(
        session=session, options=fast_options(),
        sleep_fn=lambda seconds: robot.tick(max(1, int(seconds))))
    runner.run()
    session.close()
    assert runner.collector.consumed == []
    assert runner.capture_document()["messages"] == []


# ---------------------------------------------------------------------------
# through the CLI, the way a stranger does it
# ---------------------------------------------------------------------------

def test_cli_replay_writes_json_and_html(tmp_path, capsys):
    from yantraconform.__main__ import main

    _, captured = run_and_capture(Defects(header_id_frozen=True))
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(captured), encoding="utf-8")
    out_json = tmp_path / "report.json"
    out_html = tmp_path / "report.html"

    code = main(["replay", str(capture_path), "--json", str(out_json),
                 "--html", str(out_html), "--quiet"])
    assert code == 0
    document = json.loads(out_json.read_text(encoding="utf-8"))
    assert document["summary"]["grade"] == "B"
    html = out_html.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "state.header_id_monotonic" in html


def test_cli_replay_fail_under_exits_one(tmp_path):
    from yantraconform.__main__ import main

    _, captured = run_and_capture(Defects(no_state=True, no_connection=True))
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(captured), encoding="utf-8")
    assert main(["replay", str(capture_path), "--fail-under", "99",
                 "--quiet"]) == 1
    assert main(["replay", str(capture_path), "--fail-under", "0",
                 "--quiet"]) == 0


def test_cli_replay_on_a_report_points_at_render(tmp_path, capsys):
    from yantraconform.__main__ import main

    live, _ = run_and_capture()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(live), encoding="utf-8")
    assert main(["replay", str(path), "--quiet"]) == 2
    assert "render" in capsys.readouterr().err


def test_cli_render_on_a_capture_points_at_replay(tmp_path, capsys):
    from yantraconform.__main__ import main

    _, captured = run_and_capture()
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(captured), encoding="utf-8")
    assert main(["render", str(path)]) == 2
    assert "replay" in capsys.readouterr().err
