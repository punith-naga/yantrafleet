"""Offline tests for `yantraops audit-security`.

Two backend styles:

* the real ``e2e/fakerest.py`` on a 127.0.0.1 ephemeral port — its default
  behaviour (auth ignored, writes accepted) IS demo-open mode, and
  ``FakePostgREST(rbac=True, service_key=...)`` emulates the 0007 posture;
* an injected ``httpx.MockTransport`` client scripting the hardened-read
  (Option B) / missing-schema / unreachable answers fakerest has no mode for.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from yantraops import audit
from yantraops.__main__ import main
from yantraops.audit import (FAIL, PASS, SKIP, WARN, check_env_file,
                             check_key_class, check_sarathi_token,
                             check_transport, check_webhook_secret,
                             format_audit_report, gather_audit, run_audit,
                             summarize)
from yantraops.doctor import Check
from yantraops.orchestrator import load_fakerest, repo_root

CLEAN_ENV = {"SARATHI_TOKEN": "t" * 32}          # no webhook channel
NO_ENV_FILE = Path("/nonexistent/yantrafleet.env")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _jwt(role: str) -> str:
    seg = lambda obj: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS256'})}.{seg({'role': role, 'iss': 'supabase'})}.x"


def scripted_client(read_status=200, rows=(), write_status=401,
                    table_status=None, record=None):
    """httpx.Client over MockTransport speaking just enough PostgREST."""
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST" and table == "alerts":
            return httpx.Response(write_status, json={})
        if request.method == "DELETE":
            return httpx.Response(204)
        if table == "robots":
            if read_status == 200:
                return httpx.Response(200, json=list(rows))
            return httpx.Response(read_status, json={"message": "denied"})
        ts = (table_status or {}).get(table, 200)
        if ts == 200:
            return httpx.Response(200, json=[])
        if ts == 404:
            return httpx.Response(404, json={
                "code": "42P01", "message": "relation does not exist"})
        return httpx.Response(ts, json={"message": "denied"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


@pytest.fixture()
def fake_backend():
    mod = load_fakerest(repo_root())
    fake = mod.FakePostgREST(tables={"robots": [{"id": "AMR-01"}]})
    base_url = fake.start()
    yield fake, base_url
    fake.stop()


# --------------------------------------------------------------------------
# Mode detection
# --------------------------------------------------------------------------

def test_demo_mode_against_fakerest(fake_backend):
    fake, base_url = fake_backend
    _, mode, checks = gather_audit(base_url, "sb_publishable_test",
                                   env=CLEAN_ENV, env_file=NO_ENV_FILE)
    named = by_name(checks)
    assert mode == "demo"
    assert named["anon-write"].status == FAIL
    assert "demo policies active" in named["anon-write"].detail
    assert "0006" in (named["anon-write"].fix or "")
    _, code = summarize(checks)
    assert code == 2
    # The probe row is harmless, uniquely named, and only one was written
    # (fakerest cannot DELETE, so it stays behind).
    probes = [r for r in fake.tables["alerts"]
              if str(r.get("id", "")).startswith("audit-probe-")]
    assert len(probes) == 1 and probes[0]["src"] == "yantraops-audit"


def test_hardened_read_mode_warns_on_anon_read():
    client = scripted_client(read_status=200, rows=[{"id": "AMR-01"}],
                             write_status=401)
    _, mode, checks = gather_audit(
        "https://x.supabase.co", "sb_publishable_test",
        client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    named = by_name(checks)
    assert mode == "hardened-read"
    assert named["anon-write"].status == PASS
    assert named["anon-read"].status == WARN
    _, code = summarize(checks)
    assert code == 1


def test_rbac_locked_mode_is_all_clean():
    client = scripted_client(
        read_status=401, write_status=403,
        table_status={t: 401 for t in audit.MIGRATION_TABLES})
    _, mode, checks = gather_audit(
        "https://x.supabase.co", "sb_publishable_test",
        client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    named = by_name(checks)
    assert mode == "rbac"
    assert named["anon-write"].status == PASS
    assert named["anon-read"].status == PASS
    # Migration presence cannot be probed with a locked-out anon key.
    assert all(named[f"schema-{t}"].status == SKIP
               for t in audit.MIGRATION_TABLES)
    _, code = summarize(checks)
    assert code == 0


def test_rbac_mode_against_fakerest_rbac():
    """End-to-end against fakerest's 0007 emulation: the anon key gets
    empty reads and 401 writes, so the audit must come back clean."""
    mod = load_fakerest(repo_root())
    fake = mod.FakePostgREST(tables={"robots": [{"id": "AMR-01"}]},
                             rbac=True, service_key="sk-test-service")
    base_url = fake.start()
    try:
        _, mode, checks = gather_audit(base_url, "sb_publishable_test",
                                       env=CLEAN_ENV, env_file=NO_ENV_FILE)
    finally:
        fake.stop()
    named = by_name(checks)
    assert mode == "rbac"
    assert named["anon-write"].status == PASS
    assert named["anon-read"].status == PASS
    # RLS filters rows but the tables answer 200 — schema is verifiable.
    assert all(named[f"schema-{t}"].status == PASS
               for t in audit.MIGRATION_TABLES)
    _, code = summarize(checks)
    assert code == 0
    # And the write probe left no trace behind RLS.
    assert not any(str(r.get("id", "")).startswith("audit-probe-")
                   for r in fake.tables["alerts"])


def test_read_200_but_zero_rows_with_writes_refused_reads_as_rbac():
    client = scripted_client(read_status=200, rows=[], write_status=401)
    _, mode, checks = gather_audit(
        "https://x.supabase.co", "sb_publishable_test",
        client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    assert mode == "rbac"
    assert by_name(checks)["anon-read"].status == PASS


def test_unreachable_backend_fails_and_skips_probes():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)
    client = httpx.Client(transport=httpx.MockTransport(boom))
    _, mode, checks = gather_audit(
        "https://gone.supabase.co", "sb_publishable_test",
        client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    named = by_name(checks)
    assert mode == "unknown"
    assert named["backend"].status == FAIL
    assert "unreachable" in named["backend"].detail
    skipped = [c for c in checks if c.status == SKIP]
    assert {"rls-mode", "anon-write", "anon-read"} <= {c.name for c in skipped}
    assert all(f"schema-{t}" in {c.name for c in skipped}
               for t in audit.MIGRATION_TABLES)
    _, code = summarize(checks)
    assert code == 2


def test_write_probe_cleans_up_after_itself():
    seen: list[httpx.Request] = []
    client = scripted_client(rows=[{"id": "r"}], write_status=201, record=seen)
    gather_audit("https://x.supabase.co", "sb_publishable_test",
                 client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    posts = [r for r in seen if r.method == "POST"]
    deletes = [r for r in seen if r.method == "DELETE"]
    assert len(posts) == 1 and len(deletes) == 1
    probe_id = json.loads(posts[0].content)[0]["id"]
    assert probe_id.startswith("audit-probe-")
    assert f"id=eq.{probe_id}" in str(deletes[0].url)


def test_missing_migration_table_fails_with_migrate_fix():
    client = scripted_client(rows=[{"id": "r"}], write_status=201,
                             table_status={"robot_telemetry": 404})
    _, _, checks = gather_audit(
        "https://x.supabase.co", "sb_publishable_test",
        client=client, env=CLEAN_ENV, env_file=NO_ENV_FILE)
    named = by_name(checks)
    tele = named["schema-robot_telemetry"]
    assert tele.status == FAIL
    assert "0003_telemetry.sql" in tele.detail
    assert "yantraops migrate" in (tele.fix or "")
    assert named["schema-commands"].status == PASS
    assert named["schema-maintenance_findings"].status == PASS


# --------------------------------------------------------------------------
# Local checks (pure units)
# --------------------------------------------------------------------------

def test_key_class_service_keys_fail():
    assert check_key_class("sb_secret_abc123").status == FAIL
    jwt = check_key_class(_jwt("service_role"))
    assert jwt.status == FAIL
    assert "never ship" in jwt.detail.lower()


def test_key_class_client_safe_keys_pass():
    assert check_key_class(_jwt("anon")).status == PASS
    assert check_key_class("sb_publishable_abc").status == PASS
    assert check_key_class("mystery-key-format").status == WARN


def test_sarathi_token_and_webhook_secret_checks():
    assert check_sarathi_token({}).status == WARN
    assert check_sarathi_token({"SARATHI_TOKEN": "x"}).status == PASS
    # No webhook channel configured: nothing to sign.
    assert check_webhook_secret({}).status == PASS
    assert check_webhook_secret(
        {"WEBHOOK_URL": "https://hooks.example.com/y"}).status == WARN
    assert check_webhook_secret(
        {"WEBHOOK_URL": "https://hooks.example.com/y",
         "YANTRA_WEBHOOK_SECRET": "s"}).status == PASS


def test_https_check_flags_only_remote_plain_http():
    assert check_transport("http://127.0.0.1:54321", {}).status == PASS
    assert check_transport("http://localhost:8000", {}).status == PASS
    assert check_transport("https://x.supabase.co", {}).status == PASS
    bad = check_transport("http://fleet.example.com", {})
    assert bad.status == WARN and "unencrypted" in bad.detail
    hook = check_transport("https://x.supabase.co",
                           {"WEBHOOK_URL": "http://hooks.example.com/y"})
    assert hook.status == WARN and "WEBHOOK_URL" in hook.detail


def test_env_file_permission_check(tmp_path):
    assert check_env_file(tmp_path / "absent.env") is None  # silent skip
    f = tmp_path / "yantrafleet.env"
    f.write_text("SUPABASE_KEY=secret\n")
    f.chmod(0o644)
    open_check = check_env_file(f)
    assert open_check is not None and open_check.status == WARN
    assert "chmod 600" in (open_check.fix or "")
    f.chmod(0o600)
    assert check_env_file(f).status == PASS


# --------------------------------------------------------------------------
# Report, JSON, exit codes, CLI
# --------------------------------------------------------------------------

def test_exit_code_ladder():
    ok = [Check(PASS, "a", "d"), Check(SKIP, "b", "d")]
    warn = ok + [Check(WARN, "c", "d")]
    fail = warn + [Check(FAIL, "e", "d")]
    assert summarize(ok)[1] == 0
    assert summarize(warn)[1] == 1
    assert summarize(fail)[1] == 2


def test_text_report_has_remediation_lines_and_summary():
    checks = [Check(PASS, "backend", "reachable"),
              Check(FAIL, "anon-write", "anon can write",
                    fix="apply supabase/0006_harden.sql")]
    text, code = format_audit_report("https://x.supabase.co", "demo", checks)
    assert code == 2
    assert "[FAIL] anon-write" in text
    assert "-> apply supabase/0006_harden.sql" in text
    assert "summary: 1 pass, 0 warn, 1 fail, 0 skipped" in text
    assert "mode:    demo" in text


def test_json_output_shape(capsys):
    client = scripted_client(read_status=401, write_status=401,
                             table_status={t: 401 for t in audit.MIGRATION_TABLES})
    code = run_audit("https://x.supabase.co", "sb_publishable_test",
                     json_output=True, client=client, env=CLEAN_ENV,
                     env_file=NO_ENV_FILE)
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["exit_code"] == 0
    assert doc["backend"] == "https://x.supabase.co"
    assert doc["mode"] == "rbac"
    assert set(doc["summary"]) == {"PASS", "WARN", "FAIL", "SKIP"}
    assert doc["checks"] and all(
        set(c) == {"status", "name", "detail", "fix"} for c in doc["checks"])
    names = [c["name"] for c in doc["checks"]]
    for expected in ("backend", "rls-mode", "anon-write", "anon-read",
                     "key-class", "sarathi-token", "webhook-secret", "https"):
        assert expected in names


def test_cli_audit_security_subcommand(fake_backend, capsys):
    _, base_url = fake_backend
    code = main(["audit-security", "--url", base_url,
                 "--key", "sb_publishable_test", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 2                       # fakerest is demo-open: anon writes
    assert doc["mode"] == "demo"
    assert any(c["name"] == "anon-write" and c["status"] == "FAIL"
               for c in doc["checks"])
