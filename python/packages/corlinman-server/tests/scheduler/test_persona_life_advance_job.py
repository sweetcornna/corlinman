"""R3 persona-liveness — ``persona.life_advance`` builtin contract tests.

Three surfaces:

* Registry — importing the builtins package registers ``persona.life_advance``
  so the scheduler-runtime ``run_tool`` dispatch resolves it by name.
* Action — running the builtin against a real ``agent_state.sqlite`` advances
  every row with a fresh life beat: activity, location, companion, weather, and
  an auto diary entry are written; ``recent_topics`` receives the new activity.
* Degradation branches — ``data_dir_unavailable`` (no ``data_dir`` on
  ``app_state``). The builtin must *never* raise.
* Config gate — :func:`_register_default_persona_life_advance_job` only
  registers the default cron job when ``[persona.life_advance] enabled = true``.
"""

from __future__ import annotations

from types import SimpleNamespace

from corlinman_persona.state import PersonaState
from corlinman_persona.store import PersonaStore
from corlinman_server.gateway.lifecycle.scheduler_integration import (
    DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME,
    _register_default_persona_life_advance_job,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    PERSONA_LIFE_ADVANCE_BUILTIN_NAME,
    BuiltinContext,
    _persona_life_advance_action,
    run_builtin,
)

# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_persona_life_advance_is_registered_by_name() -> None:
    """Importing the builtins package registers ``persona.life_advance`` so
    the scheduler-runtime hook can resolve it without re-importing every
    callsite."""
    assert PERSONA_LIFE_ADVANCE_BUILTIN_NAME == "persona.life_advance"
    assert PERSONA_LIFE_ADVANCE_BUILTIN_NAME in BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS[PERSONA_LIFE_ADVANCE_BUILTIN_NAME] is _persona_life_advance_action


# ---------------------------------------------------------------------------
# Degradation branches — builtins must NEVER raise.
# ---------------------------------------------------------------------------


async def test_data_dir_unavailable_returns_typed_envelope() -> None:
    """``app_state`` with no ``data_dir`` slot → typed envelope, no raise."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _persona_life_advance_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


async def test_none_app_state_returns_data_dir_unavailable() -> None:
    """Degraded boot before the state bundle attaches — same short-circuit."""
    context = BuiltinContext(app_state=None)
    out = await _persona_life_advance_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


# ---------------------------------------------------------------------------
# Happy path — the sweep actually advances life state.
# ---------------------------------------------------------------------------


async def test_sweep_advances_life_beat_and_recent_topics(tmp_path) -> None:
    """A single persona row gains a fresh life beat (activity, companions,
    weather, diary entry) and the activity is pushed onto recent_topics."""
    state_db = tmp_path / "agent_state.sqlite"

    # Seed a row with a known persona_id that has a bundled seed pack.
    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.3,
                recent_topics=["old_topic"],
            )
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _persona_life_advance_action(context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1
    assert out["rows_changed"] == 1

    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None

    # recent_topics should include a new entry (the drawn activity).
    assert len(after.recent_topics) >= 1

    # state_json["life"]["current"] should be a dict with expected keys.
    sj = after.state_json
    assert isinstance(sj, dict)
    life = sj.get("life")
    assert isinstance(life, dict)
    current = life.get("current")
    assert isinstance(current, dict)
    assert current.get("state") in {"at_academy", "on_mission", "traveling"}
    assert isinstance(current.get("activity"), str)
    assert current.get("activity")  # non-empty

    # An auto diary entry should have been appended.
    diary = sj.get("diary")
    assert isinstance(diary, list)
    assert len(diary) >= 1
    last_entry = diary[-1]
    assert last_entry.get("tag") == "auto_advance"

    # Mirror keys should be present.
    assert "life_state" in sj
    assert "life_activity" in sj
    assert "life_location" in sj


async def test_sweep_zero_rows_when_store_empty(tmp_path) -> None:
    """An empty ``agent_state.sqlite`` → ``rows_scanned = 0``, ok = True."""
    state_db = tmp_path / "agent_state.sqlite"
    # Just create the file/schema by opening and closing the store.
    async with PersonaStore(state_db):
        pass

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _persona_life_advance_action(context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 0
    assert out["rows_changed"] == 0


async def test_run_builtin_dispatches_persona_life_advance(tmp_path) -> None:
    """End-to-end via the public ``run_builtin`` entry point — the registry
    indirection is transparent and the sweep still runs."""
    state_db = tmp_path / "agent_state.sqlite"

    async with PersonaStore(state_db) as store:
        await store.upsert(PersonaState(agent_id="grantley"))

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await run_builtin(PERSONA_LIFE_ADVANCE_BUILTIN_NAME, context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1


async def test_generic_persona_uses_generic_seeds(tmp_path) -> None:
    """A persona with no bundled pack and no override falls back to the
    generic seed library; the sweep still succeeds and writes a beat."""
    state_db = tmp_path / "agent_state.sqlite"

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(agent_id="unknown_persona_xyz")
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _persona_life_advance_action(context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1
    assert out["rows_changed"] == 1


# ---------------------------------------------------------------------------
# Default-off config gate
# ---------------------------------------------------------------------------


def _make_app() -> SimpleNamespace:
    app = SimpleNamespace()
    app.state = SimpleNamespace()
    return app


def test_life_advance_job_not_registered_when_flag_absent() -> None:
    """Default config (no [persona.life_advance] section) → job NOT added."""
    app = _make_app()
    _register_default_persona_life_advance_job(app, cfg=None)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME not in names


def test_life_advance_job_not_registered_when_enabled_false() -> None:
    """``[persona.life_advance] enabled = false`` → job NOT added."""
    app = _make_app()
    cfg = {"persona": {"life_advance": {"enabled": False}}}
    _register_default_persona_life_advance_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME not in names


def test_life_advance_job_registered_when_enabled_true() -> None:
    """``[persona.life_advance] enabled = true`` → job IS added once."""
    app = _make_app()
    cfg = {"persona": {"life_advance": {"enabled": True}}}
    _register_default_persona_life_advance_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    assert DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME in names
    # Exactly once — idempotent double-call doesn't duplicate.
    _register_default_persona_life_advance_job(app, cfg=cfg)
    jobs2 = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names2 = [getattr(j, "name", None) for j in jobs2]
    assert names2.count(DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME) == 1


def test_life_advance_job_skipped_when_explicit_config_job(monkeypatch) -> None:
    """Operator already declares an explicit job → helper is a no-op."""
    app = _make_app()
    cfg = {
        "persona": {"life_advance": {"enabled": True}},
        "scheduler": {
            "jobs": [{"name": DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME, "cron": "0 0 5 * * * *"}]
        },
    }
    _register_default_persona_life_advance_job(app, cfg=cfg)
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", [])
    names = [getattr(j, "name", None) for j in jobs]
    # Should NOT have been added to the defaults list because the explicit
    # config job takes precedence.
    assert DEFAULT_PERSONA_LIFE_ADVANCE_JOB_NAME not in names
