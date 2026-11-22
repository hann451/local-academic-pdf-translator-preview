"""Unit tests for mechanical layout and math regression metrics (4 metrics)."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    LayoutRegressionReport,
    ProtectedItem,
    ProtectedItemKind,
    TranslationJobConfig,
)
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.pdf.validation.layout_regression_metrics import (
    check_column_order_monotonicity,
    check_formula_block_count_consistency,
    check_numeric_and_unit_preservation,
    check_protected_block_alignment,
    evaluate_document_layout_regression,
    extract_numeric_unit_tokens,
)
from backend.pdf.validation.preflight import inspect_pdf_preflight
from scripts.check_layout_regression import main as cli_main

pytestmark = pytest.mark.unit


def _make_block(
    block_id: str,
    *,
    bbox: list[float],
    source_text: str = "Sample prose block text.",
    kind: BlockKind = BlockKind.PARAGRAPH,
    status: BlockStatus = BlockStatus.PENDING,
    reading_order: int = 0,
    column_index: int = 0,
    semantic_category: str = "body",
    font_size_pt: float = 10.0,
    masked_source_text: str | None = None,
    protected_items: list[ProtectedItem] | None = None,
    translation_text: str | None = None,
    split_role: str | None = None,
) -> Block:
    return Block(
        id=block_id,
        page_index=0,
        bbox=bbox,
        kind=kind,
        reading_order=reading_order,
        column_index=column_index,
        source_text=source_text,
        masked_source_text=masked_source_text,
        protected_items=protected_items or [],
        translation_text=translation_text,
        status=status,
        semantic_category=semantic_category,
        font_size_pt=font_size_pt,
        split_role=split_role,  # type: ignore[arg-type]
    )


def _create_single_col_fraction_and_heading_pdf(pdf_path: Path) -> None:
    """Create a synthetic 1-col page with title, section heading, body lines, fraction formula, and figure."""
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    _, h = letter

    # Large Title (must NOT be counted as formula)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, h - 72, "ACOUSTIC AND RF SIGNAL ANALYSIS")

    # Section heading (must NOT be counted as formula)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, h - 110, "1. Introduction and Overview")

    # Body paragraph lines (normal line height)
    c.setFont("Times-Roman", 10.5)
    body_lines = [
        "In this work, we evaluate 1,000 trials over 2.4 GHz and 5 GHz bands.",
        "Each sensor spans 1.6 m with a 0.17-m-high styrene foam spacer.",
        "Detection accuracy reached 24.5% across all measured environments.",
        "Further analysis confirms consistent signal propagation in indoor rooms.",
    ]
    y = h - 140
    for ln in body_lines:
        c.drawString(72, y, ln)
        y -= 15

    # Tall stacked fraction equation with vector bar (MUST be counted as 1 formula cluster)
    c.setFont("Times-Roman", 10.5)
    c.drawString(120, h - 245, "Ratio =")
    c.drawString(175, h - 237, "Detected Events")
    c.setLineWidth(0.9)
    c.line(170, h - 243, 270, h - 243)
    c.drawString(178, h - 256, "Actual Events")
    c.drawString(290, h - 245, "(1)")

    # Lower body paragraph
    c.drawString(72, h - 295, "The ratio above remains bounded between 0.5 and 1.0.")

    # Vector figure box + label (must NOT be counted as formula)
    c.rect(120, h - 410, 260, 70, stroke=1, fill=0)
    c.line(120, h - 410, 380, h - 340)
    c.drawString(190, h - 375, "Figure Diagram Region")
    c.save()


def _create_heading_and_figure_only_pdf(pdf_path: Path) -> None:
    """Create a page containing large uppercase title, body, large section heading (y > 28%), and a figure."""
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    _, h = letter
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, h - 75, "LARGE UPPERCASE PAPER TITLE")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, h - 105, "Abstract")

    c.setFont("Times-Roman", 10.5)
    for idx in range(5):
        c.drawString(
            72,
            h - 135 - idx * 15,
            f"Normal prose line number {idx + 1} describing experimental measurements clearly.",
        )

    # Place large section heading below 28% page height so it classifies as BlockKind.HEADING
    c.setFont("Helvetica-Bold", 22)
    c.drawString(72, h - 255, "2. METHODOLOGY AND SETUP")

    c.setStrokeColorRGB(0.2, 0.2, 0.7)
    c.rect(100, h - 380, 300, 80, stroke=1, fill=0)
    c.line(100, h - 340, 400, h - 340)
    c.drawString(160, h - 335, "System Block Diagram")
    c.save()


def test_metric1_formula_count_matches_and_detects_omission(tmp_path: Path) -> None:
    pdf_path = tmp_path / "metric1_fraction.pdf"
    _create_single_col_fraction_and_heading_pdf(pdf_path)

    doc_model = inspect_pdf_preflight(pdf_path, pdf_path.name, "doc-m1")
    adapter = NativePreservingAdapter()
    blocks = adapter.parse_and_extract_blocks(doc_model, [0])

    pdf = pymupdf.open(str(pdf_path))
    try:
        res_ok = check_formula_block_count_consistency(pdf[0], blocks)
        assert res_ok.image_formula_cluster_count == 1
        assert res_ok.extracted_formula_block_count == 1
        assert res_ok.count_diff == 0
        assert res_ok.needs_visual_review is False

        # Deliberately omit extracted formula block -> regression must be flagged
        blocks_omitted = [
            b
            for b in blocks
            if b.kind != BlockKind.ISOLATED_FORMULA and b.split_role != "formula"
        ]
        res_bad = check_formula_block_count_consistency(pdf[0], blocks_omitted)
        assert res_bad.image_formula_cluster_count == 1
        assert res_bad.extracted_formula_block_count == 0
        assert res_bad.count_diff == 1
        assert res_bad.needs_visual_review is True
    finally:
        pdf.close()


def test_metric1_ignores_large_headings_and_figures(tmp_path: Path) -> None:
    pdf_path = tmp_path / "metric1_heading_fig.pdf"
    _create_heading_and_figure_only_pdf(pdf_path)

    doc_model = inspect_pdf_preflight(pdf_path, pdf_path.name, "doc-m1-hf")
    blocks = NativePreservingAdapter().parse_and_extract_blocks(doc_model, [0])

    pdf = pymupdf.open(str(pdf_path))
    try:
        res = check_formula_block_count_consistency(pdf[0], blocks)
        assert res.image_formula_cluster_count == 0
        assert res.extracted_formula_block_count == 0
        assert res.count_diff == 0
        assert res.needs_visual_review is False
        assert res.excluded_heading_clusters >= 1
    finally:
        pdf.close()


def test_metric2_numeric_and_unit_preservation_exact_and_multiplicity() -> None:
    raw = (
        "We observed 1,000 samples (total 12,345.67 units) with accuracy 24.5%, "
        "width 1.6m, height 0.17-m-high, and frequency 2.4 GHz (delay 10 \xa0ms)."
    )
    tokens = extract_numeric_unit_tokens(raw)
    assert tokens == [
        "1000",
        "12345.67",
        "24.5%",
        "1.6m",
        "0.17m",
        "2.4GHz",
        "10ms",
    ]

    # Exact preservation across Japanese translation (including NBSP & fullwidth percent)
    ja_out = (
        "合計12,345.67単位のうち1,000サンプルを観測し、精度は24.5％、"
        "幅1.6\xa0m、高さ0.17 m、周波数2.4\xa0GHz（遅延10 ms）であった。"
    )
    ok = check_numeric_and_unit_preservation(raw, raw, output_text=ja_out)
    assert ok.preservation_ratio == 1.0
    assert ok.missing_tokens == []
    assert ok.extra_tokens == []
    assert ok.needs_visual_review is False

    # Multiplicity regression: "2 m" appears twice in source, only once in processed output
    dup_src = "Plates measure 2 m and 2 m with 0.5 m spacing."
    dup_out = "板の寸法は2mで、間隔は0.5mです。"
    dup_res = check_numeric_and_unit_preservation(dup_src, dup_src, output_text=dup_out)
    assert dup_res.source_token_count == 3
    assert dup_res.matched_token_count == 2
    assert dup_res.preservation_ratio < 1.0
    assert dup_res.missing_tokens == ["2m"]
    assert dup_res.needs_visual_review is True

    # False-positive guards for identifiers, citations, prepositions, and articles
    fp_text = "Section 2.4.1 uses t10, 3D, 1st, 0x1f, {v1}, [1, 2], 5 in length, and 1 A novel step."
    assert extract_numeric_unit_tokens(fp_text) == ["5", "1"]


def test_metric2_detects_masking_swallow_of_body_numbers() -> None:
    body_ok = _make_block(
        "doc:page-1:block-1",
        bbox=[50.0, 100.0, 280.0, 140.0],
        source_text="The plate has height 0.17 m and bandwidth 20 MHz.",
        masked_source_text="The plate has height 0.17 m and bandwidth 20 MHz.",
        status=BlockStatus.TRANSLATED,
        translation_text="プレートの高さは0.17 m、帯域幅は20 MHzです。",
    )
    res_ok = check_numeric_and_unit_preservation(body_ok.source_text, [body_ok])
    assert res_ok.body_blocks_masking.preservation_ratio == 1.0
    assert res_ok.needs_visual_review is False

    # Simulate inline math masking accidentally swallowing "0.17 m" inside a placeholder without restoring it
    body_swallowed = _make_block(
        "doc:page-1:block-1",
        bbox=[50.0, 100.0, 280.0, 140.0],
        source_text="The plate has height 0.17 m and bandwidth 20 MHz.",
        masked_source_text="The plate has height {v1} and bandwidth 20 MHz.",
        protected_items=[
            ProtectedItem(
                placeholder="{v1}",
                kind=ProtectedItemKind.INLINE_FORMULA,
                original_text="m",  # Bug: "0.17" got dropped when constructing placeholder!
            )
        ],
        status=BlockStatus.PENDING,
    )
    res_swallow = check_numeric_and_unit_preservation(body_swallowed.source_text, [body_swallowed])
    assert res_swallow.body_blocks_masking.preservation_ratio < 1.0
    assert "0.17m" in res_swallow.body_blocks_masking.missing_tokens
    assert res_swallow.needs_visual_review is True


def test_metric3_column_monotonicity_valid_mixed_and_single_column() -> None:
    page_w = 595.0
    # Valid 2-column page with full-width title, abstract, and left-aligned short heading in right col
    valid_two_col = [
        _make_block("b-title", bbox=[60.0, 40.0, 535.0, 70.0], kind=BlockKind.TITLE, semantic_category="title", reading_order=0),
        _make_block("b-abs", bbox=[50.0, 80.0, 545.0, 140.0], kind=BlockKind.PARAGRAPH, semantic_category="abstract", reading_order=1),
        _make_block("b-l1", bbox=[50.0, 160.0, 285.0, 240.0], reading_order=2),
        _make_block("b-l2", bbox=[50.0, 250.0, 285.0, 360.0], reading_order=3),
        _make_block("b-r1-head", bbox=[312.0, 160.0, 410.0, 172.0], kind=BlockKind.HEADING, semantic_category="section_heading", reading_order=4),
        _make_block("b-r2", bbox=[312.0, 180.0, 545.0, 320.0], reading_order=5),
    ]
    m_ok = check_column_order_monotonicity(valid_two_col, page_w)
    assert m_ok.detected_column_count == 2
    assert m_ok.is_monotonic is True
    assert m_ok.inversion_count == 0
    assert m_ok.column_sequence == [0, 0, 1, 1]

    # Interleaved reading order: 0 -> 1 -> 0 -> 1
    interleaved = [
        _make_block("b-l1", bbox=[50.0, 160.0, 285.0, 240.0], reading_order=0),
        _make_block("b-r1", bbox=[312.0, 160.0, 545.0, 240.0], reading_order=1),
        _make_block("b-l2", bbox=[50.0, 250.0, 285.0, 360.0], reading_order=2),
        _make_block("b-r2", bbox=[312.0, 250.0, 545.0, 360.0], reading_order=3),
    ]
    m_bad = check_column_order_monotonicity(interleaved, page_w)
    assert m_bad.detected_column_count == 2
    assert m_bad.is_monotonic is False
    assert m_bad.inversion_count == 1
    assert m_bad.inversions[0].prev_block_id == "b-r1"
    assert m_bad.inversions[0].curr_block_id == "b-l2"
    assert m_bad.inversions[0].prev_col == 1
    assert m_bad.inversions[0].curr_col == 0

    # Single-column page with left-aligned heading, full-width body, and centered formula must NOT misdetect 2 cols
    single_col = [
        _make_block("s-head", bbox=[72.0, 90.0, 210.0, 105.0], kind=BlockKind.HEADING, semantic_category="section_heading", reading_order=0),
        _make_block("s-p1", bbox=[72.0, 115.0, 540.0, 200.0], reading_order=1),
        _make_block("s-eq", bbox=[180.0, 215.0, 410.0, 240.0], kind=BlockKind.ISOLATED_FORMULA, status=BlockStatus.PROTECTED_SKIP, semantic_category="formula", reading_order=2),
        _make_block("s-p2", bbox=[72.0, 255.0, 540.0, 340.0], reading_order=3),
    ]
    m_single = check_column_order_monotonicity(single_col, page_w)
    assert m_single.detected_column_count == 1
    assert m_single.is_monotonic is True
    assert m_single.inversion_count == 0


def test_metric4_protected_alignment_detects_shift_overlap_inversion_and_gap() -> None:
    above = _make_block(
        "b-above",
        bbox=[54.0, 100.0, 284.0, 140.0],
        status=BlockStatus.TRANSLATED,
        translation_text="上の本文",
    )
    formula = _make_block(
        "b-formula",
        bbox=[80.0, 150.0, 260.0, 180.0],
        kind=BlockKind.ISOLATED_FORMULA,
        status=BlockStatus.PROTECTED_SKIP,
        semantic_category="formula",
        source_text="E = mc^2 (1)",
    )
    below = _make_block(
        "b-below",
        bbox=[54.0, 190.0, 284.0, 230.0],
        status=BlockStatus.TRANSLATED,
        translation_text="下の本文",
    )
    page_blocks = [above, formula, below]

    # 1. Clean alignment
    ok_res = check_protected_block_alignment(
        page_blocks,
        rendered_bboxes={
            "b-above": [54.0, 100.0, 284.0, 138.0],
            "b-formula": [80.0, 150.0, 260.0, 180.0],
            "b-below": [54.0, 190.0, 284.0, 226.0],
        },
    )
    assert ok_res.checked_protected_blocks == 1
    assert ok_res.all_aligned is True

    # 2. Protected block shifted vertically
    shift_res = check_protected_block_alignment(
        page_blocks,
        rendered_bboxes={
            "b-above": [54.0, 100.0, 284.0, 138.0],
            "b-formula": [80.0, 165.0, 260.0, 195.0],
            "b-below": [54.0, 200.0, 284.0, 236.0],
        },
    )
    assert shift_res.all_aligned is False
    assert len(shift_res.misaligned_blocks) == 1
    assert shift_res.misaligned_blocks[0].block_id == "b-formula"
    assert shift_res.misaligned_blocks[0].dy == pytest.approx(15.0)

    # 3. Adjacent translated body overlaps protected block
    overlap_res = check_protected_block_alignment(
        page_blocks,
        rendered_bboxes={
            "b-above": [54.0, 100.0, 284.0, 162.0],
            "b-formula": [80.0, 150.0, 260.0, 180.0],
            "b-below": [54.0, 170.0, 284.0, 230.0],
        },
    )
    assert overlap_res.all_aligned is False
    assert len(overlap_res.overlapping_pairs) == 2
    assert {p.relation for p in overlap_res.overlapping_pairs} == {"above", "below"}

    # 4. Order inversion and abnormal vertical gap
    inv_gap_res = check_protected_block_alignment(
        page_blocks,
        rendered_bboxes={
            "b-above": [54.0, 200.0, 284.0, 230.0],  # Inverted below formula!
            "b-formula": [80.0, 150.0, 260.0, 180.0],
            "b-below": [54.0, 245.0, 284.0, 285.0],  # Gap increased from 10pt to 65pt!
        },
    )
    assert inv_gap_res.all_aligned is False
    assert len(inv_gap_res.order_inversions) == 1
    assert inv_gap_res.order_inversions[0].expected_relation == "above"
    assert len(inv_gap_res.abnormal_gaps) == 1
    assert inv_gap_res.abnormal_gaps[0].relation == "below"


def test_metric4_with_real_pdf_mono_and_dual_pages(tmp_path: Path) -> None:
    """Verify Metric 4 reads both mono and right-half dual PDF pages accurately."""
    orig_pdf = tmp_path / "orig.pdf"
    mono_pdf = tmp_path / "mono.pdf"
    dual_pdf = tmp_path / "dual.pdf"

    def _draw_page(target: Path, *, formula_dy: float = 0.0, overlap_below: bool = False) -> None:
        doc = pymupdf.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((54, 112), "Above body prose paragraph line.", fontsize=10, fontname="helv")
        p.insert_text((90, 155 + formula_dy), "E_total = x_1 + x_2  (1)", fontsize=11, fontname="tiit")
        below_y = 172 if overlap_below else 200
        p.insert_text((54, below_y), "Below body prose paragraph line.", fontsize=10, fontname="helv")
        doc.save(str(target))
        doc.close()

    _draw_page(orig_pdf, formula_dy=0.0, overlap_below=False)
    _draw_page(mono_pdf, formula_dy=0.0, overlap_below=False)

    # Create side-by-side dual page with shifted formula on right half
    od = pymupdf.open(str(orig_pdf))
    dd = pymupdf.open()
    dp = dd.new_page(width=595 * 2, height=842)
    dp.show_pdf_page(pymupdf.Rect(0, 0, 595, 842), od, 0)
    shifted_mono = tmp_path / "shifted_mono.pdf"
    _draw_page(shifted_mono, formula_dy=18.0, overlap_below=True)
    sm = pymupdf.open(str(shifted_mono))
    dp.show_pdf_page(pymupdf.Rect(595, 0, 595 * 2, 842), sm, 0)
    dd.save(str(dual_pdf))
    od.close()
    sm.close()
    dd.close()

    blocks = [
        _make_block(
            "doc:page-1:block-1",
            bbox=[54.0, 100.0, 260.0, 118.0],
            status=BlockStatus.TRANSLATED,
            translation_text="Above body prose paragraph line.",
        ),
        _make_block(
            "doc:page-1:block-2",
            bbox=[90.0, 142.0, 250.0, 162.0],
            kind=BlockKind.ISOLATED_FORMULA,
            status=BlockStatus.PROTECTED_SKIP,
            semantic_category="formula",
            source_text="E_total = x_1 + x_2  (1)",
        ),
        _make_block(
            "doc:page-1:block-3",
            bbox=[54.0, 188.0, 260.0, 206.0],
            status=BlockStatus.TRANSLATED,
            translation_text="Below body prose paragraph line.",
        ),
    ]

    od = pymupdf.open(str(orig_pdf))
    md = pymupdf.open(str(mono_pdf))
    dd = pymupdf.open(str(dual_pdf))
    try:
        res_mono_ok = check_protected_block_alignment(
            blocks, orig_page=od[0], mono_page=md[0]
        )
        assert res_mono_ok.all_aligned is True
        assert res_mono_ok.checked_targets == ["mono"]

        res_dual_bad = check_protected_block_alignment(
            blocks, orig_page=od[0], mono_page=md[0], dual_page=dd[0]
        )
        assert res_dual_bad.all_aligned is False
        assert any(m.target == "dual" for m in res_dual_bad.misaligned_blocks)
        assert any(o.target == "dual" for o in res_dual_bad.overlapping_pairs)
    finally:
        od.close()
        md.close()
        dd.close()


def test_cli_check_layout_regression_smoke(tmp_path: Path, generated_pdfs: dict[str, Path]) -> None:
    json_out = tmp_path / "out_metrics.json"
    rc = cli_main(
        [
            "--pdf",
            str(generated_pdfs["english_fraction_equation"]),
            "--json-out",
            str(json_out),
        ]
    )
    assert rc == 0
    assert json_out.exists()
    parsed = LayoutRegressionReport.model_validate(
        json.loads(json_out.read_text(encoding="utf-8"))
    )
    assert parsed.total_pages_evaluated == 1
    assert parsed.pages[0].formula_block_count.image_formula_cluster_count == 1
    assert parsed.pages[0].formula_block_count.extracted_formula_block_count == 1
    assert parsed.pages[0].formula_block_count.count_diff == 0
