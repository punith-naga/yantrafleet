"""``yantraops audit-security`` — probe the configured backend's security
posture and the local environment, PASS/WARN/FAIL per check.

What it answers, in one command:

* Which RLS mode is the backend actually in — demo-open, hardened
  read-only, or RBAC/locked?  (Probed, not assumed: an anon-key INSERT
  into ``alerts`` that *succeeds* means the demo-open ``demo_all``
  policies are still active.)
* Is the configured ``SUPABASE_KEY`` client-safe, or is a service key
  about to be shipped to browsers?
* Are the local secrets (``SARATHI_TOKEN``, ``YANTRA_WEBHOOK_SECRET``)
  set, are configured URLs https, is ``/etc/yantrafleet.env`` locked
  down?
* Are the schema migrations (0002/0003/0004 tables) actually applied?

Exit codes: 0 = all clean, 1 = warnings only, 2 = at least one FAIL.

Like the rest of yantraops, every dependency is injectable — the HTTP
client (any ``httpx.Client``, e.g. one built on ``httpx.MockTransport``),
the environment mapping, and the env-file path — so the whole module is
unit-testable offline.

The write probe is deliberately harmless: it inserts one ``alerts`` row
with a unique ``audit-probe-…`` id and immediately issues a DELETE for
that exact row (best-effort — a backend that refuses DELETE just keeps
one inert info row).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

from .doctor import FAIL, PASS, WARN, Check

SKIP = "SKIP"

#: table -> migration file that creates it (presence probes).
MIGRATION_TABLES: dict[str, str] = {
    "commands": "0002_commands.sql",
    "robot_telemetry": "0003_telemetry.sql",
    "maintenance_findings": "0004_maintenance.sql",
}

MIGRATE_FIX = ('run: python -m yantraops migrate --db-url '
               '"postgresql://postgres:<password>@db.<project-ref>'
               '.supabase.co:5432/postgres"')

DEFAULT_ENV_FILE = Path("/etc/yantrafleet.env")

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


# --------------------------------------------------------------------------
# Small local helpers (no I/O)
# --------------------------------------------------------------------------

def _headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def jwt_role(key: str) -> str | None:
    """The ``role`` claim of a Supabase JWT key, or None if not a JWT."""
    parts = key.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    role = claims.get("role") if isinstance(claims, dict) else None
    return str(role) if role is not None else None


def _is_local_url(url: str) -> bool:
    rest = url.split("://", 1)[-1]
    host = rest.split("/", 1)[0].rsplit(":", 1)[0] if not rest.startswith("[") \
        else rest.split("]", 1)[0] + "]"
    return host.strip("[]") in ("localhost", "127.0.0.1", "::1")


def _table_missing(status_code: int, body: str) -> bool:
    """Same PostgREST 'relation does not exist' heuristics as preflight."""
    return (status_code == 404
            or "PGRST205" in body
            or "42P01" in body
            or "does not exist" in body)


# --------------------------------------------------------------------------
# Remote probes (client injectable)
# --------------------------------------------------------------------------

def _probe_backend(base_url: str, key: str, client: Any) -> dict[str, Any]:
    """Run every anon-key probe; return a raw result dict.

    Keys: ``reachable`` (bool), ``error`` (str), ``read_status``,
    ``read_rows`` (list|None), ``write_status`` (int|None),
    ``tables`` ({table: (status, body)}).
    """
    rest = f"{base_url.rstrip('/')}/rest/v1"
    out: dict[str, Any] = {"reachable": False, "error": "", "read_status": None,
                           "read_rows": None, "write_status": None, "tables": {}}
    try:
        resp = client.get(f"{rest}/robots", params={"select": "id", "limit": "1"},
                          headers=_headers(key))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["reachable"] = True
    out["read_status"] = resp.status_code
    if resp.status_code == 200:
        try:
            out["read_rows"] = resp.json()
        except Exception:
            out["read_rows"] = None

    # Harmless write probe: unique alerts row, deleted right away.
    probe_id = f"audit-probe-{uuid.uuid4().hex[:12]}"
    row = {"id": probe_id, "sev": "info", "src": "yantraops-audit",
           "msg": "security audit write probe (safe to delete)", "ack": True}
    try:
        wresp = client.post(f"{rest}/alerts", json=[row], headers=_headers(key))
        out["write_status"] = wresp.status_code
    except Exception as exc:
        out["write_status"] = None
        out["write_error"] = f"{type(exc).__name__}: {exc}"
    if out["write_status"] in (200, 201, 204):
        try:  # best-effort cleanup; some backends refuse DELETE — fine.
            client.delete(f"{rest}/alerts", params={"id": f"eq.{probe_id}"},
                          headers=_headers(key))
        except Exception:
            pass

    # Migration-presence probes (only meaningful while anon can read).
    for table in MIGRATION_TABLES:
        try:
            tresp = client.get(f"{rest}/{table}", params={"limit": "1"},
                               headers=_headers(key))
            out["tables"][table] = (tresp.status_code, tresp.text[:200])
        except Exception as exc:
            out["tables"][table] = (None, f"{type(exc).__name__}: {exc}")
    return out


def classify_mode(probe: dict[str, Any]) -> str:
    """demo / hardened-read / rbac / no-schema — from the anon key's view."""
    if probe["read_status"] == 404 and probe["write_status"] == 404:
        # PostgREST 404 = relation does not exist: the project has no
        # Yantrika schema at all (migrations never applied). Distinct
        # from RLS lockdown, which answers 200-empty/401/403.
        return "no-schema"
    if probe["write_status"] in (200, 201, 204):
        return "demo"
    read = probe["read_status"]
    if read == 200 and probe["read_rows"]:
        return "hardened-read"
    # Anon locked out of reads (401/403), or reads answer but RLS filters
    # every row: either strict 0006 or 0007. Both mean "anon gets nothing".
    return "rbac"


# --------------------------------------------------------------------------
# Check builders
# --------------------------------------------------------------------------

def _remote_checks(base_url: str, key: str, client: Any) -> tuple[str, list[Check]]:
    """Probe the backend; return ``(mode, checks)`` — mode is ``demo`` /
    ``hardened-read`` / ``rbac`` / ``unknown`` (unreachable)."""
    probe = _probe_backend(base_url, key, client)
    if not probe["reachable"]:
        checks = [Check(FAIL, "backend", f"{base_url} unreachable "
                        f"({probe['error']})",
                        fix="check SUPABASE_URL / network; for a local demo "
                            "use `yantraops up --loopback`")]
        for name in ("rls-mode", "anon-write", "anon-read"):
            checks.append(Check(SKIP, name, "skipped — backend unreachable"))
        for table in MIGRATION_TABLES:
            checks.append(Check(SKIP, f"schema-{table}",
                                "skipped — backend unreachable"))
        return "unknown", checks

    mode = classify_mode(probe)
    checks = [Check(PASS, "backend", f"{base_url} reachable")]
    checks.append(Check(
        FAIL if mode == "no-schema" else PASS, "rls-mode",
        {"demo": "demo (anon key can read AND write — open by design)",
         "hardened-read": "hardened-read (anon reads work, writes refused — "
                          "0006 Option B posture)",
         "no-schema": "no schema — the robots table does not exist; run the "
                      "migrations first (yantraops migrate)",
         "rbac": "rbac/hardened (anon key gets nothing — 0006 strict or "
                 "0007 applied)"}[mode]))

    # Anon write probe.
    ws = probe["write_status"]
    if mode == "no-schema":
        checks.append(Check(FAIL, "anon-write",
                            "cannot probe — tables do not exist yet",
                            fix="apply the baseline migrations, then re-run "
                                "audit-security"))
    elif mode == "demo":
        checks.append(Check(
            FAIL, "anon-write",
            f"anon key INSERT into alerts succeeded (HTTP {ws}) — "
            "demo policies active",
            fix="apply supabase/0006_harden.sql or 0007_rbac.sql, then switch "
                "writers to the service_role key (docs/SECURITY.md)"))
    elif ws in (401, 403):
        checks.append(Check(PASS, "anon-write",
                            f"anon write refused (HTTP {ws})"))
    else:
        detail = (f"HTTP {ws}" if ws is not None
                  else probe.get("write_error", "no response"))
        checks.append(Check(WARN, "anon-write",
                            f"write probe inconclusive ({detail})",
                            fix="verify RLS policies manually in the Supabase "
                                "dashboard"))

    # Anon read probe — meaning depends on intent.
    rs = probe["read_status"]
    if mode == "no-schema":
        checks.append(Check(FAIL, "anon-read",
                            "cannot probe — tables do not exist yet",
                            fix="apply the baseline migrations, then re-run "
                                "audit-security"))
    elif mode == "demo":
        checks.append(Check(PASS, "anon-read",
                            f"anon can read robots (HTTP {rs}) — expected in "
                            "demo mode"))
    elif mode == "hardened-read":
        checks.append(Check(
            WARN, "anon-read",
            "anon key can read ALL fleet data (Option B read-only posture) — "
            "fine on trusted networks, not for sensitive data",
            fix="drop the hardened_read_anon policies or apply "
                "supabase/0007_rbac.sql for per-role access"))
    else:
        detail = (f"HTTP {rs}" if rs != 200
                  else "HTTP 200 but zero rows visible")
        checks.append(Check(PASS, "anon-read", f"anon read locked ({detail})"))

    # Migration presence.
    for table, migration in MIGRATION_TABLES.items():
        status_code, body = probe["tables"].get(table, (None, ""))
        name = f"schema-{table}"
        if status_code is None:
            checks.append(Check(SKIP, name, f"probe failed ({body})"))
        elif status_code == 200:
            checks.append(Check(PASS, name, f"table present ({migration})"))
        elif _table_missing(status_code, body):
            checks.append(Check(FAIL, name,
                                f"table missing — {migration} not applied "
                                f"(HTTP {status_code})", fix=MIGRATE_FIX))
        elif status_code in (401, 403):
            checks.append(Check(SKIP, name,
                                f"cannot verify with the anon key in {mode} "
                                f"mode (HTTP {status_code})"))
        else:
            checks.append(Check(WARN, name,
                                f"unexpected answer HTTP {status_code}",
                                fix="inspect the table in the Supabase "
                                    "dashboard"))
    return mode, checks


def check_key_class(key: str) -> Check:
    """Is the configured SUPABASE_KEY safe to ship client-side?"""
    if key.startswith("sb_secret_"):
        return Check(FAIL, "key-class",
                     "SUPABASE_KEY is a secret (service) key — never ship "
                     "service keys to browsers or client configs",
                     fix="use the publishable/anon key here; keep the service "
                         "key server-side only (docs/SECURITY.md)")
    role = jwt_role(key)
    if role == "service_role":
        return Check(FAIL, "key-class",
                     "SUPABASE_KEY is a service_role JWT — never ship service "
                     "keys to browsers or client configs",
                     fix="use the publishable/anon key here; keep the service "
                         "key server-side only (docs/SECURITY.md)")
    if role == "anon" or key.startswith("sb_publishable_"):
        return Check(PASS, "key-class", "publishable/anon key (client-safe)")
    return Check(WARN, "key-class",
                 "key format unrecognised — cannot confirm it is client-safe",
                 fix="use the publishable (anon) key from Settings -> API")


def check_sarathi_token(env: Mapping[str, str]) -> Check:
    if env.get("SARATHI_TOKEN"):
        return Check(PASS, "sarathi-token",
                     "SARATHI_TOKEN set — /ask requires bearer auth")
    return Check(WARN, "sarathi-token",
                 "SARATHI_TOKEN unset — the copilot /ask endpoint is open",
                 fix="export SARATHI_TOKEN=$(openssl rand -hex 32) and "
                     "restart sarathi")


def check_webhook_secret(env: Mapping[str, str]) -> Check:
    if not env.get("WEBHOOK_URL"):
        return Check(PASS, "webhook-secret",
                     "no webhook channel configured (WEBHOOK_URL unset)")
    if env.get("YANTRA_WEBHOOK_SECRET"):
        return Check(PASS, "webhook-secret",
                     "YANTRA_WEBHOOK_SECRET set — webhook posts are "
                     "HMAC-signed (X-Yantra-Signature)")
    return Check(WARN, "webhook-secret",
                 "webhook channel configured but YANTRA_WEBHOOK_SECRET unset "
                 "— receivers cannot verify the sender",
                 fix="export YANTRA_WEBHOOK_SECRET=$(openssl rand -hex 32) "
                     "and verify the X-Yantra-Signature header server-side")


def check_transport(base_url: str, env: Mapping[str, str]) -> Check:
    """WARN on plain http for any non-localhost configured URL."""
    urls = [("backend", base_url)]
    if env.get("WEBHOOK_URL"):
        urls.append(("WEBHOOK_URL", env["WEBHOOK_URL"]))
    insecure = [f"{label}: {u}" for label, u in urls
                if u.startswith("http://") and not _is_local_url(u)]
    if insecure:
        return Check(WARN, "https",
                     "plain http to a non-localhost host — credentials and "
                     "fleet data travel unencrypted (" + "; ".join(insecure) + ")",
                     fix="use https:// endpoints (Supabase is https by "
                         "default; put webhooks behind TLS)")
    return Check(PASS, "https",
                 "all configured non-localhost URLs use https (or are local)")


def check_env_file(path: Path = DEFAULT_ENV_FILE) -> Check | None:
    """Permissions of the secrets file, when present; None (silent) otherwise."""
    try:
        st = path.stat()
    except OSError:
        return None
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return Check(WARN, "env-file",
                     f"{path} is group/world accessible (mode {mode:03o}) — "
                     "it holds the service key and tokens",
                     fix=f"sudo chown root:root {path} && sudo chmod 600 {path}")
    return Check(PASS, "env-file", f"{path} permissions ok (mode {mode:03o})")


# --------------------------------------------------------------------------
# Assembly / report / CLI
# --------------------------------------------------------------------------

def gather_audit(
    url: str | None = None,
    key: str | None = None,
    *,
    client: Any = None,
    env: Mapping[str, str] | None = None,
    env_file: Path = DEFAULT_ENV_FILE,
) -> tuple[str, str, list[Check]]:
    """Run every check; return ``(base_url, mode_label, checks)``.

    ``client`` is any httpx.Client-compatible object (tests inject one built
    on ``httpx.MockTransport``); ``env`` an environ mapping; ``env_file``
    the secrets file to stat.
    """
    from .orchestrator import resolve_supabase
    e = env if env is not None else os.environ
    base_url, resolved_key = resolve_supabase(url, key)

    owns_client = client is None
    if owns_client:
        import httpx
        client = httpx.Client(timeout=5.0)
    try:
        mode, checks = _remote_checks(base_url, resolved_key, client)
    finally:
        if owns_client:
            client.close()

    checks.append(check_key_class(resolved_key))
    checks.append(check_sarathi_token(e))
    checks.append(check_webhook_secret(e))
    checks.append(check_transport(base_url, e))
    env_check = check_env_file(env_file)
    if env_check is not None:
        checks.append(env_check)
    return base_url, mode, checks


def summarize(checks: list[Check]) -> tuple[dict[str, int], int]:
    counts = {s: sum(1 for c in checks if c.status == s)
              for s in (PASS, WARN, FAIL, SKIP)}
    code = 2 if counts[FAIL] else (1 if counts[WARN] else 0)
    return counts, code


def format_audit_report(base_url: str, mode: str, checks: list[Check]) -> tuple[str, int]:
    """Render PASS/WARN/FAIL lines + remediation + summary; return (text, exit)."""
    lines = ["yantraops audit-security — backend + local posture", "",
             f"  backend: {base_url}", f"  mode:    {mode}", ""]
    width = max(len(c.name) for c in checks) + 2
    for c in checks:
        lines.append(f"  [{c.status}] {c.name:<{width}} {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"  {'':<{width + 9}}-> {c.fix}")
    counts, code = summarize(checks)
    lines.append("")
    lines.append(f"summary: {counts[PASS]} pass, {counts[WARN]} warn, "
                 f"{counts[FAIL]} fail, {counts[SKIP]} skipped")
    if code == 0:
        lines.append("verdict: no security findings")
    elif code == 1:
        lines.append("verdict: warnings only — review the -> lines above")
    else:
        lines.append("verdict: FAILING checks present — fix the -> lines "
                     "above before going beyond demo")
    return "\n".join(lines), code


def audit_json(base_url: str, mode: str, checks: list[Check]) -> dict[str, Any]:
    counts, code = summarize(checks)
    return {
        "backend": base_url,
        "mode": mode,
        "checks": [{"status": c.status, "name": c.name,
                    "detail": c.detail, "fix": c.fix} for c in checks],
        "summary": counts,
        "exit_code": code,
    }


def run_audit(
    url: str | None = None,
    key: str | None = None,
    *,
    json_output: bool = False,
    client: Any = None,
    env: Mapping[str, str] | None = None,
    env_file: Path = DEFAULT_ENV_FILE,
) -> int:
    """Entry point for ``python -m yantraops audit-security``."""
    base_url, mode, checks = gather_audit(url, key, client=client, env=env,
                                          env_file=env_file)
    if json_output:
        print(json.dumps(audit_json(base_url, mode, checks), indent=2))
        _, code = summarize(checks)
        return code
    text, code = format_audit_report(base_url, mode, checks)
    print(text)
    return code


def add_audit_parser(sub: argparse._SubParsersAction) -> None:
    aud = sub.add_parser(
        "audit-security",
        help="probe the configured backend's RLS mode, key class, secrets "
             "and schema; PASS/WARN/FAIL per check")
    aud.add_argument("--url", default=None,
                     help="Supabase URL (default: env/embedded, like `up`)")
    aud.add_argument("--key", default=None,
                     help="Supabase anon key (default: env/embedded)")
    aud.add_argument("--json", action="store_true",
                     help="machine-readable JSON report on stdout")
