"""Operator/agent-facing document tooling.

Currently:

* :mod:`corlinman_server.tools.doc_render` — the ``corlinman-md2pdf``
  console script + in-process renderer that turns Markdown into a clean,
  CJK-correct PDF (the v1.12.3 fix for garbled report output).
* :mod:`corlinman_server.tools.render_document` — the ``render_document``
  builtin tool that exposes that pipeline directly to the model (Markdown
  in → verified PDF in the agent workspace), replacing the always-on
  ``document-generator`` skill prose with an interface whose shape
  prevents hand-rolled PDFs.
"""
