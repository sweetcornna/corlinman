"""Tests for the agent status-card share-link seam (#28).

Two layers:

* :func:`corlinman_channels._status.format_status_footer_line` — the pure
  formatter that joins a configured ``public_url`` with an already-minted
  signed ``token`` into the one-line reply footer. Empty on either missing
  input; strips a trailing slash off ``public_url``.
* :func:`corlinman_channels.service.configure_status_links` +
  :func:`corlinman_channels.service._status_link_line` — the runtime seam the
  gateway wires once at bootstrap (injecting a ``session_key -> token`` minter
  closure so ``corlinman_channels`` never imports ``corlinman_server``). The
  reply paths call ``_status_link_line``; it must be a no-op returning ``""``
  unless ALL of ``public_url`` + ``enabled`` + ``minter`` (+ a non-empty
  ``session_key``) are set, and it must swallow any error the minter raises.

``configure_status_links`` mutates module globals, so every test that touches
the seam resets the state on teardown via the ``reset_status_links`` fixture
to keep sibling tests (and other test modules) hermetic.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from corlinman_channels import service
from corlinman_channels._status import format_status_footer_line

# ---------------------------------------------------------------------------
# format_status_footer_line — the pure formatter.
# ---------------------------------------------------------------------------


class TestFormatStatusFooterLine:
    def test_empty_public_url_returns_empty(self) -> None:
        assert format_status_footer_line("", "TOK") == ""

    def test_empty_token_returns_empty(self) -> None:
        assert format_status_footer_line("https://x", "") == ""

    def test_both_empty_returns_empty(self) -> None:
        assert format_status_footer_line("", "") == ""

    def test_basic_join(self) -> None:
        out = format_status_footer_line("https://bot.example.com", "TOK")
        assert out == "🔗 实时状态: https://bot.example.com/status/TOK"

    def test_trailing_slash_stripped(self) -> None:
        # A public_url with a trailing slash must not produce a double slash
        # before ``/status/``.
        out = format_status_footer_line("https://x/", "TOK")
        assert out == "🔗 实时状态: https://x/status/TOK"
        assert "//status/" not in out

    def test_custom_label(self) -> None:
        out = format_status_footer_line("https://x", "TOK", label="live status")
        assert out == "live status: https://x/status/TOK"

    def test_default_label_is_realtime_status(self) -> None:
        # The default label is the 🔗 实时状态 string the channels append.
        out = format_status_footer_line("https://x", "TOK")
        assert out.startswith("🔗 实时状态: ")


# ---------------------------------------------------------------------------
# configure_status_links + _status_link_line — the injected-minter seam.
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_status_links() -> Iterator[None]:
    """Reset the module-global status-link wiring after each test.

    ``configure_status_links`` mutates process-wide state; without this
    teardown a test that enables links would leak into siblings (and into
    other test modules that import ``service``).
    """
    yield
    service.configure_status_links()  # defaults: disabled, no url, no minter


class TestStatusLinkSeam:
    def test_disabled_by_default_after_reset(self, reset_status_links: None) -> None:
        # Establish the baseline: a fresh/reset module renders no link.
        service.configure_status_links()
        assert service._status_link_line("sess") == ""

    def test_fully_configured_renders_line(self, reset_status_links: None) -> None:
        service.configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=lambda sk: "TOK",
        )
        assert service._status_link_line("sess") == "🔗 实时状态: https://x/status/TOK"

    def test_trailing_slash_public_url_normalised(self, reset_status_links: None) -> None:
        service.configure_status_links(
            public_url="https://x/",
            enabled=True,
            minter=lambda sk: "TOK",
        )
        line = service._status_link_line("sess")
        assert line == "🔗 实时状态: https://x/status/TOK"
        assert "//status/" not in line

    def test_disabled_returns_empty(self, reset_status_links: None) -> None:
        service.configure_status_links(
            public_url="https://x",
            enabled=False,
            minter=lambda sk: "TOK",
        )
        assert service._status_link_line("sess") == ""

    def test_empty_public_url_returns_empty(self, reset_status_links: None) -> None:
        service.configure_status_links(
            public_url="",
            enabled=True,
            minter=lambda sk: "TOK",
        )
        assert service._status_link_line("sess") == ""

    def test_missing_minter_returns_empty(self, reset_status_links: None) -> None:
        service.configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=None,
        )
        assert service._status_link_line("sess") == ""

    def test_minter_exception_is_swallowed(self, reset_status_links: None) -> None:
        # A status-link minting failure must NEVER propagate and break a
        # reply — the reply path catches it and renders no footer.
        def boom(_sk: str) -> str:
            raise RuntimeError("token mint exploded")

        service.configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=boom,
        )
        # Must not raise; must degrade to no footer.
        assert service._status_link_line("sess") == ""

    def test_empty_token_from_minter_renders_empty(self, reset_status_links: None) -> None:
        # A minter that returns an empty string yields no link
        # (format_status_footer_line is empty-safe on the token side).
        service.configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=lambda sk: "",
        )
        assert service._status_link_line("sess") == ""

    def test_reconfigure_to_defaults_disables(self, reset_status_links: None) -> None:
        # Wire it on, then call configure_status_links() with no args:
        # the feature must turn fully off (this is exactly the teardown
        # contract the fixture relies on).
        service.configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=lambda sk: "TOK",
        )
        assert service._status_link_line("sess") != ""
        service.configure_status_links()
        assert service._status_link_line("sess") == ""
