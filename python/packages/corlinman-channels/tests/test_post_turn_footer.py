"""Tests for ``corlinman_channels._status.format_turn_footer`` and the
matching ``try_append_footer`` helper (W4.1).

The footer is rendered at the end of every turn that the four
mutable-spinner channels finish (Telegram / Discord / Slack / Feishu),
so the exact string shape is part of the user-facing UX contract — a
regression here would mean every reply suddenly looks different. The
table-driven assertions below pin the literal formatting.
"""

from __future__ import annotations

from corlinman_channels._status import (
    TEXT_LIMIT,
    format_turn_footer,
    try_append_footer,
)


class TestFormatTurnFooter:
    def test_format_turn_footer_full(self) -> None:
        """All fields populated → exact spec string."""
        out = format_turn_footer(
            elapsed_ms=12_000,
            tool_calls=3,
            estimated_cost_usd=0.012,
            cost_status="estimated",
        )
        assert out == "(elapsed: 12s · 3 tool calls · ~$0.0120)"

    def test_format_turn_footer_zero_cost_omits_dollar(self) -> None:
        """A turn with cost==0 should drop the $ slot entirely so the
        footer doesn't carry an empty / confusing ``$0.0000`` line."""
        out = format_turn_footer(
            elapsed_ms=5_000,
            tool_calls=1,
            estimated_cost_usd=0.0,
            cost_status="estimated",
        )
        assert "$" not in out
        assert out == "(elapsed: 5s · 1 tool call)"

    def test_format_turn_footer_unknown_cost_uses_tilde(self) -> None:
        """``cost_status='unknown'`` → ``~`` prefix (best-effort number)."""
        out = format_turn_footer(
            elapsed_ms=8_000,
            tool_calls=2,
            estimated_cost_usd=0.005,
            cost_status="unknown",
        )
        assert "~$0.0050" in out

    def test_format_turn_footer_none_status_uses_tilde(self) -> None:
        """``cost_status=None`` (data absent) → still tilde-prefixed."""
        out = format_turn_footer(
            elapsed_ms=8_000,
            tool_calls=2,
            estimated_cost_usd=0.001,
            cost_status=None,
        )
        assert "~$0.0010" in out

    def test_format_turn_footer_calculated_cost_no_tilde(self) -> None:
        """Authoritative cost (``calculated``) → no ``~`` prefix."""
        out = format_turn_footer(
            elapsed_ms=8_000,
            tool_calls=2,
            estimated_cost_usd=0.05,
            cost_status="calculated",
        )
        # No ~ before the $; a bare $0.0500.
        assert "~$" not in out
        assert "$0.0500" in out

    def test_format_turn_footer_one_tool_singular(self) -> None:
        """tool_calls=1 → 'tool call' (singular)."""
        out = format_turn_footer(
            elapsed_ms=2_000,
            tool_calls=1,
            estimated_cost_usd=None,
            cost_status=None,
        )
        assert "1 tool call" in out
        assert "tool calls" not in out  # plural must not leak

    def test_format_turn_footer_many_tools_plural(self) -> None:
        """tool_calls=N>1 → 'tool calls' (plural)."""
        out = format_turn_footer(
            elapsed_ms=2_000,
            tool_calls=5,
            estimated_cost_usd=None,
            cost_status=None,
        )
        assert "5 tool calls" in out

    def test_format_turn_footer_zero_tools_omits_count(self) -> None:
        """tool_calls=0 → no tool segment at all (a pure-chat turn)."""
        out = format_turn_footer(
            elapsed_ms=2_500,
            tool_calls=0,
            estimated_cost_usd=None,
            cost_status=None,
        )
        assert "tool" not in out
        assert out == "(elapsed: 2s)"

    def test_format_turn_footer_no_cost_no_tools(self) -> None:
        """Bare-minimum footer — just elapsed."""
        out = format_turn_footer(
            elapsed_ms=350,
            tool_calls=0,
            estimated_cost_usd=None,
            cost_status=None,
        )
        assert out == "(elapsed: 350ms)"

    def test_format_turn_footer_negative_elapsed_clamps_to_zero(self) -> None:
        """Clock skew at the source should never surface ``-2s`` to
        the user — ``format_elapsed_ms`` clamps and the footer inherits."""
        out = format_turn_footer(
            elapsed_ms=-10,
            tool_calls=0,
            estimated_cost_usd=None,
            cost_status=None,
        )
        assert out == "(elapsed: 0ms)"


class TestTryAppendFooter:
    def test_appends_with_double_newline(self) -> None:
        """The footer attaches on its own line via ``\\n\\n`` so it
        reads as a distinct closing annotation, not part of the prose."""
        out = try_append_footer("Hello world.", "(elapsed: 1s)")
        assert out == "Hello world.\n\n(elapsed: 1s)"

    def test_empty_footer_returns_message_unchanged(self) -> None:
        """No footer → no trailing whitespace, no separator."""
        out = try_append_footer("Hello.", "")
        assert out == "Hello."

    def test_empty_message_uses_footer_only(self) -> None:
        """The summary path on some channels might emit an empty body —
        the footer should still ship as the lone reply line."""
        out = try_append_footer("", "(elapsed: 1s)")
        assert out == "(elapsed: 1s)"

    def test_overflow_drops_footer(self) -> None:
        """When the composed body would exceed ``limit``, return the
        original message untouched — never lose user-facing content for
        a decorative observability line."""
        msg = "x" * (TEXT_LIMIT - 5)  # within cap, but adding footer overflows
        footer = "(elapsed: 1s · 3 tool calls · ~$0.0120)"
        out = try_append_footer(msg, footer, TEXT_LIMIT)
        assert out == msg
        assert "elapsed" not in out  # footer must not appear

    def test_exact_fit_keeps_footer(self) -> None:
        """A message whose composed length == limit should still ship
        the footer (boundary is inclusive)."""
        footer = "(elapsed: 1s)"
        sep = "\n\n"
        msg = "x" * (TEXT_LIMIT - len(footer) - len(sep))
        out = try_append_footer(msg, footer, TEXT_LIMIT)
        assert out.endswith(footer)
        assert len(out) == TEXT_LIMIT
