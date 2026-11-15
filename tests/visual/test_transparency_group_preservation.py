"""Visual and structural tests for PDF Page `/Group` transparency and ColorSpace preservation."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import ImageCms

from backend.config import AppConfig
from backend.domain.models import BlockStatus, JobState, TranslationJobConfig
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged

pytestmark = pytest.mark.visual


def _build_mixed_transparency_group_pdf(pdf_path: Path) -> tuple[bytes, pymupdf.Rect]:
    """Create an offline 5-page PDF exercising no-Group, DeviceCMYK, DeviceRGB, and shared ICCBased Groups.

    Returns `(icc_profile_bytes, cmyk_graphic_rect)` so tests can verify both ICC stream deduplication
    and pixel-exact rendering inside the translucent CMYK graphic box.
    """
    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    doc = pymupdf.open()

    # Page 0: plain RGB page with no /Group entry
    p0 = doc.new_page(width=420, height=560)
    p0.insert_textbox(
        pymupdf.Rect(40, 45, 380, 130),
        (
            "This introductory paragraph sits on a plain RGB page without a page transparency group. "
            "It must remain without a Group entry after roundtrip, mono, and dual PDF export."
        ),
        fontsize=10.5,
    )

    # Page 1: indirect /Group with /DeviceCMYK + /I true /K true + translucent overlapping CMYK rects
    p1 = doc.new_page(width=420, height=560)
    p1.insert_textbox(
        pymupdf.Rect(40, 45, 380, 155),
        (
            "This experimental paragraph sits above a translucent CMYK diagram blended under a "
            "DeviceCMYK page transparency group. Reference documentation is available at "
            "https://example.org/cmyk-note for measurement reproduction."
        ),
        fontsize=10.5,
    )
    cmyk_graphic_rect = pymupdf.Rect(50, 220, 340, 390)
    sh1 = p1.new_shape()
    sh1.draw_rect(pymupdf.Rect(60, 230, 240, 350))
    sh1.finish(color=(1, 0, 0, 0), fill=(0, 0.85, 0.85, 0), fill_opacity=0.45, width=1.5)
    sh1.draw_rect(pymupdf.Rect(150, 260, 330, 380))
    sh1.finish(color=(0, 1, 0, 0), fill=(0.9, 0.2, 0, 0), fill_opacity=0.45, width=1.5)
    sh1.commit()
    gx1 = doc.get_new_xref()
    doc.update_object(gx1, "<< /Type /Group /S /Transparency /CS /DeviceCMYK /I true /K true >>")
    doc.xref_set_key(p1.xref, "Group", f"{gx1} 0 R")

    # Page 2: direct dictionary /Group with /DeviceRGB + /I false /K true
    p2 = doc.new_page(width=420, height=560)
    p2.insert_textbox(
        pymupdf.Rect(40, 45, 380, 140),
        (
            "This section exercises a direct dictionary transparency group using DeviceRGB with "
            "explicit isolation and knockout flags so direct dictionaries preserve all attributes."
        ),
        fontsize=10.5,
    )
    sh2 = p2.new_shape()
    sh2.draw_rect(pymupdf.Rect(70, 230, 260, 350))
    sh2.finish(color=(0.1, 0.2, 0.8), fill=(0.85, 0.25, 0.15), fill_opacity=0.5, width=1.5)
    sh2.commit()
    doc.xref_set_key(p2.xref, "Group", "<< /Type /Group /S /Transparency /CS /DeviceRGB /I false /K true >>")

    # Pages 3 and 4: indirect /Group referencing shared /ICCBased stream AND a decoy /CS0 resource name
    icc_xref = doc.get_new_xref()
    doc.update_object(icc_xref, "<< /N 3 /Alternate /DeviceRGB >>")
    doc.update_stream(icc_xref, icc_bytes, compress=True)
    cs_arr_xref = doc.get_new_xref()
    doc.update_object(cs_arr_xref, f"[ /ICCBased {icc_xref} 0 R ]")
    decoy_cs_xref = doc.get_new_xref()
    doc.update_object(decoy_cs_xref, "[ /CalGray << /WhitePoint [ 0.9505 1.0 1.089 ] >> ]")

    for p_num, body_text in enumerate(
        (
            (
                "Page four uses an indirect ICCBased page transparency group while Resource CS0 "
                "points to an unrelated CalGray colorspace so preservation never hardcodes CS0."
            ),
            (
                "Page five shares the exact same ICCBased stream across pages to verify that Graftmap "
                "deduplicates indirect ICC profile streams cleanly across multiple exported pages."
            ),
        ),
        start=4,
    ):
        page = doc.new_page(width=420, height=560)
        page.insert_textbox(pymupdf.Rect(40, 45, 380, 140), body_text, fontsize=10.5)
        sh_icc = page.new_shape()
        sh_icc.draw_rect(pymupdf.Rect(80, 230, 290, 340))
        sh_icc.finish(color=(0.15, 0.45, 0.75), fill=(0.2, 0.7, 0.4), fill_opacity=0.55, width=1.2)
        sh_icc.commit()
        grp_xref = doc.get_new_xref()
        doc.update_object(grp_xref, f"<< /Type /Group /S /Transparency /CS {cs_arr_xref} 0 R >>")
        doc.xref_set_key(page.xref, "Group", f"{grp_xref} 0 R")
        # Attach unrelated /CS0 under page /Resources/ColorSpace as a decoy
        res_key = doc.xref_get_key(page.xref, "Resources")
        if res_key[0] == "xref":
            res_xref = int(res_key[1].split()[0])
            doc.xref_set_key(res_xref, "ColorSpace", f"<< /CS0 {decoy_cs_xref} 0 R /CS_OTHER {cs_arr_xref} 0 R >>")

    doc.save(str(pdf_path), garbage=0, deflate=True)
    doc.close()
    return icc_bytes, cmyk_graphic_rect


def _resolve_page_group_dict(doc: pymupdf.Document, page_idx: int) -> str | None:
    key = doc.xref_get_key(doc[page_idx].xref, "Group")
    if key[0] == "null":
        return None
    if key[0] == "dict":
        return key[1]
    if key[0] == "xref":
        return doc.xref_object(int(key[1].split()[0]))
    return str(key)


def _extract_icc_stream_xref(doc: pymupdf.Document, page_idx: int) -> tuple[int, bytes]:
    key = doc.xref_get_key(doc[page_idx].xref, "Group")
    assert key[0] == "xref"
    g_xref = int(key[1].split()[0])
    cs_key = doc.xref_get_key(g_xref, "CS")
    if cs_key[0] == "xref":
        cs_str = doc.xref_object(int(cs_key[1].split()[0]))
    else:
        cs_str = cs_key[1]
    match = re.search(r"/ICCBased\s+(\d+)\s+0\s+R", cs_str)
    assert match is not None, f"Expected /ICCBased indirect stream ref, got: {cs_key} / {cs_str}"
    icc_xref = int(match.group(1))
    return icc_xref, doc.xref_stream(icc_xref)


def _render_clip_rgb(page: pymupdf.Page, clip: pymupdf.Rect, dpi: int = 150) -> np.ndarray:
    pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()


def _max_channel_diff(arr_a: np.ndarray, arr_b: np.ndarray) -> int:
    min_h = min(arr_a.shape[0], arr_b.shape[0])
    min_w = min(arr_a.shape[1], arr_b.shape[1])
    return int(np.abs(arr_a[:min_h, :min_w].astype(np.int16) - arr_b[:min_h, :min_w].astype(np.int16)).max())


def test_transparency_group_end_to_end_roundtrip_mono_and_dual(
    isolated_config: AppConfig,
    tmp_path: Path,
) -> None:
    """Verify Page `/Group` and nested ColorSpace/ICC objects survive roundtrip, mono, and dual PDF exports."""
    src_pdf_path = tmp_path / "mixed_groups.pdf"
    icc_bytes, cmyk_rect = _build_mixed_transparency_group_pdf(src_pdf_path)
    orig_sha = compute_sha256(src_pdf_path)

    # Sanity check: naive insert_pdf() drops /Group and introduces a large CMYK-to-RGB color shift
    orig_check = pymupdf.open(str(src_pdf_path))
    naive_copy = pymupdf.open()
    naive_copy.insert_pdf(orig_check)
    naive_diff = _max_channel_diff(
        _render_clip_rgb(orig_check[1], cmyk_rect),
        _render_clip_rgb(naive_copy[1], cmyk_rect),
    )
    naive_copy.close()
    orig_check.close()
    assert naive_diff >= 20, f"Expected baseline Group-loss color shift >= 20, got {naive_diff}"

    mgr = JobManager(config=isolated_config)
    document = mgr.register_document(src_pdf_path)

    # 1. Roundtrip export path
    rt_job = mgr.create_job(
        document.id,
        TranslationJobConfig(
            only_roundtrip_preservation=True,
            output_mono_pdf=True,
            output_dual_pdf=True,
        ),
    )
    rt_finished = mgr.run_job(rt_job.id)
    assert rt_finished.state == JobState.COMPLETED
    rt_art = next(a for a in mgr.db.list_export_artifacts(rt_job.id) if a.artifact_type == "roundtrip_pdf")
    rt_path = Path(rt_art.file_path)

    # 2. Full translation export path (mono .ja.pdf + dual .dual.pdf)
    tr_job = mgr.create_job(
        document.id,
        TranslationJobConfig(
            use_mock_translator=True,
            output_mono_pdf=True,
            output_dual_pdf=True,
        ),
    )
    tr_finished = mgr.run_job(tr_job.id)
    assert tr_finished.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    assert verify_original_unchanged(src_pdf_path, orig_sha)

    artifacts = mgr.db.list_export_artifacts(tr_job.id)
    mono_path = Path(next(a for a in artifacts if a.artifact_type == "mono_ja_pdf").file_path)
    dual_path = Path(next(a for a in artifacts if a.artifact_type == "dual_bilingual_pdf").file_path)

    # Verify visual diff metric strictly reports 0.0 background & protected diff
    blocks = mgr.db.list_blocks(document.id)
    reports = mgr.pdf_adapter.verify_preservation(
        document=document,
        output_pdf_path=mono_path,
        blocks=blocks,
        page_indices=list(range(document.page_count)),
        is_roundtrip_only=False,
    )
    for rep in reports:
        assert rep.all_protected_regions_preserved is True
        assert rep.non_translated_background_diff_ratio == 0.0

    # Reopen saved files from disk (never trust pre-save memory) and inspect /Group + ICC + pixels
    orig_doc = pymupdf.open(str(src_pdf_path))
    try:
        for exported_path in (rt_path, mono_path, dual_path):
            exp_doc = pymupdf.open(str(exported_path))
            try:
                assert len(exp_doc) == 5
                # Page 0 had no /Group -> must stay absent
                assert _resolve_page_group_dict(exp_doc, 0) is None

                # Page 1: /DeviceCMYK + /I true + /K true
                g1 = _resolve_page_group_dict(exp_doc, 1)
                assert g1 is not None
                assert "/DeviceCMYK" in g1 and "/Transparency" in g1 and "/Type /Group" in g1
                assert "/I true" in g1 and "/K true" in g1

                # Page 2: /DeviceRGB + /I false + /K true
                g2 = _resolve_page_group_dict(exp_doc, 2)
                assert g2 is not None
                assert "/DeviceRGB" in g2 and "/Transparency" in g2
                assert "/I false" in g2 and "/K true" in g2

                # Pages 3 & 4: /ICCBased indirect stream deduplicated via shared Graftmap
                icc_xref_3, icc_bytes_3 = _extract_icc_stream_xref(exp_doc, 3)
                icc_xref_4, icc_bytes_4 = _extract_icc_stream_xref(exp_doc, 4)
                assert icc_xref_3 == icc_xref_4
                assert icc_bytes_3 == icc_bytes
                assert icc_bytes_4 == icc_bytes
            finally:
                exp_doc.close()

        # Pixel-identical rendering check inside translucent CMYK box and protected artwork across all outputs
        rt_doc = pymupdf.open(str(rt_path))
        mono_doc = pymupdf.open(str(mono_path))
        dual_doc = pymupdf.open(str(dual_path))
        try:
            orig_cmyk_px = _render_clip_rgb(orig_doc[1], cmyk_rect)
            rt_cmyk_px = _render_clip_rgb(rt_doc[1], cmyk_rect)
            mono_cmyk_px = _render_clip_rgb(mono_doc[1], cmyk_rect)
            assert _max_channel_diff(orig_cmyk_px, rt_cmyk_px) == 0
            assert _max_channel_diff(orig_cmyk_px, mono_cmyk_px) == 0

            # Check BOTH Left (original) and Right (translated) halves of dual PDF
            w = float(orig_doc[1].rect.width)
            dual_left_cmyk_px = _render_clip_rgb(dual_doc[1], cmyk_rect)
            dual_right_cmyk_rect = pymupdf.Rect(
                cmyk_rect.x0 + w, cmyk_rect.y0, cmyk_rect.x1 + w, cmyk_rect.y1
            )
            dual_right_cmyk_px = _render_clip_rgb(dual_doc[1], dual_right_cmyk_rect)
            assert _max_channel_diff(orig_cmyk_px, dual_left_cmyk_px) == 0
            assert _max_channel_diff(mono_cmyk_px, dual_right_cmyk_px) == 0

            # Also confirm Page 3 ICC graphic matches pixel-for-pixel
            icc_rect = pymupdf.Rect(75, 225, 295, 345)
            orig_icc_px = _render_clip_rgb(orig_doc[3], icc_rect)
            assert _max_channel_diff(orig_icc_px, _render_clip_rgb(mono_doc[3], icc_rect)) == 0
            assert _max_channel_diff(orig_icc_px, _render_clip_rgb(dual_doc[3], icc_rect)) == 0
            dual_right_icc_rect = pymupdf.Rect(icc_rect.x0 + w, icc_rect.y0, icc_rect.x1 + w, icc_rect.y1)
            assert _max_channel_diff(orig_icc_px, _render_clip_rgb(dual_doc[3], dual_right_icc_rect)) == 0

            # Confirm Japanese body text and URL link remain dark, readable, and non-missing under CMYK /Group
            p1_translated_blocks = [
                b for b in blocks if b.page_index == 1 and b.status == BlockStatus.TRANSLATED
            ]
            assert len(p1_translated_blocks) >= 1
            body_text_p1 = mono_doc[1].get_text("text")
            normalized_body_p1 = body_text_p1.replace("\xa0", " ").replace("\u2011", "-")
            assert "である。" in normalized_body_p1
            assert "https://example.org/cmyk-note" in normalized_body_p1

            text_clip = pymupdf.Rect(*p1_translated_blocks[0].bbox)
            mono_text_px = _render_clip_rgb(mono_doc[1], text_clip)
            dual_right_text_clip = pymupdf.Rect(text_clip.x0 + w, text_clip.y0, text_clip.x1 + w, text_clip.y1)
            dual_right_text_px = _render_clip_rgb(dual_doc[1], dual_right_text_clip)
            assert int(mono_text_px.min()) <= 60, "Expected dark readable Japanese glyph pixels under CMYK /Group"
            assert int(dual_right_text_px.min()) <= 60, "Expected dark readable Japanese glyphs on Dual right side"
        finally:
            rt_doc.close()
            mono_doc.close()
            dual_doc.close()
    finally:
        orig_doc.close()


def test_transparency_group_subset_pages_dual_fallback_and_windows_cleanup(
    isolated_config: AppConfig,
    tmp_path: Path,
) -> None:
    """Verify subset/reordered page mappings, dual trans fallback, and Windows file handle release."""
    src_pdf_path = tmp_path / "subset_src.pdf"
    _, cmyk_rect = _build_mixed_transparency_group_pdf(src_pdf_path)

    mgr = JobManager(config=isolated_config)
    adapter = mgr.pdf_adapter

    # Subset + reordered page_indices: [2 (DeviceRGB), 0 (none), 1 (DeviceCMYK)]
    rt_subset = tmp_path / "rt_subset.pdf"
    adapter._export_roundtrip_preserving_pdf(src_pdf_path, rt_subset, [2, 0, 1])

    orig_doc = pymupdf.open(str(src_pdf_path))
    rt_doc = pymupdf.open(str(rt_subset))
    try:
        assert len(rt_doc) == 3
        g_sub0 = _resolve_page_group_dict(rt_doc, 0)
        g_sub1 = _resolve_page_group_dict(rt_doc, 1)
        g_sub2 = _resolve_page_group_dict(rt_doc, 2)
        assert g_sub0 is not None and "/DeviceRGB" in g_sub0
        assert g_sub1 is None
        assert g_sub2 is not None and "/DeviceCMYK" in g_sub2
        assert _max_channel_diff(_render_clip_rgb(orig_doc[1], cmyk_rect), _render_clip_rgb(rt_doc[2], cmyk_rect)) == 0
    finally:
        rt_doc.close()
        orig_doc.close()

    # Dual fallback: when left orig has no /Group and right trans has a /Group, dual inherits trans /Group
    left_no_group = tmp_path / "left_plain.pdf"
    right_with_group = tmp_path / "right_cmyk.pdf"
    dual_fb = tmp_path / "dual_fallback.pdf"

    d_left = pymupdf.open()
    d_left.new_page(width=300, height=300).insert_text((30, 50), "Plain left")
    d_left.save(str(left_no_group))
    d_left.close()

    d_right = pymupdf.open()
    rp = d_right.new_page(width=300, height=300)
    sh = rp.new_shape()
    sh.draw_rect(pymupdf.Rect(40, 80, 220, 220))
    sh.finish(color=(1, 0, 0, 0), fill=(0, 0.85, 0.85, 0), fill_opacity=0.45, width=1.5)
    sh.commit()
    gx = d_right.get_new_xref()
    d_right.update_object(gx, "<< /Type /Group /S /Transparency /CS /DeviceCMYK /I true >>")
    d_right.xref_set_key(rp.xref, "Group", f"{gx} 0 R")
    d_right.save(str(right_with_group))
    d_right.close()

    adapter._export_dual_bilingual_pdf(left_no_group, right_with_group, dual_fb, [0])

    dual_check = pymupdf.open(str(dual_fb))
    right_check = pymupdf.open(str(right_with_group))
    try:
        g_dual = _resolve_page_group_dict(dual_check, 0)
        assert g_dual is not None and "/DeviceCMYK" in g_dual and "/I true" in g_dual
        right_box = pymupdf.Rect(40, 80, 220, 220)
        dual_right_box = pymupdf.Rect(340, 80, 520, 220)
        assert _max_channel_diff(_render_clip_rgb(right_check[0], right_box), _render_clip_rgb(dual_check[0], dual_right_box)) == 0
    finally:
        dual_check.close()
        right_check.close()

    # Verify Windows file descriptors are completely released immediately after export/close
    for p in (rt_subset, left_no_group, right_with_group, dual_fb):
        p.unlink()
        assert not p.exists()
