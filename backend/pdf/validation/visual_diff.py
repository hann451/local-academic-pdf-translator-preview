"""Visual preservation inspection for protected regions (Section 11, 18).

Compares original and output PDFs rendered under identical conditions (same DPI, RGB colorspace):
1. Compares protected regions (isolated formulas, figures, manual protection boxes)
   allowing minor subpixel anti-aliasing differences.
2. Excludes translated paragraph regions when verifying non-translated page background integrity.
3. Explicitly records that visual pixel comparison checks graphical preservation of
   protected regions and does NOT by itself guarantee semantic translation accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pymupdf


@dataclass
class RegionDiffResult:
    region_id: str
    page_index: int
    bbox_pt: list[float]
    max_channel_diff: float
    mean_abs_diff: float
    differing_pixel_ratio: float
    passed: bool
    note: str = ""


@dataclass
class PageVisualComparisonReport:
    page_index: int
    dpi: int
    protected_region_results: list[RegionDiffResult] = field(default_factory=list)
    non_translated_background_diff_ratio: float = 0.0
    full_page_diff_ratio: float = 0.0
    all_protected_regions_preserved: bool = True
    semantic_accuracy_guaranteed: bool = False  # Always False per Section 18 guideline


def _transform_bbox_for_page_rotation(
    pdf_path: str | Path,
    page_index: int,
    bbox_pt: list[float],
) -> list[float]:
    doc = pymupdf.open(str(pdf_path))
    try:
        page = doc[page_index]
        if page.rotation % 360 == 0:
            return list(bbox_pt)
        r = pymupdf.Rect(bbox_pt[0], bbox_pt[1], bbox_pt[2], bbox_pt[3]) * page.rotation_matrix
        r.normalize()
        return [float(r.x0), float(r.y0), float(r.x1), float(r.y1)]
    finally:
        doc.close()


def render_page_rgb_array(
    pdf_path: str | Path,
    page_index: int,
    dpi: int = 150,
) -> tuple[np.ndarray, float, float]:
    """Render a PDF page at fixed DPI into an (H, W, 3) uint8 numpy RGB array.

    Returns (rgb_array, page_width_pt, page_height_pt).
    """
    doc = pymupdf.open(str(pdf_path))
    try:
        page = doc[page_index]
        scale = dpi / 72.0
        mat = pymupdf.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, colorspace=pymupdf.csRGB, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        return arr.copy(), float(page.rect.width), float(page.rect.height)
    finally:
        doc.close()


def _bbox_pt_to_pixel_slice(
    bbox_pt: list[float],
    page_width_pt: float,
    page_height_pt: float,
    img_w: int,
    img_h: int,
    margin_px: int = 1,
) -> tuple[int, int, int, int]:
    scale_x = img_w / page_width_pt if page_width_pt > 0 else 1.0
    scale_y = img_h / page_height_pt if page_height_pt > 0 else 1.0
    x0 = max(0, int(round(bbox_pt[0] * scale_x)) + margin_px)
    y0 = max(0, int(round(bbox_pt[1] * scale_y)) + margin_px)
    x1 = min(img_w, int(round(bbox_pt[2] * scale_x)) - margin_px)
    y1 = min(img_h, int(round(bbox_pt[3] * scale_y)) - margin_px)
    if x1 <= x0:
        x0 = max(0, int(round(bbox_pt[0] * scale_x)))
        x1 = min(img_w, max(x0 + 1, int(round(bbox_pt[2] * scale_x))))
    if y1 <= y0:
        y0 = max(0, int(round(bbox_pt[1] * scale_y)))
        y1 = min(img_h, max(y0 + 1, int(round(bbox_pt[3] * scale_y))))
    return x0, y0, x1, y1


def compare_protected_regions_visually(
    original_pdf_path: str | Path,
    output_pdf_path: str | Path,
    page_index: int,
    protected_regions: list[tuple[str, list[float]]],
    translated_bboxes: list[list[float]] | None = None,
    dpi: int = 150,
    anti_aliasing_tolerance: int = 16,
    max_allowed_diff_ratio: float = 0.015,
    output_page_index: int | None = None,
) -> PageVisualComparisonReport:
    """Compare protected regions and non-translated areas between original and output PDF pages."""
    target_out_page = page_index if output_page_index is None else output_page_index
    orig_arr, orig_w, orig_h = render_page_rgb_array(original_pdf_path, page_index, dpi=dpi)
    out_arr, out_w, out_h = render_page_rgb_array(output_pdf_path, target_out_page, dpi=dpi)

    if orig_arr.shape != out_arr.shape:
        # Handle case where output is a side-by-side dual page (width = 2 * orig_w)
        if out_arr.shape[0] == orig_arr.shape[0] and abs(out_arr.shape[1] - 2 * orig_arr.shape[1]) <= 4:
            # Right half or left half
            out_arr = out_arr[:, : orig_arr.shape[1], :]
        else:
            return PageVisualComparisonReport(
                page_index=page_index,
                dpi=dpi,
                all_protected_regions_preserved=False,
                protected_region_results=[
                    RegionDiffResult(
                        region_id="page_dimension_mismatch",
                        page_index=page_index,
                        bbox_pt=[0.0, 0.0, orig_w, orig_h],
                        max_channel_diff=255.0,
                        mean_abs_diff=255.0,
                        differing_pixel_ratio=1.0,
                        passed=False,
                        note=f"Rendered shape mismatch: {orig_arr.shape} vs {out_arr.shape}",
                    )
                ],
            )

    img_h, img_w, _ = orig_arr.shape
    region_results: list[RegionDiffResult] = []
    all_passed = True

    for reg_id, bbox in protected_regions:
        rot_bbox = _transform_bbox_for_page_rotation(original_pdf_path, page_index, bbox)
        x0, y0, x1, y1 = _bbox_pt_to_pixel_slice(rot_bbox, orig_w, orig_h, img_w, img_h, margin_px=1)
        crop_orig = orig_arr[y0:y1, x0:x1].astype(np.int16)
        crop_out = out_arr[y0:y1, x0:x1].astype(np.int16)
        diff = np.abs(crop_orig - crop_out)
        max_diff = float(np.max(diff)) if diff.size > 0 else 0.0
        mean_diff = float(np.mean(diff)) if diff.size > 0 else 0.0
        # Pixels exceeding anti-aliasing tolerance in any RGB channel
        pixel_exceeded = np.any(diff > anti_aliasing_tolerance, axis=-1)
        diff_ratio = float(np.mean(pixel_exceeded)) if pixel_exceeded.size > 0 else 0.0

        passed = diff_ratio <= max_allowed_diff_ratio
        if not passed:
            all_passed = False

        region_results.append(
            RegionDiffResult(
                region_id=reg_id,
                page_index=page_index,
                bbox_pt=rot_bbox,
                max_channel_diff=max_diff,
                mean_abs_diff=mean_diff,
                differing_pixel_ratio=diff_ratio,
                passed=passed,
                note="OK" if passed else f"Diff ratio {diff_ratio:.4f} > {max_allowed_diff_ratio}",
            )
        )

    # Check non-translated background area by masking out translated bboxes
    mask = np.ones((img_h, img_w), dtype=bool)
    for t_bbox in translated_bboxes or []:
        rot_t = _transform_bbox_for_page_rotation(original_pdf_path, page_index, t_bbox)
        tx0, ty0, tx1, ty1 = _bbox_pt_to_pixel_slice(
            [
                max(0.0, rot_t[0] - 4.0),
                max(0.0, rot_t[1] - 4.0),
                min(orig_w, rot_t[2] + 4.0),
                min(orig_h, rot_t[3] + 4.0),
            ],
            orig_w,
            orig_h,
            img_w,
            img_h,
            margin_px=0,
        )
        mask[ty0:ty1, tx0:tx1] = False

    full_diff = np.abs(orig_arr.astype(np.int16) - out_arr.astype(np.int16))
    exceeded_full = np.any(full_diff > anti_aliasing_tolerance, axis=-1)
    full_page_diff_ratio = float(np.mean(exceeded_full)) if exceeded_full.size > 0 else 0.0
    bg_pixels = exceeded_full[mask]
    bg_diff_ratio = float(np.mean(bg_pixels)) if bg_pixels.size > 0 else 0.0
    if bg_diff_ratio > max_allowed_diff_ratio:
        all_passed = False

    return PageVisualComparisonReport(
        page_index=page_index,
        dpi=dpi,
        protected_region_results=region_results,
        non_translated_background_diff_ratio=bg_diff_ratio,
        full_page_diff_ratio=full_page_diff_ratio,
        all_protected_regions_preserved=all_passed,
        semantic_accuracy_guaranteed=False,
    )
