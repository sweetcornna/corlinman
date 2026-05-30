"""``corlinman-md2pdf`` — turn Markdown into a clean, CJK-correct PDF.

The v1.12.3 fix for the "messy PDF" incident: when asked to "总结成 PDF" the
agent improvised (headless-chrome with the wrong flags, then ``reportlab``
which it couldn't install, then a hand-rolled raw-PDF script whose glyph
advances were wrong → every character space-padded). This renderer gives the
agent one reliable command instead.

Pipeline (no fragile assumptions):

1. Markdown → HTML via the ``markdown`` package when it happens to be
   installed, else a small self-contained converter (no declared dependency,
   so the command works on any deploy).
2. Wrap the HTML in a print template whose ``font-family`` lists installed
   CJK fonts (Noto / Source Han / WenQuanYi) so Chinese renders as real
   glyphs with correct spacing — the actual cause of the garbled output.
3. Render HTML → PDF via an engine ladder:
   * WeasyPrint, when importable (best text shaping); else
   * headless Chrome / Chromium with the flags that actually work on the
     box (``--headless=new --no-sandbox --disable-gpu
     --disable-dev-shm-usage``).
4. Verify the output starts with ``%PDF`` and is non-empty.

Usage:
    corlinman-md2pdf REPORT.md REPORT.pdf [--title "标题"]
"""

from __future__ import annotations

import argparse
import html as _html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: CJK-capable font stack. Chrome/WeasyPrint pick the first one fontconfig can
#: resolve. WenQuanYi ships on the prod VPS; Noto/Source Han are common
#: elsewhere. Without an explicit CJK family the renderer falls back to a
#: Latin font and Chinese turns into tofu or mis-spaced glyphs.
_FONT_STACK = (
    "'Noto Sans CJK SC','Noto Sans SC','Source Han Sans SC',"
    "'WenQuanYi Micro Hei','WenQuanYi Zen Hei','Microsoft YaHei',"
    "'PingFang SC',sans-serif"
)

_CSS_TEMPLATE = """
@page {{ size: A4; margin: 18mm 16mm; }}
* {{ box-sizing: border-box; }}
body {{
  font-family: {font};
  font-size: 11pt;
  line-height: 1.7;
  color: #1a1a1a;
  margin: 0;
}}
h1 {{ font-size: 21pt; margin: 0 0 .4em; line-height: 1.25; }}
h2 {{ font-size: 15pt; margin: 1.2em 0 .4em; border-bottom: 1px solid #ddd; padding-bottom: .2em; }}
h3 {{ font-size: 12.5pt; margin: 1em 0 .35em; }}
h4 {{ font-size: 11pt; margin: .9em 0 .3em; color: #444; }}
p {{ margin: .45em 0; }}
ul, ol {{ margin: .4em 0; padding-left: 1.5em; }}
li {{ margin: .2em 0; }}
a {{ color: #1a5fb4; text-decoration: none; word-break: break-all; }}
code {{ font-family: 'DejaVu Sans Mono','Noto Sans Mono',monospace; font-size: 9.5pt;
        background: #f4f4f4; padding: .1em .3em; border-radius: 3px; }}
pre {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
       padding: 10px 12px; overflow-x: auto; }}
pre code {{ background: none; padding: 0; }}
blockquote {{ margin: .6em 0; padding: .2em 1em; border-left: 3px solid #ccc; color: #555; }}
table {{ border-collapse: collapse; width: 100%; margin: .6em 0; font-size: 10pt; }}
th, td {{ border: 1px solid #d0d7de; padding: 5px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f3f6; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 1.1em 0; }}
img {{ max-width: 100%; }}
""".strip()

_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>{body}</body></html>"""


def _markdown_to_html(md_text: str) -> str:
    """Render Markdown to an HTML fragment.

    Prefers the ``markdown`` package (tables, fenced code, sane lists). Falls
    back to :func:`_minimal_markdown` when it isn't importable so the command
    still produces a readable document on a not-yet-synced deploy.
    """
    try:
        import markdown  # noqa: PLC0415

        return str(
            markdown.markdown(
                md_text,
                extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
                output_format="html5",
            )
        )
    except Exception:  # noqa: BLE001 — any import/parse failure → built-in
        return _minimal_markdown(md_text)


def _inline(text: str) -> str:
    """Escape HTML then apply inline Markdown (code, bold, italic, links)."""
    text = _html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"(?<![\"'>=])(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)
    return text


def _minimal_markdown(md_text: str) -> str:
    """Self-contained Markdown subset → HTML (no third-party dep).

    Covers what reports actually use: ATX headings, fenced code blocks, GFM
    pipe tables, ordered/unordered lists, blockquotes, horizontal rules, and
    paragraphs with inline formatting. Deliberately small — the ``markdown``
    package is the primary path; this is the safety net.
    """
    lines = md_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    list_stack: list[str] = []  # 'ul' / 'ol'

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    while i < n:
        line = lines[i]
        # Fenced code block.
        if line.startswith("```"):
            close_lists()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(_html.escape(lines[i], quote=False))
                i += 1
            i += 1  # skip closing fence
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue
        # GFM table: header row + separator row of ---|--- .
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]+$", lines[i + 1]):
            close_lists()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_inline(c)}</th>" for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        stripped = line.strip()
        if not stripped:
            close_lists()
            i += 1
            continue
        # Headings.
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            close_lists()
            out.append(f"<blockquote>{_inline(stripped.lstrip('> '))}</blockquote>")
            i += 1
            continue
        # Unordered / ordered list items.
        ul = re.match(r"^[-*+]\s+(.*)$", stripped)
        ol = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        list_match = ul or ol
        if list_match is not None:
            want = "ul" if ul else "ol"
            if not list_stack or list_stack[-1] != want:
                close_lists()
                out.append(f"<{want}>")
                list_stack.append(want)
            out.append(f"<li>{_inline(list_match.group(1))}</li>")
            i += 1
            continue
        # Paragraph.
        close_lists()
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    close_lists()
    return "\n".join(out)


def _find_chrome() -> str | None:
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(name)
        if path:
            return path
    # Well-known absolute locations (macOS dev boxes, snap/flatpak Linux) that
    # aren't always on PATH. The prod VPS has ``google-chrome`` on PATH so the
    # loop above already covers it; these just make local testing work.
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _render_with_weasyprint(html: str, out_path: Path) -> bool:
    try:
        from weasyprint import HTML  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — not installed / missing system libs
        return False
    try:
        HTML(string=html).write_pdf(str(out_path))
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        print(f"[md2pdf] weasyprint failed, falling back: {exc}", file=sys.stderr)
        return False


def _render_with_chrome(html: str, out_path: Path) -> bool:
    chrome = _find_chrome()
    if not chrome:
        return False
    with tempfile.TemporaryDirectory() as td:
        html_file = Path(td) / "doc.html"
        html_file.write_text(html, encoding="utf-8")
        cmd = [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-zygote",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_path}",
            html_file.as_uri(),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)  # noqa: S603
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[md2pdf] chrome invocation failed: {exc}", file=sys.stderr)
            return False
        if proc.returncode != 0:
            # Retry without the newer flags for older Chrome builds.
            cmd_fallback = [
                chrome,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                f"--print-to-pdf={out_path}",
                html_file.as_uri(),
            ]
            try:
                proc = subprocess.run(cmd_fallback, capture_output=True, timeout=120)  # noqa: S603
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(f"[md2pdf] chrome fallback failed: {exc}", file=sys.stderr)
                return False
        return out_path.exists() and out_path.stat().st_size > 0


def render_markdown_to_pdf(md_path: Path, pdf_path: Path, *, title: str = "") -> Path:
    """Render ``md_path`` to ``pdf_path``. Returns the output path.

    Raises :class:`RuntimeError` with an actionable message if no rendering
    engine is available or the produced file isn't a valid PDF — so the agent
    sees a clear failure instead of silently shipping garbage.
    """
    md_text = md_path.read_text(encoding="utf-8")
    if not title:
        m = re.search(r"^#\s+(.+)$", md_text, flags=re.MULTILINE)
        title = m.group(1).strip() if m else md_path.stem
    body = _markdown_to_html(md_text)
    full_html = _HTML_TEMPLATE.format(
        title=_html.escape(title, quote=True),
        css=_CSS_TEMPLATE.format(font=_FONT_STACK),
        body=body,
    )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if not (_render_with_weasyprint(full_html, pdf_path) or _render_with_chrome(full_html, pdf_path)):
        raise RuntimeError(
            "no working PDF engine: install weasyprint, or ensure "
            "google-chrome/chromium is on PATH. Do NOT hand-roll a PDF."
        )
    # Validate the magic bytes — a 0-return engine can still emit junk.
    with pdf_path.open("rb") as fh:
        head = fh.read(5)
    if not head.startswith(b"%PDF"):
        raise RuntimeError(f"output is not a valid PDF (header={head!r})")
    return pdf_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corlinman-md2pdf",
        description="Render a Markdown file to a clean, CJK-correct PDF.",
    )
    parser.add_argument("input", help="input Markdown file (.md)")
    parser.add_argument("output", help="output PDF file (.pdf)")
    parser.add_argument("--title", default="", help="document title (default: first H1 or filename)")
    args = parser.parse_args(argv)

    md_path = Path(args.input)
    if not md_path.is_file():
        print(f"[md2pdf] input not found: {md_path}", file=sys.stderr)
        return 2
    try:
        out = render_markdown_to_pdf(md_path, Path(args.output), title=args.title)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(f"[md2pdf] ERROR: {exc}", file=sys.stderr)
        return 1
    size = out.stat().st_size
    print(f"[md2pdf] wrote {out} ({size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
