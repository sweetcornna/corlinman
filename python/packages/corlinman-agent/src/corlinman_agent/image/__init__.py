"""Image-generation builtin — reference-conditioned PNG output.

Backs PLAN_PERSONA_STUDIO W4. Provides a single ``image_with_refs``
tool that pulls character references from a persona's reference asset
bucket, drives the configured image-generation provider (OpenAI
Responses API + ``gpt-image-1`` today), and returns a path inside the
agent workspace so the existing ``send_attachment`` resolver delivers
the PNG to the user without any extra plumbing.

Public surface
--------------
* :data:`IMAGE_WITH_REFS_TOOL` — wire-stable tool name.
* :func:`image_with_refs_tool_schema` — OpenAI tool descriptor for the
  builtin schema injector.
* :func:`dispatch_image_with_refs` — async dispatcher; takes the
  active provider + both persona stores, returns a JSON envelope.
* :func:`generate_with_refs` — lower-level helper a future scheduler
  builtin (W6 ``qzone.daily_publish``) can call directly without going
  through the model-facing tool surface.
"""

from __future__ import annotations

from corlinman_agent.image.dispatch import (
    IMAGE_WITH_REFS_TOOL,
    dispatch_image_with_refs,
    image_with_refs_tool_schema,
)
from corlinman_agent.image.generate import (
    ImageGenerationError,
    ImageProviderUnavailable,
    generate_with_refs,
)

__all__ = [
    "IMAGE_WITH_REFS_TOOL",
    "ImageGenerationError",
    "ImageProviderUnavailable",
    "dispatch_image_with_refs",
    "generate_with_refs",
    "image_with_refs_tool_schema",
]
