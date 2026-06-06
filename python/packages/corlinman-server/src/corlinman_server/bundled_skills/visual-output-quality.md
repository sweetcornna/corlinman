---
name: visual-output-quality
description: Use when the agent produces PDF reports, images, posters, slides, screenshots, HTML exports, or any visual artifact where text can look ugly, overflow, or overlap.
metadata:
  openclaw:
    emoji: "🎯"
---

# Visual Output Quality

Apply this to every visual deliverable before sending it: PDF, report, image,
poster, slide, screenshot, HTML export, or generated graphic.

## Default Rules

- For PDFs and reports, use the `document-generator` path first: write Markdown,
  render with `corlinman-md2pdf`, then inspect the result before sending.
- For text-heavy images, posters, slides, and charts, prefer HTML/CSS/React or
  canvas/SVG for the text layer. Do not ask an image model to draw dense final
  text; use generated raster art only as background or illustration.
- For complex visual composition, pull/use `huashu-design`; this skill is only
  the compact always-on quality gate.

## Layout Contract

- Use stable dimensions: page size, aspect ratio, grid tracks, min/max widths,
  padding, and safe areas. Avoid layouts where text or badges can move outside
  their intended box.
- Text must have room to wrap. Use `min-width: 0`, `overflow-wrap: anywhere`,
  sensible line heights, and explicit max widths on labels, cards, and columns.
- Do not scale font size with viewport width. Do not use negative letter
  spacing. Avoid absolute positioning for flowing text unless every text box has
  a measured width/height and a safe fallback.
- The final artifact must have no overlap between readable text blocks, badges,
  counters, captions, or controls. If content is long, reduce hierarchy,
  reflow, wrap, truncate with intent, or move it to another line.

## Verification Before Sending

1. Render the final artifact at the actual output size.
2. Inspect a screenshot or rendered PDF page. Use Playwright when HTML is
   involved; for PDF, open/rasterize at least the first page or inspect the
   rendered output if tooling is available.
3. Check that the result is nonblank, readable, not cramped, and has no overlap.
4. If any element collides, overflows, clips, or makes the page ugly, revise the
   layout and render again. Send the file only after the visual check passes.
