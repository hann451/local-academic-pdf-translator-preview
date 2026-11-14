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
    TranslationJobConfig,
)
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    _fix_embedded_tounicode_smp,
    extract_inline_math_spans,
    mask_inline_protected_elements,
    unmask_inline_protected_elements,
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

    _fix_embedded_tounicode_smp(doc)
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

    # Exact legacy saved state from job-452527d817a7 block-16 (4 protected items, empty inline_math_spans)
    legacy_blk = Block(
        id="doc-de2629afd986:page-3:block-16",
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
        job_id="job-452527d817a7",
    )

    assert not issues
    assert updated_blocks[0].id == "doc-de2629afd986:page-3:block-16"
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




