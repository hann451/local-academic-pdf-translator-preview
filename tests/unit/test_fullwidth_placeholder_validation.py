"""Focused unit & safety tests for token-level NFKC placeholder detection (Section 12).

Covers half/fullwidth variants (`｛ｖ１｝`, `{ｖ1}`, `｛v１｝`, `［ｖ１］`, `{ V 1 }`, etc.)
across both `validate_translation_output` and pre-redaction PDF output gating, while verifying
that surrounding mathematical Unicode expressions are never globally NFKC-normalized.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    Document,
    ProtectedItem,
    ProtectedItemKind,
    TranslationJobConfig,
)
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    _find_unresolved_placeholders,
)
from backend.pdf.validation.preflight import compute_sha256
from backend.translation.validation.validator import (
    find_placeholder_variant_tokens,
    validate_translation_output,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "variant",
    [
        "｛ｖ１｝",
        "｛v１｝",
        "{ｖ１}",
        "{ｖ1}",
        "［ｖ１］",
        "｛　ｖ　１　｝",
        "{ V 1 }",
        "｛Ｖ１２｝",
        "{ｖ1｝",
        "{v1a}",
    ],
)
def test_validator_detects_half_and_fullwidth_placeholder_variants(variant: str) -> None:
    items = [
        ProtectedItem(
            placeholder="{v1}",
            kind=ProtectedItemKind.INLINE_FORMULA,
            original_text="𝑎² + 𝑏²",
        )
    ]
    res = validate_translation_output(
        source_text="Compute 𝑎² + 𝑏² across windows.",
        masked_source_text="Compute {v1} across windows.",
        translated_text=f"各ウィンドウで {variant} を計算する。",
        protected_items=items,
    )
    assert res.is_valid is False
    assert "PLACEHOLDER_ALTERED" in res.error_codes
    finding = next(f for f in res.findings if f.code == "PLACEHOLDER_ALTERED")
    assert variant in finding.details.get("altered", [])


def test_validator_catches_stray_fullwidth_variant_when_no_placeholders_expected() -> None:
    res = validate_translation_output(
        source_text="Consider the baseline setup without variables.",
        masked_source_text="Consider the baseline setup without variables.",
        translated_text="変数のないベースライン設定 ｛ｖ１｝ を検討する。",
        protected_items=[],
    )
    assert res.is_valid is False
    assert "PLACEHOLDER_ALTERED" in res.error_codes


def test_validator_leaves_unicode_math_and_source_literal_untouched() -> None:
    # Token-level NFKC must NEVER globally normalize math Unicode outside placeholder brackets
    math_src = "Evaluate 𝑓(𝑥) = ∑ 𝑥ᵢ² (Figure 1)."
    items = [
        ProtectedItem(
            placeholder="{v1}",
            kind=ProtectedItemKind.INLINE_FORMULA,
            original_text="𝑓(𝑥) = ∑ 𝑥ᵢ²",
        )
    ]
    res = validate_translation_output(
        source_text=math_src,
        masked_source_text="Evaluate {v1} (Figure 1).",
        translated_text="数式 {v1}（図１）を評価し、値 𝑎² はそのまま扱う。",
        protected_items=items,
    )
    assert res.is_valid is True
    assert "PLACEHOLDER_ALTERED" not in res.error_codes
    assert find_placeholder_variant_tokens("値 𝑎² と（図１）のみ") == []

    # Literal `[v1]` verbatim in source_text is allowed, whereas fullwidth alteration fails
    res_lit = validate_translation_output(
        source_text="Vector component [v1] stays bounded.",
        masked_source_text="Vector component [v1] stays bounded.",
        translated_text="ベクトル成分 [v1] は有界に保たれる。",
        protected_items=[],
    )
    assert "PLACEHOLDER_ALTERED" not in res_lit.error_codes

    res_lit_mut = validate_translation_output(
        source_text="Vector component [v1] stays bounded.",
        masked_source_text="Vector component [v1] stays bounded.",
        translated_text="ベクトル成分 ［ｖ１］ は有界に保たれる。",
        protected_items=[],
    )
    assert "PLACEHOLDER_ALTERED" in res_lit_mut.error_codes


def test_unresolved_placeholder_check_and_pdf_export_safety(tmp_path: Path) -> None:
    # Direct check catches half/fullwidth variants while allowing verbatim math Unicode
    assert _find_unresolved_placeholders("結果 ｛ｖ１｝ が出力", "x = 1") == ["｛ｖ１｝"]
    assert _find_unresolved_placeholders("結果 ［ｖ１］ が出力", "x = 1") == ["［ｖ１］"]
    assert _find_unresolved_placeholders("結果 ｛　Ｖ　１　｝ が出力", "x = 1") == ["｛　Ｖ　１　｝"]
    assert _find_unresolved_placeholders("値 𝑎² + 𝑏² の比較", "value a^2 + b^2") == []

    pdf_path = tmp_path / "fullwidth_ph_safety.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text(pymupdf.Point(72, 120), "Leaked placeholder source paragraph.", fontsize=10, fontname="helv")
    page.insert_text(pymupdf.Point(72, 200), "Manual edit leaked placeholder source.", fontsize=10, fontname="helv")
    page.insert_text(pymupdf.Point(72, 280), "Healthy translated paragraph.", fontsize=10, fontname="helv")
    pdf.save(str(pdf_path))
    pdf.close()

    doc_model = Document(
        id="doc-fw-safe",
        original_filename=pdf_path.name,
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    blk_leak = Block(
        id="doc-fw-safe:page-1:block-1",
        page_index=0,
        bbox=[70.0, 108.0, 360.0, 130.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=0,
        source_text="Leaked placeholder source paragraph.",
        translation_text="未復元記号 ｛ｖ１｝ が混入した訳文。",
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="x_1")
        ],
        status=BlockStatus.TRANSLATED,
        font_size_pt=10.0,
    )
    blk_manual_leak = Block(
        id="doc-fw-safe:page-1:block-2",
        page_index=0,
        bbox=[70.0, 188.0, 360.0, 210.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="Manual edit leaked placeholder source.",
        translation_text="初期訳文",
        manual_translation_text="手動編集で ｛　ｖ　１　｝ が残った文。",
        is_manually_edited=True,
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="x_2")
        ],
        status=BlockStatus.MANUALLY_EDITED,
        font_size_pt=10.0,
    )
    blk_ok = Block(
        id="doc-fw-safe:page-1:block-3",
        page_index=0,
        bbox=[70.0, 268.0, 360.0, 290.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=2,
        source_text="Healthy translated paragraph.",
        translation_text="正常な段落（数値１と式 x_3）を出力する。",
        protected_items=[],
        status=BlockStatus.TRANSLATED,
        font_size_pt=10.0,
    )

    mono_out = tmp_path / "fw_safe_out.pdf"
    adapter = NativePreservingAdapter()
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk_leak, blk_manual_leak, blk_ok],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-fw-safe",
    )

    assert blk_leak.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRESOLVED_PLACEHOLDER_FALLBACK" in blk_leak.warnings
    assert blk_manual_leak.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRESOLVED_PLACEHOLDER_FALLBACK" in blk_manual_leak.warnings
    assert blk_ok.status == BlockStatus.TRANSLATED

    leak_issues = [i for i in issues if i.code == "UNRESOLVED_PLACEHOLDER_FALLBACK"]
    assert {i.block_id for i in leak_issues} == {blk_leak.id, blk_manual_leak.id}

    out_pdf = pymupdf.open(str(mono_out))
    full_text = out_pdf[0].get_text("text")
    assert "｛ｖ１｝" not in full_text
    assert "ｖ" not in full_text
    assert "Leaked placeholder source paragraph." in full_text
    assert "Manual edit leaked placeholder source." in full_text
    assert "正常な" in full_text
    assert "数値１と式" in full_text
    out_pdf.close()
