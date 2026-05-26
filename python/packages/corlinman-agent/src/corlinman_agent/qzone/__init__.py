"""``qzone_publish`` builtin tool — post a 说说 to QQ空间 (W5).

Backs PLAN_PERSONA_STUDIO W5.2. Drives the QZone web endpoints
``cgi_upload_image`` + ``emotion_cgi_publish_v6`` using the QQ login
state borrowed from a running NapCat / Lagrange.Core instance via the
OneBot HTTP API (see :mod:`corlinman_agent.onebot.client`).

Public surface
--------------
* :data:`QZONE_PUBLISH_TOOL` — wire-stable tool name.
* :func:`qzone_publish_tool_schema` — OpenAI tool descriptor for the
  builtin schema injector.
* :func:`dispatch_qzone_publish` — async dispatcher; takes args_json
  + optional ``generate`` arg, returns a JSON envelope string.
* :class:`QZoneError` — :class:`RuntimeError` subclass surfaced from
  the QZone upload / publish primitives.
"""

from __future__ import annotations

from corlinman_agent.qzone.publish import (
    QZONE_PUBLISH_TOOL,
    QZoneError,
    dispatch_qzone_publish,
    qzone_publish_tool_schema,
)

__all__ = [
    "QZONE_PUBLISH_TOOL",
    "QZoneError",
    "dispatch_qzone_publish",
    "qzone_publish_tool_schema",
]
