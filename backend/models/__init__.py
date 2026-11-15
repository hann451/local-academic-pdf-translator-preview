"""Shared model catalog loader and registry exports for PaperBridge Local."""

from backend.models.catalog import (
    CatalogArtifact,
    CatalogModel,
    ModelCatalog,
    ModelLaunchSettings,
    ModelLicenseInfo,
    default_catalog_path,
    load_model_catalog,
)
from backend.models.gguf_inspector import GgufInspectionResult, inspect_gguf_file
from backend.models.registry import (
    ModelDownloadJob,
    ModelDownloadManager,
    ModelRegistryManager,
    ModelServerMatchResult,
    ModelVerificationState,
    RegisteredModel,
)

__all__ = [
    "CatalogArtifact",
    "CatalogModel",
    "GgufInspectionResult",
    "ModelCatalog",
    "ModelDownloadJob",
    "ModelDownloadManager",
    "ModelLaunchSettings",
    "ModelLicenseInfo",
    "ModelRegistryManager",
    "ModelServerMatchResult",
    "ModelVerificationState",
    "RegisteredModel",
    "default_catalog_path",
    "inspect_gguf_file",
    "load_model_catalog",
]
