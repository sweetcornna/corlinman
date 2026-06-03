"""W2.2 — gateway lifecycle registers the default ``system.update_check`` job.

Validates the entrypoint's behaviour matrix from
``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 2/W2.2:

* Default config (``[system.update_check] enabled = true``) → the
  default scheduler job is registered with the canonical name and a
  cron expression tracking ``interval_hours``.
* ``enabled = false`` → no default job is registered (the checker
  itself is also skipped per the W1.1 lifespan branch).
* Explicit ``[[scheduler.jobs]] name = "system.update_check"`` in the
  loaded config → no duplicate registration; the operator's config
  wins.

All tests drive :func:`build_app` through ``TestClient`` so the
lifespan actually runs (FastAPI only fires startup/shutdown when an
ASGI client is engaged); the assertions then read the W2.2-public
helpers off ``app.state``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from corlinman_server.gateway.lifecycle.entrypoint import (  # noqa: E402
    build_app,
    list_default_scheduler_jobs,
)
from corlinman_server.gateway.lifecycle.scheduler_integration import (  # noqa: E402
    DEFAULT_UPDATE_CHECK_JOB_NAME,
    _config_has_scheduler_job,
    _register_default_update_check_job,
)
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Pure helpers — exercised without spinning the FastAPI app
# ---------------------------------------------------------------------------


def test_config_has_scheduler_job_finds_explicit_entry() -> None:
    """A dict-shaped config that already carries an entry by the
    target name short-circuits the auto-registration. Mirrors the
    production config loader's dict shape."""
    cfg = {
        "scheduler": {
            "jobs": [
                {"name": "daily-brief", "cron": "0 0 8 * * * *"},
                {"name": DEFAULT_UPDATE_CHECK_JOB_NAME, "cron": "0 0 */12 * * * *"},
            ]
        }
    }
    assert _config_has_scheduler_job(cfg, DEFAULT_UPDATE_CHECK_JOB_NAME) is True


def test_config_has_scheduler_job_none_when_absent() -> None:
    """Both "no scheduler section" and "scheduler section with no
    matching name" must return False — the auto-registration only
    bails on a *clean* match."""
    assert _config_has_scheduler_job(None, DEFAULT_UPDATE_CHECK_JOB_NAME) is False
    assert _config_has_scheduler_job({}, DEFAULT_UPDATE_CHECK_JOB_NAME) is False
    assert _config_has_scheduler_job(
        {"scheduler": {"jobs": []}},
        DEFAULT_UPDATE_CHECK_JOB_NAME,
    ) is False
    assert _config_has_scheduler_job(
        {"scheduler": {"jobs": [{"name": "other-job"}]}},
        DEFAULT_UPDATE_CHECK_JOB_NAME,
    ) is False


def test_config_has_scheduler_job_tolerates_malformed_entries() -> None:
    """A non-list ``jobs`` value or a non-dict entry must not raise —
    the helper degrades to False so a misshapen config doesn't block
    the auto-registration."""
    assert _config_has_scheduler_job({"scheduler": {"jobs": "nope"}}, "x") is False
    assert _config_has_scheduler_job(
        {"scheduler": {"jobs": [None, 42, {"no_name": True}]}}, "x"
    ) is False


# ---------------------------------------------------------------------------
# Register-helper unit tests — drives the in-memory list without the
# full lifespan
# ---------------------------------------------------------------------------


class _AppStub:
    """Minimal ``app.state`` carrier — the helper only reads / writes
    one attribute (``corlinman_default_scheduler_jobs``), so a
    SimpleNamespace-ish stub is plenty."""

    def __init__(self) -> None:
        self.state = type("State", (), {})()


def test_register_default_job_populates_state_list() -> None:
    """A fresh ``app.state`` with no scheduler entries should end up
    with exactly one job named ``system.update_check`` whose cron
    expression embeds the requested interval."""
    app = _AppStub()
    _register_default_update_check_job(app, cfg=None, interval_hours=6)

    jobs = list_default_scheduler_jobs(app)
    assert len(jobs) == 1
    assert jobs[0].name == DEFAULT_UPDATE_CHECK_JOB_NAME
    # 7-field cron grammar (sec min hour dom mon dow year) matches the
    # rest of docs/config.example.toml.
    assert jobs[0].cron == "0 0 */6 * * * *"
    # The action carries the ``run_tool`` discriminant pointing at the
    # registered builtin name.
    assert jobs[0].action.kind == "run_tool"
    assert jobs[0].action.plugin == "system"
    assert jobs[0].action.tool == "update_check"


def test_register_default_job_clamps_zero_interval_to_one() -> None:
    """A degraded boot that hands ``interval_hours = 0`` must still
    produce a parseable cron — clamp to 1 so ``*/0`` (an invalid
    croniter step) never appears."""
    app = _AppStub()
    _register_default_update_check_job(app, cfg=None, interval_hours=0)
    jobs = list_default_scheduler_jobs(app)
    assert len(jobs) == 1
    assert jobs[0].cron == "0 0 */1 * * * *"


def test_register_default_job_skipped_when_config_has_explicit_entry() -> None:
    """The W2.2 "operator override wins" branch: when the config
    carries a job by the same name, the helper must NOT append a
    duplicate."""
    app = _AppStub()
    cfg = {
        "scheduler": {
            "jobs": [{"name": DEFAULT_UPDATE_CHECK_JOB_NAME, "cron": "0 0 */12 * * * *"}]
        }
    }
    _register_default_update_check_job(app, cfg=cfg, interval_hours=6)

    assert list_default_scheduler_jobs(app) == []


def test_register_default_job_is_idempotent_on_repeat_calls() -> None:
    """Hot-reload may re-enter the lifecycle branch with the same
    handles. The helper must de-dupe against the in-memory list so a
    second call doesn't double the entry."""
    app = _AppStub()
    _register_default_update_check_job(app, cfg=None, interval_hours=6)
    _register_default_update_check_job(app, cfg=None, interval_hours=6)

    jobs = list_default_scheduler_jobs(app)
    assert len(jobs) == 1
    assert jobs[0].name == DEFAULT_UPDATE_CHECK_JOB_NAME


def test_list_default_scheduler_jobs_returns_a_copy(tmp_path: Path) -> None:
    """Tests should be free to mutate the returned list without
    racing the lifespan — confirm we hand back a copy, not a live
    reference."""
    app = _AppStub()
    _register_default_update_check_job(app, cfg=None, interval_hours=6)
    snapshot = list_default_scheduler_jobs(app)
    snapshot.clear()
    # The on-state list is untouched.
    assert len(list_default_scheduler_jobs(app)) == 1


# ---------------------------------------------------------------------------
# End-to-end lifespan integration via TestClient
# ---------------------------------------------------------------------------


def _monkey_loaded_config(monkeypatch: pytest.MonkeyPatch, cfg: dict | None) -> None:
    """Stub the lifecycle's ``_load_config`` so we can hand it a
    dict-shaped config without writing a real TOML file. The dict
    arrives directly on the ``cfg`` parameter the lifespan reads."""
    monkeypatch.setattr(
        "corlinman_server.gateway.lifecycle.entrypoint._load_config",
        lambda path: cfg,
    )


def test_lifespan_registers_default_job_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default boot path: enabled config → the lifespan registers the
    ``system.update_check`` job on ``app.state``. The W1.1 checker is
    also installed (we don't assert its behaviour here, just its
    presence — its own tests own that)."""
    _monkey_loaded_config(
        monkeypatch,
        {"system": {"update_check": {"enabled": True, "interval_hours": 6}}},
    )
    # The cfg dict must be reachable via a non-None config_path so the
    # lifecycle's W1.1 branch fires (it gates on resolved_data_dir, not
    # cfg, but the stubbed loader keys off the path argument).
    fake_cfg_path = tmp_path / "config.toml"
    fake_cfg_path.write_text("# stubbed", encoding="utf-8")

    app = build_app(config_path=fake_cfg_path, data_dir=tmp_path / "data")

    with TestClient(app):
        jobs = list_default_scheduler_jobs(app)
        names = [j.name for j in jobs]
        assert DEFAULT_UPDATE_CHECK_JOB_NAME in names
        # Cron tracks interval_hours.
        registered = next(
            j for j in jobs if j.name == DEFAULT_UPDATE_CHECK_JOB_NAME
        )
        assert registered.cron == "0 0 */6 * * * *"


def test_lifespan_does_not_register_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled = false`` short-circuits *both* the checker
    construction and the default-job registration. The list must stay
    empty so a downstream scheduler runtime doesn't poll a checker
    that doesn't exist."""
    _monkey_loaded_config(
        monkeypatch,
        {"system": {"update_check": {"enabled": False}}},
    )
    fake_cfg_path = tmp_path / "config.toml"
    fake_cfg_path.write_text("# stubbed", encoding="utf-8")

    app = build_app(config_path=fake_cfg_path, data_dir=tmp_path / "data")

    with TestClient(app):
        jobs = list_default_scheduler_jobs(app)
        names = [j.name for j in jobs]
        assert DEFAULT_UPDATE_CHECK_JOB_NAME not in names
        # And the checker itself should be absent / None per the W1.1
        # disabled branch.
        assert getattr(app.state, "corlinman_update_checker", None) is None


def test_lifespan_skips_when_config_has_explicit_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator override path: the loaded config already declares a
    ``[[scheduler.jobs]] name = "system.update_check"`` block. The
    auto-registration must leave the in-memory default list empty so
    the runtime only fires the operator's job."""
    _monkey_loaded_config(
        monkeypatch,
        {
            "system": {"update_check": {"enabled": True, "interval_hours": 6}},
            "scheduler": {
                "jobs": [
                    {
                        "name": DEFAULT_UPDATE_CHECK_JOB_NAME,
                        "cron": "0 0 */12 * * * *",
                        "action": {"type": "run_tool", "plugin": "system", "tool": "update_check"},
                    }
                ]
            },
        },
    )
    fake_cfg_path = tmp_path / "config.toml"
    fake_cfg_path.write_text("# stubbed", encoding="utf-8")

    app = build_app(config_path=fake_cfg_path, data_dir=tmp_path / "data")

    with TestClient(app):
        jobs = list_default_scheduler_jobs(app)
        # Either no in-memory default at all (preferred) or the
        # operator's entry won — the helper returns an empty list in
        # the de-dupe branch.
        assert all(
            j.name != DEFAULT_UPDATE_CHECK_JOB_NAME for j in jobs
        ), "explicit config must win — no auto-registered default"
        # Checker must still be installed so the operator's explicit
        # job has something to poll.
        assert getattr(app.state, "corlinman_update_checker", None) is not None
