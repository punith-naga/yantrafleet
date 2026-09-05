"""v0.17 SECURITY: the simulator's command poller is site-scoped.

THE ATTACK, reproduced end to end against a real PostgreSQL 16 with
supabase/0001-0016 applied before this fix landed:

    -- as the anon (publishable) key, holding a live demo token
    insert into public.commands
      (id, robot_id, cmd, status, requested_by, site_id)
    values (gen_random_uuid(), 'AMR-01', 'estop', 'approved',
            'demo-visitor', public.yf_demo_site());
    INSERT 0 1                       -- accepted

``AMR-01`` is a REAL robot in a REAL site. 0009's ``demo_sandbox_insert``
policy checked only ``site_id``, and ``commands.robot_id`` is a bare
``text`` column with no foreign key (0002), so an anonymous internet
visitor could queue a pre-approved emergency stop naming any robot id
they liked. The row stayed inside their throwaway ``DEMO-*`` site, so
nothing in the database was harmed — the exposure was entirely here, in
the executor: :meth:`SupabaseTransport.poll_commands` fetched
``commands?status=eq.approved`` with NO site filter, normally holding a
key that bypasses RLS, and applied whatever came back.

supabase/0017_demo_command_scope.sql closes the insert side. These tests
cover the execute side, which has to hold even when the row already
exists (written under 0009, or on a project that never applied 0017).

Fully offline: httpx.MockTransport, nothing leaves the process.
"""
from __future__ import annotations

import json

import httpx
import pytest

from yantrasim.transports.supabase import (
    ANY_SITE, SupabaseTransport, resolve_site,
)

SITE = "BLR-DC1"            # 0005's site_id column default
OTHER = "PNQ-DC2"
DEMO = "DEMO-DEADBEEF01"    # a throwaway sandbox site (0009 namespace)

#: The forged row, exactly as the attack above writes it.
FORGED = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
          "robot_id": "AMR-01", "cmd": "estop", "site_id": DEMO}


class Backend:
    """The PostgREST subset ``poll_commands`` uses.

    ``honour_filters`` off models a backend that ignores the query string
    (a stale PostgREST, a proxy that ate it, a stand-in) — the case the
    client-side re-check exists for.
    """

    def __init__(self, rows: list[dict], *, honour_filters: bool = True) -> None:
        self.rows = rows
        self.honour_filters = honour_filters
        self.gets: list[httpx.Request] = []
        self.patches: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets.append(request)
            rows = self.rows
            want = request.url.params.get("site_id")
            if self.honour_filters and want:
                rows = [r for r in rows
                        if f"eq.{r.get('site_id', SITE)}" == want]
            return httpx.Response(200, json=rows)
        if request.method == "PATCH":
            self.patches.append((str(request.url),
                                 json.loads(request.content)))
            return httpx.Response(204)
        return httpx.Response(404)

    def transport(self, **kwargs) -> SupabaseTransport:
        return SupabaseTransport(
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            **kwargs)


def _applier(applied: list[tuple[str, str]]):
    def apply_fn(robot_id: str, cmd: str) -> tuple[bool, str]:
        applied.append((robot_id, cmd))
        return True, "ok"
    return apply_fn


# --------------------------------------------------------------------------
# The fix
# --------------------------------------------------------------------------

def test_a_forged_demo_command_is_never_applied() -> None:
    """The whole point: an estop queued by an anonymous sandbox visitor
    against a real robot must not reach ``apply_fn``."""
    backend = Backend([dict(FORGED)])
    applied: list[tuple[str, str]] = []
    t = backend.transport(site=SITE)
    assert t.poll_commands(_applier(applied)) == 0
    assert applied == []
    assert backend.patches == [], "not even acked — it is not ours to touch"


def test_the_query_is_filtered_server_side() -> None:
    backend = Backend([])
    t = backend.transport(site=OTHER)
    t.poll_commands(_applier([]))
    params = backend.gets[0].url.params
    assert params["site_id"] == f"eq.{OTHER}"
    assert "site_id" in params["select"].split(","), \
        "site_id must be projected, or the client-side check is blind"


def test_a_backend_that_ignores_the_filter_is_still_refused() -> None:
    """Belt and braces: every returned row is re-checked here."""
    backend = Backend([dict(FORGED),
                       {"id": "ours", "robot_id": "AMR-02", "cmd": "pause",
                        "site_id": SITE}],
                      honour_filters=False)
    applied: list[tuple[str, str]] = []
    t = backend.transport(site=SITE)
    assert t.poll_commands(_applier(applied)) == 1
    assert applied == [("AMR-02", "pause")]
    assert len(backend.patches) == 1
    assert "id=eq.ours" in backend.patches[0][0]


def test_the_sandboxs_own_command_still_runs_in_the_sandbox() -> None:
    """A simulator driving a demo sandbox executes that sandbox's own
    commands — the fix scopes the poller, it does not disable it."""
    backend = Backend([{"id": "c-1", "robot_id": f"{DEMO}-R01",
                        "cmd": "charge", "site_id": DEMO}])
    applied: list[tuple[str, str]] = []
    t = backend.transport(site=DEMO)
    assert t.poll_commands(_applier(applied)) == 1
    assert applied == [(f"{DEMO}-R01", "charge")]


# --------------------------------------------------------------------------
# Backward compatibility of the site resolution
# --------------------------------------------------------------------------

def test_the_default_site_is_the_one_this_writer_stamps() -> None:
    """No deployment silently stops executing commands.

    ``resolve_site`` is ``yantracore.site_id()`` — the exact resolution
    ``yantrasim.translate`` uses to stamp ``site_id`` on every row this
    transport writes. So the poller's default site is, by construction,
    the site the simulator was already publishing into.
    """
    from yantracore import site_id

    from yantrasim.sim import FleetSim
    from yantrasim.translate import robot_row

    assert resolve_site() == site_id()
    poller_site = SupabaseTransport().site
    assert poller_site == site_id()

    # ...and that really is the site the rows land in.
    out = FleetSim(seed=7).tick(dt_s=10.0)
    state = out.states[0]
    extras = out.extras[state["serialNumber"].replace("_", "-")]
    assert robot_row(state, extras)["site_id"] == poller_site


def test_env_var_moves_both_the_writer_and_the_poller(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANTRA_SITE_ID", OTHER)
    assert resolve_site() == OTHER
    assert SupabaseTransport().site == OTHER
    assert resolve_site(SITE) == SITE          # explicit still wins


def test_star_opts_out_of_the_filter() -> None:
    backend = Backend([dict(FORGED)])
    applied: list[tuple[str, str]] = []
    t = backend.transport(site=ANY_SITE)
    assert t.poll_commands(_applier(applied)) == 1  # deliberate opt-out
    assert applied == [("AMR-01", "estop")]
    assert "site_id" not in backend.gets[0].url.params


def test_rows_without_a_site_id_column_still_run() -> None:
    """An older backend that does not project ``site_id`` must not
    silently stop the simulator — there the server-side filter is the
    control, and the situation is logged once."""
    backend = Backend([{"id": "c-1", "robot_id": "AMR-01", "cmd": "pause"}],
                      honour_filters=False)
    applied: list[tuple[str, str]] = []
    t = backend.transport(site=SITE)
    assert t.poll_commands(_applier(applied)) == 1
    assert applied == [("AMR-01", "pause")]
