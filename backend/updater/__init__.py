"""Self-update check and apply orchestration for PaperBridge Local."""

from backend.updater.manager import (
    DEFAULT_UPDATE_BRANCH,
    DEFAULT_UPDATE_REPO,
    SystemUpdater,
    UpdateApplyResponse,
    UpdateCheckResponse,
    UpdateConflictError,
    UpdateLaunchError,
    get_local_version,
)

__all__ = [
    "DEFAULT_UPDATE_BRANCH",
    "DEFAULT_UPDATE_REPO",
    "SystemUpdater",
    "UpdateApplyResponse",
    "UpdateCheckResponse",
    "UpdateConflictError",
    "UpdateLaunchError",
    "get_local_version",
]
