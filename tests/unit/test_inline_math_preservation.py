"""Unit & regression tests for inline mathematical formula preservation,
pre-redaction fallback safety, font selection, and structured math typesetting.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from backend.pdf.validation.preflight import compute_sha256

from backend.config import AppConfig
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
    mask_inline_protected_elements,
    unmask_inline_protected_elements,
)

pytestmark = pytest.mark.unit


def _make_densepose_p3_block16_pdf(pdf_path: Path) -> Document:
    """Create a synthetic PDF reproducing the exact span structure of DensePose Page 3 block-16,
    plus an adjacent normal paragraph and an isolated equation below it.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)

    # Block 1: Normal prose paragraph above [318, 302, 558, 350]
    page.insert_text(
        pymupdf.Point(318, 315),
        "As shown in our experimental validation, discarding the phase",
        fontsize=9.0,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(318, 327),
        "information has a negative impact on the performance of our model.",
        fontsize=9.0,
        fontname="tiro",
    )

    # Block 2 (Target block-16 reproduction): [318, 357, 560, 433]
    # Use cambria.ttc if available to write real U+1D400..U+1D7FF math spans in original PDF
    cambria_path = Path(r"C:\Windows\Fonts\cambria.ttc")
    if cambria_path.exists():
        page.insert_font(fontname="F_ORIG_MATH", fontfile=str(cambria_path))
        mfont = "F_ORIG_MATH"
    else:
        mfont = "tiit"

    page.insert_text(
        pymupdf.Point(318, 366),
        "In raw CSI samples (5 consecutive samples visualized in Fig-",
        fontsize=8.97,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(318, 375),
        "ure 3(a-b)), the amplitude (𝐴) and phase (Φ) of each complex ele-",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(318, 387),
        "ment 𝑧= 𝑎+𝑏𝑖",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(373, 387),
        "are computed using the formulation 𝐴=√",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(526, 387),
        "(𝑎",
        fontsize=8.97,
        fontname=mfont,
    )
    # Superscript 2 at elevated baseline (y=384.48 vs 387.07, size=7.27)
    page.insert_text(
        pymupdf.Point(534, 384.48),
        "2",
        fontsize=7.27,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(539, 387),
        "+ 𝑏",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(551, 384.48),
        "2",
        fontsize=7.27,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(555, 387),
        ")",
        fontsize=8.97,
        fontname="tiro",
    )
    # Vector vinculum (radical overbar) above (a^2 + b^2)
    page.draw_line(
        pymupdf.Point(525.43, 378.69),
        pymupdf.Point(558.20, 378.69),
        color=(0, 0, 0),
        width=0.5,
    )
    page.insert_text(
        pymupdf.Point(318, 398),
        "and Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range of the arctan function is",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(318, 409),
        "from −𝜋",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(351, 409),
        "to 𝜋",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(368, 409),
        "and the phase values outside this range get wrapped.",
        fontsize=8.97,
        fontname="tiro",
    )

    # Block 3: Isolated equation below [371, 435, 509, 462]
    page.insert_text(
        pymupdf.Point(420, 445),
        "Δ𝜙 = Φ − Φ  (1)",
        fontsize=8.97,
        fontname=mfont,
    )

    doc.save(str(pdf_path))
    doc.close()

    return Document(
        id="doc-repro16",
        original_filename=pdf_path.name,
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )


def test_unit_a_pre_redaction_fallback_on_unrenderable_glyph(tmp_path: Path) -> None:
    """Unit A: When a translated block contains a character with no glyph in any available
    font (e.g. Private Use Area U+E000), typeset_and_export MUST detect it BEFORE redacting,
    keep the original English text in the PDF untouched, set status=FALLBACK_ORIGINAL,
    emit UNRENDERABLE_GLYPH_FALLBACK, and still typeset other valid blocks on the same page.
    """
    pdf_path = tmp_path / "unrenderable.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    blk_ok = Block(
        id="doc-repro16:page-1:block-1",
        page_index=0,
        bbox=[318.0, 302.0, 558.0, 350.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=0,
        source_text="As shown in our experimental validation, discarding the phase information has a negative impact.",
        translation_text="実験的検証で示したように、位相情報を破棄することはモデルの性能に悪影響を及ぼす。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=9.0,
    )
    # Block with PUA character \uE000 that no font has a glyph for
    blk_bad = Block(
        id="doc-repro16:page-1:block-2",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="In raw CSI samples, the amplitude and phase are computed.",
        translation_text="生のCSIサンプルにおいて、未知記号 \ue000 を含む式で計算される。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )
    blk_eq = Block(
        id="doc-repro16:page-1:block-3",
        page_index=0,
        bbox=[371.0, 435.0, 509.0, 462.0],
        kind=BlockKind.ISOLATED_FORMULA,
        reading_order=2,
        source_text="Δ𝜙 = Φ − Φ  (1)",
        status=BlockStatus.PROTECTED_SKIP,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "out_mono.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk_ok, blk_bad, blk_eq],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-test-a",
    )

    # 1. blk_bad must fall back to original with explicit warning
    assert blk_bad.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRENDERABLE_GLYPH_FALLBACK" in blk_bad.warnings
    assert any(iss.code == "UNRENDERABLE_GLYPH_FALLBACK" and iss.block_id == blk_bad.id for iss in issues)

    # 2. Original English text inside blk_bad.bbox MUST still exist in the output PDF (not redacted!)
    out_pdf = pymupdf.open(str(mono_out))
    bad_rect_text = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk_bad.bbox))
    assert "raw CSI samples" in bad_rect_text
    assert "\x00" not in bad_rect_text

    # 3. Other valid block on the same page MUST still be translated into Japanese
    ok_rect_text = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk_ok.bbox))
    assert "実験的検証" in ok_rect_text
    out_pdf.close()


def test_unit_a_pre_redaction_fallback_on_unresolved_placeholder(tmp_path: Path) -> None:
    """Unit A: When a block still contains an unresolved placeholder {v99} or malformed ｛v1｝
    at typesetting time, it MUST NOT output literal '{v99}' into the final PDF; instead it must
    preserve the original PDF content and emit UNRESOLVED_PLACEHOLDER_FALLBACK.
    """
    pdf_path = tmp_path / "unresolved_ph.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    blk_unresolved = Block(
        id="doc-repro16:page-1:block-2",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="In raw CSI samples, the amplitude and phase are computed.",
        translation_text="生のCSIサンプルにおいて、振幅 {v99} と位相を計算する。",
        protected_items=[],
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "out_unresolved.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk_unresolved],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-test-ph",
    )

    assert blk_unresolved.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRESOLVED_PLACEHOLDER_FALLBACK" in blk_unresolved.warnings
    assert any(iss.code == "UNRESOLVED_PLACEHOLDER_FALLBACK" for iss in issues)

    out_pdf = pymupdf.open(str(mono_out))
    rect_text = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk_unresolved.bbox))
    assert "{v99}" not in rect_text
    assert "raw CSI samples" in rect_text
    out_pdf.close()


def test_unit_a_fallback_when_math_font_unavailable(tmp_path: Path) -> None:
    """Unit A: When no math font is available (font_math_path=None) and the primary Japanese
    font lacks glyphs for Mathematical Alphanumeric Symbols (U+1D467 '𝑧'), typeset_and_export
    MUST NOT substitute ASCII+Times-Italic or emit \\x00; it must preserve the original PDF
    content and emit UNRENDERABLE_GLYPH_FALLBACK.
    """
    pdf_path = tmp_path / "no_math_font.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    blk = Block(
        id="doc-repro16:page-1:block-2",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="In raw CSI samples, each complex element 𝑧= 𝑎+𝑏𝑖 is computed.",
        translation_text="生のCSIサンプルにおいて、各複素要素 𝑧= 𝑎+𝑏𝑖 が計算される。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    cfg = AppConfig(runtime_dir=tmp_path / "rt")
    cfg.font_math_path = None  # Explicitly disable math fallback font
    adapter = NativePreservingAdapter(config=cfg)
    mono_out = tmp_path / "out_no_math_font.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-test-nomath",
    )

    assert blk.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRENDERABLE_GLYPH_FALLBACK" in blk.warnings
    assert any(iss.code == "UNRENDERABLE_GLYPH_FALLBACK" for iss in issues)

    out_pdf = pymupdf.open(str(mono_out))
    rect_text = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk.bbox))
    assert "raw CSI samples" in rect_text
    assert "\x00" not in rect_text
    out_pdf.close()



@pytest.mark.skipif(
    not Path(r"C:\Windows\Fonts\cambria.ttc").exists(),
    reason="Requires Windows Cambria Math/Regular font (cambria.ttc) for real math glyph rendering verification",
)
def test_unit_a_densepose_block16_inline_math_no_missing_glyphs(tmp_path: Path) -> None:
    """Unit A/B reproduction test: Translating DensePose block-16 containing
    𝑧 = 𝑎 + 𝑏𝑖, 𝐴 = √︁(𝑎2 + 𝑏2), Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎), −𝜋 to 𝜋 must render all math symbols
    with non-zero glyphs, zero \\x00 characters, and extractable Unicode math symbols.
    """
    pdf_path = tmp_path / "densepose_b16.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    src_text = (
        "In raw CSI samples (5 consecutive samples visualized in Fig- ure 3(a-b)), "
        "the amplitude (𝐴) and phase (Φ) of each complex ele- ment 𝑧= 𝑎+𝑏𝑖are computed "
        "using the formulation 𝐴=√︁(𝑎2 + 𝑏2) and Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range "
        "of the arctan function is from −𝜋to 𝜋and the phase values outside this range get wrapped."
    )
    trn_text = (
        "生のCSIサンプル（図3(a-b)に示す連続する5つのサンプル）において、各複素要素 𝑧= 𝑎+𝑏𝑖 の振幅 (𝐴) と位相 ({v1}) は、"
        "𝐴={v2}︁(𝑎2 + 𝑏2) および {v3} = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎) という式を用いて計算される。"
        "なお、アークタンジェント関数の範囲は {v4}𝜋から𝜋までであり、この範囲外の位相値はラップアラウンドされる。"
    )
    blk = Block(
        id="doc-repro16:page-1:block-2",
        page_index=0,
        bbox=[317.95, 357.07, 559.72, 433.14],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text=src_text,
        masked_source_text=src_text,
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.GREEK_OR_MATH_SYMBOL, original_text="Φ"),
            ProtectedItem(placeholder="{v2}", kind=ProtectedItemKind.GREEK_OR_MATH_SYMBOL, original_text="√"),
            ProtectedItem(placeholder="{v3}", kind=ProtectedItemKind.GREEK_OR_MATH_SYMBOL, original_text="Φ"),
            ProtectedItem(placeholder="{v4}", kind=ProtectedItemKind.GREEK_OR_MATH_SYMBOL, original_text="−"),
        ],
        translation_text=trn_text,
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "b16_mono.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-test-b16",
    )

    assert blk.status == BlockStatus.TRANSLATED
    assert issues == []

    out_pdf = pymupdf.open(str(mono_out))
    extracted = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk.bbox)).replace("\xa0", " ")
    out_pdf.close()

    assert "\x00" not in extracted
    assert "\ufe01" not in extracted
    for expected_token in ("𝑧", "𝑎", "𝑏", "𝑖", "𝐴", "Φ", "√", "𝑎𝑟𝑐𝑡𝑎𝑛", "−", "𝜋"):
        assert expected_token in extracted, f"Missing token {expected_token!r} in extracted text: {extracted!r}"
