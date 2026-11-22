"""Unit & end-to-end verification tests for display-equation recognition,
separation from adjacent prose, and multi-part formula fragment grouping.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    Document,
    JobState,
    TranslationJobConfig,
)
from backend.jobs.manager import JobManager
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.pdf.validation.layout_regression_metrics import evaluate_document_layout_regression
from backend.pdf.validation.preflight import inspect_pdf_preflight

pytestmark = pytest.mark.unit


def _make_doc(pdf_path: Path, doc_id: str = "doc-disp-math") -> Document:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    m = pymupdf.open(str(pdf_path))
    pages = len(m)
    m.close()
    return Document(
        id=doc_id,
        original_filename=pdf_path.name,
        safe_storage_path=str(pdf_path),
        sha256_hash=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=pages,
    )


def _raw_span(
    text: str,
    bbox: list[float],
    *,
    font: str = "CMR10",
    size: float = 10.0,
    flags: int = 0,
) -> dict:
    return {
        "text": text,
        "bbox": list(bbox),
        "font": font,
        "size": size,
        "flags": flags,
        "origin": [bbox[0], bbox[3] - 2.0],
    }


def _raw_line(spans: list[dict], bbox: list[float] | None = None) -> dict:
    if bbox is None:
        bbox = [
            min(s["bbox"][0] for s in spans),
            min(s["bbox"][1] for s in spans),
            max(s["bbox"][2] for s in spans),
            max(s["bbox"][3] for s in spans),
        ]
    return {"bbox": list(bbox), "spans": spans}


def _raw_block(lines: list[dict], bbox: list[float] | None = None) -> dict:
    if bbox is None:
        bbox = [
            min(ln["bbox"][0] for ln in lines),
            min(ln["bbox"][1] for ln in lines),
            max(ln["bbox"][2] for ln in lines),
            max(ln["bbox"][3] for ln in lines),
        ]
    return {"type": 0, "bbox": list(bbox), "lines": lines}


def test_classify_display_equation_positive_and_negative_matrix() -> None:
    adapter = NativePreservingAdapter()
    page_w, page_h = 595.3, 841.9

    positive_cases = [
        ("Rising width = mean(at0,t0+9) − mean(at0−10,t0−1) (4)", True, True),
        ("Falling width = mean(at0−10,t0−1) − mean(at0,t0+9) (6)", True, True),
        ("Threshold = max(at1.5s,8.5s) − min(at1.5s,8.5s) × 2 (5)", True, True),
        ("Signal to Noise Ratio = Ps / Pn (2)", False, True),
        ("hm,n,k,t = |hm,n,k,t| ej hm,n,k,t (1)", True, True),
        ("ˆHm,n,k,t = |hm,n,k,t|/∥ht∥ (2)", True, True),
    ]
    for text, has_math_font, has_sub_super in positive_cases:
        kind, sem_cat, _, rule_id, _, _, _ = adapter._classify_academic_block(
            text=text,
            line_count=1,
            p_idx=2,
            bbox=[316.0, 316.0, 544.0, 338.0],
            page_w=page_w,
            page_h=page_h,
            avg_font_size=10.0,
            body_font_size=10.0,
            frontmatter_end_y=0.0,
            has_math_font=has_math_font,
            has_sub_super=has_sub_super,
            drawing_boxes=[],
            in_references_section=False,
        )
        assert kind == BlockKind.ISOLATED_FORMULA, f"Expected ISOLATED_FORMULA for {text!r}"
        assert sem_cat == "formula"
        assert rule_id == "RULE_ISOLATED_FORMULA"

    negative_prose_or_tag_cases = [
        ("The estimator E_n = sum_i x_i / n remains stable when", False, True),
        (
            "In this paper, we propose the energy function E(x_i) = α_i + β_i for all samples [1, 2].",
            False,
            True,
        ),
        ("Note that learning rate η = 0.01 is used in training.", False, False),
        ("(L-Tx - Re-Rx)", False, False),
        ("(Re-Tx - L-Rx)", False, False),
        ("(non-zero)", False, False),
        (
            "Falling width = mean(at0−10,t0−1) − mean(at0,t0+9) (6) "
            "A point is identified as the falling point if the falling width exceeds the threshold defined by Eq. (5).",
            True,
            True,
        ),
    ]
    for text, has_math_font, has_sub_super in negative_prose_or_tag_cases:
        kind, _, _, _, _, _, _ = adapter._classify_academic_block(
            text=text,
            line_count=2,
            p_idx=1,
            bbox=[311.5, 444.0, 544.0, 468.0],
            page_w=page_w,
            page_h=page_h,
            avg_font_size=10.0,
            body_font_size=10.0,
            frontmatter_end_y=0.0,
            has_math_font=has_math_font,
            has_sub_super=has_sub_super,
            drawing_boxes=[],
            in_references_section=False,
        )
        assert kind != BlockKind.ISOLATED_FORMULA, f"Unexpected ISOLATED_FORMULA for {text!r}"

    # Control-glyph sub-figure label adjacent to a vector drawing box classifies as FIGURE
    fig_kind, fig_cat, _, fig_rule, _, _, _ = adapter._classify_academic_block(
        text="(L-Tx \x1f Re-Rx)",
        line_count=1,
        p_idx=1,
        bbox=[399.7, 189.5, 462.4, 200.9],
        page_w=page_w,
        page_h=page_h,
        avg_font_size=9.2,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        has_math_font=False,
        has_sub_super=False,
        drawing_boxes=[[367.1, 206.7, 496.8, 292.2]],
        in_references_section=False,
    )
    assert fig_kind == BlockKind.FIGURE
    assert fig_cat == "figure"
    assert fig_rule == "RULE_FIGURE_INTERNAL_TEXT"


def test_dropped_flush_right_eq_num_detachment_and_case_a_regression() -> None:
    adapter = NativePreservingAdapter()
    page_w = 595.3

    # Reproduce exact PyMuPDF Eq.(6) layout: display eq block followed by dropped `(6)` on prose L0
    eq6_blk = _raw_block(
        [
            _raw_line(
                [
                    _raw_span("Falling width = mean(", [316.0, 623.1, 414.1, 635.0], font="CMR10"),
                    _raw_span("a", [414.1, 623.1, 419.4, 635.0], font="CMMI10"),
                    _raw_span("t0−10,t0−1", [419.4, 628.0, 459.9, 640.4], font="CMSY7", size=7.0),
                    _raw_span(") ", [460.4, 623.1, 464.3, 635.0], font="CMR10"),
                    _raw_span("−", [464.3, 623.1, 474.2, 635.0], font="CMSY10"),
                    _raw_span(" mean(", [476.5, 623.1, 503.6, 635.0], font="CMR10"),
                    _raw_span("a", [503.6, 623.1, 508.8, 635.0], font="CMMI10"),
                    _raw_span("t0,t0+9", [508.9, 628.0, 535.1, 640.4], font="CMR7", size=7.0),
                    _raw_span(")", [535.6, 623.1, 539.5, 635.0], font="CMR10"),
                ],
                bbox=[316.0, 623.1, 539.5, 640.4],
            )
        ],
        bbox=[316.0, 623.1, 539.5, 640.4],
    )
    prose_with_dropped_6 = _raw_block(
        [
            _raw_line([_raw_span("(6)", [531.2, 635.3, 544.0, 645.3], font="CMR10")]),
            _raw_line(
                [
                    _raw_span(
                        "A point is identified as the falling point if the falling",
                        [325.7, 647.3, 544.0, 657.3],
                        font="CMR10",
                    )
                ]
            ),
            _raw_line(
                [
                    _raw_span(
                        "width exceeds the threshold defined by Eq. (5).",
                        [311.5, 659.3, 515.0, 669.2],
                        font="CMR10",
                    )
                ]
            ),
        ],
        bbox=[311.5, 635.3, 544.0, 669.2],
    )

    processed = adapter._preprocess_page_raw_blocks(
        [eq6_blk, prose_with_dropped_6], p_idx=2, page_w=page_w
    )
    assert len(processed) == 2

    eq_txt, _, eq_has_mf, eq_has_sub = adapter._extract_lines_and_style(processed[0]["lines"])
    prose_txt, _, _, _ = adapter._extract_lines_and_style(processed[1]["lines"])
    eq_joined = " ".join(eq_txt)
    prose_joined = " ".join(prose_txt)

    assert "Falling width" in eq_joined
    assert "(6)" in eq_joined
    assert "identified" not in eq_joined
    assert "(6)" not in prose_joined
    assert prose_joined.startswith("A point is identified")
    # Zero vertical overlap between separated equation and prose boxes
    assert processed[0]["bbox"][3] < processed[1]["bbox"][1]

    eq_kind, *_ = adapter._classify_academic_block(
        text=eq_joined,
        line_count=len(eq_txt),
        p_idx=2,
        bbox=processed[0]["bbox"],
        page_w=page_w,
        page_h=841.9,
        avg_font_size=10.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        has_math_font=eq_has_mf,
        has_sub_super=eq_has_sub,
        drawing_boxes=[],
        in_references_section=False,
    )
    assert eq_kind == BlockKind.ISOLATED_FORMULA

    # Regression: legitimate inline-prime split prose must STILL coalesce (Case A)
    prime_prev = _raw_block(
        [
            _raw_line(
                [
                    _raw_span(
                        "Where N denotes the spectral density of component N",
                        [51.0, 120.0, 280.0, 132.0],
                        font="Times-Roman",
                    )
                ]
            )
        ]
    )
    prime_cur = _raw_block(
        [
            _raw_line(
                [
                    _raw_span(
                        "′′(f) measured over the band.",
                        [51.0, 125.0, 195.0, 137.0],
                        font="Times-Roman",
                    )
                ]
            )
        ]
    )
    coalesced_prime = adapter._preprocess_page_raw_blocks(
        [prime_prev, prime_cur], p_idx=1, page_w=page_w
    )
    assert len(coalesced_prime) == 1
    p_lines, _, _, _ = adapter._extract_lines_and_style(coalesced_prime[0]["lines"])
    assert "N′′(f)" in " ".join(p_lines)


def test_fragmented_multiline_display_equation_grouping_and_distinct_eq_guard() -> None:
    adapter = NativePreservingAdapter()
    page_w, page_h = 595.3, 841.9

    # Multi-part Eq.(3) blocks including pure CMEX10 control-char delimiters + standalone (3)
    eq3_parts = [
        _raw_block(
            [
                _raw_line([_raw_span("\x1f", [70.2, 617.6, 84.6, 654.8], font="CMEX10")]),
                _raw_line(
                    [
                        _raw_span("TH", [69.2, 620.4, 84.7, 631.8], font="CMMI10"),
                        _raw_span("m,n,k,t", [85.5, 623.0, 112.0, 630.5], font="CMMI7", size=7.0),
                        _raw_span(" =", [112.0, 620.4, 123.0, 631.8], font="CMR10"),
                    ]
                ),
            ],
            bbox=[69.2, 617.6, 123.0, 654.8],
        ),
        _raw_block(
            [_raw_line([_raw_span("\x1e", [125.8, 606.1, 131.1, 643.3], font="CMEX10")])],
            bbox=[125.8, 606.1, 131.1, 643.3],
        ),
        _raw_block(
            [
                _raw_line(
                    [_raw_span("ˆHm,n,k,t−tw", [131.1, 617.9, 182.1, 639.3], font="CMMI10")]
                )
            ],
            bbox=[131.1, 617.9, 182.1, 639.3],
        ),
        _raw_block(
            [
                _raw_line([_raw_span("2", [175.8, 630.8, 179.7, 637.8], font="CMR7", size=7.0)]),
                _raw_line(
                    [_raw_span(", . . . , ˆHm,n,k,t,", [183.9, 617.9, 247.4, 631.1], font="CMMI10")]
                ),
            ],
            bbox=[175.8, 617.9, 247.4, 637.8],
        ),
        _raw_block(
            [
                _raw_line(
                    [_raw_span(". . . , ˆHm,n,k,t+tw", [164.4, 645.8, 234.6, 662.3], font="CMMI10")]
                )
            ],
            bbox=[164.4, 645.8, 234.6, 662.3],
        ),
        _raw_block(
            [_raw_line([_raw_span("2 −1", [228.3, 655.1, 246.2, 670.6], font="CMSY7", size=7.0)])],
            bbox=[228.3, 655.1, 246.2, 670.6],
        ),
        _raw_block(
            [
                _raw_line([_raw_span("\x1d", [246.7, 634.0, 252.0, 671.2], font="CMEX10")]),
                _raw_line([_raw_span("(3)", [270.4, 634.3, 283.2, 644.3], font="CMR10")]),
            ],
            bbox=[246.7, 634.0, 283.2, 671.2],
        ),
    ]
    prose_after = _raw_block(
        [
            _raw_line(
                [
                    _raw_span(
                        "In this equation, the median of the range represents the center.",
                        [50.7, 675.1, 283.2, 685.1],
                        font="CMR10",
                    )
                ]
            )
        ]
    )

    # Distinct numbered equations separated by small vertical gap must NEVER merge together
    distinct_eq_1 = _raw_block(
        [
            _raw_line([_raw_span("x1 = a + b (1)", [80.0, 200.0, 250.0, 212.0], font="CMMI10")]),
        ]
    )
    distinct_eq_2 = _raw_block(
        [
            _raw_line([_raw_span("x2 = c + d (2)", [80.0, 218.0, 250.0, 230.0], font="CMMI10")]),
        ]
    )

    grouped = adapter._group_fragmented_display_equations(
        [distinct_eq_1, distinct_eq_2] + eq3_parts + [prose_after],
        p_idx=2,
        page_w=page_w,
        page_h=page_h,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
    )
    # 2 distinct equations + 1 unified Eq.(3) + 1 prose = 4 raw blocks
    assert len(grouped) == 4
    assert grouped[2].get("_grouped_display_equation") is True
    assert grouped[2]["bbox"] == pytest.approx([69.2, 606.1, 283.2, 671.2])
    assert "(1)" in adapter._extract_lines_and_style(grouped[0]["lines"])[0][0]
    assert "(2)" in adapter._extract_lines_and_style(grouped[1]["lines"])[0][0]


def test_intra_block_math_font_splitter_and_plain_helv_coupling(tmp_path: Path) -> None:
    pdf_path = tmp_path / "splitter_mode.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=595, height=842)

    page.insert_text((312, 80), "1. Methodology", fontsize=12, fontname="hebo")

    # 1. Mixed raw block using math-keyword font ("symb" -> Symbol) -> auto-splits on parse
    eq_y = 200.0
    page.insert_text((326, eq_y), "E(t) = a + b + c (1)", fontsize=10, fontname="symb")
    page.insert_text(
        (312, eq_y + 12.0),
        "A point is identified if the value exceeds the threshold.",
        fontsize=10,
        fontname="helv",
    )

    # 2. Mixed raw block using plain "helv" (drag-split fixture convention) -> stays coupled
    h_y = 420.0
    page.insert_text((72, h_y), "Falling width = mean(a) - mean(b) (6)", fontsize=10, fontname="helv")
    page.insert_text(
        (72, h_y + 12.5),
        "A point is identified as the falling point if the falling width",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text(
        (72, h_y + 24.5),
        "exceeds the difference threshold defined by Eq. (5).",
        fontsize=10,
        fontname="helv",
    )
    doc_pdf.save(str(pdf_path))
    doc_pdf.close()

    # Sanity-check fixture geometry before parsing: math block + prose actually start in 1 MuPDF raw block
    check_doc = pymupdf.open(str(pdf_path))
    raw_texts = [
        " ".join("".join(s["text"] for s in ln["spans"]) for ln in rb.get("lines", []))
        for rb in check_doc[0].get_text("dict").get("blocks", [])
        if rb.get("type") == 0
    ]
    check_doc.close()
    assert any("E(t)" in t and "A point is identified" in t for t in raw_texts), raw_texts

    adapter = NativePreservingAdapter(config=AppConfig(runtime_dir=tmp_path / "rt"))
    blocks = adapter.parse_and_extract_blocks(_make_doc(pdf_path, "doc-split-mode"), [0])

    formula_blocks = [b for b in blocks if b.kind == BlockKind.ISOLATED_FORMULA]
    assert len(formula_blocks) == 1
    assert "(1)" in formula_blocks[0].source_text

    auto_split_prose = [
        b
        for b in blocks
        if b.kind == BlockKind.PARAGRAPH and "value exceeds the threshold" in b.source_text
    ]
    assert len(auto_split_prose) == 1
    assert auto_split_prose[0].status == BlockStatus.PENDING

    helv_coupled = [
        b
        for b in blocks
        if "Falling width" in b.source_text and "A point is identified" in b.source_text
    ]
    assert len(helv_coupled) == 1
    assert helv_coupled[0].kind == BlockKind.PARAGRAPH


def test_end_to_end_math_font_separation_and_four_metrics_pass(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    pdf_path = tmp_path / "e2e_math_4metrics.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=595, height=842)

    page.insert_text((54, 65), "1. Detection Formulation", fontsize=12, fontname="hebo")
    page.insert_text(
        (54, 100),
        "In this work, we test 1,000 trials over 2.4 GHz.",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text(
        (54, 114),
        "Each trial spans 1.6 m before threshold scoring.",
        fontsize=10,
        fontname="helv",
    )

    # Tall stacked fraction equation (height > 1.45 * median line height)
    page.insert_text((54, 162), "Detection Ratio =", fontsize=10, fontname="helv")
    page.insert_text((150, 152), "Detected Events", fontsize=9.5, fontname="helv")
    page.draw_line((145, 159), (235, 159), width=0.9)
    page.insert_text((152, 171), "Actual Events", fontsize=9.5, fontname="helv")
    page.insert_text((255, 162), "(1)", fontsize=10, fontname="helv")

    page.insert_text(
        (54, 210),
        "Further tests confirm stable results in all rooms.",
        fontsize=10,
        fontname="helv",
    )

    # Right-column mixed block with math-font ("symb") equation + dropped `(2)` + prose
    page.insert_text((326, 112), "E(t) = x(t) + y(t) + z(t)", fontsize=10, fontname="symb")
    page.insert_text((515, 124), "(2)", fontsize=10, fontname="helv")
    page.insert_text(
        (312, 138),
        "Here y denotes the measured CSI value in tests.",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text(
        (312, 152),
        "No scaling factor exceeds 0.5 during evaluation.",
        fontsize=10,
        fontname="helv",
    )
    doc_pdf.save(str(pdf_path))
    doc_pdf.close()

    mgr = JobManager(config=isolated_config)
    doc = mgr.register_document(pdf_path)
    job = mgr.create_job(
        doc.id,
        TranslationJobConfig(
            use_mock_translator=True,
            output_mono_pdf=True,
            output_dual_pdf=True,
        ),
    )
    finished = mgr.run_job(job.id)
    assert finished.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)

    blocks = mgr.db.list_blocks(doc.id)
    formulas = [b for b in blocks if b.kind == BlockKind.ISOLATED_FORMULA]
    assert len(formulas) == 2
    assert all(f.status == BlockStatus.PROTECTED_SKIP for f in formulas)

    right_prose = next(
        b for b in blocks if "measured CSI value" in b.source_text
    )
    assert right_prose.kind == BlockKind.PARAGRAPH
    assert right_prose.status == BlockStatus.TRANSLATED
    assert right_prose.translation_text != right_prose.source_text

    arts = mgr.db.list_export_artifacts(job.id)
    mono_pdf = Path(next(a.file_path for a in arts if a.artifact_type == "mono_ja_pdf"))
    dual_pdf = Path(
        next(a.file_path for a in arts if a.artifact_type == "dual_bilingual_pdf")
    )

    pres_reports = mgr.pdf_adapter.verify_preservation(
        document=doc,
        output_pdf_path=mono_pdf,
        blocks=blocks,
        page_indices=[0],
    )
    assert len(pres_reports) == 1
    assert pres_reports[0].all_protected_regions_preserved is True
    for r in pres_reports[0].protected_region_results:
        assert r.differing_pixel_ratio == 0.0

    reg_report = evaluate_document_layout_regression(
        original_pdf_path=pdf_path,
        blocks=blocks,
        page_indices=[0],
        mono_pdf_path=mono_pdf,
        dual_pdf_path=dual_pdf,
        document_id=doc.id,
        original_filename=doc.original_filename,
    )
    p0 = reg_report.pages[0]
    assert p0.numeric_preservation.body_blocks_masking.preservation_ratio == 1.0
    assert p0.column_order.inversion_count == 0
    assert p0.protected_alignment is not None
    assert p0.protected_alignment.all_aligned is True
