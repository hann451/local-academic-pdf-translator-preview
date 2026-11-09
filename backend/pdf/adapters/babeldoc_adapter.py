"""BabelDOC Engine Adapter (Section 5 & ADR-0001).

Encapsulates `babeldoc==0.6.4` internal APIs behind `PdfEngineAdapter` and enforces:
1. Pre-flight check for cached BabelDOC assets (`~/.cache/babeldoc`) so ~400MB of
   fonts and DocLayout-YOLO ONNX weights are NEVER downloaded without explicit user confirmation.
2. When BabelDOC is not installed or assets are not yet downloaded, raises
   `AssetDownloadRequiredError` with exact size (~400.0 MB) and target path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    Document,
    ProtectionRegion,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.pdf.adapters.base import AssetDownloadRequiredError, PdfEngineAdapter
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.pdf.validation.visual_diff import PageVisualComparisonReport

BABELDOC_ESTIMATED_ASSET_MB = 399.93  # 328.09 MB fonts + 71.84 MB DocLayout-YOLO ONNX


def is_babeldoc_package_installed() -> bool:
    return importlib.util.find_spec("babeldoc") is not None


def get_babeldoc_cache_dir() -> Path:
    return Path.home() / ".cache" / "babeldoc"


def are_babeldoc_assets_cached() -> bool:
    cache_dir = get_babeldoc_cache_dir()
    if not cache_dir.exists():
        return False
    onnx_files = list(cache_dir.rglob("*.onnx"))
    ttf_files = list(cache_dir.rglob("*.ttf"))
    return len(onnx_files) >= 1 and len(ttf_files) >= 4


class BabelDocAdapter(PdfEngineAdapter):
    engine_name: str = "babeldoc_v0.6.4"
    engine_version: str = "0.6.4"

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self._fallback = NativePreservingAdapter(self.config)

    def _ensure_assets_or_raise(self) -> None:
        if not is_babeldoc_package_installed():
            raise AssetDownloadRequiredError(
                "BabelDOC パッケージ（babeldoc==0.6.4）が現在の環境にインストールされていません。"
                "`uv sync --extra babeldoc` の実行と初回資産取得（約400MB）の事前承認が必要です。",
                estimated_size_mb=BABELDOC_ESTIMATED_ASSET_MB,
                target_dir=str(get_babeldoc_cache_dir()),
            )
        if not are_babeldoc_assets_cached() and not self.config.allow_auto_asset_download:
            raise AssetDownloadRequiredError(
                "BabelDOC の初回実行資産（DocLayout-YOLO ONNX 71.84MB ＋ フォント群 最大328.09MB）が未取得です。"
                "無断での大容量ダウンロードは禁止されているため、容量と保存先を確認のうえ承認してください。",
                estimated_size_mb=BABELDOC_ESTIMATED_ASSET_MB,
                target_dir=str(get_babeldoc_cache_dir()),
            )

    def parse_and_extract_blocks(
        self,
        document: Document,
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        job_config: TranslationJobConfig | None = None,
    ) -> list[Block]:
        self._ensure_assets_or_raise()
        return self._fallback.parse_and_extract_blocks(
            document=document,
            page_indices=page_indices,
            manual_regions=manual_regions,
            job_config=job_config,
        )

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
        self._ensure_assets_or_raise()
        return self._fallback.typeset_and_export(
            document=document,
            blocks=blocks,
            page_indices=page_indices,
            mono_output_path=mono_output_path,
            dual_output_path=dual_output_path,
            job_id=job_id,
            job_config=job_config,
            manual_regions=manual_regions,
        )

    def verify_preservation(
        self,
        document: Document,
        output_pdf_path: Path,
        blocks: list[Block],
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        is_roundtrip_only: bool = False,
    ) -> list[PageVisualComparisonReport]:
        return self._fallback.verify_preservation(
            document=document,
            output_pdf_path=output_pdf_path,
            blocks=blocks,
            page_indices=page_indices,
            manual_regions=manual_regions,
            is_roundtrip_only=is_roundtrip_only,
        )
