"""Offline unit tests for the migration planner/tracker — no postgres needed.

A FakeBackend records which files were "executed"; the real PsycopgBackend
is never constructed here.
"""
from __future__ import annotations

import pytest

from yantraops.migrate import (checksum_sql, discover_migrations,
                               plan_migrations, run_migrate)


class FakeBackend:
    """Records executed files; optionally raises on a chosen filename."""

    def __init__(self, applied: dict[str, str] | None = None,
                 fail_on: str | None = None):
        self.applied_rows = dict(applied or {})
        self.executed: list[str] = []
        self.fail_on = fail_on
        self.tracking_ensured = False
        self.closed = False

    def ensure_tracking(self):
        self.tracking_ensured = True

    def applied(self):
        return dict(self.applied_rows)

    def apply(self, filename, sql, checksum):
        if filename == self.fail_on:
            raise RuntimeError("syntax error at or near \"BOOM\"")
        self.executed.append(filename)
        self.applied_rows[filename] = checksum

    def close(self):
        self.closed = True


@pytest.fixture()
def migdir(tmp_path):
    d = tmp_path / "supabase"
    d.mkdir()
    # written out of order on purpose; discovery must sort by filename
    (d / "0002_commands.sql").write_text("create table b (x int);")
    (d / "0001_init.sql").write_text("create table a (x int);")
    (d / "0010_later.sql").write_text("create table j (x int);")
    (d / "notes.txt").write_text("not sql")
    return d


def test_discovery_sorts_by_filename(migdir):
    names = [p.name for p in discover_migrations(migdir)]
    assert names == ["0001_init.sql", "0002_commands.sql", "0010_later.sql"]


def test_plan_fresh_database_applies_everything(migdir):
    plan = plan_migrations(discover_migrations(migdir), applied={})
    assert [i.action for i in plan] == ["apply"] * 3


def test_plan_skips_applied_and_warns_on_drift(migdir):
    files = discover_migrations(migdir)
    good = checksum_sql((migdir / "0001_init.sql").read_text())
    applied = {"0001_init.sql": good, "0002_commands.sql": "stale-checksum"}
    plan = plan_migrations(files, applied)
    by_name = {i.filename: i for i in plan}
    assert by_name["0001_init.sql"].action == "skip"
    assert by_name["0002_commands.sql"].action == "skip-drift"
    assert "checksum" in by_name["0002_commands.sql"].note
    assert by_name["0010_later.sql"].action == "apply"
    # --force turns the drifted file into a reapply
    forced = {i.filename: i for i in plan_migrations(files, applied, force=True)}
    assert forced["0002_commands.sql"].action == "reapply"
    assert forced["0001_init.sql"].action == "skip"


def test_run_migrate_applies_in_order_and_summarizes(migdir, capsys):
    backend = FakeBackend()
    rc = run_migrate("postgresql://x", migdir, backend_factory=lambda url: backend)
    assert rc == 0
    assert backend.tracking_ensured and backend.closed
    assert backend.executed == ["0001_init.sql", "0002_commands.sql",
                                "0010_later.sql"]
    assert "3 applied, 0 skipped" in capsys.readouterr().out


def test_run_migrate_skips_already_applied(migdir, capsys):
    cs = checksum_sql((migdir / "0001_init.sql").read_text())
    backend = FakeBackend(applied={"0001_init.sql": cs})
    rc = run_migrate("postgresql://x", migdir, backend_factory=lambda url: backend)
    assert rc == 0
    assert backend.executed == ["0002_commands.sql", "0010_later.sql"]
    assert "2 applied, 1 skipped" in capsys.readouterr().out


def test_run_migrate_stops_on_failure_and_names_file(migdir, capsys):
    backend = FakeBackend(fail_on="0002_commands.sql")
    rc = run_migrate("postgresql://x", migdir, backend_factory=lambda url: backend)
    assert rc == 1
    # earlier file applied, later file never attempted
    assert backend.executed == ["0001_init.sql"]
    err = capsys.readouterr().err
    assert "FAILED in 0002_commands.sql" in err
    assert "psql" in err  # the debug hint


def test_run_migrate_dry_run_touches_nothing(migdir, capsys):
    backend = FakeBackend()
    rc = run_migrate("postgresql://x", migdir, dry_run=True,
                     backend_factory=lambda url: backend)
    assert rc == 0
    assert backend.executed == []
    out = capsys.readouterr().out
    assert "3 pending" in out and "0001_init.sql" in out


def test_run_migrate_drift_without_force_warns_and_skips(migdir, capsys):
    backend = FakeBackend(applied={"0001_init.sql": "stale"})
    rc = run_migrate("postgresql://x", migdir, backend_factory=lambda url: backend)
    assert rc == 0
    assert "0001_init.sql" not in backend.executed
    out = capsys.readouterr().out
    assert "WARNING" in out and "--force" in out
    assert "2 applied, 1 skipped" in out


def test_run_migrate_requires_db_url(migdir, capsys):
    rc = run_migrate(None, migdir, backend_factory=FakeBackend)
    assert rc == 1
    assert "--db-url is required" in capsys.readouterr().err


def test_print_order_needs_no_backend(migdir, capsys):
    def exploding_factory(url):  # pragma: no cover - must never be called
        raise AssertionError("backend must not be constructed for --print-order")
    rc = run_migrate(None, migdir, print_order=True,
                     backend_factory=exploding_factory)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("0001_init.sql") < out.index("0002_commands.sql")


def test_cli_print_order_on_real_repo(capsys):
    """`python -m yantraops migrate --print-order` against the checked-in repo."""
    from yantraops.__main__ import main
    assert main(["migrate", "--print-order"]) == 0
    out = capsys.readouterr().out
    assert "0001_init.sql" in out and "0005_sites.sql" in out
    assert out.index("0001_init.sql") < out.index("0005_sites.sql")


def test_opt_in_migrations_gated_by_default(tmp_path):
    from yantraops.migrate import discover_migrations
    (tmp_path / "0001_base.sql").write_text("create table t(x int);")
    (tmp_path / "0006_lock.sql").write_text("-- Something OPT-IN lockdown\nselect 1;")
    names = [p.name for p in discover_migrations(tmp_path)]
    assert names == ["0001_base.sql"]
    names_all = [p.name for p in discover_migrations(tmp_path, include_opt_in=True)]
    assert names_all == ["0001_base.sql", "0006_lock.sql"]


#: Every migration that must be gated behind --include-opt-in. 0006/0007
#: lock the project down; 0008 needs 0007's yf_has_role; 0009-0016 all build
#: on 0007 (and 0009's yf_can_read_site), and 0009 in particular hands
#: anonymous visitors a scoped write path — none of them may ever be applied
#: to a demo project by a plain `yantraops migrate`.
OPT_IN_MIGRATIONS = (
    "0006_harden.sql",
    "0007_rbac.sql",
    "0008_app_settings.sql",
    "0009_demo_sandbox.sql",
    "0010_share_links.sql",
    "0011_replay.sql",
    "0012_utilization.sql",
    "0013_maintenance_explain.sql",
    "0014_inbound_ops.sql",
    "0015_certification.sql",
    "0016_public_status.sql",
    "0017_demo_command_scope.sql",
)

#: The baseline files a plain `migrate` run applies. Listed explicitly so a
#: new migration cannot silently join the default set by forgetting its
#: OPT-IN banner — the equality assertion below fails instead.
BASELINE_MIGRATIONS = (
    "0001_init.sql",
    "0002_commands.sql",
    "0003_telemetry.sql",
    "0004_maintenance.sql",
    "0005_sites.sql",
)


def test_repo_opt_in_files_detected():
    from yantraops.migrate import default_migrations_dir, discover_migrations
    d = default_migrations_dir()
    base = {p.name for p in discover_migrations(d)}
    every = {p.name for p in discover_migrations(d, include_opt_in=True)}
    gated = every - base
    for name in OPT_IN_MIGRATIONS:
        assert name in gated, f"{name} must carry an OPT-IN banner"
    assert "0001_init.sql" in base


def test_baseline_set_is_exactly_the_unlocked_files():
    """A new migration must not join the default set by accident."""
    from yantraops.migrate import default_migrations_dir, discover_migrations
    d = default_migrations_dir()
    assert {p.name for p in discover_migrations(d)} == set(BASELINE_MIGRATIONS)


def test_every_repo_migration_is_baseline_or_opt_in():
    """No migration file is unaccounted for by the two lists above."""
    from yantraops.migrate import default_migrations_dir, discover_migrations
    d = default_migrations_dir()
    every = {p.name for p in discover_migrations(d, include_opt_in=True)}
    assert every == set(BASELINE_MIGRATIONS) | set(OPT_IN_MIGRATIONS)


def test_opt_in_marker_is_inside_the_scanned_header():
    """``is_opt_in`` only reads the first 400 characters, so a banner that
    drifts below that line silently un-gates the file. Assert the marker is
    genuinely within the scanned window for every gated migration."""
    from yantraops.migrate import (OPT_IN_MARKER, default_migrations_dir,
                                   is_opt_in)
    d = default_migrations_dir()
    for name in OPT_IN_MIGRATIONS:
        path = d / name
        assert path.exists(), f"{name} is missing from supabase/"
        assert is_opt_in(path)
        head = path.read_text(encoding="utf-8", errors="replace")[:400]
        assert OPT_IN_MARKER in head


def test_opt_in_migrations_carry_a_rollback_block():
    """Repo convention (supabase/README.md): every opt-in migration ends in a
    commented ROLLBACK block that restores the previous posture."""
    from yantraops.migrate import default_migrations_dir
    d = default_migrations_dir()
    for name in OPT_IN_MIGRATIONS:
        text = (d / name).read_text(encoding="utf-8", errors="replace")
        assert "ROLLBACK" in text, f"{name} has no ROLLBACK block"
        tail = text[text.index("ROLLBACK"):]
        assert "-- drop " in tail or "-- do $$" in tail or "-- update " in tail, (
            f"{name}'s ROLLBACK block has no commented statements")


def test_new_migrations_are_ordered_after_rbac():
    """0009-0016 all depend on 0007 (and 0009's helpers). Filename order is
    the ordering contract, so assert they sort after it."""
    from yantraops.migrate import default_migrations_dir, discover_migrations
    names = [p.name for p in
             discover_migrations(default_migrations_dir(), include_opt_in=True)]
    rbac = names.index("0007_rbac.sql")
    for name in ("0009_demo_sandbox.sql", "0010_share_links.sql",
                 "0011_replay.sql", "0012_utilization.sql",
                 "0013_maintenance_explain.sql", "0014_inbound_ops.sql",
                 "0015_certification.sql", "0016_public_status.sql",
                 "0017_demo_command_scope.sql"):
        assert names.index(name) > rbac
