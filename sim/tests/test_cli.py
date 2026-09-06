"""CLI parsing + a short end-to-end run with the stdout transport."""
import pytest

from yantrasim.__main__ import build_parser, main


def test_defaults_select_supabase():
    args = build_parser().parse_args([])
    assert not args.mqtt and not args.stdout
    # v0.18: None (not explicitly passed) so main() can prefer a live
    # public.app_config value over the hardcoded default (2.0) — see
    # yantrasim.__main__.DEFAULT_INTERVAL_S.
    assert args.interval is None
    assert args.seed == 42


def test_supabase_and_mqtt_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--supabase", "--mqtt"])


def test_interval_flag():
    args = build_parser().parse_args(["--supabase", "--interval", "2"])
    assert args.supabase and args.interval == 2.0


def test_short_run_stdout(capsys):
    # 3 ticks through the real loop, no network, no sleep between last ticks.
    rc = main(["--stdout", "--ticks", "3", "--interval", "0", "--seed", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tick    1" in out and "tick    3" in out
    assert "AMR_01" in out
