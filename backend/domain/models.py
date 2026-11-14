"""Core domain models and validation schemas for PaperBridge Local.

Implements all entities required by Section 14 and Section 15:
Document, Page, Block, ProtectionRegion, Translation, Glossary, Job,
ValidationIssue, ExportArtifact, and SystemDiagnostics.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BlockKind(str, enum.Enum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CAPTION = "caption"
    ISOLATED_FORMULA = "isolated_formula"
    FIGURE = "figure"
    TABLE = "table"
    REFERENCE = "reference"
    HEADER_FOOTER = "header_footer"
    UNKNOWN = "unknown"


class BlockStatus(str, enum.Enum):
    PENDING = "pending"
    PROTECTED_SKIP = "protected_skip"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    MANUALLY_EDITED = "manually_edited"
    FALLBACK_ORIGINAL = "fallback_original"
    FAILED = "failed"


class ProtectionMode(str, enum.Enum):
    DO_NOT_TRANSLATE = "do_not_translate"
    TRANSLATE_AS_BODY = "translate_as_body"
    PROTECT_AS_FORMULA = "protect_as_formula"
    PROTECT_AS_FIGURE = "protect_as_figure"


class JobState(str, enum.Enum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    PARSING = "parsing"
    AWAITING_REVIEW = "awaiting_review"
    TRANSLATING = "translating"
    VALIDATING = "validating"
    TYPESETTING = "typesetting"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


VALID_JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.CREATED: {
        JobState.PREFLIGHT,
        JobState.CANCELED,
        JobState.FAILED,
    },
    JobState.PREFLIGHT: {
        JobState.PARSING,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.PARSING: {
        JobState.AWAITING_REVIEW,
        JobState.TRANSLATING,
        JobState.TYPESETTING,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.AWAITING_REVIEW: {
        JobState.TRANSLATING,
        JobState.TYPESETTING,
        JobState.CANCELED,
        JobState.FAILED,
    },
    JobState.TRANSLATING: {
        JobState.VALIDATING,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.VALIDATING: {
        JobState.TYPESETTING,
        JobState.TRANSLATING,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.TYPESETTING: {
        JobState.COMPLETED,
        JobState.COMPLETED_WITH_WARNINGS,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.INTERRUPTED: {
        JobState.PREFLIGHT,
        JobState.PARSING,
        JobState.TRANSLATING,
        JobState.VALIDATING,
        JobState.TYPESETTING,
        JobState.CANCELED,
        JobState.FAILED,
    },
    JobState.CANCELED: {
        JobState.TRANSLATING,
        JobState.PREFLIGHT,
    },
    JobState.FAILED: {
        JobState.PREFLIGHT,
        JobState.TRANSLATING,
    },
    JobState.COMPLETED: {
        JobState.TRANSLATING,
        JobState.TYPESETTING,
    },
    JobState.COMPLETED_WITH_WARNINGS: {
        JobState.TRANSLATING,
        JobState.TYPESETTING,
    },
}


class IssueSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ProtectedItemKind(str, enum.Enum):
    INLINE_FORMULA = "inline_formula"
    GREEK_OR_MATH_SYMBOL = "greek_or_math_symbol"
    SUB_SUPERSCRIPT = "sub_superscript"
    EQUATION_NUMBER = "equation_number"
    CITATION_REF = "citation_ref"
    URL_OR_DOI = "url_or_doi"
    USER_PROTECTED = "user_protected"


class ProtectedItem(BaseModel):
    placeholder: str = Field(..., description="Placeholder token such as {v1}")
    kind: ProtectedItemKind
    original_text: str
    bbox: list[float] | None = None
    font_name: str | None = None

    @field_validator("bbox")
    @classmethod
    def _validate_bbox(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return None
        if len(v) != 4:
            raise ValueError("bbox must contain [x0, y0, x1, y1]")
        if v[2] < v[0] or v[3] < v[1]:
            raise ValueError(f"Invalid bbox coordinates: {v}")
        return [float(x) for x in v]


class InlineMathSpan(BaseModel):
    """Structured geometric/typographic annotation for an inline mathematical element
    (superscript, subscript, circumflex hat over base symbol, or radical vinculum overbar)
    extracted from the original PDF's span coordinates and vector drawings.
    """

    context_text: str = Field(
        ...,
        description="Normalized substring context in the block text (e.g. '𝑎2', '𝐿𝑐𝑙𝑠', 'ˆ𝜙', '√(𝑎2 + 𝑏2)')",
    )
    target_text: str = Field(
        ...,
        description="The specific substring within context_text that receives this styling (e.g. '2', '𝑐𝑙𝑠', 'ˆ', '(𝑎2 + 𝑏2)')",
    )
    role: Literal["super", "sub", "hat", "radicand"]
    scale: float = Field(default=1.0, gt=0)
    dy_em: float = Field(
        default=0.0,
        description="Vertical offset relative to font_size_pt (negative = up, positive = down)",
    )
    overbar: bool = Field(
        default=False,
        description="Whether to draw a horizontal radical vinculum over target_text",
    )
    orig_overbar_bbox: list[float] | None = Field(
        default=None,
        description="Original PDF bbox [x0, y0, x1, y1] of the inline radical vinculum bar to clear on redaction",
    )
    ambiguous: bool = Field(
        default=False,
        description="True when conflicting super/sub/normal roles exist for this context in the block",
    )


class ContinuationCandidate(BaseModel):
    counterpart_block_id: str
    direction: Literal["previous", "next"]
    relation: Literal[
        "same_page_cross_column",
        "adjacent_pages_two_column",
        "adjacent_pages_one_column",
    ]
    confidence: Literal["high", "review_candidate"]
    reasons: list[str] = Field(default_factory=list)
    use_as_context: bool = False


class Block(BaseModel):
    """Canonical block representation matching Section 15 schema."""

    id: str = Field(..., description="Format: document-id:page-N:block-M (1-indexed page in ID)")
    page_index: int = Field(..., ge=0, description="0-indexed internal page index")
    bbox: list[float] = Field(..., description="[x0, y0, x1, y1] in backend PDF top-left pt coordinates")
    kind: BlockKind = BlockKind.PARAGRAPH
    reading_order: int = Field(default=0, ge=0)
    column_index: int = Field(default=0, ge=0)
    source_text: str
    masked_source_text: str | None = None
    protected_items: list[ProtectedItem] = Field(default_factory=list)
    inline_math_spans: list[InlineMathSpan] = Field(default_factory=list)
    continuation_candidates: list[ContinuationCandidate] = Field(default_factory=list)
    translation_text: str | None = None
    manual_translation_text: str | None = None
    status: BlockStatus = BlockStatus.PENDING
    warnings: list[str] = Field(default_factory=list)
    font_size_pt: float = Field(default=10.0, gt=0)
    fitted_font_size_pt: float | None = None
    is_uncertain_region: bool = False
    protection_source: str = Field(default="auto", description="'auto' | 'manual' | 'none'")
    semantic_category: str = Field(
        default="body",
        description="Academic layout role: 'title' | 'author' | 'affiliation' | 'email_or_contact' | 'publishing_metadata' | 'running_header' | 'running_footer' | 'reference_heading' | 'reference' | 'abstract' | 'keywords' | 'acknowledgements' | 'section_heading' | 'body' | 'caption' | 'formula' | 'figure' | 'table'",
    )
    protection_reason: str | None = Field(
        default=None,
        description="Human-readable reason for protection or override (e.g. '自動：論文タイトル')",
    )
    protection_rule_id: str | None = Field(
        default=None,
        description="Rule ID that triggered auto-protection (e.g. 'RULE_FRONTMATTER_TITLE')",
    )
    protection_confidence: str = Field(
        default="high",
        description="'high' | 'review_candidate'",
    )
    user_protection_override: str | None = Field(
        default=None,
        description="Explicit user block-level override: 'force_translate' | 'force_protect' | None",
    )
    translation_provenance: str = Field(
        default="none",
        description="'llm_inference' | 'cache_hit' | 'manual_edit' | 'reused_block' | 'protected_skip' | 'fallback_original' | 'none'",
    )
    edit_revision: int = Field(default=0, ge=0)
    manual_validation_errors: list[str] = Field(default_factory=list)
    manual_validation_warnings: list[str] = Field(default_factory=list)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float]) -> list[float]:
        if len(v) != 4:
            raise ValueError("bbox must have 4 elements [x0, y0, x1, y1]")
        x0, y0, x1, y1 = (float(x) for x in v)
        if x1 < x0 or y1 < y0:
            raise ValueError(f"bbox x1>=x0 and y1>=y0 required, got {v}")
        return [x0, y0, x1, y1]

    @property
    def display_page_number(self) -> int:
        """1-indexed page number for UI display."""
        return self.page_index + 1

    @property
    def effective_translation(self) -> str | None:
        """Manual edit takes precedence over model translation."""
        if self.manual_translation_text is not None:
            return self.manual_translation_text
        return self.translation_text


class ProtectionRegion(BaseModel):
    """Manual or detected protection region on a page (Section 7 & 8)."""

    id: str
    document_id: str
    page_index: int = Field(..., ge=0)
    bbox: list[float] = Field(..., description="[x0, y0, x1, y1] in backend PDF top-left pt coordinates")
    mode: ProtectionMode
    source: str = Field(default="manual", description="'manual' or 'auto'")
    note: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v: list[float]) -> list[float]:
        if len(v) != 4:
            raise ValueError("bbox must have 4 elements [x0, y0, x1, y1]")
        x0, y0, x1, y1 = (float(x) for x in v)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"ProtectionRegion bbox must have positive area: {v}")
        return [x0, y0, x1, y1]


class Page(BaseModel):
    """Page metadata and coordinate box records (Section 8 & 15)."""

    document_id: str
    page_index: int = Field(..., ge=0, description="0-indexed internal representation")
    width_pt: float = Field(..., gt=0)
    height_pt: float = Field(..., gt=0)
    rotation_deg: int = Field(default=0)
    media_box: list[float] = Field(..., min_length=4, max_length=4)
    crop_box: list[float] = Field(..., min_length=4, max_length=4)
    has_extractable_text: bool = True
    is_scanned_like: bool = False
    cid_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    column_count: int = Field(default=1, ge=1)

    @property
    def display_page_number(self) -> int:
        return self.page_index + 1

    @field_validator("rotation_deg")
    @classmethod
    def _validate_rotation(cls, v: int) -> int:
        norm = v % 360
        if norm not in (0, 90, 180, 270):
            raise ValueError(f"Page rotation must be a multiple of 90, got {v}")
        return norm


class Document(BaseModel):
    """Registered PDF document metadata (Section 15)."""

    id: str
    original_filename: str
    safe_storage_path: str
    sha256_hash: str
    file_size_bytes: int = Field(..., ge=0)
    page_count: int = Field(..., ge=0)
    is_encrypted: bool = False
    is_corrupted: bool = False
    is_scanned_pdf: bool = False
    extractable_Sync_status: str = "ok"
    unsupported_reason: str | None = None
    protection_revision: int = Field(default=0, ge=0)
    edit_revision: int = Field(default=0, ge=0)
    pages: list[Page] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class GlossaryEntry(BaseModel):
    source: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)
    notes: str | None = None


class Glossary(BaseModel):
    id: str
    name: str
    source_lang: str = "en"
    target_lang: str = "ja"
    version: int = Field(default=1, ge=1)
    entries: list[GlossaryEntry] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now_iso)


class Translation(BaseModel):
    """Separated record of model translation vs manual edit (Section 15)."""

    id: str
    block_id: str
    document_id: str
    source_text: str
    masked_source_text: str
    model_output_text: str | None = None
    manual_edit_text: str | None = None
    model_name: str
    quantization: str = "unknown"
    is_mock: bool = False
    cache_key: str
    attempt_count: int = 1
    used_fallback_original: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class ValidationIssue(BaseModel):
    """Detailed validation or typesetting issue (Section 11, 12, 15)."""

    id: str
    job_id: str
    document_id: str
    page_index: int = Field(..., ge=0)
    block_id: str | None = None
    bbox: list[float] | None = None
    code: str
    severity: IssueSeverity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    fallback_applied: bool = False
    created_at: str = Field(default_factory=utc_now_iso)

    @property
    def display_page_number(self) -> int:
        return self.page_index + 1


class ExportArtifact(BaseModel):
    """Generated PDF or report artifact (Section 14 & 15)."""

    id: str
    job_id: str
    document_id: str
    artifact_type: str = Field(
        ...,
        description="'mono_ja_pdf' | 'dual_bilingual_pdf' | 'roundtrip_pdf' | 'validation_report_json'",
    )
    file_path: str
    sha256_hash: str
    file_size_bytes: int
    is_finalized: bool = True
    is_mock_translation: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


class TranslationJobConfig(BaseModel):
    page_range_spec: str | None = Field(
        default=None, description="1-indexed page range e.g. '1,3-5' or None for all"
    )
    source_lang: str = "en"
    target_lang: str = "ja"
    form: str = Field(default="plain", pattern="^(plain|polite)$")
    model_name: str = "plamo-3-translate-2b-Q4_K_M"
    quantization: str = "Q4_K_M"
    context_length: int = Field(default=8192, ge=1024, le=32768)
    translate_captions: bool = True
    translate_tables: bool = False
    translate_references: bool = False
    translate_figures: bool = False
    min_font_size_pt: float = Field(default=8.0, ge=6.0, le=14.0)
    glossary_id: str | None = None
    output_mono_pdf: bool = True
    output_dual_pdf: bool = True
    only_roundtrip_preservation: bool = False
    pause_for_review: bool = False
    use_mock_translator: bool = False


class Job(BaseModel):
    """Job state and progress tracking (Section 14 & 15)."""

    id: str
    document_id: str
    state: JobState = JobState.CREATED
    current_stage: str = "created"
    config: TranslationJobConfig = Field(default_factory=TranslationJobConfig)
    total_pages: int = 0
    processed_pages: int = 0
    total_blocks: int = 0
    processed_blocks: int = 0
    retry_count: int = 0
    warning_count: int = 0
    error_message: str | None = None
    original_sha256: str = ""
    original_hash_verified: bool = True
    is_mock_run: bool = False
    cancel_requested: bool = False
    glossary_version_used: int | None = None
    retypeset_revision: int = Field(default=0, ge=0)
    based_on_edit_revision: int = Field(default=0, ge=0)
    based_on_protection_revision: int = Field(default=0, ge=0)
    applied_block_revisions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    execution_provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None

    def can_transition_to(self, next_state: JobState) -> bool:
        return next_state in VALID_JOB_TRANSITIONS.get(self.state, set())

    def transition_to(self, next_state: JobState, stage_description: str | None = None) -> None:
        if not self.can_transition_to(next_state):
            raise ValueError(
                f"Invalid job state transition: {self.state.value} -> {next_state.value}"
            )
        self.state = next_state
        if stage_description:
            self.current_stage = stage_description
        self.updated_at = utc_now_iso()
        if next_state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS):
            self.completed_at = self.updated_at


class SystemDiagnostics(BaseModel):
    """Initial diagnostics information (Section 13.A)."""

    os_name: str
    os_version: str
    cpu_name: str
    cpu_cores: int
    cpu_threads: int
    ram_total_mb: int
    gpu_name: str
    gpu_vram_mb: int
    gpu_driver_version: str
    python_version: str
    pdf_engine_primary: str
    babeldoc_installed: bool
    babeldoc_version: str | None = None
    babeldoc_assets_cached: bool = False
    japanese_font_available: bool = False
    japanese_font_path: str | None = None
    llama_server_url: str
    llama_connection_status: str = Field(
        default="not_connected",
        description="'connected' | 'server_unreachable' | 'model_not_found' | 'model_load_failed'",
    )
    loaded_model_name: str | None = None
    llama_model_path: str | None = None
    llama_build_info: str | None = None
    llama_compute_backend: str = Field(
        default="unknown",
        description="Verified compute backend e.g. 'Vulkan GPU (ggml-vulkan.dll)' | 'CPU' | 'unknown'",
    )
    llama_server_pid: int | None = None
    loopback_only_enforced: bool = True
