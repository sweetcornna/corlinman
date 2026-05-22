"""Azure OpenAI Service provider adapter.

Azure exposes the OpenAI chat-completion API but with three deviations from
the public OpenAI endpoint:

* the base URL is per-resource — ``https://<resource>.openai.azure.com``;
* the model is addressed by a **deployment id** in the path,
  ``/openai/deployments/<deployment>/chat/completions``, rather than as a
  ``model`` body field;
* requests are authenticated with an ``api-key:`` header (not
  ``Authorization: Bearer``) and carry a mandatory ``api-version`` query
  parameter.

The wire body and the streaming SSE shape are otherwise identical to
OpenAI's, so this adapter subclasses :class:`OpenAIProvider` and only
overrides the client construction — the chat-completion stream loop and the
tool-call aggregation logic are inherited verbatim. We use the
``openai`` SDK's :class:`openai.AsyncAzureOpenAI` client, which already
implements deployment routing, ``api-version`` injection and ``api-key``
auth; ``model`` is passed as the deployment id.

Config (``[providers.<name>]`` with ``kind = "azure"``):

* ``base_url`` — the resource endpoint, e.g.
  ``https://my-resource.openai.azure.com``.
* ``api_key`` — the Azure resource key (or ``AZURE_OPENAI_API_KEY`` env).
* ``params.api_version`` *(optional)* — the API version; defaults to
  :data:`DEFAULT_API_VERSION`. Also read from ``AZURE_OPENAI_API_VERSION``.

The alias's ``model`` field carries the **deployment id** — that is what
Azure routes on, so callers configure ``[models.aliases.<x>] model =
"<deployment>"``.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import structlog

from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


#: Azure GA chat-completions API version used when a spec omits one. Kept
#: current with a GA (non-preview) release so tool calling is supported.
DEFAULT_API_VERSION = "2024-10-21"


class AzureProvider(OpenAIProvider):
    """Azure OpenAI Service adapter (OpenAI wire shape, Azure URL/auth)."""

    name: ClassVar[str] = "azure"
    kind: ClassVar[ProviderKind] = ProviderKind.AZURE

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        instance_name: str | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            env_key="AZURE_OPENAI_API_KEY",
        )
        self._api_version = (
            api_version
            or os.environ.get("AZURE_OPENAI_API_VERSION")
            or DEFAULT_API_VERSION
        )
        if instance_name:
            self.__dict__["name"] = instance_name

    @classmethod
    def build(cls, spec: ProviderSpec) -> AzureProvider:
        """Construct from a :class:`ProviderSpec`.

        ``base_url`` is required — it carries the resource endpoint and
        there is no sensible global default for it (every Azure resource
        has a unique hostname). ``params.api_version`` overrides the
        :data:`DEFAULT_API_VERSION` GA pin.
        """
        if not spec.base_url:
            raise ValueError(
                f"azure provider {spec.name!r} requires base_url in config "
                "(the resource endpoint, e.g. https://<resource>.openai.azure.com)"
            )
        api_version = spec.params.get("api_version") if spec.params else None
        return cls(
            api_key=spec.api_key,
            base_url=spec.base_url,
            api_version=api_version if isinstance(api_version, str) else None,
            instance_name=spec.name,
        )

    def _make_client(self) -> Any:
        """Build an :class:`openai.AsyncAzureOpenAI` client.

        Azure's client handles the ``/openai/deployments/<deployment>``
        path rewrite and the ``api-version`` query param — the model id
        passed to ``chat.completions.create`` is treated as the deployment
        id.

        Auth shape: Azure resource-key auth uses the ``api-key:`` request
        header, **not** ``Authorization: Bearer``. The openai SDK would
        otherwise emit a bearer header for a plain ``api_key`` (newer
        Azure resources accept that, but the documented / universally
        correct header is ``api-key``). We force it by injecting
        ``api-key`` via ``default_headers`` and suppressing the SDK's
        ``Authorization`` header with the SDK's ``Omit`` sentinel.
        """
        from openai import AsyncAzureOpenAI  # type: ignore[import-not-found]
        from openai._types import Omit  # type: ignore[import-not-found]

        # ``Omit`` is the SDK's "drop this header entirely" sentinel; the
        # SDK accepts it at runtime for any header value even though the
        # ``default_headers`` stub is typed ``dict[str, str]``.
        default_headers: dict[str, Any] = {
            "api-key": self._api_key or "",
            "Authorization": Omit(),
        }
        return AsyncAzureOpenAI(
            api_key=self._api_key,
            azure_endpoint=self._base_url or "",
            api_version=self._api_version,
            default_headers=default_headers,
        )

    @classmethod
    def supports(cls, model: str) -> bool:
        # Azure deployments are operator-named (any string) — never claim
        # a model via the legacy prefix fallback; always addressed via an
        # explicit alias whose ``model`` is the deployment id.
        return False


__all__ = ["DEFAULT_API_VERSION", "AzureProvider"]
