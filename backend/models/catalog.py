"""Single-source model catalog loader shared across backend, launcher, and UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class CatalogArtifact(BaseModel):
    """Pinned upstream release artifact metadata (revision, size, SHA-256)."""

    revision: str
    size_bytes: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    label: str = "official"


class ModelLicenseInfo(BaseModel):
    """Per-model license metadata rendered dynamically without hardcoded model branding."""

    id: str
    name: str
    tagline: str | None = None
    url_ja: str | None = None
    url_en: str | None = None
    model_card_url: str | None = None
    summary_ja: str = ""
    disclosure_note_ja: str | None = None
    requires_agreement: bool = True


class ModelLaunchSettings(BaseModel):
    """Consistent launch parameters passed to llama-server for this model."""

    alias: str
    context_length: int = Field(default=8192, ge=1024, le=32768)
    parallel_slots: int = Field(default=1, ge=1)
    gpu_layers_auto: int = Field(default=99, ge=0)
    extra_args: list[str] = Field(default_factory=list)


class CatalogModel(BaseModel):
    """Curated model definition separated cleanly from local machine registrations."""

    model_id: str
    display_name: str
    short_name: str
    provider: str
    family: str
    parameter_size: str
    quantization: str
    is_default: bool = False
    source_repo: str
    source_repo_type: str = "huggingface"
    pinned_revision: str
    filename: str
    relative_subdir: str
    download_url: str
    model_card_url: str
    known_size_bytes: int = Field(..., gt=0)
    known_sha256: str = Field(..., min_length=64, max_length=64)
    accepted_artifacts: list[CatalogArtifact] = Field(default_factory=list)
    supported_backends: list[str] = Field(default_factory=lambda: ["llama.cpp"])
    expected_gguf_architecture: str = "plamo3"
    expected_gguf_name_prefix: str = ""
    prompt_format: str = "pfml3"
    prompt_version: str = "pfml3-v1"
    stop_tokens: list[str] = Field(default_factory=lambda: ["<|plamo:tag|>", "<|plamo:bos|>"])
    context_length_default: int = Field(default=8192, ge=1024, le=32768)
    context_length_max: int = Field(default=32768, ge=1024, le=262144)
    launch_settings: ModelLaunchSettings
    catalog_verification_status: Literal[
        "verified_runtime",
        "catalog_verified_runtime_unverified",
        "experimental",
    ] = "catalog_verified_runtime_unverified"
    hardware_note_ja: str = ""
    license: ModelLicenseInfo

    def all_accepted_artifacts(self) -> list[CatalogArtifact]:
        items = list(self.accepted_artifacts)
        primary_sha = self.known_sha256.lower()
        if not any(a.sha256.lower() == primary_sha for a in items):
            items.insert(
                0,
                CatalogArtifact(
                    revision=self.pinned_revision,
                    size_bytes=self.known_size_bytes,
                    sha256=primary_sha,
                    label="primary",
                ),
            )
        return items

    def match_artifact_by_sha256(self, sha256_hex: str) -> CatalogArtifact | None:
        normalized = sha256_hex.strip().lower()
        for art in self.all_accepted_artifacts():
            if art.sha256.lower() == normalized:
                return art
        return None


class ModelCatalog(BaseModel):
    schema_version: int = 1
    catalog_id: str = "paperbridge-local-models"
    models: list[CatalogModel] = Field(default_factory=list)
    unsupported_policies: dict[str, str] = Field(default_factory=dict)

    def get_model(self, model_id: str) -> CatalogModel | None:
        for item in self.models:
            if item.model_id == model_id:
                return item
        return None

    def get_default_model(self) -> CatalogModel | None:
        for item in self.models:
            if item.is_default:
                return item
        return self.models[0] if self.models else None

    def match_by_sha256(self, sha256_hex: str) -> tuple[CatalogModel, CatalogArtifact] | None:
        for item in self.models:
            art = item.match_artifact_by_sha256(sha256_hex)
            if art is not None:
                return item, art
        return None


def default_catalog_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "model_catalog.json").resolve()


def load_model_catalog(catalog_path: Path | None = None) -> ModelCatalog:
    target = Path(catalog_path) if catalog_path else default_catalog_path()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return ModelCatalog.model_validate(raw)
