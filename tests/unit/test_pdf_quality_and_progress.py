"""Unit tests for PDF rendering fixes, ACM math/table/header classification,
shared-handle visual diff speedup, and JobManager progress/ETA tracking.
"""

from __future__ import annotations

import time
from pathlib import Path

import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    JobState,
    TranslationJobConfig,
)
from backend.jobs.manager import JobManager
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    _MATH_FONT_KEYWORDS,
    _find_unrenderable_chars,
    _measure_rendered_line_width,
    _restore_elided_separation_scn,
    _select_char_font,
    fit_japanese_text_in_bbox,
)
from backend.pdf.validation.visual_diff import compare_protected_regions_visually

pytestmark = pytest.mark.unit


def _raw_span(
    text: str,
    bbox: list[float],
    *,
    font: str = "LinLibertineT",
    size: float = 10.0,
) -> dict:
    return {
        "text": text,
        "bbox": list(bbox),
        "font": font,
        "size": size,
        "origin": [bbox[0], bbox[3]],
    }


def _raw_line(spans: list[dict], bbox: list[float] | None = None) -> dict:
    b = bbox or [
        min(s["bbox"][0] for s in spans),
        min(s["bbox"][1] for s in spans),
        max(s["bbox"][2] for s in spans),
        max(s["bbox"][3] for s in spans),
    ]
    return {"bbox": b, "spans": spans}


def _raw_block(lines: list[dict], bbox: list[float] | None = None) -> dict:
    b = bbox or [
        min(ln["bbox"][0] for ln in lines),
        min(ln["bbox"][1] for ln in lines),
        max(ln["bbox"][2] for ln in lines),
        max(ln["bbox"][3] for ln in lines),
    ]
    return {"type": 0, "bbox": b, "lines": lines}


def test_restore_elided_separation_scn_patches_missing_scn_without_touching_literals_or_valid_scn(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "sep.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((50, 100), "Placeholder", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    doc = pymupdf.open(str(pdf_path))
    page = doc[0]
    cs_xref = doc.get_new_xref()
    doc.update_object(
        cs_xref,
        "[/Separation /Black /DeviceCMYK <</FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 0 0 1] /N 1>>]",
    )
    # Attach /CS1 to page's Resources/ColorSpace (handling indirect /Resources dict)
    res_type, res_val = doc.xref_get_key(page.xref, "Resources")
    if res_type == "xref":
        doc.xref_set_key(int(res_val.split()[0]), "ColorSpace", f"<</CS1 {cs_xref} 0 R>>")
    else:
        doc.xref_set_key(page.xref, "Resources/ColorSpace", f"<</CS1 {cs_xref} 0 R>>")

    stream_xref = page.get_contents()[0]
    # First BT: elided `1 scn` right after `/CS1 cs`, and string literal containing `/CS1 cs`
    # Second BT: already has `/CS1 cs 0.5 scn` -> must NOT get `1 scn` inserted
    stream_data = (
        b"q\n"
        b"BT /F0 12 Tf 1 0 0 1 50 320 Tm /CS1 cs (Visible /CS1 cs literal) Tj ET\n"
        b"BT /F0 12 Tf 1 0 0 1 50 280 Tm /CS1 cs 0.5 scn (ValidHalf) Tj ET\n"
        b"Q\n"
    )
    doc.update_stream(stream_xref, stream_data)

    restored_count = _restore_elided_separation_scn(doc, page)
    assert restored_count == 1
    patched = doc.xref_stream(stream_xref)
    assert b"/CS1 cs 1 scn (Visible /CS1 cs literal) Tj" in patched
    assert b"/CS1 cs 0.5 scn (ValidHalf) Tj" in patched
    # Idempotent on second pass
    assert _restore_elided_separation_scn(doc, page) == 0
    doc.close()


def test_control_chars_rejected_by_select_char_font_and_find_unrenderable_chars() -> None:
    ja_font = pymupdf.Font("helv")
    assert _select_char_font(" ", "F_JA", ja_font) == ("F_JA", ja_font)
    assert _select_char_font("\t", "F_JA", ja_font) == ("F_JA", ja_font)
    for cp in (0x02, 0x03, 0x04, 0x05, 0x06, 0x7F):
        ch = chr(cp)
        assert _select_char_font(ch, "F_JA", ja_font) is None

    missing = _find_unrenderable_chars("a\x02 \x05b\x7f", "F_JA", ja_font)
    assert "CTRL(U+0002)" in missing
    assert "CTRL(U+0005)" in missing
    assert "CTRL(U+007F)" in missing
    assert all(not item.startswith(" ") for item in missing)


def test_acm_math_fonts_delimiter_underbrace_and_running_header_rules() -> None:
    adp = NativePreservingAdapter()
    for kw in ("txex", "txsy", "rtxmi", "rtxr", "linlibertinei", "linlibertinezi"):
        assert kw in _MATH_FONT_KEYWORDS

    # Multi-char txex horizontal underbrace block must qualify as pure delimiter block
    brace_lines = [
        _raw_line(
            [
                _raw_span("\x05", [202.3, 629.7, 206.8, 639.7], font="txex-bar"),
                _raw_span("\x03\x06\x06\x06\x04\x05\x06\x06", [120.2, 634.1, 206.9, 644.1], font="txex"),
            ]
        )
    ]
    assert adp._is_pure_math_delimiter_block(brace_lines) is True

    # Underbrace grouping merges equation fragments + brace + small underbrace labels into one block
    eq_blocks = [
        _raw_block([_raw_line([_raw_span("H =", [86.0, 618.0, 118.0, 628.0], font="LinLibertineI")])]),
        _raw_block(brace_lines, bbox=[120.2, 629.7, 206.9, 644.1]),
        _raw_block(
            [
                _raw_line(
                    [_raw_span("Multi-Path Channel", [133.0, 646.5, 194.0, 654.2], font="LinLibertineT", size=7.3)]
                )
            ]
        ),
        _raw_block(
            [
                _raw_line(
                    [_raw_span("e^{-j2π}", [209.4, 616.1, 246.3, 632.1], font="LinLibertineI", size=10.0)]
                )
            ]
        ),
        _raw_block(
            [
                _raw_line(
                    [_raw_span("Cyclic Shift", [210.0, 634.9, 246.3, 642.1], font="LinLibertineT", size=7.3)]
                ),
                _raw_line(
                    [_raw_span("Diversity", [213.3, 642.8, 240.7, 650.1], font="LinLibertineT", size=7.3)]
                ),
            ]
        ),
        _raw_block([_raw_line([_raw_span("(2)", [310.0, 618.6, 322.0, 628.6], font="LinLibertineT")])]),
    ]
    grouped = adp._group_fragmented_display_equations(
        eq_blocks,
        p_idx=2,
        page_w=486.0,
        page_h=720.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
    )
    assert len(grouped) == 1
    assert grouped[0].get("_grouped_display_equation") is True

    # ACM running headers (article:page format) on page >= 2
    kind, sem, _, rule_id, _, _, _ = adp._classify_academic_block(
        text="WiFi Sensing with Channel State Information: A Survey 46:3",
        line_count=1,
        p_idx=2,
        bbox=[45.7, 55.1, 440.2, 65.0],
        page_w=486.0,
        page_h=720.0,
        avg_font_size=10.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
        has_math_font=False,
        has_sub_super=False,
        in_references_section=False,
    )
    assert kind == BlockKind.HEADER_FOOTER
    assert sem == "running_header"
    assert rule_id == "RULE_RUNNING_HEADER_FOOTER"

    kind2, _, _, _, _, _, _ = adp._classify_academic_block(
        text="46:18 Y. Ma et al.",
        line_count=1,
        p_idx=17,
        bbox=[45.7, 55.1, 440.2, 65.0],
        page_w=486.0,
        page_h=720.0,
        avg_font_size=10.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
        has_math_font=False,
        has_sub_super=False,
        in_references_section=False,
    )
    assert kind2 == BlockKind.HEADER_FOOTER

    # PyMuPDF splits wide ACM headers (left title + right `46:3`) into line_count=2 on same visual row
    for hdr_text, p_no in [
        ("WiFi Sensing with Channel State Information: A Survey 46:3", 2),
        ("46:18 Y. Ma et al.", 17),
    ]:
        hk, hsem, _, hrule, _, _, _ = adp._classify_academic_block(
            text=hdr_text,
            line_count=2,
            p_idx=p_no,
            bbox=[45.7, 55.1, 440.2, 65.0],
            page_w=486.0,
            page_h=720.0,
            avg_font_size=10.0,
            body_font_size=10.0,
            frontmatter_end_y=0.0,
            drawing_boxes=[],
            has_math_font=False,
            has_sub_super=False,
            in_references_section=False,
        )
        assert hk == BlockKind.HEADER_FOOTER
        assert hsem == "running_header"
        assert hrule == "RULE_RUNNING_HEADER_FOOTER"

    # Negative guard: tall 2-line body paragraph at top must NOT match in_top_acm
    body_k, _, _, _, _, _, _ = adp._classify_academic_block(
        text="This two-line body paragraph continues smoothly across the page top margin without header tags.",
        line_count=2,
        p_idx=2,
        bbox=[45.7, 55.1, 440.2, 79.0],
        page_w=486.0,
        page_h=720.0,
        avg_font_size=10.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
        has_math_font=False,
        has_sub_super=False,
        in_references_section=False,
    )
    assert body_k == BlockKind.PARAGRAPH


def test_fit_japanese_text_in_bbox_fast_and_exact() -> None:
    ja_font = pymupdf.Font("japan")
    para = (
        "Wi-Fiチャネル状態情報（CSI）を用いた無線センシング技術では、各サブキャリアにおける振幅減衰と"
        "位相シフトを高い精度で推定することで、人間の呼吸検知・ジェスチャ認識・屋内位置推定など多様な"
        "非接触アプリケーションが実現されている。さらにMIMO-OFDMシステムにおけるマルチパス伝搬特性を"
        "モデル化し、環境変動に対するロバスト性を確保するためには、送信機・受信機間の周波数オフセットや"
        "サンプリング時間オフセットを適切に補正する必要がある。" * 3
    )
    assert len(para) >= 600
    bbox = [45.0, 100.0, 440.0, 280.0]
    box_w = bbox[2] - bbox[0]
    t0 = time.perf_counter()
    fitted_sz, wrapped_lines, _ = fit_japanese_text_in_bbox(
        para,
        bbox,
        initial_font_size=10.0,
        min_font_size=8.0,
        ja_font=ja_font,
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0
    assert wrapped_lines
    for line in wrapped_lines:
        assert _measure_rendered_line_width(line, fitted_sz, ja_font=ja_font) <= box_w + 1e-3


def test_detect_ruled_table_boxes_catches_grid_and_booktabs_and_ignores_page1_rules() -> None:
    adp = NativePreservingAdapter()
    # Grid table drawings (2+ H rules + interior V rules)
    grid_drawings = [
        {"rect": pymupdf.Rect(45.7, 100.0, 440.2, 100.0)},
        {"rect": pymupdf.Rect(45.7, 118.0, 440.2, 118.0)},
        {"rect": pymupdf.Rect(150.0, 100.2, 150.0, 155.0)},
    ]
    grid_raw = [
        _raw_block([_raw_line([_raw_span("Method", [50, 103, 140, 115])])]),
        _raw_block([_raw_line([_raw_span("Accuracy", [160, 103, 240, 115])])]),
        _raw_block([_raw_line([_raw_span("CSI-Net", [50, 125, 140, 137])])]),
    ]
    assert len(adp._detect_ruled_table_boxes(grid_drawings, grid_raw, 486.0, 720.0)) == 1

    # Pure booktabs table (3 aligned H rules, tight header gap 16pt, multi-column cells)
    booktabs_drawings = [
        {"rect": pymupdf.Rect(46.0, 100.0, 440.0, 100.0)},
        {"rect": pymupdf.Rect(46.0, 116.0, 440.0, 116.0)},
        {"rect": pymupdf.Rect(46.0, 180.0, 440.0, 180.0)},
    ]
    booktabs_raw = [
        _raw_block([_raw_line([_raw_span("Col A", [50, 103, 120, 113])])]),
        _raw_block([_raw_line([_raw_span("Col B", [200, 103, 280, 113])])]),
        _raw_block([_raw_line([_raw_span("Val 1", [50, 125, 120, 135])])]),
        _raw_block([_raw_line([_raw_span("98.5%", [200, 125, 280, 135])])]),
    ]
    assert len(adp._detect_ruled_table_boxes(booktabs_drawings, booktabs_raw, 486.0, 720.0)) == 1

    # Negative guard: Page 1 horizontal rules enclosing long Abstract/Introduction must NOT trigger
    page1_drawings = [
        {"rect": pymupdf.Rect(45.7, 152.3, 440.2, 152.3)},
        {"rect": pymupdf.Rect(45.7, 426.2, 440.2, 426.2)},
        {"rect": pymupdf.Rect(45.7, 541.7, 440.2, 541.7)},
    ]
    page1_raw = [
        _raw_block([_raw_line([_raw_span("Abstract " + "word " * 60, [45.7, 170.0, 440.0, 310.0])])]),
        _raw_block([_raw_line([_raw_span("1 INTRODUCTION", [45.7, 445.0, 180.0, 458.0])])]),
        _raw_block([_raw_line([_raw_span("In this survey " + "text " * 40, [45.7, 465.0, 440.0, 530.0])])]),
        _raw_block([_raw_line([_raw_span("CCS Concepts: General", [45.7, 320.0, 300.0, 340.0])])]),
    ]
    assert adp._detect_ruled_table_boxes(page1_drawings, page1_raw, 486.0, 720.0) == []


def test_verify_preservation_reused_handles_match_standalone_and_job_tracks_progress_and_eta(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    pdf_path = tmp_path / "two_page.pdf"
    doc_pdf = pymupdf.open()
    for p in range(2):
        page = doc_pdf.new_page(width=595, height=842)
        page.insert_text((54, 70), f"{p + 1}. Section Title", fontsize=12, fontname="hebo")
        page.insert_text(
            (54, 110),
            f"Paragraph on page {p + 1} with 2.4 GHz measurements for verification.",
            fontsize=10,
            fontname="helv",
        )
        page.insert_text((54, 150), "x(t) = a + b   (1)", fontsize=10, fontname="symb")
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
    assert finished.started_at is not None
    assert finished.progress_percent == 100.0
    assert finished.processed_pages == 2
    assert finished.total_pages == 2
    assert finished.estimated_remaining_seconds == 0.0
    assert finished.stage_detail is not None

    # Compare reused-handle visual diff vs path-based visual diff
    arts = mgr.db.list_export_artifacts(job.id)
    mono_pdf = Path(next(a.file_path for a in arts if a.artifact_type == "mono_ja_pdf"))
    with pymupdf.open(doc.safe_storage_path) as s_doc, pymupdf.open(str(mono_pdf)) as o_doc:
        standalone = compare_protected_regions_visually(
            doc.safe_storage_path,
            mono_pdf,
            0,
            [("reg-formula", [50.0, 138.0, 200.0, 160.0])],
        )
        reused = compare_protected_regions_visually(
            doc.safe_storage_path,
            mono_pdf,
            0,
            [("reg-formula", [50.0, 138.0, 200.0, 160.0])],
            orig_doc=s_doc,
            out_doc=o_doc,
        )
        assert reused.full_page_diff_ratio == pytest.approx(standalone.full_page_diff_ratio)
        assert reused.all_protected_regions_preserved == standalone.all_protected_regions_preserved

    # Retypeset also maintains 100% completion metrics
    rt_res = mgr.retypeset_job(job.id, allow_validation_warnings=True)
    rt_job = rt_res["job"]
    assert rt_job.progress_percent == 100.0
    assert rt_job.processed_pages == 2
    assert rt_job.estimated_remaining_seconds == 0.0
