"""GrantStore — memoried interactive approvals (W3-1, plan §1.2.4).

Pins the three hard rules: arg-scoped keys, ``always`` in SQLite (never
the rules file), and session/always lifecycles. The "grant cannot beat
deny" rule is enforced in the gate and tested there.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_agent.authz.grants import GrantStore, arg_digest
from corlinman_agent.authz.model import Subject

_S1 = Subject(session_key="acme::s1", tenant_id="acme")
_S2 = Subject(session_key="acme::s2", tenant_id="acme")
_OTHER_TENANT = Subject(session_key="globex::s1", tenant_id="globex")


def test_arg_digest_is_argument_specific() -> None:
    ls = arg_digest("run_shell", {"command": "ls"})
    rm = arg_digest("run_shell", {"command": "rm -rf /"})
    assert ls != rm
    assert ls == arg_digest("run_shell", {"command": "ls"})  # stable


def test_session_grant_scoped_to_session_and_args(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "session")
    assert store.is_granted(_S1, "run_shell", {"command": "ls"})
    # Different args / different session / different tenant: all uncovered.
    assert not store.is_granted(_S1, "run_shell", {"command": "rm x"})
    assert not store.is_granted(_S2, "run_shell", {"command": "ls"})
    assert not store.is_granted(_OTHER_TENANT, "run_shell", {"command": "ls"})


def test_once_records_nothing(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "once")
    assert not store.is_granted(_S1, "run_shell", {"command": "ls"})


def test_always_grant_survives_a_fresh_store(tmp_path: Path) -> None:
    """The durable tier is SQLite-backed — a new store on the same
    data_dir (a new process) still honours it."""
    GrantStore(tmp_path).record(_S1, "run_shell", {"command": "ls"}, "always")
    fresh = GrantStore(tmp_path)
    assert fresh.is_granted(_S1, "run_shell", {"command": "ls"})
    assert fresh.is_granted(_S2, "run_shell", {"command": "ls"})  # any session
    assert not fresh.is_granted(_OTHER_TENANT, "run_shell", {"command": "ls"})
    # And it lives in the grants DB, not the rules file.
    assert (tmp_path / "authz" / "grants.sqlite3").exists()
    assert not (tmp_path / "settings.json").exists()


def test_clear_session_grants_scoped_and_global(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {}, "session")
    store.record(_S2, "web_search", {}, "session")
    store.clear_session_grants("acme::s1")
    assert not store.is_granted(_S1, "run_shell", {})
    assert store.is_granted(_S2, "web_search", {})
    store.clear_session_grants()
    assert not store.is_granted(_S2, "web_search", {})


def test_clear_session_grants_leaves_always_alone(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "always")
    store.clear_session_grants()
    assert store.is_granted(_S1, "run_shell", {"command": "ls"})


def test_revoke_always(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "always")
    store.revoke_always(_S1, "run_shell", {"command": "ls"})
    assert not store.is_granted(_S1, "run_shell", {"command": "ls"})
    # Revocation is durable too.
    assert not GrantStore(tmp_path).is_granted(_S1, "run_shell", {"command": "ls"})


def test_unwritable_data_dir_degrades_to_memory(tmp_path: Path) -> None:
    """An unwritable DB must degrade the always tier to process memory —
    an approval never turns into a deny or a crash."""
    blocker = tmp_path / "authz"
    blocker.write_text("not a directory", encoding="utf-8")
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "always")
    assert store.is_granted(_S1, "run_shell", {"command": "ls"})


def test_scoped_always_grant_only_matches_its_scope(tmp_path: Path) -> None:
    """Channel-side defaults (W3-3) narrow to surface+user; the check path
    honours the narrower keys already."""
    store = GrantStore(tmp_path)
    granter = Subject(
        session_key="acme::s1", tenant_id="acme", surface="qq", user_id="u1"
    )
    store.record(
        granter, "run_shell", {"command": "ls"}, "always",
        scope_surface=True, scope_user=True,
    )
    assert store.is_granted(granter, "run_shell", {"command": "ls"})
    other_user = Subject(
        session_key="acme::s9", tenant_id="acme", surface="qq", user_id="u2"
    )
    assert not store.is_granted(other_user, "run_shell", {"command": "ls"})
    other_surface = Subject(
        session_key="acme::s9", tenant_id="acme", surface="web", user_id="u1"
    )
    assert not store.is_granted(other_surface, "run_shell", {"command": "ls"})


# ---------------------------------------------------------------------------
# W3-4 — admin surface APIs (list / keyed revoke) + cross-process
# invalidation via the mtime watermark.
# ---------------------------------------------------------------------------


def test_list_always_returns_rows_with_metadata(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "always")
    store.record(
        _OTHER_TENANT, "web_search", {"query": "x"}, "always"
    )
    rows = store.list_always()
    assert len(rows) == 2
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["run_shell"]["tenant"] == "acme"
    assert by_tool["run_shell"]["arg_digest"] == arg_digest(
        "run_shell", {"command": "ls"}
    )
    assert isinstance(by_tool["run_shell"]["created_at"], float)
    # Session grants never appear in the durable listing.
    store.record(_S1, "read_file", {"path": "/x"}, "session")
    assert len(store.list_always()) == 2


def test_revoke_always_entry_by_exact_key(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    store.record(_S1, "run_shell", {"command": "ls"}, "always")
    digest = arg_digest("run_shell", {"command": "ls"})
    assert store.revoke_always_entry(
        tenant="acme", tool="run_shell", arg_digest=digest
    )
    assert not store.is_granted(_S1, "run_shell", {"command": "ls"})
    # Second revoke of the same key: nothing there any more.
    assert not store.revoke_always_entry(
        tenant="acme", tool="run_shell", arg_digest=digest
    )


def test_cross_process_revocation_reaches_a_warm_store(tmp_path: Path) -> None:
    """The agent-process contract (W3-4 AC5): a warm store (mirror
    loaded) drops a grant revoked by ANOTHER store instance on the same
    DB — the mtime check runs on every is_granted."""
    agent = GrantStore(tmp_path)
    agent.record(_S1, "run_shell", {"command": "ls"}, "always")
    assert agent.is_granted(_S1, "run_shell", {"command": "ls"})  # warm

    gateway = GrantStore(tmp_path)
    digest = arg_digest("run_shell", {"command": "ls"})
    assert gateway.revoke_always_entry(
        tenant="acme", tool="run_shell", arg_digest=digest
    )

    assert not agent.is_granted(_S1, "run_shell", {"command": "ls"})


def test_cross_process_new_grant_reaches_a_warm_store(tmp_path: Path) -> None:
    agent = GrantStore(tmp_path)
    assert not agent.is_granted(_S1, "run_shell", {"command": "ls"})  # warm+empty

    GrantStore(tmp_path).record(_S1, "run_shell", {"command": "ls"}, "always")
    assert agent.is_granted(_S1, "run_shell", {"command": "ls"})
