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
    InlineMathSpan,
    ProtectedItem,
    ProtectedItemKind,
    ProtectionMode,
    ProtectionRegion,
    TranslationJobConfig,
)
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    _build_styled_line_plan,
    _fix_embedded_tounicode_smp,
    extract_inline_math_spans,
    fit_japanese_text_in_bbox,
    mask_inline_protected_elements,
    unmask_inline_protected_elements,
    wrap_japanese_kinsoku,
)
from backend.translation.validation.validator import validate_translation_output

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
        "are computed using the formulation ",
        fontsize=8.97,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(510, 387),
        "𝐴=√",
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
        "to ",
        fontsize=8.97,
        fontname="tiro",
    )
    page.insert_text(
        pymupdf.Point(360, 409),
        "𝜋",
        fontsize=8.97,
        fontname=mfont,
    )
    page.insert_text(
        pymupdf.Point(369, 409),
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

    _fix_embedded_tounicode_smp(doc, target_font_tags=("F_ORIG_MATH",))
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
    page_fonts = [f[3] for f in out_pdf[0].get_fonts()]
    out_pdf.close()

    assert "\x00" not in extracted
    assert "\ufe01" not in extracted
    assert any("Cambria" in fn or "Segoe" in fn for fn in page_fonts)
    for expected_token in ("𝑧", "𝑎", "𝑏", "𝑖", "𝐴", "Φ", "√", "𝑎𝑟𝑐𝑡𝑎𝑛", "−", "𝜋"):
        assert expected_token in extracted, f"Missing token {expected_token!r} in extracted text: {extracted!r}"


def test_unit_b_variation_selector_scoped_handling(tmp_path: Path) -> None:
    """Section 3: Known TeX operator + variation selector pairs (√+U+FE01, ∑+U+FE01) are
    normalized for rendering, whereas unknown variation selector combinations (e.g. A+U+FE05)
    are NOT blindly stripped and instead trigger safe FALLBACK_ORIGINAL + warning.
    """
    pdf_path = tmp_path / "vs_test.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    blk_unknown_vs = Block(
        id="doc-repro16:page-1:block-2",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="In raw CSI samples, A\ufe05 is an unsupported variation sequence.",
        translation_text="生のCSIサンプルにおいて、A\ufe05 は未対応の異体字セレクタ組み合わせである。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "vs_unknown_out.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk_unknown_vs],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-test-vs",
    )

    assert blk_unknown_vs.status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRENDERABLE_GLYPH_FALLBACK" in blk_unknown_vs.warnings
    assert any(iss.code == "UNRENDERABLE_GLYPH_FALLBACK" for iss in issues)


# ==============================================================================
# Unit C: Math Masking, Prose-vs-Math Disambiguation, Collision, & Span Space Recovery
# ==============================================================================
def test_unit_c_densepose_all_14_blocks_math_masking() -> None:
    """Section 6: Verify that all inline math patterns across DensePose From WiFi
    (complex numbers z = a + bi, radicals A = sqrt(a^2 + b^2), arctan(b/a), -pi to pi,
    circumflex hats, subscripts, superscripts, scientific notation 1e-5, sets, and MSE)
    are masked intact without splitting inside operators/variation selectors.
    """
    src_b16 = (
        "In raw CSI samples (5 consecutive samples visualized in Fig- ure 3(a-b)), "
        "the amplitude (𝐴) and phase (Φ) of each complex ele- ment 𝑧= 𝑎+𝑏𝑖 are computed "
        "using the formulation 𝐴=√︁(𝑎2 + 𝑏2) and Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range "
        "of the arctan function is from −𝜋 to 𝜋 and the phase values outside this range "
        "get wrapped, leading to a discontinuity in phase values. Our first sanitization "
        "step is to unwrap the phase following [10]:"
    )
    masked_b16, items_b16 = mask_inline_protected_elements(src_b16)
    orig_texts = [it.original_text for it in items_b16]
    assert orig_texts == [
        "𝐴",
        "Φ",
        "𝑧= 𝑎+𝑏𝑖",
        "𝐴=√︁(𝑎2 + 𝑏2)",
        "Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎)",
        "−𝜋",
        "𝜋",
    ]
    assert unmask_inline_protected_elements(masked_b16, items_b16) == src_b16

    # Check block-18 (circumflex hat + subscript without swallowing parenthetical prose)
    src_b18 = "ˆ𝜙𝑓 is the sanitized phase values at subcarrier 𝑓 (the 𝑓th frequency)."
    masked_b18, items_b18 = mask_inline_protected_elements(src_b18)
    assert [it.original_text for it in items_b18] == ["ˆ𝜙𝑓", "𝑓", "𝑓"]
    assert unmask_inline_protected_elements(masked_b18, items_b18) == src_b18

    # Check block-13 / block-14 / block-19 / block-26 patterns
    src_misc = (
        "where 𝑀𝑆𝐸(·) uses {𝑃2, 𝑃3, 𝑃4, 𝑃5} and {𝑃∗ 2, 𝑃∗ 3, 𝑃∗ 5} with losses "
        "𝐿𝑐𝑙𝑠, 𝐿𝑏𝑜𝑥, 𝐿𝑑𝑝, 𝐿𝑘𝑝, 𝐿𝑡𝑟 and 𝜆𝑑𝑝= 0.6, learning rate 1𝑒−5 to 1𝑒−3."
    )
    masked_misc, items_misc = mask_inline_protected_elements(src_misc)
    misc_origs = [it.original_text for it in items_misc]
    for expected in (
        "𝑀𝑆𝐸(·)",
        "{𝑃2, 𝑃3, 𝑃4, 𝑃5}",
        "𝐿𝑐𝑙𝑠",
        "𝐿𝑏𝑜𝑥",
        "𝐿𝑑𝑝",
        "𝐿𝑘𝑝",
        "𝐿𝑡𝑟",
        "𝜆𝑑𝑝= 0.6",
        "1𝑒−5",
        "1𝑒−3",
    ):
        assert expected in misc_origs
    assert unmask_inline_protected_elements(masked_misc, items_misc) == src_misc


def test_unit_c_prose_parenthetical_vs_math_function_disambiguation() -> None:
    """Section 6: Ordinary English prose words followed by parenthetical glosses without spaces
    such as `subcarriers(frequencies)` or `samples(frequencies)` must NOT be masked as inline math,
    whereas true mathematical function calls (`arctan(b/a)`, `𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎)`, `sin(x)`, `f(x)`, `N''(f)`)
    are masked.
    """
    src_b20 = (
        "where 𝑖 denotes the index of the measurements in the five consecu- tive samples, "
        "and 𝑗 denotes the index of the subcarriers(frequencies) and samples(frequencies)."
    )
    masked_b20, items_b20 = mask_inline_protected_elements(src_b20)
    origs_b20 = [it.original_text for it in items_b20]
    assert origs_b20 == ["𝑖", "𝑗"]
    assert "subcarriers(frequencies)" in masked_b20
    assert "samples(frequencies)" in masked_b20

    src_funcs = "Compute arctan(b/a), sin(2πf), f(x), P(y|x), and N''(f) in(frequencies)."
    masked_funcs, items_funcs = mask_inline_protected_elements(src_funcs)
    origs_funcs = [it.original_text for it in items_funcs]
    assert "arctan(b/a)" in origs_funcs
    assert "sin(2πf)" in origs_funcs
    assert "f(x)" in origs_funcs
    assert "P(y|x)" in origs_funcs
    assert "N''(f)" in origs_funcs
    assert "in(frequencies)" in masked_funcs


def test_unit_c_preexisting_literal_placeholder_no_collision() -> None:
    """Section 6: If the source text already contains a literal `{v1}`, masking must not
    collide with it, validation must pass when placeholders are preserved, and unmasking
    must restore both the literal `{v1}` and all formulas without chain-expansion.
    """
    src = "Suppose vertex set {v1} satisfies 𝑧= 𝑎+𝑏𝑖 and 𝐴=√︁(𝑎2 + 𝑏2)."
    masked, items = mask_inline_protected_elements(src)
    assert len(items) == 3
    assert [it.placeholder for it in items] == ["{v1}", "{v2}", "{v3}"]
    assert items[0].original_text == "{v1}"
    assert items[1].original_text == "𝑧= 𝑎+𝑏𝑖"
    assert items[2].original_text == "𝐴=√︁(𝑎2 + 𝑏2)"

    # Simulate Japanese translation with reordered placeholders ({v2} and {v3} before {v1})
    ja_trans = "式 {v2} および {v3} を頂点集合 {v1} が満たすとする。"
    val = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text=ja_trans,
        protected_items=items,
    )
    assert val.is_valid, f"Validation failed unexpectedly: {val.findings}"

    unmasked = unmask_inline_protected_elements(ja_trans, items)
    assert unmasked == "式 𝑧= 𝑎+𝑏𝑖 および 𝐴=√︁(𝑎2 + 𝑏2) を頂点集合 {v1} が満たすとする。"


def test_unit_c_placeholder_reordering_and_validation_checks() -> None:
    """Section 6: Reordered placeholders in Japanese ({v2} ... {v1}) are valid, while
    missing, duplicated, altered (fullwidth/spaced), and unknown placeholders are flagged.
    """
    src = "The amplitude 𝐴=√︁(𝑎2 + 𝑏2) and phase Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎) are computed."
    masked, items = mask_inline_protected_elements(src)
    assert [it.placeholder for it in items] == ["{v1}", "{v2}"]

    # 1. Reordered placeholders in Japanese -> VALID
    res_reordered = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text="位相 {v2} と振幅 {v1} が計算される。",
        protected_items=items,
    )
    assert res_reordered.is_valid

    # 2. Missing placeholder -> PLACEHOLDER_MISSING
    res_missing = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text="振幅 {v1} が計算される。",
        protected_items=items,
    )
    assert not res_missing.is_valid
    assert "PLACEHOLDER_MISSING" in res_missing.error_codes

    # 3. Duplicated placeholder -> PLACEHOLDER_DUPLICATED
    res_dup = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text="振幅 {v1} と {v1} および位相 {v2} が計算される。",
        protected_items=items,
    )
    assert not res_dup.is_valid
    assert "PLACEHOLDER_DUPLICATED" in res_dup.error_codes

    # 4. Altered placeholder (fullwidth or spaces) -> PLACEHOLDER_ALTERED
    res_altered = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text="振幅 ｛v1｝ と位相 { v2 } が計算される。",
        protected_items=items,
    )
    assert not res_altered.is_valid
    assert "PLACEHOLDER_ALTERED" in res_altered.error_codes

    # 5. Unknown / hallucinated placeholder -> PLACEHOLDER_HALLUCINATED
    res_hallucinated = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text="振幅 {v1} と位相 {v2} および {v99} が計算される。",
        protected_items=items,
    )
    assert not res_hallucinated.is_valid
    assert "PLACEHOLDER_HALLUCINATED" in res_hallucinated.error_codes


def test_unit_c_inter_span_space_recovery() -> None:
    """Section 6: `_extract_lines_and_style` recovers missing inter-span spaces when a horizontal
    word-space gap exists between a math span and a prose span, without inserting spaces inside
    subscript/superscript attachments or scientific notation.
    """
    adapter = NativePreservingAdapter()
    raw_lines = [
        {
            "spans": [
                {"text": "ment ", "size": 8.97, "font": "LinLibertineT", "flags": 4, "bbox": [318.0, 368.0, 331.49, 379.0]},
                {"text": "𝑧= 𝑎+𝑏𝑖", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [331.49, 368.0, 370.45, 379.0]},
                {"text": "are computed using ", "size": 8.88, "font": "LinLibertineT", "flags": 4, "bbox": [372.93, 368.0, 460.0, 379.0]},
                {"text": "𝐴=√︁(", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [460.0, 368.0, 529.02, 379.0]},
                {"text": "𝑎", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [529.02, 368.0, 534.72, 379.0]},
                {"text": "2", "size": 7.27, "font": "LinLibertineT", "flags": 5, "bbox": [535.03, 365.0, 539.26, 374.0]},
                {"text": " + ", "size": 8.97, "font": "txsys", "flags": 4, "bbox": [539.26, 368.0, 548.55, 379.0]},
                {"text": "𝑏", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [548.55, 368.0, 553.67, 379.0]},
                {"text": "2", "size": 7.27, "font": "LinLibertineT", "flags": 5, "bbox": [554.21, 365.0, 558.20, 374.0]},
                {"text": ")", "size": 8.97, "font": "txsys", "flags": 4, "bbox": [558.20, 368.0, 561.0, 379.0]},
            ]
        },
        {
            "spans": [
                {"text": "from ", "size": 8.90, "font": "LinLibertineT", "flags": 4, "bbox": [318.0, 392.0, 336.0, 403.0]},
                {"text": "−𝜋", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [336.0, 392.0, 347.89, 403.0]},
                {"text": "to ", "size": 8.90, "font": "LinLibertineT", "flags": 4, "bbox": [350.97, 392.0, 360.0, 403.0]},
                {"text": "𝜋", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [360.0, 392.0, 365.70, 403.0]},
                {"text": "and rate 1", "size": 8.90, "font": "LinLibertineT", "flags": 4, "bbox": [368.78, 392.0, 410.0, 403.0]},
                {"text": "𝑒", "size": 8.97, "font": "LibertineMathMI", "flags": 6, "bbox": [410.0, 392.0, 414.0, 403.0]},
                {"text": "−", "size": 8.97, "font": "txsys", "flags": 4, "bbox": [416.78, 392.0, 421.0, 403.0]},
                {"text": "5.", "size": 8.90, "font": "LinLibertineT", "flags": 4, "bbox": [423.15, 392.0, 430.0, 403.0]},
            ]
        },
    ]
    text_lines, _, has_math, has_sub = adapter._extract_lines_and_style(raw_lines)
    assert has_math is True
    assert has_sub is True
    assert text_lines[0] == "ment 𝑧= 𝑎+𝑏𝑖 are computed using 𝐴=√︁(𝑎2 + 𝑏2)"
    assert text_lines[1] == "from −𝜋 to 𝜋 and rate 1𝑒−5."


# ==============================================================================
# Unit D: Structured Inline Math (Superscripts, Subscripts, Hats, Radical Overbar)
# ==============================================================================
def test_unit_d_superscript_and_radical_overbar_extraction_and_rendering(tmp_path: Path) -> None:
    """Section 5: Verify that `extract_inline_math_spans` and `typeset_and_export` preserve:
    1. Superscripts (`𝑎2`, `𝑏2`) at smaller font size (`0.78 * fitted_size`) and elevated `origin_y`.
    2. Radical vinculum overbar (`√(𝑎2 + 𝑏2)`) redrawn above the translated `(𝑎2 + 𝑏2)` span.
    """
    pdf_path = tmp_path / "unit_d_b16.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    src_text = (
        "In raw CSI samples (5 consecutive samples visualized in Fig- ure 3(a-b)), "
        "the amplitude (𝐴) and phase (Φ) of each complex ele- ment 𝑧= 𝑎+𝑏𝑖 are computed "
        "using the formulation 𝐴=√︁(𝑎2 + 𝑏2) and Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range "
        "of the arctan function is from −𝜋 to 𝜋 and the phase values outside this range "
        "get wrapped."
    )
    masked, items = mask_inline_protected_elements(src_text)
    blk = Block(
        id="doc-repro16:page-1:block-16",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text=src_text,
        masked_source_text=masked,
        protected_items=items,
        translation_text=(
            "生のCSIサンプル（図3(a-b)）では、各複素要素 {v3} の振幅 ({v1}) と位相 ({v2}) は、"
            "式 {v4} と {v5} を用いて計算される。arctanの範囲は {v6} から {v7} である。"
        ),
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "unit_d_b16_out.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-unit-d-b16",
    )

    assert blk.status == BlockStatus.TRANSLATED
    assert not issues
    roles = {(m.role, m.context_text, m.target_text) for m in blk.inline_math_spans}
    assert ("radicand", "√(𝑎2 + 𝑏2)", "(𝑎2 + 𝑏2)") in roles
    assert ("super", "𝑎2", "2") in roles
    assert ("super", "𝑏2", "2") in roles

    out_pdf = pymupdf.open(str(mono_out))
    page = out_pdf[0]
    d = page.get_text("dict", clip=pymupdf.Rect(blk.bbox))
    all_spans = [s for b in d["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
    sup_spans = [s for s in all_spans if s["text"].strip() == "2"]
    normal_spans = [s for s in all_spans if "𝑎" in s["text"] or "𝑏" in s["text"]]
    assert len(sup_spans) >= 2
    assert normal_spans
    for ss in sup_spans:
        assert ss["size"] < blk.fitted_font_size_pt * 0.85
        # Find normal span on the same line
        same_line_norm = [
            ns for ns in normal_spans if abs(ns["origin"][1] - ss["origin"][1]) < 6.0
        ]
        assert same_line_norm
        assert ss["origin"][1] < same_line_norm[0]["origin"][1] - 1.5

    # Verify that a horizontal radical overbar line was drawn in the output PDF
    horiz_lines = [
        dr["rect"]
        for dr in page.get_drawings()
        if dr.get("rect")
        and abs(dr["rect"].y1 - dr["rect"].y0) <= 1.2
        and (dr["rect"].x1 - dr["rect"].x0) >= 15.0
    ]
    assert len(horiz_lines) >= 2  # Original (covered by white rect) + new translated radical bar
    out_pdf.close()


def test_unit_d_circumflex_hat_and_subscript_positioning(tmp_path: Path) -> None:
    """Section 5: Verify that circumflex hats (`ˆ𝜙𝑓`, `ˆ𝑖𝑝`, `ˆ𝑀`) are centered over the base
    character and raised vertically, and subscripts (`𝑓`, `𝑐𝑙𝑠`, `𝑑𝑝`) are rendered at smaller
    size and lowered `origin_y`.
    """
    pdf_path = tmp_path / "unit_d_hat_sub.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    src_text = "where ˆ𝜙𝑓 is the phase and 𝐿𝑐𝑙𝑠, 𝐿𝑑𝑝 are losses."
    masked, items = mask_inline_protected_elements(src_text)
    blk = Block(
        id="doc-repro16:page-1:block-18",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text=src_text,
        masked_source_text=masked,
        protected_items=items,
        inline_math_spans=[
            InlineMathSpan(context_text="ˆ𝜙", target_text="ˆ", role="hat", scale=1.0, dy_em=-0.23),
            InlineMathSpan(context_text="𝜙𝑓", target_text="𝑓", role="sub", scale=0.78, dy_em=0.18),
            InlineMathSpan(context_text="𝐿𝑐𝑙𝑠", target_text="𝑐𝑙𝑠", role="sub", scale=0.78, dy_em=0.18),
            InlineMathSpan(context_text="𝐿𝑑𝑝", target_text="𝑑𝑝", role="sub", scale=0.78, dy_em=0.18),
        ],
        translation_text="ここで {v1} は位相であり、{v2}、{v3} は損失である。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "unit_d_hat_sub_out.pdf"
    _, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-unit-d-hat",
    )

    assert blk.status == BlockStatus.TRANSLATED
    assert not issues

    out_pdf = pymupdf.open(str(mono_out))
    d = out_pdf[0].get_text("dict", clip=pymupdf.Rect(blk.bbox))
    all_spans = [s for b in d["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
    out_pdf.close()

    hat_span = next(s for s in all_spans if "ˆ" in s["text"])
    phi_span = next(s for s in all_spans if "𝜙" in s["text"])
    sub_f_span = next(s for s in all_spans if "𝑓" in s["text"])
    sub_cls_span = next(s for s in all_spans if "𝑐𝑙𝑠" in s["text"])

    # Hat is raised above phi and overlaps horizontally with phi
    assert hat_span["origin"][1] < phi_span["origin"][1] - 1.2
    assert abs(hat_span["origin"][0] - phi_span["origin"][0]) < 4.0

    # Subscripts are lowered below baseline and have smaller font size
    assert sub_f_span["size"] < phi_span["size"] * 0.85
    assert sub_f_span["origin"][1] > phi_span["origin"][1] + 1.0
    assert sub_cls_span["size"] < phi_span["size"] * 0.85
    assert sub_cls_span["origin"][1] > phi_span["origin"][1] + 1.0


def test_unit_d_non_radical_lines_and_plain_text_not_misclassified() -> None:
    """Section 5: Horizontal rules that do not start at `√` or do not end at an expression
    boundary must NOT be classified as radical overbars, and normal-baseline alphanumeric
    tokens (`𝐸1`, `1𝑒−5`) must NOT be guessed as subscripts/superscripts.
    """
    raw_lines = [
        {
            "bbox": [50.0, 200.0, 300.0, 210.0],
            "spans": [
                {"text": "For instance, ", "size": 9.0, "origin": [50.0, 208.0], "bbox": [50.0, 200.0, 110.0, 210.0]},
                {"text": "𝐸", "size": 9.0, "origin": [110.0, 208.0], "bbox": [110.0, 200.0, 116.0, 210.0]},
                {"text": "1", "size": 9.0, "origin": [116.5, 208.0], "bbox": [116.5, 200.0, 121.0, 210.0]},
                {"text": " and rate 1𝑒−5 with √", "size": 9.0, "origin": [121.0, 208.0], "bbox": [121.0, 200.0, 210.0, 210.0]},
                {"text": "(x + y) in text", "size": 9.0, "origin": [210.0, 208.0], "bbox": [210.0, 200.0, 290.0, 210.0]},
            ],
        }
    ]
    # A full-width table/underline rule from x=50 to x=300 (does not start at √ x1=210)
    unrelated_drawings = [
        {"rect": pymupdf.Rect(50.0, 200.5, 300.0, 200.5), "width": 0.5},
        {"rect": pymupdf.Rect(210.0, 200.5, 350.0, 200.5), "width": 0.5},
    ]
    spans = extract_inline_math_spans(raw_lines, unrelated_drawings)
    assert spans == []


# ==============================================================================
# Unit E: Compatibility with Saved Jobs (`retypeset`), Manual Edits, and New Jobs
# ==============================================================================
def test_unit_e_saved_job_retypeset_and_manual_edit_compatibility(tmp_path: Path) -> None:
    """Section 7 (Path ②): Verify that an already-saved job block from the database
    (which has legacy 4-item `protected_items`, empty `inline_math_spans=[]`, and either
    `translation_text` or `manual_translation_text` with `user_protection_override`)
    can be retypeset cleanly without re-running LLM inference or breaking block IDs,
    recovering missing SMP glyphs, superscripts, and radical overbar from the original PDF.
    """
    pdf_path = tmp_path / "unit_e_saved.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    # Synthetic legacy saved state (4 protected items, empty inline_math_spans)
    legacy_blk = Block(
        id="doc-sample000001:page-3:block-16",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=15,
        source_text=(
            "In raw CSI samples (5 consecutive samples visualized in Fig- ure 3(a-b)), "
            "the amplitude (𝐴) and phase (Φ) of each complex ele- ment 𝑧= 𝑎+𝑏𝑖are computed "
            "using the formulation 𝐴=√︁(𝑎2 + 𝑏2) and Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range "
            "of the arctan function is from −𝜋to 𝜋and the phase values outside this range "
            "get wrapped."
        ),
        masked_source_text=(
            "In raw CSI samples (5 consecutive samples visualized in Fig- ure 3(a-b)), "
            "the amplitude (𝐴) and phase ({v1}) of each complex ele- ment 𝑧= 𝑎+𝑏𝑖are computed "
            "using the formulation 𝐴={v2}︁(𝑎2 + 𝑏2) and {v3} = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎). Note that the range "
            "of the arctan function is from {v4}𝜋to 𝜋and the phase values outside this range "
            "get wrapped."
        ),
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="Φ"),
            ProtectedItem(placeholder="{v2}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="√"),
            ProtectedItem(placeholder="{v3}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="Φ"),
            ProtectedItem(placeholder="{v4}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="−"),
        ],
        inline_math_spans=[],
        translation_text=(
            "生のCSIサンプルでは、各複素要素 𝑧= 𝑎+𝑏𝑖 の振幅 (𝐴) と位相 ({v1}) は、"
            "式 𝐴={v2}︁(𝑎2 + 𝑏2) と {v3} = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎) で計算され、範囲は {v4}𝜋 から 𝜋 である。"
        ),
        manual_translation_text=(
            "【手動修正】各複素要素 𝑧= 𝑎+𝑏𝑖 の振幅 (𝐴) と位相 ({v1}) は、"
            "式 𝐴={v2}︁(𝑎2 + 𝑏2) および {v3} = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎)（範囲 {v4}𝜋〜𝜋）で算出される。"
        ),
        status=BlockStatus.MANUALLY_EDITED,
        user_protection_override="force_translate",
        font_size_pt=8.97,
    )

    adapter = NativePreservingAdapter()
    mono_out = tmp_path / "unit_e_saved_mono.pdf"
    dual_out = tmp_path / "unit_e_saved_dual.pdf"
    updated_blocks, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[legacy_blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=dual_out,
        job_id="job-sample000001",
    )

    assert not issues
    assert updated_blocks[0].id == "doc-sample000001:page-3:block-16"
    assert updated_blocks[0].status == BlockStatus.MANUALLY_EDITED
    assert updated_blocks[0].user_protection_override == "force_translate"
    assert len(updated_blocks[0].inline_math_spans) >= 3

    out_pdf = pymupdf.open(str(mono_out))
    txt = out_pdf[0].get_text("text", clip=pymupdf.Rect(legacy_blk.bbox))
    out_pdf.close()

    assert "【手動修正】" in txt
    for token in ("𝑧", "𝑎", "𝑏", "𝑖", "𝐴", "Φ", "√", "𝑎𝑟𝑐𝑡𝑎𝑛", "−", "𝜋"):
        assert token in txt


def test_unit_e_new_job_parse_and_typeset_end_to_end(tmp_path: Path) -> None:
    """Section 7 (Path ①): Verify that extracting blocks from a new PDF populates
    recovered inter-span spaces, full inline math masking (`{v1}..{v7}`), and
    structured `inline_math_spans` right at `parse_and_extract_blocks` time.
    """
    pdf_path = tmp_path / "unit_e_new.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    adapter = NativePreservingAdapter()
    blocks = adapter.parse_and_extract_blocks(
        document=doc_model,
        page_indices=[0],
        job_config=TranslationJobConfig(),
    )
    b16 = next(b for b in blocks if "amplitude" in b.source_text)
    assert "𝑧= 𝑎+𝑏𝑖 are" in b16.source_text
    assert "−𝜋 to 𝜋 and" in b16.source_text
    origs = [it.original_text for it in b16.protected_items]
    assert "𝑧= 𝑎+𝑏𝑖" in origs
    assert "𝐴=√(𝑎2 + 𝑏2)" in origs
    assert "Φ = 𝑎𝑟𝑐𝑡𝑎𝑛(𝑏/𝑎)" in origs
    assert "−𝜋" in origs
    assert "𝜋" in origs
    roles = {(m.role, m.context_text, m.target_text) for m in b16.inline_math_spans}
    assert ("radicand", "√(𝑎2 + 𝑏2)", "(𝑎2 + 𝑏2)") in roles
    assert ("super", "𝑎2", "2") in roles
    assert ("super", "𝑏2", "2") in roles


def test_item4_tounicode_smp_minimal_repro_and_idempotence(tmp_path: Path) -> None:
    """Item 4: Demonstrate minimal reproduction of MuPDF's 5-digit hex `<1xxxx>`
    ToUnicode bug before `_fix_embedded_tounicode_smp`, verify that the fix restores exact
    SMP characters (`𝑃` U+1D443, `𝜙` U+1D719) alongside BMP characters (`A`), and
    verify idempotence when applied twice.
    """
    from backend.pdf.adapters.native_preserving_adapter import (
        _fix_embedded_tounicode_smp,
    )

    math_fp = AppConfig(runtime_dir=tmp_path / "rt_cfg").font_math_path
    assert math_fp is not None and math_fp.exists()

    # 1. Minimal failing reproduction BEFORE fix
    raw_doc = pymupdf.open()
    p0 = raw_doc.new_page(width=300, height=120)
    p0.insert_font(fontname="F_MATH", fontfile=str(math_fp))
    p0.insert_text(pymupdf.Point(20, 50), "A 𝑃 𝜙", fontsize=12.0, fontname="F_MATH")
    unfixed_pdf = tmp_path / "unfixed_smp.pdf"
    raw_doc.save(str(unfixed_pdf), garbage=2, deflate=True)
    raw_doc.close()

    reopened_unfixed = pymupdf.open(str(unfixed_pdf))
    unfixed_text = reopened_unfixed[0].get_text("text")
    reopened_unfixed.close()
    # Before fix, MuPDF writes <1d443> in /ToUnicode and reads back a corrupted BMP char instead of U+1D443 ('𝑃')
    assert "𝑃" not in unfixed_text
    assert "𝜙" not in unfixed_text

    # 2. Apply scoped `_fix_embedded_tounicode_smp` once and then a second time (idempotence)
    fix_doc = pymupdf.open(str(unfixed_pdf))
    updated_first = _fix_embedded_tounicode_smp(fix_doc, target_font_tags=("F_JA", "F_MATH"))
    assert updated_first == 1
    updated_second = _fix_embedded_tounicode_smp(fix_doc, target_font_tags=("F_JA", "F_MATH"))
    assert updated_second == 0

    fixed_pdf = tmp_path / "fixed_smp.pdf"
    fix_doc.save(str(fixed_pdf), garbage=2, deflate=True)
    fix_doc.close()

    reopened_fixed = pymupdf.open(str(fixed_pdf))
    fixed_text = reopened_fixed[0].get_text("text")
    reopened_fixed.close()
    assert "A" in fixed_text
    assert "𝑃" in fixed_text
    assert "𝜙" in fixed_text


def test_item4_tounicode_cmap_formats_and_original_font_protection(tmp_path: Path) -> None:
    """Item 4: Verify `_rewrite_tounicode_cmap_smp_bytes` on valid surrogate pairs, mixed
    BMP+SMP lines, `beginbfrange`, and unsupported CMap sections (`begincidchar`), and
    verify that original PDF font `/ToUnicode` streams are never modified.
    """
    from backend.pdf.adapters.native_preserving_adapter import (
        _collect_document_tounicode_xrefs,
        _fix_embedded_tounicode_smp,
        _rewrite_tounicode_cmap_smp_bytes,
    )

    sample_cmap = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"4 beginbfchar\n"
        b"<0001> <0041> <0002> <1d443>\n"
        b"<0003> <D835DC43>\n"
        b"<0004> <00660069>\n"
        b"endbfchar\n"
        b"2 beginbfrange\n"
        b"<0010> <0010> <1d719>\n"
        b"<0020> <0021> [<0042> <1d44e>]\n"
        b"endbfrange\n"
        b"1 begincidchar\n"
        b"<0030> <1d443>\n"
        b"endcidchar\n"
    )
    rewritten = _rewrite_tounicode_cmap_smp_bytes(sample_cmap)
    # BMP <0041>, valid surrogate <D835DC43>, ligature <00660069>, and begincidchar <1d443> untouched
    assert b"<0001> <0041> <0002> <D835DC43>" in rewritten
    assert b"<0003> <D835DC43>" in rewritten
    assert b"<0004> <00660069>" in rewritten
    # bfrange SMP entries converted to UTF-16BE surrogate pairs
    assert b"<0010> <0010> <D835DF19>" in rewritten
    assert b"<0020> <0021> [<0042> <D835DC4E>]" in rewritten
    # Unsupported begincidchar section left untouched
    assert b"1 begincidchar\n<0030> <1d443>\nendcidchar" in rewritten
    # Idempotent on second application
    assert _rewrite_tounicode_cmap_smp_bytes(rewritten) == rewritten

    # Verify original PDF font CMap is never modified when protected or not named F_JA / F_MATH
    math_fp = AppConfig(runtime_dir=tmp_path / "rt_cfg").font_math_path
    assert math_fp is not None
    orig_doc = pymupdf.open()
    page = orig_doc.new_page(width=200, height=100)
    page.insert_font(fontname="F_ORIG_FONT", fontfile=str(math_fp))
    page.insert_text(pymupdf.Point(10, 40), "𝑃", fontsize=11.0, fontname="F_ORIG_FONT")
    orig_pdf = tmp_path / "orig_font.pdf"
    orig_doc.save(str(orig_pdf), garbage=2, deflate=True)
    orig_doc.close()

    check_doc = pymupdf.open(str(orig_pdf))
    orig_tu_xrefs = _collect_document_tounicode_xrefs(check_doc)
    assert len(orig_tu_xrefs) == 1
    tu_xref = next(iter(orig_tu_xrefs))
    before_bytes = check_doc.xref_stream(tu_xref)
    assert b"<1d411>" in before_bytes.lower()

    updated = _fix_embedded_tounicode_smp(
        check_doc,
        target_font_tags=("F_JA", "F_MATH"),
        protected_tounicode_xrefs=orig_tu_xrefs,
    )
    assert updated == 0
    assert check_doc.xref_stream(tu_xref) == before_bytes
    check_doc.close()


def test_item5_overlapping_translated_blocks_fallback_to_original(tmp_path: Path) -> None:
    """Item 5: When two translatable blocks on the same page deeply overlap vertically
    (such as `page-5:block-13`..`14` or `page-7:block-26`..`27`), both blocks must safely
    fall back to `BlockStatus.FALLBACK_ORIGINAL` with `TRANSLATED_BLOCK_OVERLAP_FALLBACK`
    before redaction so rendered Japanese lines never collide and split fractions stay intact.
    """
    pdf_path = tmp_path / "overlap_blocks.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(pymupdf.Point(318, 450), "Upper split paragraph line with {P*_2, ..., P*_14}", fontsize=9.0)
    page.insert_text(pymupdf.Point(318, 468), "Lower split paragraph continuation line in original PDF", fontsize=9.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-overlap-test",
        original_filename="overlap_blocks.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    b1 = Block(
        id="doc-overlap-test:page-0:block-13",
        page_index=0,
        reading_order=0,
        bbox=[317.62, 435.59, 559.71, 468.49],
        kind=BlockKind.PARAGRAPH,
        source_text="Upper split paragraph line with {P*_2, ..., P*_14}",
        masked_source_text="Upper split paragraph line with {v1}",
        translation_text="上側の分割段落の日本語訳であり、下側ブロックと大きく重複しています。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=9.0,
    )
    b2 = Block(
        id="doc-overlap-test:page-0:block-14",
        page_index=0,
        reading_order=1,
        bbox=[317.95, 457.01, 558.20, 479.45],
        kind=BlockKind.PARAGRAPH,
        source_text="Lower split paragraph continuation line in original PDF",
        masked_source_text="Lower split paragraph continuation line in original PDF",
        translation_text="下側の分割段落の日本語訳です。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=9.0,
    )

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    mono_out = tmp_path / "overlap_mono.pdf"
    updated_blocks, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[b1, b2],
        page_indices=[0],
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-overlap",
        job_config=TranslationJobConfig(),
    )

    assert updated_blocks[0].status == BlockStatus.FALLBACK_ORIGINAL
    assert updated_blocks[1].status == BlockStatus.FALLBACK_ORIGINAL
    assert "TRANSLATED_BLOCK_OVERLAP_FALLBACK" in updated_blocks[0].warnings
    assert "TRANSLATED_BLOCK_OVERLAP_FALLBACK" in updated_blocks[1].warnings
    assert sum(1 for iss in issues if iss.code == "TRANSLATED_BLOCK_OVERLAP_FALLBACK") == 2

    out_doc = pymupdf.open(str(mono_out))
    out_txt = out_doc[0].get_text("text")
    out_doc.close()
    assert "Upper split paragraph line" in out_txt
    assert "Lower split paragraph continuation line" in out_txt
    assert "上側の分割段落" not in out_txt


def test_item6_case1_placeholder_missing_dup_unknown_altered(tmp_path: Path) -> None:
    """Item 6 (Case 1): Verify missing, duplicated, unknown (`{v99}`), and altered
    (`[v1]`, `{v1_mod}`, `｛v1｝`, `{ v1 }`) placeholders are caught by validation and
    trigger safe `FALLBACK_ORIGINAL` before redaction.
    """
    from backend.pdf.adapters.native_preserving_adapter import (
        _find_unresolved_placeholders,
    )

    p_items = [
        ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="𝛼 = 1"),
    ]
    # 1. Missing placeholder
    res_missing = validate_translation_output(
        source_text="Where 𝛼 = 1 holds.",
        masked_source_text="Where {v1} holds.",
        translated_text="ここで成り立つ。",
        protected_items=p_items,
        finish_reason="stop",
    )
    assert not res_missing.is_valid
    assert any(f.code == "PLACEHOLDER_MISSING" for f in res_missing.findings)

    # 2. Duplicated placeholder
    res_dup = validate_translation_output(
        source_text="Where 𝛼 = 1 holds.",
        masked_source_text="Where {v1} holds.",
        translated_text="{v1} と {v1} が成り立つ。",
        protected_items=p_items,
        finish_reason="stop",
    )
    assert not res_dup.is_valid
    assert any(f.code == "PLACEHOLDER_DUPLICATED" for f in res_dup.findings)

    # 3. Unknown placeholder {v99}
    res_unk = validate_translation_output(
        source_text="Where 𝛼 = 1 holds.",
        masked_source_text="Where {v1} holds.",
        translated_text="{v1} と {v99} が成り立つ。",
        protected_items=p_items,
        finish_reason="stop",
    )
    assert not res_unk.is_valid
    assert any(f.code == "PLACEHOLDER_HALLUCINATED" for f in res_unk.findings)

    # 4. Altered placeholders ([v1], {v1_mod}, ｛v1｝, { v1 }) caught both by validator and typeset pre-check
    for altered in ("[v1] が成り立つ。", "{v1} と {v1_mod} が成り立つ。", "｛v1｝ が成り立つ。", "{ v1 } が成り立つ。"):
        res_alt = validate_translation_output(
            source_text="Where 𝛼 = 1 holds.",
            masked_source_text="Where {v1} holds.",
            translated_text=altered,
            protected_items=p_items,
            finish_reason="stop",
        )
        assert not res_alt.is_valid
        assert any(f.code in ("PLACEHOLDER_ALTERED", "PLACEHOLDER_MISSING") for f in res_alt.findings)
        unresolved = _find_unresolved_placeholders(
            unmask_inline_protected_elements(altered, p_items),
            "Where 𝛼 = 1 holds.",
        )
        assert len(unresolved) >= 1, f"Expected altered placeholder in {altered!r} to be detected"


def test_item6_case2_and_3_literal_placeholder_and_pua_collision() -> None:
    """Item 6 (Cases 2 & 3): Verify that pre-existing literal `{v1}` AND pre-existing
    literal Private Use Area characters (`\\uE100`, `\\uE101`) in `source_text` never
    collide with generated sentinels or chain-expand during unmasking.
    """
    src = "Literal {v1} and PUA \ue100 \ue101 with math 𝛼 = 𝛽 + 1 in text."
    masked, items = mask_inline_protected_elements(src)
    # Literal PUA characters must still be intact in masked text
    assert "\ue100" in masked
    assert "\ue101" in masked
    # Both literal {v1} and math 𝛼 = 𝛽 + 1 are protected as distinct placeholders
    assert len(items) == 2
    assert items[0].original_text == "{v1}"
    assert items[1].original_text == "𝛼 = 𝛽 + 1"

    restored = unmask_inline_protected_elements(masked, items)
    assert restored == src


def test_item6_case4_repeated_identical_math_string_with_conflicting_roles() -> None:
    """Item 6 (Case 4): When the exact same string (e.g. `𝑥2` or `𝑎1`) appears multiple
    times within the same block with different sub/superscript structures (e.g. `x^2` and
    `x_2`, or `a_1` and baseline `a1`), `extract_inline_math_spans` must detect the
    conflict and exclude the ambiguous span so neither occurrence gets mis-styled.
    """
    raw_lines = [
        {
            "bbox": [10.0, 10.0, 220.0, 22.0],
            "spans": [
                # First occurrence: x^2 (superscript 2, origin y=15.0 < baseline 18.0)
                {"text": "𝑥", "size": 10.0, "origin": [10.0, 18.0], "bbox": [10.0, 10.0, 16.0, 20.0]},
                {"text": "2", "size": 7.0, "origin": [16.2, 15.0], "bbox": [16.2, 9.0, 20.0, 16.0]},
                {"text": " and ", "size": 10.0, "origin": [21.0, 18.0], "bbox": [21.0, 10.0, 42.0, 20.0]},
                # Second occurrence: x_2 (subscript 2, origin y=20.5 > baseline 18.0)
                {"text": "𝑥", "size": 10.0, "origin": [43.0, 18.0], "bbox": [43.0, 10.0, 49.0, 20.0]},
                {"text": "2", "size": 7.0, "origin": [49.2, 20.5], "bbox": [49.2, 14.0, 53.0, 22.0]},
                {"text": " plus ", "size": 10.0, "origin": [54.0, 18.0], "bbox": [54.0, 10.0, 80.0, 20.0]},
                # Third occurrence: a_1 (subscript 1) followed by baseline normal "𝑎1"
                {"text": "𝑎", "size": 10.0, "origin": [81.0, 18.0], "bbox": [81.0, 10.0, 87.0, 20.0]},
                {"text": "1", "size": 7.0, "origin": [87.2, 20.5], "bbox": [87.2, 14.0, 91.0, 22.0]},
                {"text": " vs 𝑎1", "size": 10.0, "origin": [92.0, 18.0], "bbox": [92.0, 10.0, 125.0, 20.0]},
                # Unambiguous control: y^3 (only appears as superscript)
                {"text": " 𝑦", "size": 10.0, "origin": [126.0, 18.0], "bbox": [126.0, 10.0, 134.0, 20.0]},
                {"text": "3", "size": 7.0, "origin": [134.2, 15.0], "bbox": [134.2, 9.0, 138.0, 16.0]},
            ],
        }
    ]
    spans = extract_inline_math_spans(raw_lines)
    non_ambiguous_spans = [s for s in spans if not s.ambiguous]
    ambiguous_spans = [s for s in spans if s.ambiguous]
    ctx_set = {(s.context_text, s.target_text, s.role) for s in non_ambiguous_spans}
    # Conflicting "𝑥2" (super vs sub) and "𝑎1" (sub vs normal) must not be kept as unambiguous,
    # and must instead be propagated explicitly with ambiguous=True.
    assert not any(s.context_text == "𝑥2" for s in non_ambiguous_spans)
    assert not any(s.context_text == "𝑎1" for s in non_ambiguous_spans)
    assert any(s.context_text == "𝑥2" and s.ambiguous for s in ambiguous_spans)
    assert any(s.context_text == "𝑎1" and s.ambiguous for s in ambiguous_spans)
    # Unambiguous "𝑦3" (super) is preserved
    assert ("𝑦3", "3", "super") in ctx_set


def test_item6_case5_and_6_manual_edit_reordering_and_no_false_span_application(tmp_path: Path) -> None:
    """Item 6 (Cases 5 & 6): Verify that manual edits can reorder/duplicate `{vN}`
    placeholders while retaining their math spans, and that deleting `{vN}` and typing
    plain text (e.g. `x2` or `Modelx2`) never falsely triggers source `inline_math_spans`.
    """
    pdf_path = tmp_path / "manual_edit_case56.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text(pymupdf.Point(20, 40), "Values x2 and y3 are used.", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-man-56",
        original_filename="manual_edit_case56.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    # Case 5: Reordered {v2} before {v1}, and {v1} used while {v2} is kept
    # Case 6: If {v1} ("x2") is omitted by user and user writes plain "x2", x2 is NOT styled as subscript
    blk = Block(
        id="doc-man-56:page-0:block-1",
        page_index=0,
        reading_order=0,
        bbox=[20.0, 25.0, 360.0, 80.0],
        kind=BlockKind.PARAGRAPH,
        source_text="Values x2 and y3 are used.",
        masked_source_text="Values {v1} and {v2} are used.",
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="x2"),
            ProtectedItem(placeholder="{v2}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="y3"),
        ],
        inline_math_spans=[
            InlineMathSpan(context_text="x2", target_text="2", role="sub", scale=0.78, dy_em=0.18),
            InlineMathSpan(context_text="y3", target_text="3", role="super", scale=0.78, dy_em=-0.29),
        ],
        translation_text="値 {v1} と {v2} を使用する。",
        # User removed {v1} and typed plain identifier "x2" manually, while keeping {v2} twice
        manual_translation_text="手動で {v2}（もう一度 {v2}）および普通の文字列 x2 を記述した。",
        status=BlockStatus.MANUALLY_EDITED,
        font_size_pt=10.0,
    )

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    mono_out = tmp_path / "manual_56_mono.pdf"
    updated_blocks, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-man-56",
        job_config=TranslationJobConfig(),
    )
    assert not issues
    assert updated_blocks[0].status == BlockStatus.MANUALLY_EDITED

    out_doc = pymupdf.open(str(mono_out))
    raw_dict = out_doc[0].get_text("dict")
    out_doc.close()
    spans = [
        s
        for b in raw_dict["blocks"]
        if b.get("type", 0) == 0
        for ln in b["lines"]
        for s in ln["spans"]
    ]
    # "3" in {v2} ("y3") was rendered at superscript scale (< 9.0pt), whereas "x2" was rendered as normal baseline text
    three_spans = [s for s in spans if "3" in s["text"]]
    x2_spans = [s for s in spans if "x2" in s["text"] or s["text"].strip() == "2"]
    assert three_spans and all(float(s["size"]) < 9.0 for s in three_spans)
    assert x2_spans and all(float(s["size"]) >= 9.5 for s in x2_spans)


def test_item6_case7_missing_math_font_triggers_safe_fallback(tmp_path: Path) -> None:
    """Item 6 (Case 7): When `font_math_path` is unavailable (`None`), a block containing
    SMP mathematical symbols (`𝑃` U+1D443) that `NotoSerifJP` lacks must safely fall back
    to `FALLBACK_ORIGINAL` with `UNRENDERABLE_GLYPH_FALLBACK` before redaction.
    """
    pdf_path = tmp_path / "no_math_font.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=150)
    page.insert_text(pymupdf.Point(20, 50), "Original English with math P.", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-nomath",
        original_filename="no_math_font.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )
    blk = Block(
        id="doc-nomath:page-0:block-1",
        page_index=0,
        reading_order=0,
        bbox=[20.0, 35.0, 300.0, 75.0],
        kind=BlockKind.PARAGRAPH,
        source_text="Original English with math 𝑃.",
        masked_source_text="Original English with math {v1}.",
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="𝑃"),
        ],
        translation_text="数式 {v1} を含む翻訳。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=10.0,
    )

    cfg = AppConfig(runtime_dir=tmp_path / "rt", font_math_path=None)
    adapter = NativePreservingAdapter(config=cfg)
    mono_out = tmp_path / "nomath_mono.pdf"
    updated_blocks, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-nomath",
        job_config=TranslationJobConfig(),
    )
    assert updated_blocks[0].status == BlockStatus.FALLBACK_ORIGINAL
    assert "UNRENDERABLE_GLYPH_FALLBACK" in updated_blocks[0].warnings
    assert any(iss.code == "UNRENDERABLE_GLYPH_FALLBACK" for iss in issues)

    out_doc = pymupdf.open(str(mono_out))
    out_txt = out_doc[0].get_text("text")
    out_doc.close()
    assert "Original English with math P." in out_txt


def test_item6_case8_adjacent_fallback_and_protected_skip_not_erased_by_redaction(tmp_path: Path) -> None:
    """Item 6 (Case 8): When a `TRANSLATED` block's bbox is flush against an adjacent
    `FALLBACK_ORIGINAL` block above/below or a `PROTECTED_SKIP` block on the left/right,
    redacting the `TRANSLATED` block must never erase glyphs in the adjacent blocks.
    """
    pdf_path = tmp_path / "adj_redaction.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    page.insert_text(pymupdf.Point(30, 48), "TOP FALLBACK ORIGINAL LINE", fontsize=10.0)
    page.insert_text(pymupdf.Point(30, 68), "MIDDLE TRANSLATED LINE TO REPLACE", fontsize=10.0)
    page.insert_text(pymupdf.Point(30, 92), "BOTTOM PROTECTED SKIP FORMULA E=mc2", fontsize=10.0)
    page.insert_text(pymupdf.Point(280, 68), "RIGHT SIDE PROTECTED TEXT", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-adj",
        original_filename="adj_redaction.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    b_top = Block(
        id="doc-adj:page-0:block-1",
        page_index=0,
        reading_order=0,
        bbox=[28.0, 36.0, 260.0, 53.0],
        kind=BlockKind.PARAGRAPH,
        source_text="TOP FALLBACK ORIGINAL LINE",
        status=BlockStatus.FALLBACK_ORIGINAL,
        font_size_pt=10.0,
    )
    b_mid = Block(
        id="doc-adj:page-0:block-2",
        page_index=0,
        reading_order=1,
        # Flush within 0.2pt of b_top, b_bot, and b_right
        bbox=[28.0, 52.8, 278.2, 79.2],
        kind=BlockKind.PARAGRAPH,
        source_text="MIDDLE TRANSLATED LINE TO REPLACE",
        masked_source_text="MIDDLE TRANSLATED LINE TO REPLACE",
        translation_text="中央の翻訳済みテキスト",
        status=BlockStatus.TRANSLATED,
        font_size_pt=10.0,
    )
    b_bot = Block(
        id="doc-adj:page-0:block-3",
        page_index=0,
        reading_order=2,
        bbox=[28.0, 79.0, 260.0, 98.0],
        kind=BlockKind.ISOLATED_FORMULA,
        source_text="BOTTOM PROTECTED SKIP FORMULA E=mc2",
        status=BlockStatus.PROTECTED_SKIP,
        font_size_pt=10.0,
    )
    b_right = Block(
        id="doc-adj:page-0:block-4",
        page_index=0,
        reading_order=3,
        bbox=[278.0, 54.0, 460.0, 78.0],
        kind=BlockKind.PARAGRAPH,
        source_text="RIGHT SIDE PROTECTED TEXT",
        status=BlockStatus.FALLBACK_ORIGINAL,
        font_size_pt=10.0,
    )

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    mono_out = tmp_path / "adj_mono.pdf"
    updated_blocks, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[b_top, b_mid, b_bot, b_right],
        page_indices=[0],
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-adj",
        job_config=TranslationJobConfig(),
    )
    assert not issues
    assert updated_blocks[1].status == BlockStatus.TRANSLATED

    out_doc = pymupdf.open(str(mono_out))
    out_txt = out_doc[0].get_text("text")
    out_doc.close()
    assert "TOP FALLBACK ORIGINAL LINE" in out_txt
    assert "BOTTOM PROTECTED SKIP FORMULA E=mc2" in out_txt
    assert "RIGHT SIDE PROTECTED TEXT" in out_txt
    assert "中央の翻訳済みテキスト" in out_txt
    assert "MIDDLE TRANSLATED LINE TO REPLACE" not in out_txt


@pytest.mark.skipif(
    not Path(r"C:\Windows\Fonts\YuGothR.ttc").exists()
    or not Path(r"C:\Windows\Fonts\cambria.ttc").exists(),
    reason="Requires Windows YuGothR.ttc and cambria.ttc fonts",
)
def test_yugothr_fallback_prefers_math_font_for_minus_and_hat(tmp_path: Path) -> None:
    """When NotoSerifJP is absent and Japanese rendering falls back to YuGothR.ttc,
    math operators (U+2212) and accents (U+02C6) must route to F_MATH (Cambria) so
    YuGothR's shared-GID ToUnicode collisions (U+FF0D / U+0302) do not corrupt extraction.
    """
    pdf_path = tmp_path / "yugothr_math.pdf"
    doc_model = _make_densepose_p3_block16_pdf(pdf_path)

    src_text = "where ˆ𝜙𝑓 is the phase in −𝜋 to 𝜋."
    masked, items = mask_inline_protected_elements(src_text)
    blk = Block(
        id="doc-yugothr:page-1:block-1",
        page_index=0,
        bbox=[318.0, 357.0, 560.0, 433.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text=src_text,
        masked_source_text=masked,
        protected_items=items,
        inline_math_spans=[
            InlineMathSpan(context_text="ˆ𝜙", target_text="ˆ", role="hat", scale=1.0, dy_em=-0.23),
            InlineMathSpan(context_text="𝜙𝑓", target_text="𝑓", role="sub", scale=0.78, dy_em=0.18),
        ],
        translation_text="ここで位相 {v1} の範囲は {v2} から {v3} までである。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=8.97,
    )

    cfg = AppConfig(
        runtime_dir=tmp_path / "rt",
        font_serif_path=Path(r"C:\Windows\Fonts\YuGothR.ttc"),
        font_sans_path=Path(r"C:\Windows\Fonts\YuGothR.ttc"),
        font_math_path=Path(r"C:\Windows\Fonts\cambria.ttc"),
    )
    adapter = NativePreservingAdapter(config=cfg)
    mono_out = tmp_path / "yugothr_mono.pdf"
    updated, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-yugothr",
    )

    assert not issues
    assert updated[0].status == BlockStatus.TRANSLATED

    out_pdf = pymupdf.open(str(mono_out))
    extracted = out_pdf[0].get_text("text", clip=pymupdf.Rect(blk.bbox)).replace("\xa0", " ")
    out_pdf.close()

    assert "−" in extracted
    assert "－" not in extracted
    assert "ˆ" in extracted
    assert "𝜋" in extracted


def test_review_item1_conflicting_math_roles_e2e_fallback_and_warning(tmp_path: Path) -> None:
    """PR review #1: A block containing x^2 (super), x_2 (sub), and normal x2 must NOT
    silently drop InlineMathSpans and flatten everything to plain 'x2'. It must either
    preserve each role unambiguously or safely fall back to original + warning before redaction.
    """
    pdf_path = tmp_path / "item1_ambiguous_math.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=400)
    # Build one paragraph block [40, 40, 420, 80] with x^2, x_2, and baseline x2
    page.insert_text(pymupdf.Point(40, 60), "Consider x", fontsize=10.0)
    page.insert_text(pymupdf.Point(89, 55), "2", fontsize=6.5)  # superscript
    page.insert_text(pymupdf.Point(94, 60), ", x", fontsize=10.0)
    page.insert_text(pymupdf.Point(108, 63), "2", fontsize=6.5)  # subscript
    page.insert_text(pymupdf.Point(114, 60), ", and model x2 here.", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-rev-item1",
        original_filename="item1_ambiguous_math.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    blocks = adapter.parse_and_extract_blocks(
        document=doc_model,
        page_indices=[0],
        job_config=TranslationJobConfig(),
    )
    assert len(blocks) == 1
    blk = blocks[0]
    blk.translation_text = "x2 と x2 と通常モデル x2 を考える。"
    blk.status = BlockStatus.TRANSLATED

    mono_out = tmp_path / "item1_mono.pdf"
    updated, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-rev-item1",
    )

    assert updated[0].status == BlockStatus.FALLBACK_ORIGINAL
    assert "AMBIGUOUS_INLINE_MATH_FALLBACK" in updated[0].warnings
    assert any(iss.code == "AMBIGUOUS_INLINE_MATH_FALLBACK" for iss in issues)

    out_pdf = pymupdf.open(str(mono_out))
    d = out_pdf[0].get_text("dict")
    spans = [s for b in d["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
    out_txt = out_pdf[0].get_text("text")
    out_pdf.close()

    # Original PDF math roles (super 2 raised, sub 2 lowered, normal x2 retained) must survive intact
    assert "Consider x" in out_txt
    assert "考える" not in out_txt
    assert any(s["text"].strip() == "2" and s["size"] < 8.0 and s["origin"][1] < 58.0 for s in spans)
    assert any(s["text"].strip() == "2" and s["size"] < 8.0 and s["origin"][1] > 61.5 for s in spans)


def test_review_item2_fit_checks_horizontal_width_including_long_tokens_and_kinsoku(tmp_path: Path) -> None:
    """PR review #2: fit_japanese_text_in_bbox and wrap_japanese_kinsoku must verify
    horizontal extent of every rendered line against bbox width: long formulas, long URLs/words,
    mixed Japanese+math, and kinsoku line-end punctuation must never overflow horizontally.
    """
    cfg = AppConfig(runtime_dir=tmp_path / "rt")
    ja_font = pymupdf.Font(fontfile=str(cfg.font_serif_path)) if cfg.font_serif_path and cfg.font_serif_path.exists() else pymupdf.Font("japan-s")
    math_font = pymupdf.Font(fontfile=str(cfg.font_math_path)) if cfg.font_math_path and cfg.font_math_path.exists() else None
    math_tag = "F_MATH" if math_font is not None else None

    narrow_bbox = [20.0, 20.0, 110.0, 220.0]  # 90pt wide, very tall (200pt)
    box_w = narrow_bbox[2] - narrow_bbox[0]

    # 1. Long unbreakable inline formula wider than 90pt even at 8.0pt
    long_formula = "𝑎_1+𝑎_2+𝑎_3+𝑎_4+𝑎_5+𝑎_6+𝑎_7+𝑎_8=∑_𝑖^𝑁 𝑏_𝑖"
    _, _, fits_formula = fit_japanese_text_in_bbox(
        long_formula,
        narrow_bbox,
        initial_font_size=10.0,
        min_font_size=8.0,
        ja_font=ja_font,
        math_font=math_font,
    )
    assert fits_formula is False

    # 2. Long English word & long URL wider than 90pt even at 8.0pt
    for long_tok in (
        "https://example.org/very/long/academic/supplement/appendix/dataset_v2.json",
        "Pneumonoultramicroscopicsilicovolcanoconiosis",
    ):
        _, _, fits_tok = fit_japanese_text_in_bbox(
            long_tok,
            narrow_bbox,
            initial_font_size=10.0,
            min_font_size=8.0,
            ja_font=ja_font,
            math_font=math_font,
        )
        assert fits_tok is False

    # 3. Japanese before & after math that fits only if wrapped cleanly, never overflowing bbox width
    mixed_text = "この解析では 𝑎+𝑏+𝑐=𝑑 を用いて結果を計算する。"
    fitted_sz, wrapped, fits_mixed = fit_japanese_text_in_bbox(
        mixed_text,
        narrow_bbox,
        initial_font_size=10.0,
        min_font_size=8.0,
        ja_font=ja_font,
        math_font=math_font,
    )
    assert fits_mixed is True
    for ln in wrapped:
        _, _, line_w, _, _ = _build_styled_line_plan(
            line_str=ln,
            font_size_pt=fitted_sz,
            ja_font_tag="F_JA",
            ja_font=ja_font,
            math_font_tag=math_tag,
            math_font=math_font,
        )
        assert line_w <= box_w + 1e-3

    # 4. Kinsoku overhang check: punctuation near right edge must neither start next line nor overflow box_w
    kinsoku_text = "日本語の文章を狭い枠へ配置するテストです。"
    k_sz, k_lines, k_fits = fit_japanese_text_in_bbox(
        kinsoku_text,
        [20.0, 20.0, 120.0, 220.0],  # 100pt wide (close to 10 chars at 10pt)
        initial_font_size=10.0,
        min_font_size=8.0,
        ja_font=ja_font,
        math_font=math_font,
    )
    assert k_fits is True
    for ln in k_lines:
        assert not ln.startswith(("。", "、", "）", "」"))
        _, _, lw, _, _ = _build_styled_line_plan(
            line_str=ln,
            font_size_pt=k_sz,
            ja_font_tag="F_JA",
            ja_font=ja_font,
            math_font_tag=math_tag,
            math_font=math_font,
        )
        assert lw <= 100.0 + 1e-3

    # Fractional initial font size stepping (e.g. 8.97 -> 8.47 -> 8.0) must still test min_font_size=8.0
    frac_sz, frac_lines, frac_fits = fit_japanese_text_in_bbox(
        "本手法の総損失は、次のように計算される。",
        [317.69, 538.49, 486.13, 547.46],
        initial_font_size=8.97,
        min_font_size=8.0,
        ja_font=ja_font,
        math_font=math_font,
    )
    assert frac_fits is True
    assert frac_sz == 8.0
    assert len(frac_lines) == 1

    # Also verify end-to-end typeset triggers TYPESETTING_OVERFLOW_FALLBACK on long URL
    pdf_path = tmp_path / "item2_overflow.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text(pymupdf.Point(20, 40), "Short src.", fontsize=9.0)
    doc.save(str(pdf_path))
    doc.close()
    doc_model = Document(
        id="doc-rev-item2",
        original_filename="item2_overflow.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )
    blk = Block(
        id="doc-rev-item2:page-1:block-1",
        page_index=0,
        bbox=[20.0, 25.0, 110.0, 225.0],
        kind=BlockKind.PARAGRAPH,
        source_text="Short src.",
        translation_text="https://example.org/very/long/academic/supplement/appendix/dataset_v2.json",
        status=BlockStatus.TRANSLATED,
        font_size_pt=9.0,
    )
    mono_out = tmp_path / "item2_mono.pdf"
    updated, issues = NativePreservingAdapter(config=cfg).typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-rev-item2",
    )
    assert updated[0].status == BlockStatus.FALLBACK_ORIGINAL
    assert "TYPESETTING_OVERFLOW_FALLBACK" in updated[0].warnings
    assert any(iss.code == "TYPESETTING_OVERFLOW_FALLBACK" for iss in issues)


def test_review_item3_stacked_multi_char_super_and_sub_coordinates(tmp_path: Path) -> None:
    """PR review #3: Multi-char stacked super/subscripts (P^{abc}_{i}, P^{*}_{index}, P^{ab}_{cd})
    followed by '+ Q' or Japanese must align subscript start x0 with superscript GROUP start x0,
    keep subscript characters contiguous, and start following text at max(sup_x1, sub_x1).
    """
    pdf_path = tmp_path / "item3_stacked.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=400)
    page.insert_text(pymupdf.Point(40, 60), "Base text to replace", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-rev-item3",
        original_filename="item3_stacked.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    spans_list = [
        # Case 1: P^{abc}_{i} + Q  (super 'abc' wider than sub 'i')
        InlineMathSpan(context_text="𝑃abc", target_text="abc", role="super", scale=0.78, dy_em=-0.29),
        InlineMathSpan(context_text="𝑃abc i", target_text="i", role="sub", scale=0.78, dy_em=0.18),
        # Case 2: P^{*}_{index} は (sub 'index' wider than super '∗')
        InlineMathSpan(context_text="𝑃∗", target_text="∗", role="super", scale=0.78, dy_em=-0.29),
        InlineMathSpan(context_text="𝑃∗ index", target_text="index", role="sub", scale=0.78, dy_em=0.18),
        # Case 3: P^{ab}_{cd} + Q
        InlineMathSpan(context_text="𝑃ab", target_text="ab", role="super", scale=0.78, dy_em=-0.29),
        InlineMathSpan(context_text="𝑃ab cd", target_text="cd", role="sub", scale=0.78, dy_em=0.18),
    ]
    trn = "式 𝑃abc i + 𝑄、𝑃∗ index は係数、𝑃ab cd + 𝑄 である。"
    blk = Block(
        id="doc-rev-item3:page-1:block-1",
        page_index=0,
        bbox=[40.0, 40.0, 560.0, 120.0],
        kind=BlockKind.PARAGRAPH,
        source_text="Base text to replace",
        inline_math_spans=spans_list,
        translation_text=trn,
        status=BlockStatus.TRANSLATED,
        font_size_pt=10.0,
    )

    mono_out = tmp_path / "item3_mono.pdf"
    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    updated, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-rev-item3",
    )
    assert not issues
    assert updated[0].status == BlockStatus.TRANSLATED

    out_pdf = pymupdf.open(str(mono_out))
    d = out_pdf[0].get_text("dict")
    pdf_spans = [s for b in d["blocks"] if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]]
    out_pdf.close()

    # Check P^{abc}_{i} + Q (with space and without space)
    spans_list_nospace = [
        InlineMathSpan(context_text="𝑃abc", target_text="abc", role="super", scale=0.78, dy_em=-0.29),
        InlineMathSpan(context_text="𝑃abci", target_text="i", role="sub", scale=0.78, dy_em=0.18),
    ]
    _, _, w_nospace, _, _ = _build_styled_line_plan(
        "𝑃abci + 𝑄",
        font_size_pt=10.0,
        ja_font_tag="F_JA",
        ja_font=None,
        inline_math_spans=spans_list_nospace,
    )
    assert w_nospace > 0

    s_abc = next(s for s in pdf_spans if "abc" in s["text"])
    s_i = next(s for s in pdf_spans if s["text"].strip() == "i" and s["size"] < 8.5)
    assert abs(s_i["bbox"][0] - s_abc["bbox"][0]) < 1.0, f"sub 'i' x0={s_i['bbox'][0]} != super 'abc' x0={s_abc['bbox'][0]}"
    # Trailing '+' (ja_font) and '𝑄' (math_font) are rendered in adjacent spans after max(sup, sub)
    after_abc_plus = [s for s in pdf_spans if s["bbox"][0] > s_abc["bbox"][0] and abs(s["origin"][1] - (s_abc["origin"][1] + 2.9)) < 10.0 and s["text"].strip() == "+"]
    after_abc_q = [s for s in pdf_spans if s["bbox"][0] > s_abc["bbox"][0] and "𝑄" in s["text"]]
    assert after_abc_plus and after_abc_plus[0]["bbox"][0] >= s_abc["bbox"][2] - 0.5
    assert after_abc_q and after_abc_q[0]["bbox"][0] > after_abc_plus[0]["bbox"][2] - 0.5

    # Check P^{*}_{index} followed by Japanese 'は係数'
    s_star = next(s for s in pdf_spans if "∗" in s["text"])
    s_index = next(s for s in pdf_spans if "index" in s["text"])
    assert abs(s_index["bbox"][0] - s_star["bbox"][0]) < 1.0, f"sub 'index' x0={s_index['bbox'][0]} != super '*' x0={s_star['bbox'][0]}"
    after_index = next(s for s in pdf_spans if "は係数" in s["text"])
    assert after_index["bbox"][0] >= s_index["bbox"][2] - 0.5, f"'は係数' x0={after_index['bbox'][0]} overlaps 'index' x1={s_index['bbox'][2]}"

    # Check P^{ab}_{cd} + Q
    s_ab = next(s for s in pdf_spans if s["text"].strip() == "ab" and s["size"] < 8.5)
    s_cd = next(s for s in pdf_spans if s["text"].strip() == "cd" and s["size"] < 8.5)
    assert abs(s_cd["bbox"][0] - s_ab["bbox"][0]) < 1.0
    after_cd_plus = [s for s in pdf_spans if s["bbox"][0] > s_ab["bbox"][0] and abs(s["origin"][1] - (s_ab["origin"][1] + 2.9)) < 10.0 and s["text"].strip() == "+"]
    assert after_cd_plus and after_cd_plus[0]["bbox"][0] >= max(s_ab["bbox"][2], s_cd["bbox"][2]) - 0.5

    # Also verify extraction handles multi-char stacked super/sub spans (P^{abc}_{i}, P^{*}_{index}, P^{ab}_{cd})
    extracted_stacked = extract_inline_math_spans(
        [
            {
                "spans": [
                    {"text": "𝑃", "size": 10.0, "origin": [40.0, 60.0], "bbox": [40.0, 50.0, 46.0, 62.0]},
                    {"text": "abc", "size": 7.0, "origin": [46.0, 56.0], "bbox": [46.0, 49.0, 58.0, 57.0]},
                    {"text": "i", "size": 7.0, "origin": [46.2, 63.0], "bbox": [46.2, 57.0, 49.0, 64.0]},
                    {"text": " and 𝑃", "size": 10.0, "origin": [60.0, 60.0], "bbox": [60.0, 50.0, 82.0, 62.0]},
                    {"text": "ab", "size": 7.0, "origin": [82.0, 56.0], "bbox": [82.0, 49.0, 90.0, 57.0]},
                    {"text": "cd", "size": 7.0, "origin": [82.2, 63.0], "bbox": [82.2, 57.0, 90.2, 64.0]},
                ]
            }
        ]
    )
    ex_pairs = {(s.context_text, s.target_text, s.role) for s in extracted_stacked if not s.ambiguous}
    assert ("𝑃abc", "abc", "super") in ex_pairs
    assert ("𝑃abci", "i", "sub") in ex_pairs
    assert ("𝑃ab", "ab", "super") in ex_pairs
    assert ("𝑃abcd", "cd", "sub") in ex_pairs


def test_review_item4_order_independent_protection_convergence(tmp_path: Path) -> None:
    """PR review #4: Reordering blocks on a page must produce identical protection status,
    warnings, and visual PDF output even when a later block triggers fallback after
    overlapping an earlier candidate block.
    """
    pdf_path = tmp_path / "item4_order.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text(pymupdf.Point(40, 60), "FIRST BLOCK CONTENT HERE", fontsize=10.0)
    page.insert_text(pymupdf.Point(40, 74), "SECOND OVERFLOW BLOCK CONTENT", fontsize=10.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-rev-item4",
        original_filename="item4_order.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    def _make_pair() -> tuple[Block, Block]:
        # Mild overlap: A=[40, 48, 240, 78] (h=30, area=6000), B=[40, 72, 240, 88] (h=16, area=3200)
        # Overlap height=6, x=200 => area=1200 => ir(B)=0.375? Keep 0.05 < ir < 0.20 so Pre-check 0 does NOT fire:
        # A=[40, 45, 240, 75] (area=6000), B=[40, 72, 240, 102] (area=6000): overlap y=3.0 < 4.5 (Pre-check 0 y_ov>=4.5 skips),
        # area=600 => ir=0.10 > 0.05 (Phase 2 PROTECTED_REGION_OVERLAP_FALLBACK DOES fire!)
        blk_a = Block(
            id="doc-rev-item4:page-1:block-1",
            page_index=0,
            bbox=[40.0, 45.0, 240.0, 75.0],
            kind=BlockKind.PARAGRAPH,
            reading_order=1,
            source_text="FIRST BLOCK CONTENT HERE",
            translation_text="最初のブロックの翻訳文です。",
            status=BlockStatus.TRANSLATED,
            font_size_pt=10.0,
        )
        blk_b = Block(
            id="doc-rev-item4:page-1:block-2",
            page_index=0,
            bbox=[40.0, 72.0, 240.0, 102.0],
            kind=BlockKind.PARAGRAPH,
            reading_order=2,
            source_text="SECOND OVERFLOW BLOCK CONTENT",
            # Deliberately overflows blk_b bbox -> TYPESETTING_OVERFLOW_FALLBACK
            translation_text="とても長い日本語訳文を狭い枠へ詰め込もうとして最小フォントサイズでも必ず溢れるテスト文です。" * 4,
            status=BlockStatus.TRANSLATED,
            font_size_pt=10.0,
        )
        return blk_a, blk_b

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    a1, b1 = _make_pair()
    out_ab = tmp_path / "item4_ab.pdf"
    res_ab, _ = adapter.typeset_and_export(
        document=doc_model,
        blocks=[a1, b1],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=out_ab,
        dual_output_path=None,
        job_id="job-item4-ab",
    )

    a2, b2 = _make_pair()
    out_ba = tmp_path / "item4_ba.pdf"
    res_ba, _ = adapter.typeset_and_export(
        document=doc_model,
        blocks=[b2, a2],
        page_indices=[0],
        job_config=TranslationJobConfig(),
        mono_output_path=out_ba,
        dual_output_path=None,
        job_id="job-item4-ba",
    )

    by_id_ab = {b.id: (b.status, tuple(b.warnings)) for b in res_ab}
    by_id_ba = {b.id: (b.status, tuple(b.warnings)) for b in res_ba}
    assert by_id_ab == by_id_ba
    assert by_id_ab["doc-rev-item4:page-1:block-1"][0] == BlockStatus.FALLBACK_ORIGINAL
    assert "PROTECTED_REGION_OVERLAP_FALLBACK" in by_id_ab["doc-rev-item4:page-1:block-1"][1]
    assert by_id_ab["doc-rev-item4:page-1:block-2"][0] == BlockStatus.FALLBACK_ORIGINAL
    assert "TYPESETTING_OVERFLOW_FALLBACK" in by_id_ab["doc-rev-item4:page-1:block-2"][1]

    d1, d2 = pymupdf.open(str(out_ab)), pymupdf.open(str(out_ba))
    pix1 = d1[0].get_pixmap(dpi=100).tobytes("png")
    pix2 = d2[0].get_pixmap(dpi=100).tobytes("png")
    txt1 = d1[0].get_text("text")
    txt2 = d2[0].get_text("text")
    d1.close()
    d2.close()
    assert txt1 == txt2
    assert pix1 == pix2


def test_review_item5_overbar_whiteout_preserves_adjacent_protected_objects(tmp_path: Path) -> None:
    """PR review #5: Clearing an approved block's `orig_overbar_bbox` with white rect must
    respect `protected_boxes` (PROTECTED_SKIP / FALLBACK_ORIGINAL / manual ProtectionRegion)
    and never paint white over adjacent protected text or vector lines.
    """
    pdf_path = tmp_path / "item5_overbar_prot.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    # Translatable block bbox=[40, 60, 220, 100] with radical overbar at [120, 61.0, 160, 61.5]
    page.insert_text(pymupdf.Point(40, 74), "Formula A=√(a2+b2) ends.", fontsize=9.0)
    page.draw_line(pymupdf.Point(120, 61.2), pymupdf.Point(160, 61.2), color=(0, 0, 0), width=0.8)
    # Adjacent PROTECTED_SKIP block directly above overbar padding ([115, 48, 220, 60.4]):
    # Note expanded whiteout rect `oy0 - 1.0 = 60.0` reaches 0.4pt INTO protected bbox [48..60.4]!
    page.insert_text(pymupdf.Point(122, 59.0), "PROTECTED KEEP", fontsize=9.0, color=(0, 0, 0))
    page.draw_line(pymupdf.Point(120, 60.2), pymupdf.Point(160, 60.2), color=(0, 0, 0), width=1.0)
    doc.save(str(pdf_path))
    doc.close()

    doc_model = Document(
        id="doc-rev-item5",
        original_filename="item5_overbar_prot.pdf",
        safe_storage_path=str(pdf_path),
        sha256_hash=compute_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )

    blk_trans = Block(
        id="doc-rev-item5:page-1:block-1",
        page_index=0,
        bbox=[40.0, 60.6, 220.0, 100.0],
        kind=BlockKind.PARAGRAPH,
        reading_order=1,
        source_text="Formula 𝐴=√(𝑎2+𝑏2) ends.",
        inline_math_spans=[
            InlineMathSpan(
                context_text="√(𝑎2+𝑏2)",
                target_text="(𝑎2+𝑏2)",
                role="radicand",
                scale=1.0,
                dy_em=0.0,
                overbar=True,
                orig_overbar_bbox=[120.0, 61.0, 160.0, 61.5],
            )
        ],
        translation_text="式 𝐴=√(𝑎2+𝑏2) である。",
        status=BlockStatus.TRANSLATED,
        font_size_pt=9.0,
    )
    blk_prot = Block(
        id="doc-rev-item5:page-1:block-2",
        page_index=0,
        bbox=[115.0, 48.0, 220.0, 60.4],
        kind=BlockKind.ISOLATED_FORMULA,
        reading_order=2,
        source_text="PROTECTED KEEP",
        status=BlockStatus.PROTECTED_SKIP,
        font_size_pt=9.0,
    )
    manual_reg = ProtectionRegion(
        id="reg-item5",
        document_id=doc_model.id,
        page_index=0,
        bbox=[115.0, 48.0, 220.0, 60.4],
        mode=ProtectionMode.PROTECT_AS_FORMULA,
    )

    mono_out = tmp_path / "item5_mono.pdf"
    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    updated, issues = adapter.typeset_and_export(
        document=doc_model,
        blocks=[blk_trans, blk_prot],
        page_indices=[0],
        manual_regions=[manual_reg],
        job_config=TranslationJobConfig(),
        mono_output_path=mono_out,
        dual_output_path=None,
        job_id="job-rev-item5",
    )
    assert not issues
    assert updated[0].status == BlockStatus.TRANSLATED

    out_pdf = pymupdf.open(str(mono_out))
    # Verify protected line at y=60.2 (inside [60.15..60.35] inside protected bbox) is NOT painted white
    prot_pix = out_pdf[0].get_pixmap(clip=pymupdf.Rect(125, 60.15, 155, 60.35), dpi=288)
    samples = prot_pix.samples
    n_comp = prot_pix.n
    has_dark_pixel = any(
        samples[i] < 200 or samples[i + 1] < 200 or samples[i + 2] < 200
        for i in range(0, len(samples), n_comp)
    )
    prot_txt = out_pdf[0].get_text("text", clip=pymupdf.Rect(115.0, 48.0, 220.0, 60.5))
    out_pdf.close()

    assert "PROTECTED KEEP" in prot_txt
    assert has_dark_pixel, "Overbar whiteout painted white over protected graphic inside protected bbox"
