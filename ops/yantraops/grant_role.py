"""``yantraops grant-role`` — grant (or update) a user's RBAC role via a real
Postgres connection, without hand-editing SQL in the Supabase dashboard.

Why this exists
----------------
``supabase/0007_rbac.sql`` locks ``public.user_roles`` down so only an
*existing* admin can write to it through the API (``rbac_user_roles_admin_all``).
That is correct and intentional, but it creates a chicken-and-egg problem for
the very first admin: nobody has an admin role yet, so nobody can grant one
through the normal path. Until now the documented fix
(``docs/SECURITY.md`` "Seeding roles", the commented block at the bottom of
``0007_rbac.sql``) was "uncomment this SQL and paste it into the Supabase
SQL Editor by hand" — which works, but is exactly the kind of manual,
easy-to-skip step that leaves a fresh production deploy sitting in
demo-open mode indefinitely rather than actually being hardened. This module
turns that into a real, tested, one-line command:

    python -m yantraops grant-role --db-url "<postgres-url>" \\
        --email you@example.com --role admin

Same injectable-backend pattern as ``migrate.py`` (offline-testable with a
fake backend; ``psycopg`` imported lazily so importing this module costs
nothing extra). Reuses the same ``--db-url`` you already have for
``migrate`` — this needs the direct Postgres connection (not the anon/
service_role API keys), because it writes ``auth.users``-joined rows that
the RBAC policies themselves would otherwise block anyone but an admin from
writing — exactly the bootstrap gap this closes.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Protocol

#: Mirrors the hierarchy documented in docs/SECURITY.md and enforced by
#: 0007_rbac.sql's yf_rank()/yf_has_role() — kept as a plain tuple (not
#: imported from SQL) since this is argparse-time validation, not a
#: database round trip.
ROLES = ("operator", "engineer", "manager", "admin")

DEFAULT_SITE = "BLR-DC1"

INSTALL_HINT = (
    "yantraops grant-role needs psycopg. Install it with:\n"
    '  pip install --break-system-packages "psycopg[binary]>=3.1"\n'
    "  (or: pip install -e ops/)"
)

NO_USER_ROLES_TABLE_HINT = (
    "public.user_roles does not exist yet — apply supabase/0007_rbac.sql "
    "first:\n"
    "  python -m yantraops migrate --db-url <url> --include-opt-in"
)


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------

class GrantBackend(Protocol):
    """What the runner needs from a database backend."""

    def grant(self, email: str, role: str, site: str) -> str | None:
        """Grant ``role`` at ``site`` to the auth.users row for ``email``.

        Returns the granted user's id, or ``None`` if no auth.users row
        matches that email (they haven't signed up yet).
        """
        ...

    def close(self) -> None: ...


class PsycopgGrantBackend:
    """Real Postgres backend (psycopg 3)."""

    def __init__(self, db_url: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised via message
            raise RuntimeError(INSTALL_HINT) from exc
        self._psycopg = psycopg
        self._conn = psycopg.connect(db_url, autocommit=False)

    def grant(self, email: str, role: str, site: str) -> str | None:
        with self._conn.transaction():
            # user_roles missing entirely (0007 never applied) surfaces here
            # as UndefinedTable — translated to NO_USER_ROLES_TABLE_HINT by
            # run_grant_role rather than a raw psycopg traceback.
            row = self._conn.execute(
                """
                insert into public.user_roles (user_id, role, site_id)
                select id, %(role)s, %(site)s
                  from auth.users
                 where email = %(email)s
                on conflict (user_id, site_id) do update
                  set role = excluded.role
                returning user_id
                """,
                {"role": role, "site": site, "email": email},
            ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run_grant_role(
    db_url: str | None,
    email: str | None,
    role: str,
    site: str = DEFAULT_SITE,
    *,
    backend_factory: Callable[[str], Any] | None = None,
) -> int:
    """Entry point for ``python -m yantraops grant-role``. Returns an exit code."""
    if not db_url:
        print("yantraops grant-role: --db-url is required (the same Postgres "
              "URL you used for `migrate` — see supabase/README.md)",
              file=sys.stderr)
        return 1
    if not email:
        print("yantraops grant-role: --email is required", file=sys.stderr)
        return 1
    if role not in ROLES:
        print(f"yantraops grant-role: --role must be one of {', '.join(ROLES)} "
              f"(got {role!r})", file=sys.stderr)
        return 1

    factory = backend_factory or PsycopgGrantBackend
    try:
        backend = factory(db_url)
    except RuntimeError as exc:
        print(f"yantraops grant-role: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"yantraops grant-role: could not connect to the database:\n"
              f"  {type(exc).__name__}: {exc}\n"
              "  check the --db-url (host, password, port 5432) and that your "
              "network allows outbound Postgres connections", file=sys.stderr)
        return 1

    try:
        try:
            user_id = backend.grant(email, role, site)
        except Exception as exc:
            msg = str(exc)
            if "user_roles" in msg and (
                "does not exist" in msg or "UndefinedTable" in type(exc).__name__):
                print(f"yantraops grant-role: {NO_USER_ROLES_TABLE_HINT}",
                      file=sys.stderr)
                return 1
            print(f"yantraops grant-role: FAILED\n  {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

        if user_id is None:
            print(f"yantraops grant-role: no auth.users row for {email!r} — "
                  "they need to sign up (or sign in once) before a role can "
                  "be granted; then re-run this command", file=sys.stderr)
            return 1

        print(f"granted {role!r} at site {site!r} to {email} (user_id={user_id})")
        return 0
    finally:
        try:
            backend.close()
        except Exception:
            pass
