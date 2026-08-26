"""End-to-end offline tests for the CLI (file source + dry-run)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yantrabridge.__main__ import main
from yantrabridge.sources import read_jsonl

SAMPLE = Path(__file__).resolve().parent.parent / "sample.jsonl"


class TestReadJsonl:
    def test_reads_sample(self) -> None:
        msgs = list(read_jsonl(SAMPLE))
        assert len(msgs) == 5
        assert all(m["version"] == "2.1.0" for m in msgs)

    def test_bad_json_raises_with_lineno(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.jsonl"
        p.write_text('{"ok": true}\nnot-json\n')
        with pytest.raises(ValueError, match="bad.jsonl:2"):
            list(read_jsonl(p))

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "gaps.jsonl"
        p.write_text('\n{"serialNumber": "A"}\n\n')
        assert len(list(read_jsonl(p))) == 1


class TestCliDryRun:
    def test_dry_run_prints_rows_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["--file", str(SAMPLE), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out

        # 3 distinct robots in the sample
        assert "-- robots (upsert) (3) --" in out
        # alerts: AGV-003 battery low, AGV-001 gripperStalled FATAL,
        # AGV-003 batteryLow WARNING error  => 3 deduped alerts
        assert "-- alerts (insert, deduped) (3) --" in out
        assert "heartbeat" in out and "dry-run" in out

        # each printed row is valid single-line JSON
        rows = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        robot_ids = {r["id"] for r in rows if "status" in r}
        assert robot_ids == {"AGV-001", "AGV-002", "AGV-003"}
        # AGV-001's final state (FATAL) wins the upsert
        agv1 = next(r for r in rows if r.get("id") == "AGV-001")
        assert agv1["status"] == "fault"
        sevs = sorted(r["sev"] for r in rows if "sev" in r)
        assert sevs == ["crit", "warn", "warn"]

    def test_requires_exactly_one_source(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == 2
        assert main(["--file", "x.jsonl", "--mqtt-host", "h"]) == 2
        assert "exactly one source" in capsys.readouterr().err
