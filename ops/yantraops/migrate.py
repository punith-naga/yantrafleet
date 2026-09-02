"""``yantraops migrate`` — apply ``supabase/*.sql`` to a real Postgres DB.

Design notes
------------
* Files are applied in **filename order** (``sorted()`` on the basename), so
  the ``0001_...`` / ``0002_...`` prefixes are the ordering contract.
* Applied files are tracked in ``_yf_migrations(filename pk, applied_at,
  checksum)``.  A file whose checksum matches is skipped; a file whose
  checksum *changed* since it was applied produces a warning and is only
  reapplied under ``--force``.
* Never partial: each file runs in its own transaction, and the tracking row
  is written inside the same transaction, so a failing file leaves the DB
  exactly as it was before that file.
* The SQL-executing backend is injectable (``backend_factory``) so the
  planner/tracking logic is unit-testable offline with a fake backend.
  ``psycopg`` is imported lazily inside :class:`PsycopgBackend`, which keeps
  ``migrate --print-order`` working with zero third-party deps.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

TRACKING_TABLE = "_yf_migrations"

TRACKING_DDL = f"""\
create table if not exists {TRACKING_TABLE} (
  filename   text primary key,
  applied_at timestamptz not null default now(),
  checksum   text not null
)"""

PSQL_HINT = (
    "hint: to debug interactively, run the failing file yourself:\n"
    '      psql "<your --db-url>" -f {path}\n'
    "      (or paste its contents into the Supabase dashboard SQL editor)"
)

INSTALL_HINT = (
    "yantraops migrate needs psycopg. Install it with:\n"
    '  pip install --break-system-packages "psycopg[binary]>=3.1"\n'
    "  (or: pip install -e ops/)"
)


# --------------------------------------------------------------------------
# Discovery / checksums
# --------------------------------------------------------------------------

def default_migrations_dir() -> Path:
    """``<repo>/supabase`` — located the same way the orchestrator finds the repo."""
    from .orchestrator import repo_root  # stdlib-only import chain
    return repo_root() / "supabase"


def discover_migrations(directory: Path) -> list[Path]:
    """All ``*.sql`` files in ``directory``, sorted by filename."""
    return sorted(
        (p for p in Path(directory).glob("*.sql") if p.is_file()),
        key=lambda p: p.name)


def checksum_sql(sql: str) -> str:
    """sha256 hex digest of a migration file's text (newline-normalized)."""
    canonical = sql.replace("\r\n", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------

@dataclass
class PlanItem:
    """One migration file and what we intend to do with it."""

    path: Path
    sql: str
    checksum: str
    action: str          # "apply" | "skip" | "reapply" | "skip-drift"
    note: str = ""

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def pending(self) -> bool:
        return self.action in ("apply", "reapply")


def plan_migrations(
    files: list[Path],
    applied: dict[str, str],
    force: bool = False,
) -> list[PlanItem]:
    """Decide, per file, whether to apply, skip, or warn about drift.

    ``applied`` maps filename -> checksum recorded in ``_yf_migrations``.
    """
    plan: list[PlanItem] = []
    for path in files:
        sql = path.read_text(encoding="utf-8")
        cs = checksum_sql(sql)
        recorded = applied.get(path.name)
        if recorded is None:
            item = PlanItem(path, sql, cs, "apply")
        elif recorded == cs:
            item = PlanItem(path, sql, cs, "skip", "already applied")
        elif force:
            item = PlanItem(path, sql, cs, "reapply",
                            "checksum changed since first applied (--force)")
        else:
            item = PlanItem(
                path, sql, cs, "skip-drift",
                "WARNING: file changed since it was applied "
                "(checksum mismatch) — rerun with --force to reapply")
        plan.append(item)
    return plan


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class MigrationBackend(Protocol):
    """What the runner needs from a database backend."""

    def ensure_tracking(self) -> None: ...
    def applied(self) -> dict[str, str]: ...
    def apply(self, filename: str, sql: str, checksum: str) -> None: ...
    def close(self) -> None: ...


class PsycopgBackend:
    """Real Postgres backend (psycopg 3). Each ``apply`` is one transaction."""

    def __init__(self, db_url: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised via message
            raise RuntimeError(INSTALL_HINT) from exc
        self._psycopg = psycopg
        self._conn = psycopg.connect(db_url, autocommit=False)

    def ensure_tracking(self) -> None:
        with self._conn.transaction():
            self._conn.execute(TRACKING_DDL)

    def applied(self) -> dict[str, str]:
        with self._conn.transaction():
            rows = self._conn.execute(
                f"select filename, checksum from {TRACKING_TABLE}").fetchall()
        return {filename: checksum for filename, checksum in rows}

    def apply(self, filename: str, sql: str, checksum: str) -> None:
        # One transaction per file: the DDL and its tracking row commit
        # together or not at all.  psycopg3 uses the simple query protocol
        # for parameterless execute(), so multi-statement files are fine.
        with self._conn.transaction():
            self._conn.execute(sql)
            self._conn.execute(
                f"insert into {TRACKING_TABLE} (filename, checksum) "
                "values (%s, %s) "
                "on conflict (filename) do update "
                "  set checksum = excluded.checksum, applied_at = now()",
                (filename, checksum))

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run_migrate(
    db_url: str | None,
    migrations_dir: Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    print_order: bool = False,
    backend_factory: Callable[[str], Any] | None = None,
) -> int:
    """Entry point for ``python -m yantraops migrate``. Returns an exit code."""
    directory = Path(migrations_dir) if migrations_dir else default_migrations_dir()
    if not directory.is_dir():
        print(f"yantraops migrate: no such migrations directory: {directory}",
              file=sys.stderr)
        return 1
    files = discover_migrations(directory)
    if not files:
        print(f"yantraops migrate: no *.sql files found in {directory}",
              file=sys.stderr)
        return 1

    if print_order:  # zero-deps path: no DB, no psycopg
        for i, path in enumerate(files, 1):
            print(f"{i:>3}. {path.name}")
        return 0

    if not db_url:
        print("yantraops migrate: --db-url is required (see supabase/README.md "
              "for where to find it in the Supabase dashboard)", file=sys.stderr)
        return 1

    factory = backend_factory or PsycopgBackend
    try:
        backend = factory(db_url)
    except RuntimeError as exc:
        print(f"yantraops migrate: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"yantraops migrate: could not connect to the database:\n"
              f"  {type(exc).__name__}: {exc}\n"
              "  check the --db-url (host, password, port 5432) and that your "
              "network allows outbound Postgres connections", file=sys.stderr)
        return 1

    try:
        backend.ensure_tracking()
        applied = backend.applied()
        plan = plan_migrations(files, applied, force=force)

        for item in plan:
            if item.note and item.action == "skip-drift":
                print(f"  ! {item.filename}: {item.note}")

        if dry_run:
            pending = [i for i in plan if i.pending]
            if pending:
                print("pending (would apply in this order):")
                for item in pending:
                    print(f"  -> {item.filename}"
                          + (f"  [{item.note}]" if item.note else ""))
            else:
                print("nothing pending — database is up to date.")
            skipped = len(plan) - len(pending)
            print(f"dry run: {len(pending)} pending, {skipped} already applied.")
            return 0

        n_applied = n_skipped = 0
        for item in plan:
            if not item.pending:
                n_skipped += 1
                continue
            verb = "reapplying" if item.action == "reapply" else "applying"
            print(f"  {verb} {item.filename} ...", flush=True)
            try:
                backend.apply(item.filename, item.sql, item.checksum)
            except Exception as exc:
                print(
                    f"\nyantraops migrate: FAILED in {item.filename}\n"
                    f"  {type(exc).__name__}: {exc}\n"
                    "  nothing from this file was committed (one transaction "
                    "per file); earlier files remain applied.\n"
                    "  " + PSQL_HINT.format(path=item.path),
                    file=sys.stderr)
                return 1
            n_applied += 1

        print(f"done: {n_applied} applied, {n_skipped} skipped.")
        return 0
    finally:
        try:
            backend.close()
        except Exception:
            pass
