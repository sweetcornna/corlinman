"""binding_prefs_store — per-binding /model + /new persistence."""

from __future__ import annotations

from pathlib import Path

from corlinman_server.binding_prefs_store import (
    bump_session_epoch,
    get_prefs,
    set_model_override,
)

_B = ("qq", "bot1", "group42", "user7")


def test_absent_row_returns_defaults(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    prefs = get_prefs(*_B, db_path=db)
    assert prefs.model_override is None
    assert prefs.session_epoch == 0


def test_set_and_clear_model_override(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    prefs = set_model_override(*_B, "gpt-4o-mini", db_path=db)
    assert prefs.model_override == "gpt-4o-mini"
    assert get_prefs(*_B, db_path=db).model_override == "gpt-4o-mini"

    cleared = set_model_override(*_B, None, db_path=db)
    assert cleared.model_override is None
    assert get_prefs(*_B, db_path=db).model_override is None


def test_epoch_bumps_monotonically_and_keeps_model(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    set_model_override(*_B, "alias-x", db_path=db)
    assert bump_session_epoch(*_B, db_path=db).session_epoch == 1
    after = bump_session_epoch(*_B, db_path=db)
    assert after.session_epoch == 2
    assert after.model_override == "alias-x"


def test_bindings_are_isolated(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    other = ("telegram", "bot2", "chat9", "user7")
    set_model_override(*_B, "model-a", db_path=db)
    bump_session_epoch(*other, db_path=db)
    assert get_prefs(*other, db_path=db).model_override is None
    assert get_prefs(*_B, db_path=db).session_epoch == 0
