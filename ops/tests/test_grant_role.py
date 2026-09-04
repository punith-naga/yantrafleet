"""Offline unit tests for ``yantraops grant-role`` — no postgres needed.

A FakeBackend stands in for PsycopgGrantBackend; the real backend's lazy
psycopg import means importing grant_role.py itself never needs psycopg
either (mirrors test_migrate.py's approach for the same reason).
"""
from __future__ import annotations

import pytest

from yantraops.grant_role import ROLES, run_grant_role


class FakeBackend:
    """Records grant() calls; ``users`` maps email -> user_id (auth.users)."""

    def __init__(self, users: dict[str, str] | None = None,
                 raise_on_grant: Exception | None = None):
        self.users = dict(users or {})
        self.granted: list[tuple[str, str, str]] = []
        self.raise_on_grant = raise_on_grant
        self.closed = False

    def grant(self, email, role, site):
        if self.raise_on_grant is not None:
            raise self.raise_on_grant
        uid = self.users.get(email)
        if uid is not None:
            self.granted.append((email, role, site))
        return uid

    def close(self):
        self.closed = True


def _factory(backend):
    return lambda db_url: backend


def test_grants_role_to_existing_user(capsys):
    backend = FakeBackend(users={"lead@example.com": "u-1"})
    code = run_grant_role("postgresql://x", "lead@example.com", "admin",
                          backend_factory=_factory(backend))
    assert code == 0
    assert backend.granted == [("lead@example.com", "admin", "BLR-DC1")]
    assert backend.closed
    out = capsys.readouterr().out
    assert "granted 'admin'" in out
    assert "lead@example.com" in out
    assert "u-1" in out


def test_grants_role_at_a_specific_site(capsys):
    backend = FakeBackend(users={"eng@example.com": "u-2"})
    code = run_grant_role("postgresql://x", "eng@example.com", "engineer",
                          "PUNE-DC2", backend_factory=_factory(backend))
    assert code == 0
    assert backend.granted == [("eng@example.com", "engineer", "PUNE-DC2")]


def test_unknown_email_fails_with_signup_hint(capsys):
    backend = FakeBackend(users={})
    code = run_grant_role("postgresql://x", "nobody@example.com", "admin",
                          backend_factory=_factory(backend))
    assert code == 1
    assert backend.granted == []
    assert backend.closed  # still cleaned up
    err = capsys.readouterr().err
    assert "no auth.users row" in err
    assert "sign up" in err


def test_missing_db_url_fails_before_connecting(capsys):
    code = run_grant_role(None, "lead@example.com", "admin")
    assert code == 1
    assert "--db-url is required" in capsys.readouterr().err


def test_missing_email_fails_before_connecting(capsys):
    code = run_grant_role("postgresql://x", None, "admin")
    assert code == 1
    assert "--email is required" in capsys.readouterr().err


@pytest.mark.parametrize("bad_role", ["superadmin", "Admin", "", "root"])
def test_invalid_role_rejected_before_connecting(bad_role, capsys):
    code = run_grant_role("postgresql://x", "lead@example.com", bad_role)
    assert code == 1
    err = capsys.readouterr().err
    assert "--role must be one of" in err
    for r in ROLES:
        assert r in err


def test_user_roles_table_missing_gives_migrate_hint(capsys):
    backend = FakeBackend(
        raise_on_grant=Exception('relation "public.user_roles" does not exist'))
    code = run_grant_role("postgresql://x", "lead@example.com", "admin",
                          backend_factory=_factory(backend))
    assert code == 1
    err = capsys.readouterr().err
    assert "user_roles does not exist" in err
    assert "--include-opt-in" in err


def test_unexpected_backend_error_surfaces_and_still_closes(capsys):
    backend = FakeBackend(raise_on_grant=RuntimeError("connection reset"))
    code = run_grant_role("postgresql://x", "lead@example.com", "admin",
                          backend_factory=_factory(backend))
    assert code == 1
    assert backend.closed
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "connection reset" in err


def test_backend_construction_failure_reports_install_hint(capsys):
    def factory(db_url):
        raise RuntimeError("yantraops grant-role needs psycopg. Install it with:")
    code = run_grant_role("postgresql://x", "lead@example.com", "admin",
                          backend_factory=factory)
    assert code == 1
    assert "needs psycopg" in capsys.readouterr().err


def test_connection_failure_reports_actionable_message(capsys):
    def factory(db_url):
        raise ConnectionRefusedError("could not connect to server")
    code = run_grant_role("postgresql://x", "lead@example.com", "admin",
                          backend_factory=factory)
    assert code == 1
    err = capsys.readouterr().err
    assert "could not connect to the database" in err
    assert "could not connect to server" in err


def test_all_four_roles_accepted():
    for role in ROLES:
        backend = FakeBackend(users={"u@example.com": "u-x"})
        code = run_grant_role("postgresql://x", "u@example.com", role,
                              backend_factory=_factory(backend))
        assert code == 0, f"role {role!r} should be accepted"


def test_cli_wiring_parses_grant_role_with_defaults():
    """`python -m yantraops grant-role ...` argparse wiring: role defaults to
    admin (the bootstrap use case), site defaults to DEFAULT_SITE, and
    unknown --role values are rejected by argparse itself (choices=ROLES)
    before run_grant_role ever sees them."""
    from yantraops.__main__ import build_parser
    from yantraops.grant_role import DEFAULT_SITE

    args = build_parser().parse_args(
        ["grant-role", "--db-url", "postgresql://x", "--email", "a@b.com"])
    assert args.command == "grant-role"
    assert args.db_url == "postgresql://x"
    assert args.email == "a@b.com"
    assert args.role == "admin"          # bootstrap default
    assert args.site == DEFAULT_SITE

    args2 = build_parser().parse_args(
        ["grant-role", "--db-url", "x", "--email", "e@x.com",
         "--role", "engineer", "--site", "PUNE-DC2"])
    assert args2.role == "engineer"
    assert args2.site == "PUNE-DC2"

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["grant-role", "--db-url", "x", "--email", "e@x.com",
             "--role", "superadmin"])


def test_regrant_updates_role_idempotently(capsys):
    """Calling grant-role twice (e.g. promoting operator -> admin) is just
    two calls to the same idempotent upsert -- the CLI has no separate
    'update' verb, on-conflict-do-update handles it."""
    backend = FakeBackend(users={"lead@example.com": "u-1"})
    first = run_grant_role("postgresql://x", "lead@example.com", "operator",
                           backend_factory=_factory(backend))
    second = run_grant_role("postgresql://x", "lead@example.com", "admin",
                            backend_factory=_factory(backend))
    assert first == 0 and second == 0
    assert backend.granted == [
        ("lead@example.com", "operator", "BLR-DC1"),
        ("lead@example.com", "admin", "BLR-DC1"),
    ]
