"""``qzone_*`` builtin tools — post + read + comment on QQ空间.

Backs PLAN_PERSONA_STUDIO W5.2 (publish) and the persona-life migration
(read + comment). Drives the reverse-engineered QZone web endpoints using
the QQ login state borrowed from a running NapCat / Lagrange.Core instance
via the OneBot HTTP API (see :mod:`corlinman_agent.onebot.client`).

Public surface
--------------
* :data:`QZONE_PUBLISH_TOOL` + :func:`qzone_publish_tool_schema` +
  :func:`dispatch_qzone_publish` — post a 说说 (text / images / generated).
* :data:`QZONE_COMMENT_TOOLS` + :func:`qzone_comment_tool_schemas` +
  the ``dispatch_qzone_{list_feed,get_post,post_comment,list_friends}``
  dispatchers — read the 好友动态 timeline and comment on posts.
* :class:`QZoneError` — :class:`RuntimeError` subclass surfaced from the
  QZone upload / publish primitives.
"""

from __future__ import annotations

from corlinman_agent.qzone.comment import (
    QZONE_COMMENT_TOOLS,
    QZONE_GET_POST_TOOL,
    QZONE_LIST_FEED_TOOL,
    QZONE_LIST_FRIENDS_TOOL,
    QZONE_POST_COMMENT_TOOL,
    dispatch_qzone_get_post,
    dispatch_qzone_list_feed,
    dispatch_qzone_list_friends,
    dispatch_qzone_post_comment,
    qzone_comment_tool_schemas,
)
from corlinman_agent.qzone.publish import (
    QZONE_PUBLISH_TOOL,
    QZoneError,
    dispatch_qzone_publish,
    qzone_publish_tool_schema,
)

__all__ = [
    "QZONE_COMMENT_TOOLS",
    "QZONE_GET_POST_TOOL",
    "QZONE_LIST_FEED_TOOL",
    "QZONE_LIST_FRIENDS_TOOL",
    "QZONE_POST_COMMENT_TOOL",
    "QZONE_PUBLISH_TOOL",
    "QZoneError",
    "dispatch_qzone_get_post",
    "dispatch_qzone_list_feed",
    "dispatch_qzone_list_friends",
    "dispatch_qzone_post_comment",
    "dispatch_qzone_publish",
    "qzone_comment_tool_schemas",
    "qzone_publish_tool_schema",
]
