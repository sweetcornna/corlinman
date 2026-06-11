"""OpenAI-compatible provider — the escape hatch for vLLM / Ollama / etc.

Any gateway that implements the OpenAI wire format can be wired up as a
:class:`[providers.<name>] kind = "openai_compatible"` entry pointing to
its own ``base_url``. The behaviour is identical to
:class:`OpenAIProvider` — only ``kind`` and ``name`` differ, and the
``base_url`` is **required** (validated at build time) rather than
defaulted to ``api.openai.com``.

Feature C (§1 of the contract) treats this as a first-class provider kind
so the admin UI can distinguish "built-in OpenAI" from "bring-your-own
OpenAI-wire-format gateway".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec


class OpenAICompatibleProvider(OpenAIProvider):
    """Bring-your-own OpenAI-wire-format provider.

    Instantiate via :meth:`build` from a spec whose ``kind`` is
    ``openai_compatible`` and whose ``base_url`` is set.
    """

    # ``name`` stays instance-settable so the registry can stamp it from
    # the spec (users pick their own names for local gateways).
    name: ClassVar[str] = "openai_compatible"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPATIBLE

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        env_key: str = "OPENAI_API_KEY",
        instance_name: str | None = None,
        image_model: str | None = None,
        image_capable: bool = False,
        tools_enabled: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("openai_compatible provider requires a base_url")
        # ``env_key`` is the env var consulted when no explicit ``api_key``
        # is given (and re-read on the reactive 401 path). The generic
        # openai_compatible kind keeps the historic ``OPENAI_API_KEY``
        # default; vendor wrappers (Mistral / Groq / …) MUST pass their own
        # vendor env var so a missing vendor key fails loudly instead of
        # silently sending the user's OpenAI bearer to a third-party host.
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            env_key=env_key,
            image_model=image_model,
            image_capable=image_capable,
        )
        # Operator-declared tool capability. ``tools = false`` on the
        # ``[providers.<name>].params`` block marks every model behind
        # this gateway as tool-less (e.g. a small local Ollama model that
        # 400s on a ``tools`` array) — see :meth:`supports_tools`.
        self._tools_enabled = tools_enabled
        # Shadow the class-level ``name`` so registry lookups (and the
        # logger attr below) report the user-chosen name. mypy complains
        # about re-assigning a ``ClassVar``, so we set it via __dict__.
        if instance_name:
            self.__dict__["name"] = instance_name

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAICompatibleProvider:
        if not spec.base_url:
            raise ValueError(
                f"openai_compatible provider {spec.name!r} requires base_url in config"
            )
        return cls(
            base_url=spec.base_url,
            api_key=spec.api_key,
            instance_name=spec.name,
            image_model=spec.image_model,
            image_capable=spec.image_capable,
            tools_enabled=tools_param_enabled(spec.params),
        )

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Same schema as :class:`OpenAIProvider` — pure OpenAI wire."""
        return OpenAIProvider.params_schema()

    @classmethod
    def supports(cls, model: str) -> bool:
        # openai_compatible never claims a model via the legacy prefix
        # fallback — it's always addressed explicitly via an alias.
        return False

    def supports_tools(self, model: str) -> bool:
        """Honour the operator's ``[providers.<name>].params tools = false``.

        A per-alias ``tools = false`` override travels separately, via the
        resolver's merged-params dict (popped by the servicer before the
        params reach the vendor SDK) — this method only reflects the
        provider-level declaration.
        """
        return self._tools_enabled


def tools_param_enabled(params: Mapping[str, Any]) -> bool:
    """Read the ``tools`` capability flag off a params mapping.

    Only an explicit ``tools = false`` disables tool support; absent /
    truthy / malformed values keep the historic always-on behaviour so
    existing configs are unaffected.
    """
    return params.get("tools") is not False
