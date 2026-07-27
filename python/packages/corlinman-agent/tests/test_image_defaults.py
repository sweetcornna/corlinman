"""Global image-generation binding parsing.

Counterpart to ``test_voice_defaults_take_effect``. Before this existed
there was no global "use this model for images" setting at all — image
generation picked the first provider flagged ``image_capable`` and
otherwise fell through to the chat provider, so the model hub had nothing
to bind and an operator had no way to steer it short of editing a persona.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from corlinman_agent.image.defaults import (
    ImageDefaults,
    apply_image_config,
    get_image_defaults,
    image_defaults_from_config,
    reset_image_defaults,
)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    reset_image_defaults()
    yield
    reset_image_defaults()


def test_parses_the_models_block() -> None:
    d = image_defaults_from_config(
        {"image_provider": "img", "image_model": "gpt-image-2", "default": "gpt-5.2"}
    )
    assert d == ImageDefaults(provider="img", model="gpt-image-2")
    assert d.configured is True


def test_unset_block_is_inert() -> None:
    assert image_defaults_from_config(None).configured is False
    assert image_defaults_from_config({}).configured is False
    assert image_defaults_from_config({"default": "gpt-5.2"}).configured is False


def test_model_alone_counts_as_configured() -> None:
    # Provider is optional — the resolver can route a bare model id.
    assert image_defaults_from_config({"image_model": "gpt-image-2"}).configured


def test_blank_strings_do_not_count() -> None:
    d = image_defaults_from_config({"image_provider": "  ", "image_model": ""})
    assert d.configured is False


def test_apply_installs_process_wide() -> None:
    apply_image_config({"image_provider": "img", "image_model": "gpt-image-2"})
    assert get_image_defaults().model == "gpt-image-2"
    reset_image_defaults()
    assert get_image_defaults().configured is False
