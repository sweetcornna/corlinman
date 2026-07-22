from __future__ import annotations

import sys
from pathlib import Path

from corlinman_server.scheduler.builtins import registry


def test_private_builtin_loader_is_opt_in(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CORLINMAN_SCHEDULER_PRIVATE_MODULES", raising=False)
    monkeypatch.delenv("CORLINMAN_SCHEDULER_PRIVATE_PATH", raising=False)
    assert registry.load_private_builtin_modules() == []


def test_private_builtin_loader_imports_out_of_repo_module(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    module_name = "operator_private_job_for_test"
    (tmp_path / f"{module_name}.py").write_text(
        "from corlinman_server.scheduler.builtins.registry import register_builtin\n"
        "async def action(context):\n"
        "    return {'ok': True, 'private': True}\n"
        "register_builtin('operator.private_test', action)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_PATH", str(tmp_path))
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_MODULES", module_name)
    try:
        assert registry.load_private_builtin_modules() == [module_name]
        assert "operator.private_test" in registry.BUILTIN_ACTIONS
    finally:
        registry.BUILTIN_ACTIONS.pop("operator.private_test", None)
        sys.modules.pop(module_name, None)
        assert str(tmp_path.resolve()) not in sys.path


def test_private_builtin_loader_rejects_cached_module_outside_root(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:  # type: ignore[no-untyped-def]
    module_name = "operator_private_collision_for_test"
    outside = tmp_path / "outside"
    private = tmp_path / "private"
    outside.mkdir()
    private.mkdir()
    (outside / f"{module_name}.py").write_text("value = 'outside'\n")
    (private / f"{module_name}.py").write_text(
        "from corlinman_server.scheduler.builtins.registry import register_builtin\n"
        "async def action(context): return {'ok': True}\n"
        "register_builtin('operator.collision', action)\n"
    )
    monkeypatch.syspath_prepend(str(outside))
    __import__(module_name)
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_PATH", str(private))
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_MODULES", module_name)
    try:
        assert registry.load_private_builtin_modules() == []
        assert "operator.collision" not in registry.BUILTIN_ACTIONS
        assert caplog.records[-1].error_type == "ImportError"
        assert str(private.resolve()) not in sys.path
    finally:
        sys.modules.pop(module_name, None)


def test_private_builtin_loader_rolls_back_partial_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    module_name = "operator_private_partial_job_for_test"
    original = registry.BUILTIN_ACTIONS.get("operator.existing")

    async def public_action(_context):  # type: ignore[no-untyped-def]
        return {"ok": True, "public": True}

    registry.register_builtin("operator.existing", public_action)
    (tmp_path / f"{module_name}.py").write_text(
        "from corlinman_server.scheduler.builtins.registry import register_builtin\n"
        "async def action(context): return {'ok': True, 'private': True}\n"
        "register_builtin('operator.partial', action)\n"
        "register_builtin('operator.existing', action)\n"
        "raise RuntimeError('failed after registration')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_PATH", str(tmp_path))
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_MODULES", module_name)
    try:
        assert registry.load_private_builtin_modules() == []
        assert "operator.partial" not in registry.BUILTIN_ACTIONS
        assert registry.BUILTIN_ACTIONS["operator.existing"] is public_action
    finally:
        sys.modules.pop(module_name, None)
        if original is None:
            registry.BUILTIN_ACTIONS.pop("operator.existing", None)
        else:
            registry.BUILTIN_ACTIONS["operator.existing"] = original


def test_private_builtin_loader_redacts_exception_message(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:  # type: ignore[no-untyped-def]
    module_name = "operator_private_bad_job_for_test"
    (tmp_path / f"{module_name}.py").write_text(
        "raise RuntimeError('private prompt or credential must not be logged')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_PATH", str(tmp_path))
    monkeypatch.setenv("CORLINMAN_SCHEDULER_PRIVATE_MODULES", module_name)
    try:
        assert registry.load_private_builtin_modules() == []
        assert "private prompt or credential" not in caplog.text
        assert caplog.records[-1].error_type == "RuntimeError"
    finally:
        sys.modules.pop(module_name, None)
        assert str(tmp_path.resolve()) not in sys.path
