"""BUG-05 repro: read_file infinite re-read on a single line > MAX_READ_CHARS.

When line 1 is longer than MAX_READ_CHARS, the truncated slice contains zero
newlines, so complete_lines == 0 and next_offset == offset — the model is told
to "continue from next_offset" but that offset re-reads the exact same head,
an infinite paging loop. The fix advances next_offset to offset+1 and emits a
truncation marker for the over-long line.
"""

from __future__ import annotations

import json
from pathlib import Path

from corlinman_agent.coding._common import MAX_READ_CHARS
from corlinman_agent.coding.files import dispatch_read_file


def test_overlong_single_line_advances_offset(tmp_path: Path) -> None:
    p = tmp_path / "huge.txt"
    # One line, longer than MAX_READ_CHARS, followed by more lines.
    p.write_text(("A" * (MAX_READ_CHARS + 5000)) + "\nsecond line\nthird line\n")

    out = json.loads(
        dispatch_read_file(
            args_json=json.dumps({"path": "huge.txt"}),
            workspace=tmp_path,
        )
    )
    assert out["truncated"] is True
    # The bug: next_offset == offset (1) -> infinite loop. After the fix it
    # must advance past the over-long line.
    assert out["next_offset"] is not None
    assert out["next_offset"] > 1, (
        f"next_offset did not advance past the long line: {out['next_offset']}"
    )
    assert out["next_offset"] == 2


def test_overlong_line_emits_truncation_marker(tmp_path: Path) -> None:
    p = tmp_path / "huge.txt"
    p.write_text(("B" * (MAX_READ_CHARS + 5000)) + "\nsecond line\n")
    out = json.loads(
        dispatch_read_file(
            args_json=json.dumps({"path": "huge.txt"}),
            workspace=tmp_path,
        )
    )
    # The content must not exceed the read cap and should carry a marker
    # so the model knows the single line was clipped.
    assert len(out["content"]) <= MAX_READ_CHARS + 64
    assert "truncated" in out["content"].lower()
