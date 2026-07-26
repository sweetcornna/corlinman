"""QQ multi-instance configuration and runtime support."""

from typing import TYPE_CHECKING, Any

from corlinman_server.gateway.qq_instances.admin import (
    QqAdminError,
    QqInstanceAdminService,
    QqInstanceSnapshot,
    RetainedQqInstanceStore,
)
from corlinman_server.gateway.qq_instances.config import (
    materialize_qq_fleet,
    normalize_qq_fleet,
)
from corlinman_server.gateway.qq_instances.models import (
    DEFAULT_QQ_INSTANCE_ID,
    InvalidQqInstanceId,
    QqFleetConfig,
    QqInstanceConfig,
    QqInstanceId,
    parse_instance_id,
)

if TYPE_CHECKING:
    from corlinman_server.gateway.qq_instances.runtime import (
        QqIdentityRegistry,
        QqRuntimeHandle,
        QqRuntimeRegistry,
    )


def __getattr__(name: str) -> Any:
    if name not in {"QqIdentityRegistry", "QqRuntimeHandle", "QqRuntimeRegistry"}:
        raise AttributeError(name)
    from corlinman_server.gateway.qq_instances import runtime

    return getattr(runtime, name)


__all__ = [
    "DEFAULT_QQ_INSTANCE_ID",
    "InvalidQqInstanceId",
    "QqAdminError",
    "QqFleetConfig",
    "QqIdentityRegistry",
    "QqInstanceAdminService",
    "QqInstanceConfig",
    "QqInstanceId",
    "QqInstanceSnapshot",
    "QqRuntimeHandle",
    "QqRuntimeRegistry",
    "RetainedQqInstanceStore",
    "materialize_qq_fleet",
    "normalize_qq_fleet",
    "parse_instance_id",
]
