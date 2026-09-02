"""Offline unit tests for `yantraops doctor` — fakes for importers/sockets/HTTP."""
from __future__ import annotations

from pathlib import Path

from yantraops import doctor
from yantraops.doctor import (FAIL, PASS, WARN, check_free_port, check_python,
                              check_pypi_package, check_repo_package,
                              check_supabase, format_report, gather_checks,
                              run_doctor)


def _fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "copilot" / "sarathi").mkdir(parents=True)
    (tmp_path / "copilot" / "sarathi" / "app.py").write_text("app = None")
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "index.html").write_text("<html></html>")
    return tmp_path


def importer_all_present(name):
    return object()


def importer_none(name):
    return None


def test_python_version_gate():
    assert check_python((3, 10, 0)).status == PASS
    assert check_python((3, 12, 4)).status == PASS
    bad = check_python((3, 9, 18))
    assert bad.status == FAIL and "3.10" in bad.detail


def test_package_checks_use_injected_importer():
    ok = check_repo_package("yantracore", importer_all_present)
    assert ok.status == PASS
    missing = check_repo_package("yantrasim", importer_none)
    assert missing.status == FAIL
    assert missing.fix == "pip install -e sim/"
    assert check_pypi_package("httpx", importer_none).fix == "pip install httpx"


def test_free_port_check():
    assert check_free_port(lambda: 54321).status == PASS

    def blocked():
        raise OSError("bind: permission denied")
    res = check_free_port(blocked)
    assert res.status == FAIL


def test_supabase_check_unset_env_is_not_a_failure():
    res = check_supabase(env={})
    assert res.status == PASS
    assert "not set" in res.detail


def test_supabase_check_blocked_network_warns_not_fails():
    def getter(url, headers):
        raise ConnectionError("blocked by proxy")
    res = check_supabase(env={"SUPABASE_URL": "https://x.supabase.co"},
                         http_get=getter)
    assert res.status == WARN  # explicitly not FAIL


def test_supabase_check_reachable():
    res = check_supabase(env={"SUPABASE_URL": "https://x.supabase.co",
                              "SUPABASE_KEY": "anon"},
                         http_get=lambda url, headers: 200)
    assert res.status == PASS


def test_run_doctor_all_green(tmp_path, capsys):
    rc = run_doctor(root=_fake_repo(tmp_path), env={},
                    importer=importer_all_present, port_fn=lambda: 12345)
    assert rc == 0
    out = capsys.readouterr().out
    for name in ("python", "yantracore", "yantrasim", "yantrabridge",
                 "yantradetect", "yantranotify", "sarathi", "httpx",
                 "fastapi", "uvicorn", "console", "supabase", "free-port"):
        assert f"] {name}" in out
    assert "yantraops up --loopback" in out


def test_run_doctor_missing_package_fails_with_pip_line(tmp_path, capsys):
    def importer(name):
        return None if name == "yantradetect" else object()
    rc = run_doctor(root=_fake_repo(tmp_path), env={},
                    importer=importer, port_fn=lambda: 12345)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAIL] yantradetect" in out
    assert "pip install -e detector/" in out


def test_run_doctor_missing_console_fails(tmp_path, capsys):
    root = _fake_repo(tmp_path)
    (root / "console" / "index.html").unlink()
    rc = run_doctor(root=root, env={}, importer=importer_all_present,
                    port_fn=lambda: 12345)
    assert rc == 1
    assert "[FAIL] console" in capsys.readouterr().out


def test_warns_do_not_break_exit_zero(tmp_path, capsys):
    def getter(url, headers):
        raise ConnectionError("no route")
    rc = run_doctor(root=_fake_repo(tmp_path),
                    env={"SUPABASE_URL": "https://x.supabase.co"},
                    importer=importer_all_present, port_fn=lambda: 1,
                    http_get=getter)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[WARN] supabase" in out and "warning" in out


def test_format_report_collapses_pip_fixes():
    checks = [doctor.Check(FAIL, "httpx", "missing", fix="pip install httpx"),
              doctor.Check(FAIL, "fastapi", "missing", fix="pip install fastapi")]
    text, code = format_report(checks)
    assert code == 1
    assert "pip install httpx fastapi" in text


def test_gather_checks_covers_everything(tmp_path):
    checks = gather_checks(root=_fake_repo(tmp_path), env={},
                           importer=importer_all_present, port_fn=lambda: 1)
    names = [c.name for c in checks]
    assert names[0] == "python" and "free-port" in names
    assert len(names) == 13


def test_doctor_cli_runs_against_real_environment(capsys):
    """The real `doctor` command in this dev checkout should be green."""
    from yantraops.__main__ import main
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "verdict:" in out
    assert rc == 0, out
