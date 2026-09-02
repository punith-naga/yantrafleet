"""Offline tests for the bring-your-own-recording import pipeline.

An MCAP file is built in-test with the ``mcap`` writer (no ROS, raw JSON
channels), then imported against ``httpx.MockTransport`` — nothing leaves
the process.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import yantrabridge.__main__ as cli
from yantrabridge import importer
from yantrabridge.importer import (
    ImportEvent,
    Record,
    TopicMap,
    TopicMapError,
    collect,
    jsonl_records,
    looks_like_vda_state,
    read_mcap,
    replay,
)
from yantrabridge.sink import SupabaseSink
from yantrabridge.sources import read_jsonl

SAMPLE = Path(__file__).resolve().parent.parent / "sample.jsonl"

BASE = datetime(2026, 8, 26, 9, 0, 0, tzinfo=timezone.utc)


def _iso(i: int) -> str:
    dt = BASE + timedelta(seconds=i)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ns(i: int) -> int:
    return int((BASE + timedelta(seconds=i)).timestamp() * 1e9)


def vda_msg(serial: str, i: int, *, fatal: bool = False) -> dict[str, Any]:
    return {
        "headerId": i,
        "timestamp": _iso(i),
        "version": "2.1.0",
        "manufacturer": "yantra",
        "serialNumber": serial,
        "driving": not fatal,
        "paused": False,
        "agvPosition": {"x": 1.0 + i, "y": 2.0 + i, "theta": 0.0},
        "velocity": {"vx": 0.5, "vy": 0.0, "omega": 0.0},
        "batteryState": {"batteryCharge": 80.0 - 0.5 * i, "charging": False},
        "operatingMode": "AUTOMATIC",
        "errors": (
            [{"errorType": "motorOverheat", "errorLevel": "FATAL",
              "errorDescription": "left drive motor over temperature"}]
            if fatal else []
        ),
        "safetyState": {"eStop": "NONE", "fieldViolation": False},
        "actionStates": [],
    }


def write_mcap(path: Path, messages: list[tuple[str, int, dict[str, Any]]],
               *, encoding: str = "json") -> None:
    """messages: (topic, log_time_ns, payload)."""
    from mcap.writer import Writer

    with open(path, "wb") as fh:
        w = Writer(fh)
        w.start()
        schema = w.register_schema(name="test.json", encoding="jsonschema",
                                   data=b"{}")
        channels: dict[str, int] = {}
        for topic, log_time, payload in messages:
            if topic not in channels:
                channels[topic] = w.register_channel(
                    topic=topic, message_encoding=encoding, schema_id=schema)
            w.add_message(channels[topic], log_time=log_time,
                          data=json.dumps(payload).encode(),
                          publish_time=log_time)
        w.finish()


@pytest.fixture()
def vda_mcap(tmp_path: Path) -> Path:
    """2 robots, 20 messages (interleaved), one FATAL error on AGV-102."""
    msgs = []
    for i in range(20):
        serial = "AGV-101" if i % 2 == 0 else "AGV-102"
        msg = vda_msg(serial, i, fatal=(i == 13))
        msgs.append((f"uagv/v2/yantra/{serial}/state", _ns(i), msg))
    path = tmp_path / "fleet.mcap"
    write_mcap(path, msgs)
    return path


class SinkRecorder:
    """Capture PostgREST requests; used via a monkeypatched SupabaseSink."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(201)

    def rows(self, table: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for req in self.requests:
            if req.url.path == f"/rest/v1/{table}":
                out.extend(json.loads(req.content.decode()))
        return out


@pytest.fixture()
def sink_recorder(monkeypatch: pytest.MonkeyPatch) -> SinkRecorder:
    rec = SinkRecorder()

    def factory(url: Any = None, key: Any = None, **kw: Any) -> SupabaseSink:
        kw.setdefault("transport", httpx.MockTransport(rec.handler))
        return SupabaseSink("https://example.supabase.co", "test-key", **kw)

    monkeypatch.setattr(cli, "SupabaseSink", factory)
    return rec


# ---------------------------------------------------------------------------
# MCAP reading
# ---------------------------------------------------------------------------

class TestReadMcap:
    def test_reads_json_channels(self, vda_mcap: Path) -> None:
        records = list(read_mcap(vda_mcap))
        assert len(records) == 20
        assert records[0].topic == "uagv/v2/yantra/AGV-101/state"
        assert records[0].log_time_ns == _ns(0)
        assert records[0].msg["serialNumber"] == "AGV-101"

    def test_missing_mcap_package_raises_clear_error(
        self, vda_mcap: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "mcap.reader", None)
        with pytest.raises(RuntimeError, match=r"yantrabridge\[import\]"):
            next(read_mcap(vda_mcap))

    def test_vda_detection(self) -> None:
        assert looks_like_vda_state(vda_msg("A", 0))
        assert not looks_like_vda_state({"veh": "x", "soc": 50})
        assert not looks_like_vda_state({"serialNumber": "A"})  # no VDA body


# ---------------------------------------------------------------------------
# Bulk import (MCAP -> mock transport)
# ---------------------------------------------------------------------------

class TestBulkImport:
    def test_bulk_import_end_to_end(
        self, vda_mcap: Path, sink_recorder: SinkRecorder,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["import", "--mcap", str(vda_mcap)])
        assert rc == 0

        # telemetry: one history row per message, original timestamps kept
        telem = sink_recorder.rows("robot_telemetry")
        assert len(telem) == 20
        ts_seen = {t["ts"] for t in telem}
        assert ts_seen == {_iso(i) for i in range(20)}
        first = next(t for t in telem if t["ts"] == _iso(0))
        assert first["robot_id"] == "AGV-101"
        assert first["battery"] == 80.0
        assert first["pos"] == [1.0, 2.0]

        # robots: final state per robot (last message wins)
        robots = {r["id"]: r for r in sink_recorder.rows("robots")}
        assert set(robots) == {"AGV-101", "AGV-102"}
        assert robots["AGV-101"]["updated_at"] == _iso(18)
        assert robots["AGV-102"]["updated_at"] == _iso(19)
        assert robots["AGV-102"]["status"] == "active"  # fault cleared by i=15
        assert robots["AGV-102"]["battery"] == 80.0 - 0.5 * 19

        # alerts: exactly one, from the FATAL error, deduped
        alerts = sink_recorder.rows("alerts")
        assert len(alerts) == 1
        assert alerts[0]["sev"] == "crit"
        assert alerts[0]["src"] == "AGV-102"
        assert "motorOverheat" in alerts[0]["msg"]
        assert alerts[0]["created_at"] == _iso(13)

        # heartbeat still written once
        assert len(sink_recorder.rows("fleet_meta")) == 1

        out = capsys.readouterr().out
        assert "wrote: 2 robots (upsert), 20 robot_telemetry rows, 1 alerts" in out

    def test_jsonl_import_reuses_pipeline(
        self, sink_recorder: SinkRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["import", "--jsonl", str(SAMPLE)])
        assert rc == 0
        telem = sink_recorder.rows("robot_telemetry")
        assert len(telem) == 5  # one history row per sample message
        # original message timestamps preserved
        sample_ts = {m["timestamp"] for m in read_jsonl(SAMPLE)}
        assert {t["ts"] for t in telem} == sample_ts
        assert len(sink_recorder.rows("robots")) == 3
        assert len(sink_recorder.rows("alerts")) == 3

    def test_requires_exactly_one_recording(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["import"]) == 2
        assert cli.main(["import", "--mcap", "a.mcap", "--jsonl", "b.jsonl"]) == 2
        assert "exactly one recording" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Topic map (non-VDA custom schema)
# ---------------------------------------------------------------------------

CUSTOM_MAP = {
    "/acme/*/telemetry": {
        "robot_id": "topic[1]",
        "battery": "power.soc",
        "pos.x": "pose.x",
        "pos.y": "pose.y",
        "speed": "vel",
        "status": "mode",
        "status_map": {"MOVING": "active", "DOCKED": "charging"},
    }
}


def custom_msg(i: int) -> dict[str, Any]:
    return {
        "power": {"soc": 60.0 - i},
        "pose": {"x": float(i), "y": float(2 * i)},
        "vel": 0.25 * i,
        "mode": "MOVING" if i % 2 == 0 else "DOCKED",
    }


class TestTopicMap:
    def test_custom_schema_via_topic_map(self, tmp_path: Path) -> None:
        msgs = [(f"/acme/forklift-{7 + (i % 2)}/telemetry", _ns(i),
                 custom_msg(i)) for i in range(6)]
        path = tmp_path / "acme.mcap"
        write_mcap(path, msgs)

        tm = TopicMap.parse(CUSTOM_MAP)
        robots, telemetry, alerts, summary = collect(read_mcap(path),
                                                     topic_map=tm)
        assert alerts == []
        assert len(telemetry) == 6
        row0 = telemetry[0]
        assert row0["robot_id"] == "forklift-7"
        assert row0["battery"] == 60.0
        assert row0["pos"] == [0.0, 0.0]
        assert row0["speed"] == 0.0
        assert row0["status"] == "active"       # MOVING via status_map
        assert row0["ts"] == _iso(0)            # mcap log_time preserved
        assert telemetry[1]["status"] == "charging"  # DOCKED via status_map

        by_id = {r["id"]: r for r in robots}
        assert set(by_id) == {"forklift-7", "forklift-8"}
        assert by_id["forklift-7"]["updated_at"] == _iso(4)  # last msg wins
        assert summary.robots == 2 and summary.telemetry == 6
        assert summary.topics["/acme/forklift-7/telemetry"].kind == "mapped"

    def test_topic_map_cli_flag(
        self, tmp_path: Path, sink_recorder: SinkRecorder,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "acme.mcap"
        write_mcap(path, [("/acme/f1/telemetry", _ns(0), custom_msg(0))])
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(CUSTOM_MAP))
        rc = cli.main(["import", "--mcap", str(path),
                       "--topic-map", str(map_path)])
        assert rc == 0
        telem = sink_recorder.rows("robot_telemetry")
        assert len(telem) == 1 and telem[0]["robot_id"] == "f1"

    def test_literal_and_missing_fields(self) -> None:
        entry = {"robot_id": "=bot-1", "battery": "nope.missing",
                 "speed": "vel"}
        mapped = importer.map_message(entry, "/t", {"vel": 1.5}, _iso(0))
        assert mapped is not None
        robot, telem = mapped
        assert robot["id"] == "bot-1"
        assert telem == {"robot_id": "bot-1", "ts": _iso(0), "speed": 1.5}

    def test_message_without_robot_id_is_skipped(self, tmp_path: Path) -> None:
        tm = TopicMap.parse({"/t": {"robot_id": "veh.name"}})
        robots, telemetry, _, summary = collect(
            [Record("/t", _ns(0), {"veh": {}})], topic_map=tm)
        assert robots == [] and telemetry == []
        assert summary.skipped == 1

    def test_bad_specs_rejected(self) -> None:
        with pytest.raises(TopicMapError, match="robot_id"):
            TopicMap.parse({"/t": {"battery": "soc"}})
        with pytest.raises(TopicMapError, match="unknown field"):
            TopicMap.parse({"/t": {"robot_id": "id", "batery": "soc"}})
        with pytest.raises(TopicMapError, match="object"):
            TopicMap.parse(["nope"])


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_summary(
        self, vda_mcap: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # any network attempt must fail loudly
        monkeypatch.setattr(
            cli, "SupabaseSink",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!")))
        rc = cli.main(["import", "--mcap", str(vda_mcap), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "messages: 20 (0 skipped)" in out
        assert "uagv/v2/yantra/AGV-101/state: 10 msgs (10 decoded, kind=vda)" in out
        assert "uagv/v2/yantra/AGV-102/state: 10 msgs (10 decoded, kind=vda)" in out
        assert f"time range: {_iso(0)} .. {_iso(19)}" in out
        assert ("would write: 2 robots (upsert), 20 robot_telemetry rows, "
                "1 alerts") in out
        # translated rows are printed for inspection
        assert "-- robots (upsert) (2) --" in out
        assert "-- alerts (insert, deduped) (1) --" in out
        assert "-- robot_telemetry: 20 rows" in out

    def test_dry_run_counts_undecodable_and_unmatched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "mixed.mcap"
        from mcap.writer import Writer

        with open(path, "wb") as fh:
            w = Writer(fh)
            w.start()
            schema = w.register_schema(name="s", encoding="", data=b"")
            ch_bin = w.register_channel(topic="/cam/raw",
                                        message_encoding="cdr",
                                        schema_id=schema)
            ch_vda = w.register_channel(topic="uagv/v2/y/A1/state",
                                        message_encoding="json",
                                        schema_id=schema)
            w.add_message(ch_bin, log_time=_ns(0),
                          data=b"\x00\x01\xff", publish_time=_ns(0))
            w.add_message(ch_vda, log_time=_ns(1),
                          data=json.dumps(vda_msg("A1", 1)).encode(),
                          publish_time=_ns(1))
            w.finish()

        rc = cli.main(["import", "--mcap", str(path), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "messages: 2 (1 skipped)" in out
        assert "/cam/raw: 1 msgs (0 decoded, kind=skipped)" in out
        assert "would write: 1 robots (upsert), 1 robot_telemetry rows" in out


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_paces_and_orders_events(self, vda_mcap: Path) -> None:
        sleeps: list[float] = []
        events: list[ImportEvent] = []
        summary = replay(
            read_mcap(vda_mcap), rate=4.0,
            on_event=events.append, sleep=sleeps.append)

        assert len(events) == 20
        # timestamp order preserved, live robot upsert row per message
        ts = [e.telemetry_row["ts"] for e in events]
        assert ts == [_iso(i) for i in range(20)]
        assert all(e.robot_row is not None for e in events)
        # 19 gaps of 1s at 4x -> 0.25s each
        assert len(sleeps) == 19
        assert all(abs(s - 0.25) < 1e-6 for s in sleeps)
        assert summary.robots == 2 and summary.alerts == 1

    def test_replay_cli_pushes_per_message(
        self, vda_mcap: Path, sink_recorder: SinkRecorder,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        real_replay = importer.replay
        monkeypatch.setattr(
            importer, "replay",
            lambda *a, **k: real_replay(
                *a, **{**k, "sleep": lambda s: None}))
        rc = cli.main(["import", "--mcap", str(vda_mcap), "--rate", "1000"])
        assert rc == 0
        robots = sink_recorder.rows("robots")
        assert len(robots) == 20  # one live upsert per message
        assert len(sink_recorder.rows("robot_telemetry")) == 20
        assert len(sink_recorder.rows("alerts")) == 1
        assert len(sink_recorder.rows("fleet_meta")) == 1
        assert "wrote: 2 robots" in capsys.readouterr().out

    def test_rate_zero_rejected_by_replay_helper(self) -> None:
        with pytest.raises(ValueError, match="rate"):
            replay([], rate=0, on_event=lambda e: None, sleep=lambda s: None)


# ---------------------------------------------------------------------------
# Sink: telemetry insert shape
# ---------------------------------------------------------------------------

class TestInsertTelemetry:
    def make_sink(self, rec: SinkRecorder) -> SupabaseSink:
        return SupabaseSink("https://example.supabase.co", "k",
                            transport=httpx.MockTransport(rec.handler))

    def test_plain_insert_no_on_conflict(self) -> None:
        rec = SinkRecorder()
        rows = [{"robot_id": "A", "ts": _iso(0), "battery": 50.0}]
        with self.make_sink(rec) as sink:
            assert sink.insert_telemetry(rows) == 1
        req = rec.requests[0]
        assert req.url.path == "/rest/v1/robot_telemetry"
        assert "on_conflict" not in dict(req.url.params)
        assert "return=minimal" in req.headers["prefer"]
        assert json.loads(req.content.decode()) == rows

    def test_chunking(self) -> None:
        rec = SinkRecorder()
        rows = [{"robot_id": "A", "ts": _iso(i)} for i in range(7)]
        with self.make_sink(rec) as sink:
            assert sink.insert_telemetry(rows, chunk_size=3) == 7
        sizes = [len(json.loads(r.content.decode())) for r in rec.requests]
        assert sizes == [3, 3, 1]


# ---------------------------------------------------------------------------
# JSONL record wrapping
# ---------------------------------------------------------------------------

def test_jsonl_records_carry_payload_timestamps() -> None:
    recs = list(jsonl_records(read_jsonl(SAMPLE)))
    assert len(recs) == 5
    assert recs[0].topic == ""
    assert recs[0].log_time_ns > 0
