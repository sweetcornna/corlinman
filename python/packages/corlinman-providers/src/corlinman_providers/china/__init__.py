"""China-bucket LLM adapters: DeepSeek, Qwen (DashScope), GLM (智谱), Moonshot.

All four share an OpenAI-compatible REST shape but differ in ``base_url``
and auth env var; each concrete adapter is a thin subclass of
:class:`corlinman_providers.openai_provider.OpenAIProvider`. DeepSeek /
Qwen / GLM additionally route through
:class:`corlinman_providers.china._errors.ChinaOpenAIProvider`, which
re-maps vendor-specific error bodies (DeepSeek 402 balance, DashScope
throttling, GLM business codes) to the right
:mod:`corlinman_providers.failover` class.
"""

from __future__ import annotations

from corlinman_providers.china._errors import map_china_error
from corlinman_providers.china.deepseek import DeepSeekProvider
from corlinman_providers.china.glm import GLMProvider
from corlinman_providers.china.moonshot import MoonshotProvider
from corlinman_providers.china.qwen import QwenProvider

__all__ = [
    "DeepSeekProvider",
    "GLMProvider",
    "MoonshotProvider",
    "QwenProvider",
    "map_china_error",
]
