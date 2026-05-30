"""Tests for the ``persona_life.*`` builtin tools.

State is persisted into the real corlinman-persona runtime-state store
(``agent_state.sqlite``) against a ``tmp_path`` DB, so these exercise the
genuine read-merge-upsert path — including the ``mood`` mirror, the
``recent_topics`` push, and preservation of unrelated ``state_json`` keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from corlinman_agent.persona.life import (
    _MAX_HISTORY_ENTRIES,
    _MAX_SEED_ITEMS_PER_CATEGORY,
    _UNBOUND_PERSONA_KEY,
    _trim,
    dispatch_persona_life_diary_add,
    dispatch_persona_life_event_seed,
    dispatch_persona_life_get,
    dispatch_persona_life_get_seeds,
    dispatch_persona_life_set_seeds,
    dispatch_persona_life_set_state,
)
from corlinman_persona.placeholders import PersonaResolver
from corlinman_persona.state import PersonaState
from corlinman_persona.store import DEFAULT_TENANT_ID, PersonaStore


@pytest.fixture
async def store(tmp_path: Path):
    s = await PersonaStore.open_or_create(tmp_path / "agent_state.sqlite")
    try:
        yield s
    finally:
        await s.close()


def _args(**kw) -> str:
    return json.dumps(kw)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_empty_returns_default(store) -> None:
    out = json.loads(
        await dispatch_persona_life_get(
            args_json=_args(), persona_id="grantley", state_store=store
        )
    )
    assert out["ok"] is True
    assert out["persona_id"] == "grantley"
    assert out["current"]["state"] == "at_academy"
    assert out["diary_tail"] == []
    assert out["diary_total"] == 0


async def test_unbound_persona_uses_default_key(store) -> None:
    out = json.loads(
        await dispatch_persona_life_get(
            args_json=_args(), persona_id=None, state_store=store
        )
    )
    assert out["persona_id"] == _UNBOUND_PERSONA_KEY
    # Confirm it actually wrote under that key after a set_state.
    await dispatch_persona_life_set_state(
        args_json=_args(state="resting", activity="睡觉"),
        persona_id=None,
        state_store=store,
    )
    row = await store.get(_UNBOUND_PERSONA_KEY, tenant_id=DEFAULT_TENANT_ID)
    assert row is not None
    assert row.state_json["life"]["current"]["state"] == "resting"


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


async def test_set_state_persists_archives_and_mirrors(store) -> None:
    first = json.loads(
        await dispatch_persona_life_set_state(
            args_json=_args(
                state="on_mission",
                location="北境森林",
                activity="护送商队",
                companions=["艾尔戈"],
                mood="警觉",
            ),
            persona_id="grantley",
            state_store=store,
        )
    )
    assert first["ok"] is True
    assert first["current"]["companions"] == ["艾尔戈"]
    assert set(first["changed"]) >= {"state", "location", "activity"}

    second = json.loads(
        await dispatch_persona_life_set_state(
            args_json=_args(state="at_academy", location="骑士学院", activity="训练"),
            persona_id="grantley",
            state_store=store,
        )
    )
    assert second["ok"] is True

    # Native column mirror + recent_topics push happened on the real row.
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None
    assert row.mood == "警觉"  # last non-empty mood we set (second had none)
    assert "护送商队" in row.recent_topics
    assert "训练" in row.recent_topics
    # Each meaningful change is archived: the synthetic default → mission,
    # then mission → academy (faithful to the hermes behavior).
    history = row.state_json["life"]["history"]
    assert len(history) == 2
    assert history[0]["to"]["activity"] == "护送商队"
    assert history[-1]["from"]["activity"] == "护送商队"
    assert history[-1]["to"]["activity"] == "训练"


async def test_set_state_mirrors_placeholder_keys(store) -> None:
    """The life surfaces through the CURRENT persona system's
    ``{{persona.*}}`` resolver — flat life_* keys + mood + recent_topics."""
    await dispatch_persona_life_set_state(
        args_json=_args(
            state="on_mission",
            location="北境森林",
            activity="护送商队",
            companions=["艾尔戈", "奥斯卡"],
            mood="警觉",
            story_arc="护送商队任务",
        ),
        persona_id="grantley",
        state_store=store,
    )
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None
    sj = row.state_json
    assert sj["life_state"] == "on_mission"
    assert sj["life_location"] == "北境森林"
    assert sj["life_activity"] == "护送商队"
    assert sj["life_companions"] == "艾尔戈, 奥斯卡"
    assert sj["life_story_arc"] == "护送商队任务"

    # The persona placeholder resolver (keyed by agent_id == persona_id)
    # surfaces them verbatim into a persona system_prompt.
    resolver = PersonaResolver(store)
    assert await resolver.resolve("life_location", "grantley") == "北境森林"
    assert await resolver.resolve("life_state", "grantley") == "on_mission"
    assert await resolver.resolve("life_companions", "grantley") == "艾尔戈, 奥斯卡"
    assert await resolver.resolve("mood", "grantley") == "警觉"
    assert "护送商队" in await resolver.resolve("recent_topics", "grantley")


async def test_set_state_mood_explicit_omit_clear(store) -> None:
    """mood mirrors onto the native column only when explicitly provided;
    an omitted mood preserves the evolution-managed value, an explicit ""
    clears it — never a silent drift (review finding)."""
    # Pre-seed an evolution-managed mood.
    await store.upsert(
        PersonaState(agent_id="grantley", mood="开心"), tenant_id=DEFAULT_TENANT_ID
    )
    # Omit mood → native column preserved.
    await dispatch_persona_life_set_state(
        args_json=_args(state="resting", activity="休息"),
        persona_id="grantley",
        state_store=store,
    )
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None and row.mood == "开心"  # not clobbered

    # Explicit mood → mirrored.
    await dispatch_persona_life_set_state(
        args_json=_args(state="resting", mood="疲惫"),
        persona_id="grantley",
        state_store=store,
    )
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None and row.mood == "疲惫"

    # Explicit empty mood → cleared (mirror not skipped).
    await dispatch_persona_life_set_state(
        args_json=_args(state="resting", mood=""),
        persona_id="grantley",
        state_store=store,
    )
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None and row.mood == ""
    assert row.state_json["life"]["current"]["mood"] == ""  # consistent with column


async def test_set_state_rejects_unknown_state(store) -> None:
    out = json.loads(
        await dispatch_persona_life_set_state(
            args_json=_args(state="banana"), persona_id="grantley", state_store=store
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_set_state_preserves_unrelated_state_json_and_fatigue(store) -> None:
    # Pre-seed a row the way the evolution loop / seeder would: a custom
    # state_json key + a non-zero fatigue.
    await store.upsert(
        PersonaState(
            agent_id="grantley",
            mood="neutral",
            fatigue=0.7,
            state_json={"favorite_color": "blue"},
        ),
        tenant_id=DEFAULT_TENANT_ID,
    )
    await dispatch_persona_life_diary_add(
        args_json=_args(entry="今天很累"), persona_id="grantley", state_store=store
    )
    row = await store.get("grantley", tenant_id=DEFAULT_TENANT_ID)
    assert row is not None
    assert row.fatigue == pytest.approx(0.7)  # life tools never touch fatigue
    assert row.state_json["favorite_color"] == "blue"  # unrelated key survives
    assert len(row.state_json["diary"]) == 1


# ---------------------------------------------------------------------------
# diary
# ---------------------------------------------------------------------------


async def test_diary_add_and_tail(store) -> None:
    for i in range(3):
        await dispatch_persona_life_diary_add(
            args_json=_args(entry=f"entry-{i}", tag="thoughts"),
            persona_id="grantley",
            state_store=store,
        )
    out = json.loads(
        await dispatch_persona_life_get(
            args_json=_args(diary_tail=2), persona_id="grantley", state_store=store
        )
    )
    assert out["diary_total"] == 3
    assert [d["entry"] for d in out["diary_tail"]] == ["entry-1", "entry-2"]


async def test_diary_add_rejects_empty(store) -> None:
    out = json.loads(
        await dispatch_persona_life_diary_add(
            args_json=_args(entry="   "), persona_id="grantley", state_store=store
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_per_persona_isolation(store) -> None:
    await dispatch_persona_life_diary_add(
        args_json=_args(entry="grantley-private"),
        persona_id="grantley",
        state_store=store,
    )
    await dispatch_persona_life_diary_add(
        args_json=_args(entry="other-private"),
        persona_id="other_persona",
        state_store=store,
    )
    a = json.loads(
        await dispatch_persona_life_get(
            args_json=_args(), persona_id="grantley", state_store=store
        )
    )
    b = json.loads(
        await dispatch_persona_life_get(
            args_json=_args(), persona_id="other_persona", state_store=store
        )
    )
    assert a["diary_total"] == 1
    assert b["diary_total"] == 1
    assert a["diary_tail"][0]["entry"] == "grantley-private"
    assert b["diary_tail"][0]["entry"] == "other-private"


def test_trim_caps_history() -> None:
    long = list(range(_MAX_HISTORY_ENTRIES + 50))
    trimmed = _trim(long, _MAX_HISTORY_ENTRIES)
    assert len(trimmed) == _MAX_HISTORY_ENTRIES
    assert trimmed[-1] == _MAX_HISTORY_ENTRIES + 49  # tail kept (newest)


# ---------------------------------------------------------------------------
# event_seed
# ---------------------------------------------------------------------------


async def test_event_seed_grantley_uses_bundled_pack() -> None:
    # grantley.yaml overrides mission_scenario entirely, so a 'mission'
    # draw's scenario must come from the bundled Knights-College list.
    grantley_missions = {
        "护送商队穿越北境森林",
        "调查山村的孩子失踪案",
        "陪学院教授去古战场取样",
        "替骑士团捎信去港口",
        "协助镇压走私团伙",
        "替商会找回被劫的家传剑",
        "驱除山道上的野兽",
        "守夜监视一处可疑古迹",
        "替村子里的老人寻找走失的猎犬",
        "押送一名嫌犯回首都",
    }
    out = json.loads(
        await dispatch_persona_life_event_seed(
            args_json=_args(kind="mission"), persona_id="grantley"
        )
    )
    assert out["ok"] is True
    assert out["seed"]["scenario"] in grantley_missions


async def test_event_seed_operator_override(tmp_path: Path) -> None:
    seeds_dir = tmp_path / "persona_life"
    seeds_dir.mkdir(parents=True)
    (seeds_dir / "grantley.events.yaml").write_text(
        "companion:\n  - ZZTOP_ONLY\n", encoding="utf-8"
    )
    out = json.loads(
        await dispatch_persona_life_event_seed(
            args_json=_args(kind="mission"),
            persona_id="grantley",
            data_dir=tmp_path,
        )
    )
    # Override replaces only the 'companion' category.
    assert out["seed"]["companion"] == "ZZTOP_ONLY"


async def test_event_seed_invalid_kind() -> None:
    out = json.loads(
        await dispatch_persona_life_event_seed(
            args_json=_args(kind="banana"), persona_id="grantley"
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_event_seed_freeform_draws_every_category() -> None:
    out = json.loads(
        await dispatch_persona_life_event_seed(
            args_json=_args(kind="freeform"), persona_id="grantley"
        )
    )
    assert out["ok"] is True
    # freeform draws one cue per known category.
    assert "mission_scenario" in out["seed"]
    assert "weather" in out["seed"]


# ---------------------------------------------------------------------------
# set_seeds / get_seeds (the /persona-wizard authoring tools)
# ---------------------------------------------------------------------------


async def test_set_seeds_writes_and_event_seed_uses_it(tmp_path: Path) -> None:
    out = json.loads(
        await dispatch_persona_life_set_seeds(
            args_json=_args(
                persona_id="sherlock",
                seeds={
                    "companion": ["华生", "赫德森太太"],
                    "mission_scenario": ["调查一桩离奇命案"],
                },
            ),
            data_dir=tmp_path,
        )
    )
    assert out["ok"] is True
    assert out["categories"]["companion"] == 2
    assert (tmp_path / "persona_life" / "sherlock.events.yaml").is_file()

    # The authored library now drives event_seed for that persona.
    seed = json.loads(
        await dispatch_persona_life_event_seed(
            args_json=_args(kind="mission"), persona_id="sherlock", data_dir=tmp_path
        )
    )
    assert seed["seed"]["companion"] in {"华生", "赫德森太太"}
    assert seed["seed"]["scenario"] == "调查一桩离奇命案"


async def test_set_seeds_merge_vs_replace(tmp_path: Path) -> None:
    await dispatch_persona_life_set_seeds(
        args_json=_args(persona_id="x", seeds={"companion": ["A"], "tension": ["T1"]}),
        data_dir=tmp_path,
    )
    # merge=True replaces only the named category, preserves the rest.
    merged = json.loads(
        await dispatch_persona_life_set_seeds(
            args_json=_args(persona_id="x", seeds={"companion": ["B"]}, merge=True),
            data_dir=tmp_path,
        )
    )
    assert merged["merged"] is True
    on_disk = yaml.safe_load(
        (tmp_path / "persona_life" / "x.events.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["companion"] == ["B"]
    assert on_disk["tension"] == ["T1"]  # preserved by merge

    # default (merge=False) replaces the whole file.
    await dispatch_persona_life_set_seeds(
        args_json=_args(persona_id="x", seeds={"companion": ["C"]}),
        data_dir=tmp_path,
    )
    on_disk = yaml.safe_load(
        (tmp_path / "persona_life" / "x.events.yaml").read_text(encoding="utf-8")
    )
    assert on_disk == {"companion": ["C"]}  # tension dropped


async def test_set_seeds_rejects_traversal_slug(tmp_path: Path) -> None:
    out = json.loads(
        await dispatch_persona_life_set_seeds(
            args_json=_args(persona_id="../evil", seeds={"companion": ["A"]}),
            data_dir=tmp_path,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"
    # nothing escaped the persona_life dir
    assert not list(tmp_path.glob("**/*evil*"))


async def test_set_seeds_rejects_empty(tmp_path: Path) -> None:
    out = json.loads(
        await dispatch_persona_life_set_seeds(
            args_json=_args(persona_id="x", seeds={}), data_dir=tmp_path
        )
    )
    assert out["error"] == "invalid_args"


async def test_set_seeds_caps_items(tmp_path: Path) -> None:
    big = [f"item{i}" for i in range(_MAX_SEED_ITEMS_PER_CATEGORY + 100)]
    out = json.loads(
        await dispatch_persona_life_set_seeds(
            args_json=_args(persona_id="x", seeds={"companion": big}),
            data_dir=tmp_path,
        )
    )
    assert out["categories"]["companion"] == _MAX_SEED_ITEMS_PER_CATEGORY
    assert any("trimmed" in d for d in out["dropped"])


async def test_get_seeds_reports_override_and_effective(tmp_path: Path) -> None:
    # No override yet → effective = bundled grantley pack (no override flag).
    before = json.loads(
        await dispatch_persona_life_get_seeds(
            args_json=_args(persona_id="grantley"), data_dir=tmp_path
        )
    )
    assert before["has_override"] is False
    assert "艾尔戈" in before["seeds"]["companion"]  # from the bundled pack

    await dispatch_persona_life_set_seeds(
        args_json=_args(persona_id="grantley", seeds={"companion": ["新同伴"]}),
        data_dir=tmp_path,
    )
    after = json.loads(
        await dispatch_persona_life_get_seeds(
            args_json=_args(persona_id="grantley"), data_dir=tmp_path
        )
    )
    assert after["has_override"] is True
    assert after["seeds"]["companion"] == ["新同伴"]  # override wins for that category
