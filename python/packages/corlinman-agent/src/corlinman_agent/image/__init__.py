"""Image-generation builtins — reference-conditioned + plain output.

Two sibling tools share the same OpenAI Responses backend
(``gpt-image-1``):

* ``image_with_refs`` — generation conditioned on a persona's reference
  pack. Backs PLAN_PERSONA_STUDIO W4 and is the path :mod:`qzone` uses
  internally when publishing illustrated 说说 posts.
* ``image_generate`` — plain text-to-image generation. No persona /
  asset_store wiring; the agent reaches for it when there is no
  suitable reference pack to condition on. Intentionally kept
  **isolated** from :mod:`qzone` so a regression in either flow cannot
  leak into the other.

Public surface
--------------
* :data:`IMAGE_WITH_REFS_TOOL`, :data:`IMAGE_GENERATE_TOOL` — wire-
  stable tool names.
* :func:`image_with_refs_tool_schema`, :func:`image_generate_tool_schema`
  — OpenAI tool descriptors for the builtin schema injector.
* :func:`dispatch_image_with_refs`, :func:`dispatch_image_generate` —
  async dispatchers; the refs variant takes the persona stores, the
  plain variant takes only a provider.
* :func:`generate_with_refs`, :func:`generate_plain` — lower-level
  provider helpers a scheduler builtin can call directly without going
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
    generate_plain,
    generate_with_refs,
)
from corlinman_agent.image.plain import (
    IMAGE_GENERATE_TOOL,
    dispatch_image_generate,
    image_generate_tool_schema,
)

__all__ = [
    "IMAGE_GENERATE_TOOL",
    "IMAGE_WITH_REFS_TOOL",
    "ImageGenerationError",
    "ImageProviderUnavailable",
    "dispatch_image_generate",
    "dispatch_image_with_refs",
    "generate_plain",
    "generate_with_refs",
    "image_generate_tool_schema",
    "image_with_refs_tool_schema",
]
