"""Managed NapCat lifecycle boundary."""

from corlinman_server.system.napcat_manager.client import NapCatManagerClient
from corlinman_server.system.napcat_manager.inventory import NapCatInventory
from corlinman_server.system.napcat_manager.manager import NapCatManager
from corlinman_server.system.napcat_manager.models import (
    ManagerRequest,
    ManagerResponse,
    NapCatDescriptor,
    NapCatInstanceRecord,
    NapCatObservedState,
)
from corlinman_server.system.napcat_manager.protocol import (
    NapCatGenerationConflict,
    NapCatInstanceConflict,
    NapCatInstanceNotFound,
    NapCatManagerError,
    NapCatManagerUnavailable,
    NapCatResourceNotOwned,
    NapCatUnsupportedOperation,
)

__all__ = [
    "ManagerRequest",
    "ManagerResponse",
    "NapCatDescriptor",
    "NapCatGenerationConflict",
    "NapCatInstanceConflict",
    "NapCatInstanceNotFound",
    "NapCatInstanceRecord",
    "NapCatInventory",
    "NapCatManager",
    "NapCatManagerClient",
    "NapCatManagerError",
    "NapCatManagerUnavailable",
    "NapCatObservedState",
    "NapCatResourceNotOwned",
    "NapCatUnsupportedOperation",
]
