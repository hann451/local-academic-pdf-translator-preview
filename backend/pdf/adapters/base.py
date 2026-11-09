"""Abstract base interface for PDF Engine Adapters (Section 5 & 6).

Encapsulates PDF parsing, element classification, reading order detection,
protection region application, inline formula masking, typesetting, and export
so internal library APIs (PyMuPDF / BabelDOC) do not leak into domain or API layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from backend.domain.models import (
    Block,
    Document,
    ProtectionRegion,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.pdf.validation.visual_diff import PageVisualComparisonReport


class AssetDownloadRequiredError(RuntimeError):
    """Raised when an engine requires external assets (e.g. BabelDOC ~400MB)
    and automatic download is disabled pending user confirmation (Section 0.3, 16, 17).
    """

    def __init__(self, message: str, estimated_size_mb: float, target_dir: str) -> None:
        super().__init__(message)
        self.estimated_size_mb = estimated_size_mb
        self.target_dir = target_dir


class PdfEngineAdapter(ABC):
    engine_name: str = "base"
    engine_version: str = "0.1.0"

    @abstractmethod
    def parse_and_extract_blocks(
        self,
        document: Document,
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        job_config: TranslationJobConfig | None = None,
    ) -> list[Block]:
        """Execute Stages 4-7:
        4. Candidate element analysis (body, heading, figure, table, isolated/inline formula).
        5. Reading order determination (1-column & 2-column).
        6. Protection region resolution (auto + manual overrides).
        7. Translation unit generation with `{vN}` inline formula/symbol masking.
        """
        raise NotImplementedError

    @abstractmethod
    def typeset_and_export(
        self,
        document: Document,
        blocks: list[Block],
        page_indices: list[int],
        mono_output_path: Path | None,
        dual_output_path: Path | None,
        job_id: str,
        job_config: TranslationJobConfig,
        manual_regions: list[ProtectionRegion] | None = None,
    ) -> tuple[list[Block], list[ValidationIssue]]:
        """Execute Stages 10 & 12:
        10. Japanese typesetting with kinsoku line wrapping, min font size enforcement,
            zero overlap with figures/formulas, and fallback-to-original on overflow.
        12. Output Monolingual Japanese PDF and/or Side-by-Side Bilingual Dual PDF.
        """
        raise NotImplementedError

    @abstractmethod
    def verify_preservation(
        self,
        document: Document,
        output_pdf_path: Path,
        blocks: list[Block],
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        is_roundtrip_only: bool = False,
    ) -> list[PageVisualComparisonReport]:
        """Execute Stage 11: Verify original hash immutability and visual preservation of protected areas."""
        raise NotImplementedError
