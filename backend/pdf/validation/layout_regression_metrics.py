"""Mechanical layout and math regression metrics for multi-column academic PDFs.

Provides four decoupled, quantitative page- and document-level metrics that detect
regressions without visual inspection:
1. Formula block count consistency (`check_formula_block_count_consistency`)
2. Multiplicity-aware comma-grouped number & unit preservation (`check_numeric_and_unit_preservation`)
3. Column reading-order monotonicity (`check_column_order_monotonicity`)
4. Untranslated protected-block coordinate stability & neighbor alignment (`check_protected_block_alignment`)
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pymupdf

from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    ColumnOrderInversion,
    ColumnOrderMonotonicityMetrics,
    FormulaBlockCountMetrics,
    FormulaClusterCandidate,
    LayoutRegressionReport,
    NumericPreservationBreakdown,
    NumericPreservationMetrics,
    PageLayoutRegressionMetrics,
    ProtectedAbnormalGap,
    ProtectedBlockAlignmentMetrics,
    ProtectedBlockMisalignment,
    ProtectedOrderInversion,
    ProtectedOverlapPair,
)
from backend.pdf.adapters.native_preserving_adapter import unmask_inline_protected_elements
from backend.pdf.coordinates import bbox_intersection_ratio, bbox_iou
from backend.translation.validation.validator import (
    _CITATION_BRACKET_RE,
    _MEASUREMENT_UNIT_TOKENS,
    _PLACEHOLDER_RE,
)

_NON_BODY_SEMANTIC_CATEGORIES: frozenset[str] = frozenset(
    {
        "title",
        "author",
        "affiliation",
        "email_or_contact",
        "publishing_metadata",
        "running_header",
        "running_footer",
        "abstract",
        "keywords",
        "figure",
        "table",
    }
)

_HEADING_SEMANTIC_CATEGORIES: frozenset[str] = frozenset(
    {
        "title",
        "section_heading",
        "reference_heading",
        "author",
        "affiliation",
        "running_header",
        "running_footer",
        "publishing_metadata",
    }
)

_SYNTHETIC_PLACEHOLDER_TEXTS: frozenset[str] = frozenset(
    {
        "[Vector Figure / Diagram]",
        "[Raster Image]",
        "[Table Grid]",
    }
)

_MATH_OPERATOR_RE = re.compile(r"[=<>≤≥≈≠±∑∏∫∂∇∞∈∉⊂⊆∪∩√]")
_CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]")

_SYMBOL_UNIT_TOKENS: tuple[str, ...] = (
    "°C",
    "°F",
    "℃",
    "%",
    "％",
    "°",
    "Ω",
    "μm",
    "µm",
    "μs",
    "µs",
    "μW",
    "µW",
    "μA",
    "µA",
    "μV",
    "µV",
    "μF",
    "µF",
)

_ALL_UNIT_TOKENS: tuple[str, ...] = tuple(
    sorted(
        set(_MEASUREMENT_UNIT_TOKENS) | set(_SYMBOL_UNIT_TOKENS),
        key=lambda u: (-len(u), u),
    )
)

# Preposition "in" is excluded when separated by whitespace ("5 in length"),
# and single-letter "A" requires an extra guard against English article usage ("1 A novel step").
_SPACED_UNIT_TOKENS: tuple[str, ...] = tuple(
    u for u in _ALL_UNIT_TOKENS if u not in {"in", "A"}
)

_UNIT_NORMALIZATION_MAP: dict[str, str] = {
    "％": "%",
    "℃": "°C",
    "µm": "um",
    "μm": "um",
    "µs": "us",
    "μs": "us",
    "µW": "uW",
    "μW": "uW",
    "µA": "uA",
    "μA": "uA",
    "µV": "uV",
    "μV": "uV",
    "µF": "uF",
    "μF": "uF",
}

_ALPHA_UNIT_ALTERNATION = "|".join(
    re.escape(u) for u in _ALL_UNIT_TOKENS if u[0].isalpha()
)
_SYMBOL_UNIT_ALTERNATION = "|".join(
    re.escape(u) for u in _ALL_UNIT_TOKENS if not u[0].isalpha()
)
_SPACED_ALPHA_UNIT_ALTERNATION = "|".join(
    re.escape(u) for u in _SPACED_UNIT_TOKENS if u[0].isalpha()
)

# Atomic core + optional attached/hyphenated/spaced unit suffix.
# Guards against matching inside identifiers (t10, 3D, 1st, 0x1f), multi-dot section numbers (2.4.1),
# or spaced English articles/variables ("1 A method", "1 m = 5").
_NUMBER_CORE_RE = r"(?>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_ATTACHED_UNIT_PART = (
    rf"(?:(?:{_ALPHA_UNIT_ALTERNATION})(?![A-Za-z0-9_}}])|(?:{_SYMBOL_UNIT_ALTERNATION}))"
)
_HYPHEN_OR_SPACED_ALPHA_UNIT = (
    rf"(?:(?:-\s*|[ \t\xa0]+)(?:(?:{_SPACED_ALPHA_UNIT_ALTERNATION})(?![A-Za-z0-9_}}])(?![ \t\xa0]*[=<>≤≥≈≠±*/])|A(?![A-Za-z0-9_}}])(?![ \t\xa0]+[A-Za-z])(?![ \t\xa0]*[=<>≤≥≈≠±*/])))"
)
_SPACED_SYMBOL_UNIT = rf"(?:[ \t\xa0]*(?:{_SYMBOL_UNIT_ALTERNATION}))"

_NUMBER_AND_UNIT_TOKEN_RE = re.compile(
    rf"(?<![a-zA-Z0-9_{{.{''}])"
    rf"(?P<num>{_NUMBER_CORE_RE})"
    rf"(?:"
    rf"(?P<unit_attached>{_ATTACHED_UNIT_PART})"
    rf"|(?P<unit_spaced>{_HYPHEN_OR_SPACED_ALPHA_UNIT}|{_SPACED_SYMBOL_UNIT})"
    rf"|(?!\.\d)(?![a-zA-Z0-9_}}])"
    rf")"
)


def _normalize_numeric_value(raw_num: str) -> str | None:
    no_commas = raw_num.replace(",", "")
    try:
        val = float(no_commas)
    except ValueError:
        return None
    if val.is_integer() and "e" not in no_commas.lower():
        return str(int(val))
    return f"{val:.12g}"


def _normalize_unit(raw_unit: str | None) -> str:
    if not raw_unit:
        return ""
    stripped = raw_unit.lstrip("- \t\r\n\xa0")
    return _UNIT_NORMALIZATION_MAP.get(stripped, stripped)


def extract_numeric_unit_tokens(text: str) -> list[str]:
    """Extract normalized numeric tokens with optional attached/spaced units.

    Examples:
        ``1,000`` -> ``"1000"``
        ``12,345.67`` -> ``"12345.67"``
        ``1.6 m`` / ``1.6m`` -> ``"1.6m"``
        ``0.17-m-high`` -> ``"0.17m"``
        ``2.4 GHz`` -> ``"2.4GHz"``
        ``24.5％`` / ``24.5 %`` -> ``"24.5%"``
    """
    if not text:
        return []
    cleaned = _PLACEHOLDER_RE.sub(" ", text)
    cleaned = _CITATION_BRACKET_RE.sub(" ", cleaned)
    tokens: list[str] = []
    for match in _NUMBER_AND_UNIT_TOKEN_RE.finditer(cleaned):
        norm_num = _normalize_numeric_value(match.group("num"))
        if norm_num is None:
            continue
        raw_unit = match.group("unit_attached") or match.group("unit_spaced") or ""
        norm_unit = _normalize_unit(raw_unit)
        tokens.append(f"{norm_num}{norm_unit}")
    return tokens


def _compare_numeric_counters(
    source_text: str,
    target_text: str,
) -> NumericPreservationBreakdown:
    src_tokens = extract_numeric_unit_tokens(source_text)
    tgt_tokens = extract_numeric_unit_tokens(target_text)
    src_counter = Counter(src_tokens)
    tgt_counter = Counter(tgt_tokens)

    matched = sum((src_counter & tgt_counter).values())
    src_total = sum(src_counter.values())
    tgt_total = sum(tgt_counter.values())
    ratio = round(matched / src_total, 6) if src_total > 0 else 1.0
    missing = sorted((src_counter - tgt_counter).elements())
    extra = sorted((tgt_counter - src_counter).elements())
    return NumericPreservationBreakdown(
        source_token_count=src_total,
        target_token_count=tgt_total,
        matched_token_count=matched,
        preservation_ratio=ratio,
        missing_tokens=missing,
        extra_tokens=extra,
    )


def _detect_column_intervals(
    page_blocks: list[Block],
    page_width_pt: float,
    *,
    full_width_ratio: float = 0.55,
) -> tuple[list[tuple[float, float]], list[float]]:
    """Cluster narrow body-like blocks into left-to-right column x-intervals and centers."""
    if page_width_pt <= 0:
        return [(0.0, 1.0)], [0.5]

    min_gutter_pt = max(8.0, page_width_pt * 0.015)
    min_col_sep_pt = page_width_pt * 0.22

    narrow_items: list[tuple[Block, float, float, float]] = []
    has_full_width_body_paragraph = False
    for b in page_blocks:
        w = float(b.bbox[2]) - float(b.bbox[0])
        h = float(b.bbox[3]) - float(b.bbox[1])
        if w <= 0 or h <= 0:
            continue
        if (
            b.kind == BlockKind.PARAGRAPH
            and b.semantic_category not in ("abstract", "keywords")
            and w >= page_width_pt * full_width_ratio
        ):
            has_full_width_body_paragraph = True
        if (
            w >= page_width_pt * 0.52
            or b.kind in (BlockKind.TITLE, BlockKind.HEADER_FOOTER, BlockKind.FIGURE, BlockKind.TABLE)
            or b.semantic_category in _NON_BODY_SEMANTIC_CATEGORIES
        ):
            continue
        xc = (float(b.bbox[0]) + float(b.bbox[2])) / 2.0
        narrow_items.append((b, float(b.bbox[0]), float(b.bbox[2]), xc))

    if len(narrow_items) < 2:
        return [(0.0, page_width_pt)], [page_width_pt / 2.0]

    narrow_items.sort(key=lambda it: (it[1], it[2]))
    clusters: list[list[tuple[Block, float, float, float]]] = []
    intervals: list[tuple[float, float]] = []

    for item in narrow_items:
        _, x0, x1, _ = item
        if not intervals or x0 > intervals[-1][1] + min_gutter_pt:
            intervals.append((x0, x1))
            clusters.append([item])
        else:
            prev_x0, prev_x1 = intervals[-1]
            intervals[-1] = (min(prev_x0, x0), max(prev_x1, x1))
            clusters[-1].append(item)

    if len(intervals) < 2:
        return [(0.0, page_width_pt)], [page_width_pt / 2.0]

    def _is_substantive_prose(blk: Block) -> bool:
        if blk.kind in (BlockKind.PARAGRAPH, BlockKind.HEADING, BlockKind.REFERENCE):
            return len(blk.source_text.strip()) >= 3
        if blk.kind == BlockKind.ISOLATED_FORMULA:
            return False
        return len(re.sub(r"\s+", "", blk.source_text)) >= 15

    # Filter out orphan clusters consisting only of equations/tiny labels when validating multi-column layout
    substantive_indices = [
        idx
        for idx, cl in enumerate(clusters)
        if any(_is_substantive_prose(it[0]) for it in cl)
    ]
    if len(substantive_indices) < 2:
        return [(0.0, page_width_pt)], [page_width_pt / 2.0]

    if has_full_width_body_paragraph:
        para_clusters = sum(
            1
            for idx in substantive_indices
            if any(
                it[0].kind in (BlockKind.PARAGRAPH, BlockKind.REFERENCE)
                for it in clusters[idx]
            )
        )
        if para_clusters < 2:
            return [(0.0, page_width_pt)], [page_width_pt / 2.0]

    # Merge clusters whose representative centers are closer than min_col_sep_pt
    merged_intervals: list[tuple[float, float]] = []
    merged_centers: list[float] = []
    for idx in substantive_indices:
        iv = intervals[idx]
        xs = [it[3] for it in clusters[idx]]
        center = float(statistics.median(xs))
        if not merged_intervals:
            merged_intervals.append(iv)
            merged_centers.append(center)
        else:
            gutter = iv[0] - merged_intervals[-1][1]
            if (center - merged_centers[-1]) < min_col_sep_pt or gutter < min_gutter_pt:
                merged_intervals[-1] = (
                    min(merged_intervals[-1][0], iv[0]),
                    max(merged_intervals[-1][1], iv[1]),
                )
                merged_centers[-1] = (merged_centers[-1] + center) / 2.0
            else:
                merged_intervals.append(iv)
                merged_centers.append(center)

    if len(merged_intervals) < 2:
        return [(0.0, page_width_pt)], [page_width_pt / 2.0]

    return merged_intervals, merged_centers


def _boxes_overlap_vertically(a: list[float], b: list[float], min_ratio: float = 0.35) -> bool:
    y_ov = min(a[3], b[3]) - max(a[1], b[1])
    x_ov = min(a[2], b[2]) - max(a[0], b[0])
    if x_ov <= 0.0 or y_ov <= 0.0:
        return False
    ha = max(1e-3, a[3] - a[1])
    hb = max(1e-3, b[3] - b[1])
    return (y_ov / min(ha, hb)) >= min_ratio


def _overlaps_any_box(
    box: list[float],
    targets: list[list[float]],
    *,
    min_intersection_ratio: float = 0.35,
    min_iou: float = 0.20,
) -> bool:
    for tgt in targets:
        if (
            bbox_iou(box, tgt) >= min_iou
            or bbox_intersection_ratio(box, tgt) >= min_intersection_ratio
            or bbox_intersection_ratio(tgt, box) >= min_intersection_ratio
        ):
            return True
    return False


def _close_small_vertical_gaps(active: np.ndarray, max_gap: int) -> np.ndarray:
    """Fill 1D False runs of length <= max_gap between True runs."""
    if active.size == 0 or max_gap <= 0:
        return active.copy()
    out = active.copy()
    n = len(out)
    i = 0
    while i < n:
        if out[i]:
            i += 1
            continue
        start = i
        while i < n and not out[i]:
            i += 1
        end = i
        if start > 0 and end < n and (end - start) <= max_gap:
            out[start:end] = True
    return out


def check_formula_block_count_consistency(
    page: pymupdf.Page,
    page_blocks: list[Block],
    *,
    dpi: int = 150,
    height_ratio_threshold: float = 1.45,
    ink_threshold: int = 225,
) -> FormulaBlockCountMetrics:
    """Metric 1: Compare image-detected tall formula clusters against extracted formula blocks."""
    page_w = float(page.rect.width)
    page_h = float(page.rect.height)
    scale = dpi / 72.0
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        colorspace=pymupdf.csGRAY,
        alpha=False,
    )
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    ink = gray < ink_threshold

    img_h, img_w = ink.shape
    scale_x = img_w / page_w if page_w > 0 else scale
    scale_y = img_h / page_h if page_h > 0 else scale

    extracted_formula_blocks = [
        b
        for b in page_blocks
        if b.kind == BlockKind.ISOLATED_FORMULA or b.split_role == "formula"
    ]
    formula_bboxes = [b.bbox for b in extracted_formula_blocks]

    def _mask_rect(box: list[float], margin_pt: float = 0.5) -> None:
        rx0 = max(0, int(np.floor((box[0] - margin_pt) * scale_x)))
        ry0 = max(0, int(np.floor((box[1] - margin_pt) * scale_y)))
        rx1 = min(img_w, int(np.ceil((box[2] + margin_pt) * scale_x)))
        ry1 = min(img_h, int(np.ceil((box[3] + margin_pt) * scale_y)))
        if rx1 > rx0 and ry1 > ry0:
            ink[ry0:ry1, rx0:rx1] = False

    excluded_non_formula = 0

    # 1. Mask out known non-formula blocks (figures, tables, headers/footers, frontmatter titles)
    non_formula_blocks = [
        b
        for b in page_blocks
        if b.kind in (BlockKind.FIGURE, BlockKind.TABLE, BlockKind.HEADER_FOOTER, BlockKind.TITLE)
        or b.semantic_category in (
            "figure",
            "table",
            "running_header",
            "running_footer",
            "publishing_metadata",
            "title",
            "author",
            "affiliation",
            "email_or_contact",
        )
    ]
    for b in non_formula_blocks:
        if _overlaps_any_box(b.bbox, formula_bboxes, min_intersection_ratio=0.30, min_iou=0.20):
            continue
        _mask_rect(b.bbox, margin_pt=0.5)
        excluded_non_formula += 1

    # 2. Mask out raw raster image blocks from PyMuPDF
    raw_dict = page.get_text("dict")
    raw_text_blocks = [rb for rb in raw_dict.get("blocks", []) if rb.get("type", 0) == 0]
    for rb in raw_dict.get("blocks", []):
        if rb.get("type") == 1:
            rbox = [float(v) for v in rb.get("bbox", [0, 0, 0, 0])]
            if len(rbox) == 4 and not _overlaps_any_box(
                rbox, formula_bboxes, min_intersection_ratio=0.30, min_iou=0.20
            ):
                _mask_rect(rbox, margin_pt=0.5)
                excluded_non_formula += 1

    # 3. Mask out large non-formula vector figure/table boxes even if figure blocks were omitted
    for drw in page.get_drawings():
        r = drw.get("rect")
        if r is None:
            continue
        dbox = [float(r.x0), float(r.y0), float(r.x1), float(r.y1)]
        dw, dh = dbox[2] - dbox[0], dbox[3] - dbox[1]
        if dw >= 40.0 and dh >= 22.0 and not (dw > page_w * 0.92 and dh > page_h * 0.92):
            if _overlaps_any_box(dbox, formula_bboxes, min_intersection_ratio=0.25, min_iou=0.15):
                continue
            inner_texts = []
            for rb in raw_text_blocks:
                rbox = [float(v) for v in rb.get("bbox", [0, 0, 0, 0])]
                if len(rbox) == 4 and bbox_intersection_ratio(rbox, dbox) >= 0.40:
                    for ln in rb.get("lines", []):
                        inner_texts.append(
                            "".join(str(s.get("text", "")) for s in ln.get("spans", []))
                        )
            joined_inner = " ".join(inner_texts)
            if not _MATH_OPERATOR_RE.search(joined_inner):
                _mask_rect(dbox, margin_pt=0.5)
                excluded_non_formula += 1

    # 4. Mask top/bottom running header/footer bands unless a known formula sits there
    top_band_y = page_h * 0.055
    bot_band_y = page_h * 0.945
    if not any(fb[1] < top_band_y for fb in formula_bboxes):
        py_top = min(img_h, max(0, int(np.floor(top_band_y * scale_y))))
        if py_top > 0:
            ink[:py_top, :] = False
    if not any(fb[3] > bot_band_y for fb in formula_bboxes):
        py_bot = max(0, min(img_h, int(np.ceil(bot_band_y * scale_y))))
        if py_bot < img_h:
            ink[py_bot:, :] = False

    # 5. Extract horizontal projection bands per detected column
    col_intervals, _ = _detect_column_intervals(page_blocks, page_w)
    col_slices: list[tuple[int, int]] = []
    if len(col_intervals) <= 1:
        col_slices = [(0, img_w)]
    else:
        boundaries = [0]
        for idx in range(len(col_intervals) - 1):
            mid_gutter = (col_intervals[idx][1] + col_intervals[idx + 1][0]) / 2.0
            boundaries.append(max(0, min(img_w, int(round(mid_gutter * scale_x)))))
        boundaries.append(img_w)
        for idx in range(len(boundaries) - 1):
            if boundaries[idx + 1] > boundaries[idx]:
                col_slices.append((boundaries[idx], boundaries[idx + 1]))

    max_intra_gap_px = max(1, int(round(1.0 * scale_y)))
    col_raw_bands: list[list[dict[str, Any]]] = []
    for col_idx, (sx0, sx1) in enumerate(col_slices):
        strip = ink[:, sx0:sx1]
        active_rows = strip.sum(axis=1) >= 2
        closed_rows = _close_small_vertical_gaps(active_rows, max_intra_gap_px)
        bands: list[dict[str, Any]] = []
        y = 0
        while y < img_h:
            if not closed_rows[y]:
                y += 1
                continue
            y0_px = y
            while y < img_h and closed_rows[y]:
                y += 1
            y1_px = y
            sub = strip[y0_px:y1_px, :]
            col_mask = np.any(sub, axis=0)
            nz = np.flatnonzero(col_mask)
            if nz.size == 0:
                continue
            x0_px = sx0 + int(nz[0])
            x1_px = sx0 + int(nz[-1]) + 1
            x0_pt = x0_px / scale_x
            y0_pt = y0_px / scale_y
            x1_pt = x1_px / scale_x
            y1_pt = y1_px / scale_y
            h_pt = y1_pt - y0_pt
            w_pt = x1_pt - x0_pt
            is_thin_bar = h_pt <= 3.0 and w_pt >= 8.0
            if w_pt < 3.5 and h_pt < 3.0 and not is_thin_bar:
                continue
            bands.append(
                {
                    "bbox": [x0_pt, y0_pt, x1_pt, y1_pt],
                    "h_pt": h_pt,
                    "w_pt": w_pt,
                    "col_idx": col_idx,
                    "is_bar": is_thin_bar,
                }
            )
        col_raw_bands.append(bands)

    # 6. Compute representative median body line height and normal inter-line gap
    heading_and_title_bboxes = [
        b.bbox
        for b in page_blocks
        if b.kind in (BlockKind.HEADING, BlockKind.TITLE)
        or b.semantic_category in _HEADING_SEMANTIC_CATEGORIES
    ]
    body_heights: list[float] = []
    all_normal_range_heights: list[float] = []
    for bands in col_raw_bands:
        for band in bands:
            bh = band["h_pt"]
            if 3.5 <= bh <= 18.0 and not band["is_bar"]:
                all_normal_range_heights.append(bh)
                if not _overlaps_any_box(
                    band["bbox"],
                    heading_and_title_bboxes + formula_bboxes,
                    min_intersection_ratio=0.35,
                    min_iou=0.20,
                ):
                    body_heights.append(bh)

    if len(body_heights) >= 3:
        median_line_h = float(statistics.median(body_heights))
    elif all_normal_range_heights:
        median_line_h = float(statistics.median(all_normal_range_heights))
    else:
        median_line_h = 10.0

    normal_gaps: list[float] = []
    for bands in col_raw_bands:
        for prev_b, curr_b in zip(bands[:-1], bands[1:]):
            if (
                not prev_b["is_bar"]
                and not curr_b["is_bar"]
                and prev_b["h_pt"] <= median_line_h * 1.25
                and curr_b["h_pt"] <= median_line_h * 1.25
            ):
                gap = curr_b["bbox"][1] - prev_b["bbox"][3]
                if 0.5 <= gap <= median_line_h * 1.6:
                    normal_gaps.append(gap)
    median_line_gap = float(statistics.median(normal_gaps)) if normal_gaps else median_line_h * 0.45

    # 7. Merge tightly stacked formula bands (fraction bars, limits, scripts) without merging prose paragraphs
    bar_merge_gap = max(6.5, median_line_h * 0.75)
    tight_merge_gap = max(1.2, min(2.4, median_line_gap * 0.50))

    merged_clusters: list[dict[str, Any]] = []
    for bands in col_raw_bands:
        col_clusters: list[dict[str, Any]] = []
        for band in bands:
            if not col_clusters:
                col_clusters.append(
                    {
                        "bbox": list(band["bbox"]),
                        "col_idx": band["col_idx"],
                        "has_bar": band["is_bar"],
                        "last_is_bar": band["is_bar"],
                    }
                )
                continue
            prev = col_clusters[-1]
            pb = prev["bbox"]
            cb = band["bbox"]
            v_gap = cb[1] - pb[3]
            h_overlap = min(pb[2], cb[2]) - max(pb[0], cb[0])
            horiz_near = h_overlap > 0.0 or abs(
                ((pb[0] + pb[2]) / 2.0) - ((cb[0] + cb[2]) / 2.0)
            ) <= max(18.0, min(pb[2] - pb[0], cb[2] - cb[0]) * 0.6)

            merge_for_bar = (prev["last_is_bar"] or band["is_bar"]) and v_gap <= bar_merge_gap and horiz_near
            merge_for_tight = v_gap <= tight_merge_gap and h_overlap > 0.0
            if merge_for_bar or merge_for_tight:
                prev["bbox"] = [
                    min(pb[0], cb[0]),
                    min(pb[1], cb[1]),
                    max(pb[2], cb[2]),
                    max(pb[3], cb[3]),
                ]
                prev["has_bar"] = prev["has_bar"] or band["is_bar"]
                prev["last_is_bar"] = band["is_bar"]
            else:
                col_clusters.append(
                    {
                        "bbox": list(band["bbox"]),
                        "col_idx": band["col_idx"],
                        "has_bar": band["is_bar"],
                        "last_is_bar": band["is_bar"],
                    }
                )
        merged_clusters.extend(col_clusters)

    # Merge cross-gutter clusters belonging to a single spanning formula
    if len(col_slices) > 1 and len(merged_clusters) > 1:
        gutter_xs = [s[0] / scale_x for s in col_slices[1:]]
        deduped: list[dict[str, Any]] = []
        used_idx: set[int] = set()
        for i, cl_a in enumerate(merged_clusters):
            if i in used_idx:
                continue
            cur = dict(cl_a)
            cur["bbox"] = list(cl_a["bbox"])
            for j in range(i + 1, len(merged_clusters)):
                if j in used_idx:
                    continue
                cl_b = merged_clusters[j]
                if cl_a["col_idx"] == cl_b["col_idx"]:
                    continue
                ba, bb = cur["bbox"], cl_b["bbox"]
                y_ov = min(ba[3], bb[3]) - max(ba[1], bb[1])
                y_un = max(ba[3], bb[3]) - min(ba[1], bb[1])
                if y_un > 0 and (y_ov / y_un) >= 0.50:
                    x_gap = max(ba[0], bb[0]) - min(ba[2], bb[2])
                    touches_gutter = any(
                        min(ba[2], bb[2]) - 4.0 <= gx <= max(ba[0], bb[0]) + 4.0
                        for gx in gutter_xs
                    )
                    if x_gap <= 8.0 and touches_gutter:
                        cur["bbox"] = [
                            min(ba[0], bb[0]),
                            min(ba[1], bb[1]),
                            max(ba[2], bb[2]),
                            max(ba[3], bb[3]),
                        ]
                        used_idx.add(j)
            deduped.append(cur)
        merged_clusters = deduped

    # 8. Filter tall clusters with heading/title & figure/table text-attribute guards
    min_tall_height = max(12.0, median_line_h * height_ratio_threshold)
    image_clusters: list[FormulaClusterCandidate] = []
    excluded_headings = 0
    figure_table_bboxes = [
        b.bbox for b in page_blocks if b.kind in (BlockKind.FIGURE, BlockKind.TABLE)
    ]

    for cl in merged_clusters:
        cb = cl["bbox"]
        h_pt = cb[3] - cb[1]
        w_pt = cb[2] - cb[0]
        if h_pt < min_tall_height or w_pt < 6.0:
            continue

        overlaps_known_formula = _overlaps_any_box(
            cb, formula_bboxes, min_intersection_ratio=0.30, min_iou=0.15
        )
        if not overlaps_known_formula:
            is_heading_overlap = _overlaps_any_box(
                cb, heading_and_title_bboxes, min_intersection_ratio=0.35, min_iou=0.20
            ) or any(_boxes_overlap_vertically(cb, hb, min_ratio=0.60) for hb in heading_and_title_bboxes)
            if is_heading_overlap:
                excluded_headings += 1
                continue

            # Guard against large single-line title/heading misclassified as PARAGRAPH
            large_non_math_heading = False
            for blk in page_blocks:
                if blk.kind in (BlockKind.ISOLATED_FORMULA, BlockKind.FIGURE, BlockKind.TABLE):
                    continue
                if (
                    _overlaps_any_box(cb, [blk.bbox], min_intersection_ratio=0.35, min_iou=0.20)
                    and blk.font_size_pt >= median_line_h * 1.30
                    and not _MATH_OPERATOR_RE.search(blk.source_text)
                    and len(blk.source_text.strip()) < 140
                ):
                    large_non_math_heading = True
                    break
            if large_non_math_heading:
                excluded_headings += 1
                continue

            if _overlaps_any_box(
                cb, figure_table_bboxes, min_intersection_ratio=0.35, min_iou=0.20
            ):
                excluded_non_formula += 1
                continue

            if not cl.get("has_bar"):
                clip_dict = page.get_text("dict", clip=pymupdf.Rect(cb[0], cb[1], cb[2], cb[3]))
                clip_lines = [
                    ln
                    for rb in clip_dict.get("blocks", [])
                    if rb.get("type", 0) == 0
                    for ln in rb.get("lines", [])
                ]
                clip_text = " ".join(
                    "".join(str(s.get("text", "")) for s in ln.get("spans", []))
                    for ln in clip_lines
                ).strip()
                clip_fonts = [
                    str(s.get("font", "")).lower()
                    for ln in clip_lines
                    for s in ln.get("spans", [])
                    if str(s.get("text", "")).strip()
                ]
                has_math_font = any(
                    any(k in fn for k in ("cmmi", "cmsy", "cmex", "math", "txsys", "txexs", "txmia"))
                    for fn in clip_fonts
                )
                has_eq_num = bool(re.search(r"\(\s*\d+(?:\.\d+)*[a-z]?\s*\)\s*$", clip_text))
                alpha_words = re.findall(r"[A-Za-z]{4,}", clip_text)
                if (
                    len(clip_lines) >= 2
                    and len(alpha_words) >= 4
                    and not _MATH_OPERATOR_RE.search(clip_text)
                    and not has_math_font
                    and not has_eq_num
                ):
                    excluded_non_formula += 1
                    continue

        image_clusters.append(
            FormulaClusterCandidate(
                bbox=[round(v, 2) for v in cb],
                height_pt=round(h_pt, 2),
                column_index=int(cl["col_idx"]),
                height_ratio_to_median=round(h_pt / max(median_line_h, 1e-3), 3),
            )
        )

    img_count = len(image_clusters)
    ext_count = len(extracted_formula_blocks)
    count_diff = img_count - ext_count
    return FormulaBlockCountMetrics(
        dpi=dpi,
        median_line_height_pt=round(median_line_h, 2),
        height_ratio_threshold=height_ratio_threshold,
        image_formula_cluster_count=img_count,
        extracted_formula_block_count=ext_count,
        count_diff=count_diff,
        needs_visual_review=(count_diff != 0),
        image_clusters=image_clusters,
        extracted_formula_block_ids=[b.id for b in extracted_formula_blocks],
        excluded_heading_clusters=excluded_headings,
        excluded_non_formula_regions=excluded_non_formula,
    )


def check_numeric_and_unit_preservation(
    raw_page_text: str,
    extracted_blocks_or_text: list[Block] | str,
    *,
    output_text: str | None = None,
) -> NumericPreservationMetrics:
    """Metric 2: Compare multiplicity-aware numeric & unit tokens across extraction and translation."""
    if isinstance(extracted_blocks_or_text, str):
        page_extraction = _compare_numeric_counters(raw_page_text, extracted_blocks_or_text)
        body_masking = page_extraction.model_copy(deep=True)
        body_output: NumericPreservationBreakdown | None = None
        page_out = (
            _compare_numeric_counters(raw_page_text, output_text)
            if output_text is not None
            else None
        )
        primary = page_out if page_out is not None else page_extraction
    else:
        blocks = list(extracted_blocks_or_text)
        extracted_text_parts = [
            b.source_text
            for b in blocks
            if b.source_text and b.source_text.strip() not in _SYNTHETIC_PLACEHOLDER_TEXTS
        ]
        page_extraction = _compare_numeric_counters(
            raw_page_text, " ".join(extracted_text_parts)
        )

        body_blocks = [
            b
            for b in blocks
            if b.kind in (BlockKind.PARAGRAPH, BlockKind.CAPTION, BlockKind.HEADING)
            and b.status != BlockStatus.PROTECTED_SKIP
        ]
        body_src = " ".join(b.source_text for b in body_blocks)
        body_unmasked_masked = " ".join(
            unmask_inline_protected_elements(
                b.masked_source_text or b.source_text, b.protected_items
            )
            for b in body_blocks
        )
        body_masking = _compare_numeric_counters(body_src, body_unmasked_masked)

        has_translations = any(bool(b.effective_translation) for b in body_blocks)
        if has_translations:
            body_out_str = " ".join(
                unmask_inline_protected_elements(
                    b.effective_translation, b.protected_items
                )
                if b.effective_translation
                else b.source_text
                for b in body_blocks
            )
            body_output = _compare_numeric_counters(body_src, body_out_str)
        else:
            body_output = None

        page_out = (
            _compare_numeric_counters(raw_page_text, output_text)
            if output_text is not None
            else None
        )
        primary = page_out if page_out is not None else page_extraction

    needs_review = (
        primary.preservation_ratio < 1.0
        or page_extraction.preservation_ratio < 1.0
        or body_masking.preservation_ratio < 1.0
        or (body_output is not None and body_output.preservation_ratio < 1.0)
    )
    return NumericPreservationMetrics(
        source_token_count=primary.source_token_count,
        target_token_count=primary.target_token_count,
        matched_token_count=primary.matched_token_count,
        preservation_ratio=primary.preservation_ratio,
        missing_tokens=list(primary.missing_tokens),
        extra_tokens=list(primary.extra_tokens),
        page_extraction=page_extraction,
        body_blocks_masking=body_masking,
        body_blocks_output=body_output,
        page_output=page_out,
        needs_visual_review=needs_review,
    )


def check_column_order_monotonicity(
    page_blocks: list[Block],
    page_width_pt: float,
    *,
    full_width_ratio: float = 0.55,
) -> ColumnOrderMonotonicityMetrics:
    """Metric 3: Check non-decreasing column indices along reading order to catch column interleaving."""
    ordered = sorted(page_blocks, key=lambda b: b.reading_order)
    col_intervals, col_centers = _detect_column_intervals(
        ordered, page_width_pt, full_width_ratio=full_width_ratio
    )
    detected_col_count = len(col_intervals)
    gutter_centers = [
        (col_intervals[i][1] + col_intervals[i + 1][0]) / 2.0
        for i in range(len(col_intervals) - 1)
    ]

    evaluated: list[tuple[Block, int]] = []
    excluded_full_width = 0

    for blk in ordered:
        w = float(blk.bbox[2]) - float(blk.bbox[0])
        is_excluded_kind = blk.kind in (
            BlockKind.TITLE,
            BlockKind.HEADER_FOOTER,
            BlockKind.FIGURE,
            BlockKind.TABLE,
        ) or blk.semantic_category in _NON_BODY_SEMANTIC_CATEGORIES
        spans_gutter = detected_col_count >= 2 and any(
            float(blk.bbox[0]) < gc - 8.0 and float(blk.bbox[2]) > gc + 8.0
            for gc in gutter_centers
        )
        if is_excluded_kind or w >= page_width_pt * full_width_ratio or spans_gutter:
            excluded_full_width += 1
            continue

        x_center = (float(blk.bbox[0]) + float(blk.bbox[2])) / 2.0
        col_idx = min(
            range(detected_col_count),
            key=lambda idx: abs(x_center - col_centers[idx]),
        )
        evaluated.append((blk, col_idx))

    inversions: list[ColumnOrderInversion] = []
    for (prev_blk, prev_col), (curr_blk, curr_col) in zip(evaluated[:-1], evaluated[1:]):
        if curr_col < prev_col:
            inversions.append(
                ColumnOrderInversion(
                    prev_block_id=prev_blk.id,
                    curr_block_id=curr_blk.id,
                    prev_col=prev_col,
                    curr_col=curr_col,
                    bbox=list(curr_blk.bbox),
                )
            )

    return ColumnOrderMonotonicityMetrics(
        detected_column_count=detected_col_count,
        is_monotonic=(len(inversions) == 0),
        inversion_count=len(inversions),
        inversions=inversions,
        evaluated_block_count=len(evaluated),
        excluded_full_width_count=excluded_full_width,
        column_centers_pt=[round(c, 2) for c in col_centers],
        column_sequence=[c for _, c in evaluated],
    )


def _extract_normalized_output_elements(
    out_page: pymupdf.Page,
    *,
    is_dual: bool,
    orig_page_w: float,
) -> dict[str, Any]:
    """Extract spans, drawings, and images from mono or right-half dual output page."""
    out_w = float(out_page.rect.width)
    out_h = float(out_page.rect.height)
    x_offset = orig_page_w if (is_dual and out_w >= orig_page_w * 1.85) else 0.0

    def _norm_box(raw_box: list[float] | tuple[float, ...]) -> list[float] | None:
        x0, y0, x1, y1 = (float(v) for v in raw_box[:4])
        if x_offset > 0.0:
            if x1 <= x_offset + 1.0:
                return None
            x0 = max(0.0, x0 - x_offset)
            x1 = x1 - x_offset
        if x1 <= x0 or y1 <= y0:
            return None
        return [x0, y0, x1, y1]

    spans: list[dict[str, Any]] = []
    raw_dict = out_page.get_text("dict")
    for rb in raw_dict.get("blocks", []):
        if rb.get("type", 0) != 0:
            continue
        for ln in rb.get("lines", []):
            for sp in ln.get("spans", []):
                txt = str(sp.get("text", ""))
                if not txt.strip():
                    continue
                sbox = _norm_box(sp.get("bbox", [0, 0, 0, 0]))
                if sbox is None:
                    continue
                spans.append({"bbox": sbox, "text": txt})

    drawings: list[list[float]] = []
    for drw in out_page.get_drawings():
        r = drw.get("rect")
        if r is None:
            continue
        dbox = _norm_box([r.x0, r.y0, r.x1, r.y1])
        if dbox is None:
            continue
        dw, dh = dbox[2] - dbox[0], dbox[3] - dbox[1]
        if dw > orig_page_w * 0.92 and dh > out_h * 0.92:
            continue
        drawings.append(dbox)

    images: list[list[float]] = []
    for info in out_page.get_image_info(xrefs=False):
        ibox = _norm_box(info.get("bbox", [0, 0, 0, 0]))
        if ibox is not None:
            images.append(ibox)

    return {"spans": spans, "drawings": drawings, "images": images}


def _compact_alnum_chars(text: str) -> set[str]:
    return {
        ch
        for ch in text
        if not ch.isspace() and ch not in ("\x1f", "\xa0")
    }


def _union_boxes(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    ]


def _locate_protected_bbox_in_output(
    prot_blk: Block,
    elements: dict[str, Any],
) -> tuple[list[float] | None, set[int]]:
    """Locate the output bbox of a protected block using span-level matching (immune to column block merging)."""
    orig = prot_blk.bbox
    src_stripped = prot_blk.source_text.strip()
    spans: list[dict[str, Any]] = elements["spans"]
    drawings: list[list[float]] = elements["drawings"]
    images: list[list[float]] = elements["images"]

    if src_stripped in _SYNTHETIC_PLACEHOLDER_TEXTS:
        candidates = images if src_stripped == "[Raster Image]" else (drawings + images)
        # Stage 1: strict in-place match (prevents absorbing nearby arrows/borders outside orig)
        win_strict = [orig[0] - 2.0, orig[1] - 2.0, orig[2] + 2.0, orig[3] + 2.0]
        strict_hits = [
            c
            for c in candidates
            if (bbox_intersection_ratio(c, orig) >= 0.45 or bbox_iou(c, orig) >= 0.50)
            and c[0] >= win_strict[0] - 2.0
            and c[2] <= win_strict[2] + 2.0
            and c[1] >= win_strict[1] - 2.0
            and c[3] <= win_strict[3] + 2.0
            and (c[2] - c[0]) <= (orig[2] - orig[0]) * 1.4 + 6.0
            and (c[3] - c[1]) <= (orig[3] - orig[1]) * 1.4 + 6.0
        ]
        if strict_hits:
            return _union_boxes(strict_hits), set()

        # Stage 2: widened window fallback for shifted figure/image detection
        win_wide = [orig[0] - 15.0, orig[1] - 48.0, orig[2] + 15.0, orig[3] + 48.0]
        wide_hits = [
            c
            for c in candidates
            if (bbox_intersection_ratio(c, win_wide) >= 0.30 or bbox_iou(c, orig) >= 0.15)
            and (c[2] - c[0]) <= (orig[2] - orig[0]) * 1.9 + 12.0
            and (c[3] - c[1]) <= (orig[3] - orig[1]) * 1.9 + 12.0
        ]
        if wide_hits:
            return _union_boxes(wide_hits), set()
        return None, set()

    src_has_cjk = bool(_CJK_CHAR_RE.search(src_stripped))
    src_chars = _compact_alnum_chars(src_stripped)
    src_tokens = [
        tok for tok in re.findall(r"[A-Za-z0-9\u0370-\u03ff\U0001d400-\U0001d7ff]+", src_stripped)
        if len(tok) >= 2
    ]

    def _span_matches(sp_text: str) -> bool:
        st = sp_text.strip()
        if not st:
            return False
        if not src_has_cjk and _CJK_CHAR_RE.search(st):
            return False
        sp_chars = _compact_alnum_chars(st)
        if not sp_chars:
            return False
        sp_word_tokens = [
            tok for tok in re.findall(r"[A-Za-z0-9\u0370-\u03ff\U0001d400-\U0001d7ff]+", st)
            if len(tok) >= 2
        ]
        if src_tokens and sp_word_tokens:
            if not all(
                any(wt in st_tok or st_tok in wt for st_tok in src_tokens)
                for wt in sp_word_tokens
            ):
                return False
            return True
        if src_tokens and any(tok in st for tok in src_tokens):
            return True
        if not src_chars:
            return False
        overlap = len(sp_chars & src_chars)
        return overlap >= max(1, int(len(sp_chars) * 0.65))

    for pad_x, pad_y in ((2.5, 2.5), (12.0, 48.0), (24.0, 400.0)):
        win = [orig[0] - pad_x, orig[1] - pad_y, orig[2] + pad_x, orig[3] + pad_y]
        matched_indices: set[int] = set()
        matched_boxes: list[list[float]] = []
        for idx, sp in enumerate(spans):
            sbox = sp["bbox"]
            if (
                sbox[0] >= win[0]
                and sbox[2] <= win[2]
                and sbox[1] >= win[1]
                and sbox[3] <= win[3]
                and _span_matches(sp["text"])
            ):
                matched_indices.add(idx)
                matched_boxes.append(sbox)

        if matched_boxes:
            union_b = _union_boxes(matched_boxes)
            assert union_b is not None
            # Include thin horizontal fraction bars inside or immediately between matched spans
            extra_boxes = list(matched_boxes)
            for dbox in drawings:
                dh = dbox[3] - dbox[1]
                dw = dbox[2] - dbox[0]
                if (
                    dh <= 3.0
                    and dw >= 8.0
                    and dbox[0] >= union_b[0] - 8.0
                    and dbox[2] <= union_b[2] + 8.0
                    and dbox[1] >= union_b[1] - 4.0
                    and dbox[3] <= union_b[3] + 4.0
                ):
                    extra_boxes.append(dbox)
            return _union_boxes(extra_boxes), matched_indices

    return None, set()


def _same_column_blocks(a: Block, b: Block) -> bool:
    x_ov = min(float(a.bbox[2]), float(b.bbox[2])) - max(float(a.bbox[0]), float(b.bbox[0]))
    if x_ov > 4.0:
        return True
    if a.column_index > 0 and b.column_index > 0 and a.column_index == b.column_index:
        return True
    return False


def _find_adjacent_body_blocks(
    prot_blk: Block,
    page_blocks: list[Block],
    rendered_bboxes: dict[str, list[float]] | None,
) -> tuple[Block | None, Block | None]:
    """Find immediate above and below body/prose blocks in the same column as `prot_blk`."""
    same_col_all = [
        b
        for b in page_blocks
        if b.id != prot_blk.id and _same_column_blocks(b, prot_blk)
    ]

    def _is_neighbor_body(blk: Block) -> bool:
        if blk.kind not in (BlockKind.PARAGRAPH, BlockKind.CAPTION, BlockKind.HEADING):
            return False
        if rendered_bboxes is not None and blk.id in rendered_bboxes:
            return blk.status != BlockStatus.PROTECTED_SKIP
        if blk.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED):
            return True
        # When parsing directly from source PDF (e.g., CLI without serialized translations),
        # treat non-protected body blocks (PENDING) as the adjacent prose anchors.
        return blk.status == BlockStatus.PENDING

    candidates = [b for b in same_col_all if _is_neighbor_body(b)]
    above_cands = [b for b in candidates if float(b.bbox[3]) <= float(prot_blk.bbox[1]) + 2.0]
    below_cands = [b for b in candidates if float(b.bbox[1]) >= float(prot_blk.bbox[3]) - 2.0]

    above_blk: Block | None = None
    if above_cands:
        cand = max(above_cands, key=lambda b: float(b.bbox[3]))
        intervening = any(
            float(o.bbox[1]) >= float(cand.bbox[3]) - 1.0
            and float(o.bbox[3]) <= float(prot_blk.bbox[1]) + 1.0
            for o in same_col_all
            if o.id not in (cand.id, prot_blk.id)
        )
        if not intervening:
            above_blk = cand

    below_blk: Block | None = None
    if below_cands:
        cand = min(below_cands, key=lambda b: float(b.bbox[1]))
        intervening = any(
            float(o.bbox[1]) >= float(prot_blk.bbox[3]) - 1.0
            and float(o.bbox[3]) <= float(cand.bbox[1]) + 1.0
            for o in same_col_all
            if o.id not in (cand.id, prot_blk.id)
        )
        if not intervening:
            below_blk = cand

    return above_blk, below_blk


def _locate_neighbor_bbox_in_output(
    nb_blk: Block,
    prot_blk: Block,
    prot_out_bbox: list[float] | None,
    relation: Literal["above", "below"],
    same_col_blocks: list[Block],
    elements: dict[str, Any],
    claimed_protected_span_indices: set[int],
) -> list[float] | None:
    """Locate the rendered bbox of an adjacent body block in the output PDF page."""
    spans: list[dict[str, Any]] = elements["spans"]
    nb_box = nb_blk.bbox
    p_ref = prot_out_bbox if prot_out_bbox is not None else prot_blk.bbox

    # Determine bounding search window without leaking into non-adjacent sibling blocks
    x_min = min(float(nb_box[0]), float(p_ref[0])) - 12.0
    x_max = max(float(nb_box[2]), float(p_ref[2])) + 12.0

    other_blocks = [b for b in same_col_blocks if b.id not in (nb_blk.id, prot_blk.id)]
    if relation == "above":
        prev_above = [b for b in other_blocks if float(b.bbox[3]) <= float(nb_box[1]) + 2.0]
        y_min = (
            max(float(b.bbox[3]) for b in prev_above) + 0.5
            if prev_above
            else float(nb_box[1]) - 16.0
        )
        y_min = min(y_min, float(nb_box[1]) - 4.0)
        y_max = max(float(nb_box[3]), float(p_ref[3])) + 18.0
    else:
        next_below = [b for b in other_blocks if float(b.bbox[1]) >= float(nb_box[3]) - 2.0]
        y_min = min(float(nb_box[1]), float(p_ref[1])) - 18.0
        y_max = (
            min(float(b.bbox[1]) for b in next_below) - 0.5
            if next_below
            else float(nb_box[3]) + 24.0
        )
        y_max = max(y_max, float(nb_box[3]) + 8.0)

    eff_text = nb_blk.effective_translation
    expected_str = (
        unmask_inline_protected_elements(eff_text, nb_blk.protected_items)
        if eff_text
        else None
    )
    expected_chars = _compact_alnum_chars(expected_str) if expected_str else set()
    source_chars = _compact_alnum_chars(nb_blk.source_text)

    prot_mid_y = (float(p_ref[1]) + float(p_ref[3])) / 2.0
    matched_boxes: list[list[float]] = []

    for idx, sp in enumerate(spans):
        if idx in claimed_protected_span_indices:
            continue
        sb = sp["bbox"]
        scx = (sb[0] + sb[2]) / 2.0
        scy = (sb[1] + sb[3]) / 2.0
        if not (x_min <= scx <= x_max and y_min <= scy <= y_max):
            continue

        # Exclude spans whose center clearly belongs to another block (beyond protected block)
        if relation == "above" and scy > float(p_ref[3]) + 2.0:
            continue
        if relation == "below" and scy < float(p_ref[1]) - 2.0:
            continue

        # Exclude spans that clearly belong to an unrelated neighbor further away
        belongs_to_other = False
        for ob in other_blocks:
            ob_b = ob.bbox
            if (
                ob_b[0] - 4.0 <= scx <= ob_b[2] + 4.0
                and ob_b[1] - 1.5 <= scy <= ob_b[3] + 1.5
                and not (nb_box[1] - 1.5 <= scy <= nb_box[3] + 1.5)
            ):
                belongs_to_other = True
                break
        if belongs_to_other:
            continue

        st = sp["text"]
        if expected_chars:
            sp_chars = _compact_alnum_chars(st)
            if sp_chars and (len(sp_chars & expected_chars) > 0 or len(sp_chars & source_chars) > 0):
                matched_boxes.append(sb)
        else:
            # Without explicit translated text, filter using vertical zone around nb_box / seam
            if relation == "above":
                if scy <= prot_mid_y or float(nb_box[1]) - 4.0 <= scy <= float(p_ref[1]) + 12.0:
                    matched_boxes.append(sb)
            else:
                if scy >= prot_mid_y or float(p_ref[3]) - 12.0 <= scy <= float(nb_box[3]) + 12.0:
                    matched_boxes.append(sb)

    return _union_boxes(matched_boxes)


def _evaluate_alignment_for_target(
    *,
    target_name: Literal["mono", "dual", "provided"],
    protected_blocks: list[Block],
    page_blocks: list[Block],
    out_page: pymupdf.Page | None,
    orig_elements: dict[str, Any] | None,
    orig_page_w: float,
    rendered_bboxes: dict[str, list[float]] | None,
    position_tolerance_pt: float,
    size_tolerance_pt: float,
    min_iou: float,
    overlap_tolerance_pt: float,
    max_below_gap_increase_pt: float,
    max_above_gap_increase_pt: float,
) -> tuple[
    list[ProtectedBlockMisalignment],
    list[ProtectedOverlapPair],
    list[ProtectedOrderInversion],
    list[ProtectedAbnormalGap],
]:
    misaligned: list[ProtectedBlockMisalignment] = []
    overlaps: list[ProtectedOverlapPair] = []
    inversions: list[ProtectedOrderInversion] = []
    gaps: list[ProtectedAbnormalGap] = []

    elements = (
        _extract_normalized_output_elements(
            out_page,
            is_dual=(target_name == "dual"),
            orig_page_w=orig_page_w,
        )
        if out_page is not None
        else None
    )

    # First locate all protected blocks so their spans are excluded when locating neighbors
    prot_orig_ref_map: dict[str, list[float]] = {}
    prot_out_map: dict[str, list[float] | None] = {}
    claimed_span_indices: set[int] = set()
    for pb in protected_blocks:
        ref_box = pb.bbox
        if rendered_bboxes is None and orig_elements is not None:
            orig_located, _ = _locate_protected_bbox_in_output(pb, orig_elements)
            if orig_located is not None and bbox_iou(orig_located, pb.bbox) > 0.05:
                ref_box = orig_located
        prot_orig_ref_map[pb.id] = ref_box

        if rendered_bboxes is not None:
            prot_out_map[pb.id] = (
                [float(v) for v in rendered_bboxes[pb.id]]
                if pb.id in rendered_bboxes
                else None
            )
        elif elements is not None:
            out_box, span_ids = _locate_protected_bbox_in_output(pb, elements)
            prot_out_map[pb.id] = out_box
            claimed_span_indices.update(span_ids)
        else:
            prot_out_map[pb.id] = None

    for pb in protected_blocks:
        orig_b = prot_orig_ref_map.get(pb.id, pb.bbox)
        out_b = prot_out_map.get(pb.id)
        if out_b is None:
            misaligned.append(
                ProtectedBlockMisalignment(
                    block_id=pb.id,
                    target=target_name,
                    orig_bbox=list(orig_b),
                    output_bbox=None,
                    reason="出力PDF上で非翻訳保護ブロックが検出できませんでした。",
                )
            )
            continue

        orig_cx = (orig_b[0] + orig_b[2]) / 2.0
        orig_cy = (orig_b[1] + orig_b[3]) / 2.0
        out_cx = (out_b[0] + out_b[2]) / 2.0
        out_cy = (out_b[1] + out_b[3]) / 2.0
        dx = out_cx - orig_cx
        dy = out_cy - orig_cy
        dw = (out_b[2] - out_b[0]) - (orig_b[2] - orig_b[0])
        dh = (out_b[3] - out_b[1]) - (orig_b[3] - orig_b[1])
        iou_val = bbox_iou(orig_b, out_b)

        reasons: list[str] = []
        if abs(dx) > position_tolerance_pt:
            reasons.append(f"dx={dx:+.2f}pt")
        if abs(dy) > position_tolerance_pt:
            reasons.append(f"dy={dy:+.2f}pt")
        if abs(dw) > size_tolerance_pt:
            reasons.append(f"dw={dw:+.2f}pt")
        if abs(dh) > size_tolerance_pt:
            reasons.append(f"dh={dh:+.2f}pt")
        if iou_val < min_iou:
            reasons.append(f"iou={iou_val:.3f}<{min_iou}")

        if reasons:
            misaligned.append(
                ProtectedBlockMisalignment(
                    block_id=pb.id,
                    target=target_name,
                    orig_bbox=list(orig_b),
                    output_bbox=list(out_b),
                    dx=round(dx, 2),
                    dy=round(dy, 2),
                    dw=round(dw, 2),
                    dh=round(dh, 2),
                    iou=round(iou_val, 4),
                    reason=", ".join(reasons),
                )
            )

        above_blk, below_blk = _find_adjacent_body_blocks(pb, page_blocks, rendered_bboxes)
        same_col_all = [
            b for b in page_blocks if b.id != pb.id and _same_column_blocks(b, pb)
        ]

        neighbors: list[tuple[Literal["above", "below"], Block | None]] = [
            ("above", above_blk),
            ("below", below_blk),
        ]
        for rel, nb in neighbors:
            if nb is None:
                continue
            if rendered_bboxes is not None:
                nb_out = (
                    [float(v) for v in rendered_bboxes[nb.id]]
                    if nb.id in rendered_bboxes
                    else None
                )
            elif elements is not None:
                nb_out = _locate_neighbor_bbox_in_output(
                    nb_blk=nb,
                    prot_blk=pb,
                    prot_out_bbox=out_b,
                    relation=rel,
                    same_col_blocks=same_col_all,
                    elements=elements,
                    claimed_protected_span_indices=claimed_span_indices,
                )
            else:
                nb_out = None

            if nb_out is None:
                continue

            # 1. Overlap check
            x_ov = min(out_b[2], nb_out[2]) - max(out_b[0], nb_out[0])
            y_ov = min(out_b[3], nb_out[3]) - max(out_b[1], nb_out[1])
            if x_ov > 1.0 and y_ov > overlap_tolerance_pt:
                overlaps.append(
                    ProtectedOverlapPair(
                        protected_block_id=pb.id,
                        neighbor_block_id=nb.id,
                        relation=rel,
                        target=target_name,
                        overlap_area_pt2=round(x_ov * y_ov, 2),
                        vertical_overlap_pt=round(y_ov, 2),
                        protected_bbox=list(out_b),
                        neighbor_bbox=list(nb_out),
                    )
                )

            # 2. Order inversion check
            nb_out_cy = (nb_out[1] + nb_out[3]) / 2.0
            if rel == "above":
                orig_gap = max(0.0, float(orig_b[1]) - float(nb.bbox[3]))
                out_gap = float(out_b[1]) - float(nb_out[3])
                if nb_out_cy >= out_cy:
                    inversions.append(
                        ProtectedOrderInversion(
                            protected_block_id=pb.id,
                            neighbor_block_id=nb.id,
                            expected_relation="above",
                            target=target_name,
                            actual_gap_pt=round(out_gap, 2),
                            protected_bbox=list(out_b),
                            neighbor_bbox=list(nb_out),
                        )
                    )
                allowed_above_gap = orig_gap + max(
                    max_above_gap_increase_pt,
                    (float(nb.bbox[3]) - float(nb.bbox[1])) * 0.75,
                )
                if out_gap > allowed_above_gap:
                    gaps.append(
                        ProtectedAbnormalGap(
                            protected_block_id=pb.id,
                            neighbor_block_id=nb.id,
                            relation="above",
                            target=target_name,
                            orig_gap_pt=round(orig_gap, 2),
                            output_gap_pt=round(out_gap, 2),
                            gap_delta_pt=round(out_gap - orig_gap, 2),
                            protected_bbox=list(out_b),
                            neighbor_bbox=list(nb_out),
                        )
                    )
            else:
                orig_gap = max(0.0, float(nb.bbox[1]) - float(orig_b[3]))
                out_gap = float(nb_out[1]) - float(out_b[3])
                if nb_out_cy <= out_cy:
                    inversions.append(
                        ProtectedOrderInversion(
                            protected_block_id=pb.id,
                            neighbor_block_id=nb.id,
                            expected_relation="below",
                            target=target_name,
                            actual_gap_pt=round(out_gap, 2),
                            protected_bbox=list(out_b),
                            neighbor_bbox=list(nb_out),
                        )
                    )
                allowed_below_gap = orig_gap + max(
                    max_below_gap_increase_pt,
                    orig_gap * 1.5,
                )
                if out_gap > allowed_below_gap:
                    gaps.append(
                        ProtectedAbnormalGap(
                            protected_block_id=pb.id,
                            neighbor_block_id=nb.id,
                            relation="below",
                            target=target_name,
                            orig_gap_pt=round(orig_gap, 2),
                            output_gap_pt=round(out_gap, 2),
                            gap_delta_pt=round(out_gap - orig_gap, 2),
                            protected_bbox=list(out_b),
                            neighbor_bbox=list(nb_out),
                        )
                    )

    return misaligned, overlaps, inversions, gaps


def check_protected_block_alignment(
    page_blocks: list[Block],
    *,
    orig_page: pymupdf.Page | None = None,
    mono_page: pymupdf.Page | None = None,
    dual_page: pymupdf.Page | None = None,
    rendered_bboxes: dict[str, list[float]] | None = None,
    position_tolerance_pt: float = 4.0,
    size_tolerance_pt: float = 6.0,
    min_iou: float = 0.70,
    overlap_tolerance_pt: float = 2.5,
    max_below_gap_increase_pt: float = 20.0,
    max_above_gap_increase_pt: float = 36.0,
) -> ProtectedBlockAlignmentMetrics:
    """Metric 4: Verify coordinate preservation and adjacent body contact for protected blocks."""
    protected_blocks = [
        b
        for b in page_blocks
        if b.split_role == "formula"
        or (
            b.status == BlockStatus.PROTECTED_SKIP
            and (
                b.kind in (BlockKind.ISOLATED_FORMULA, BlockKind.FIGURE, BlockKind.TABLE)
                or b.protection_source == "manual"
            )
        )
    ]
    checked_count = len(protected_blocks)
    if checked_count == 0 or (
        mono_page is None and dual_page is None and rendered_bboxes is None
    ):
        targets_present: list[str] = []
        if mono_page is not None:
            targets_present.append("mono")
        if dual_page is not None:
            targets_present.append("dual")
        if rendered_bboxes is not None:
            targets_present.append("provided")
        return ProtectedBlockAlignmentMetrics(
            checked_protected_blocks=checked_count,
            all_aligned=True,
            checked_targets=targets_present,
        )

    orig_page_w = (
        float(orig_page.rect.width)
        if orig_page is not None
        else (
            float(mono_page.rect.width)
            if mono_page is not None
            else (
                max((float(b.bbox[2]) for b in page_blocks), default=595.0) + 36.0
            )
        )
    )

    targets: list[tuple[Literal["mono", "dual", "provided"], pymupdf.Page | None, dict[str, list[float]] | None]] = []
    if mono_page is not None:
        targets.append(("mono", mono_page, None))
    if dual_page is not None:
        targets.append(("dual", dual_page, None))
    if rendered_bboxes is not None:
        targets.append(("provided", None, rendered_bboxes))

    all_misaligned: list[ProtectedBlockMisalignment] = []
    all_overlaps: list[ProtectedOverlapPair] = []
    all_inversions: list[ProtectedOrderInversion] = []
    all_gaps: list[ProtectedAbnormalGap] = []
    checked_targets: list[str] = []

    orig_elements = (
        _extract_normalized_output_elements(
            orig_page,
            is_dual=False,
            orig_page_w=orig_page_w,
        )
        if orig_page is not None and rendered_bboxes is None
        else None
    )

    for tgt_name, tgt_page, tgt_bboxes in targets:
        checked_targets.append(tgt_name)
        mis, ovl, inv, gap = _evaluate_alignment_for_target(
            target_name=tgt_name,
            protected_blocks=protected_blocks,
            page_blocks=page_blocks,
            out_page=tgt_page,
            orig_elements=orig_elements,
            orig_page_w=orig_page_w,
            rendered_bboxes=tgt_bboxes,
            position_tolerance_pt=position_tolerance_pt,
            size_tolerance_pt=size_tolerance_pt,
            min_iou=min_iou,
            overlap_tolerance_pt=overlap_tolerance_pt,
            max_below_gap_increase_pt=max_below_gap_increase_pt,
            max_above_gap_increase_pt=max_above_gap_increase_pt,
        )
        all_misaligned.extend(mis)
        all_overlaps.extend(ovl)
        all_inversions.extend(inv)
        all_gaps.extend(gap)

    all_aligned = not (all_misaligned or all_overlaps or all_inversions or all_gaps)
    return ProtectedBlockAlignmentMetrics(
        checked_protected_blocks=checked_count,
        misaligned_blocks=all_misaligned,
        overlapping_pairs=all_overlaps,
        order_inversions=all_inversions,
        abnormal_gaps=all_gaps,
        all_aligned=all_aligned,
        checked_targets=checked_targets,
    )


def evaluate_page_layout_regression(
    orig_page: pymupdf.Page,
    page_blocks: list[Block],
    page_index: int,
    *,
    mono_page: pymupdf.Page | None = None,
    dual_page: pymupdf.Page | None = None,
    dpi: int = 150,
) -> PageLayoutRegressionMetrics:
    """Evaluate all 4 layout/math regression metrics for a single page."""
    page_w = float(orig_page.rect.width)
    raw_page_text = orig_page.get_text("text")
    output_page_text: str | None = None
    if mono_page is not None:
        output_page_text = mono_page.get_text("text")

    formula_metrics = check_formula_block_count_consistency(
        orig_page, page_blocks, dpi=dpi
    )
    numeric_metrics = check_numeric_and_unit_preservation(
        raw_page_text, page_blocks, output_text=output_page_text
    )
    column_metrics = check_column_order_monotonicity(page_blocks, page_w)

    alignment_metrics: ProtectedBlockAlignmentMetrics | None = None
    if mono_page is not None or dual_page is not None:
        alignment_metrics = check_protected_block_alignment(
            page_blocks,
            orig_page=orig_page,
            mono_page=mono_page,
            dual_page=dual_page,
        )

    reasons: list[str] = []
    if formula_metrics.needs_visual_review:
        reasons.append(
            f"formula_count_diff={formula_metrics.count_diff:+d}"
            f" (img={formula_metrics.image_formula_cluster_count}, ext={formula_metrics.extracted_formula_block_count})"
        )
    if numeric_metrics.needs_visual_review:
        reasons.append(
            f"numeric_preservation={numeric_metrics.preservation_ratio:.3f}"
            f" (body_mask={numeric_metrics.body_blocks_masking.preservation_ratio:.3f})"
        )
    if not column_metrics.is_monotonic:
        reasons.append(f"column_inversions={column_metrics.inversion_count}")
    if alignment_metrics is not None and not alignment_metrics.all_aligned:
        reasons.append(
            f"alignment_issues(mis={len(alignment_metrics.misaligned_blocks)},"
            f" ovl={len(alignment_metrics.overlapping_pairs)},"
            f" inv={len(alignment_metrics.order_inversions)},"
            f" gap={len(alignment_metrics.abnormal_gaps)})"
        )

    return PageLayoutRegressionMetrics(
        page_index=page_index,
        display_page_number=page_index + 1,
        formula_block_count=formula_metrics,
        numeric_preservation=numeric_metrics,
        column_order=column_metrics,
        protected_alignment=alignment_metrics,
        needs_visual_review=bool(reasons),
        review_reasons=reasons,
    )


def evaluate_document_layout_regression(
    original_pdf_path: str | Path,
    blocks: list[Block],
    page_indices: list[int] | None = None,
    *,
    mono_pdf_path: str | Path | None = None,
    dual_pdf_path: str | Path | None = None,
    document_id: str | None = None,
    original_filename: str | None = None,
    dpi: int = 150,
) -> LayoutRegressionReport:
    """Evaluate all 4 layout/math regression metrics across the requested document pages."""
    blocks_by_page: dict[int, list[Block]] = {}
    for b in blocks:
        blocks_by_page.setdefault(b.page_index, []).append(b)

    orig_doc = pymupdf.open(str(original_pdf_path))
    mono_doc = (
        pymupdf.open(str(mono_pdf_path))
        if mono_pdf_path is not None and Path(mono_pdf_path).exists()
        else None
    )
    dual_doc = (
        pymupdf.open(str(dual_pdf_path))
        if dual_pdf_path is not None and Path(dual_pdf_path).exists()
        else None
    )

    try:
        orig_total = len(orig_doc)
        target_pages = (
            list(page_indices)
            if page_indices is not None
            else list(range(orig_total))
        )
        page_reports: list[PageLayoutRegressionMetrics] = []

        for seq_idx, p_idx in enumerate(target_pages):
            if p_idx < 0 or p_idx >= orig_total:
                continue
            orig_page = orig_doc[p_idx]

            mono_page: pymupdf.Page | None = None
            if mono_doc is not None and len(mono_doc) > 0:
                m_idx = p_idx if len(mono_doc) == orig_total else seq_idx
                if 0 <= m_idx < len(mono_doc):
                    mono_page = mono_doc[m_idx]

            dual_page: pymupdf.Page | None = None
            if dual_doc is not None and len(dual_doc) > 0:
                d_idx = p_idx if len(dual_doc) == orig_total else seq_idx
                if 0 <= d_idx < len(dual_doc):
                    dual_page = dual_doc[d_idx]

            p_rep = evaluate_page_layout_regression(
                orig_page=orig_page,
                page_blocks=blocks_by_page.get(p_idx, []),
                page_index=p_idx,
                mono_page=mono_page,
                dual_page=dual_page,
                dpi=dpi,
            )
            page_reports.append(p_rep)

        flagged_indices = [p.page_index for p in page_reports if p.needs_visual_review]
        flagged_pages = [p.display_page_number for p in page_reports if p.needs_visual_review]

        total_img_formulas = sum(
            p.formula_block_count.image_formula_cluster_count for p in page_reports
        )
        total_ext_formulas = sum(
            p.formula_block_count.extracted_formula_block_count for p in page_reports
        )
        total_col_inversions = sum(
            p.column_order.inversion_count for p in page_reports
        )
        min_num_ratio = min(
            (p.numeric_preservation.preservation_ratio for p in page_reports),
            default=1.0,
        )
        min_body_mask_ratio = min(
            (p.numeric_preservation.body_blocks_masking.preservation_ratio for p in page_reports),
            default=1.0,
        )
        total_misaligned = sum(
            len(p.protected_alignment.misaligned_blocks)
            for p in page_reports
            if p.protected_alignment is not None
        )
        total_overlaps = sum(
            len(p.protected_alignment.overlapping_pairs)
            for p in page_reports
            if p.protected_alignment is not None
        )
        total_order_inv = sum(
            len(p.protected_alignment.order_inversions)
            for p in page_reports
            if p.protected_alignment is not None
        )
        total_abnormal_gaps = sum(
            len(p.protected_alignment.abnormal_gaps)
            for p in page_reports
            if p.protected_alignment is not None
        )

        summary = {
            "flagged_page_count": len(flagged_pages),
            "formula_count_mismatch_pages": [
                p.display_page_number
                for p in page_reports
                if p.formula_block_count.needs_visual_review
            ],
            "total_image_formula_clusters": total_img_formulas,
            "total_extracted_formula_blocks": total_ext_formulas,
            "min_numeric_preservation_ratio": round(min_num_ratio, 6),
            "min_body_masking_preservation_ratio": round(min_body_mask_ratio, 6),
            "total_column_order_inversions": total_col_inversions,
            "total_protected_misaligned": total_misaligned,
            "total_protected_overlaps": total_overlaps,
            "total_protected_order_inversions": total_order_inv,
            "total_protected_abnormal_gaps": total_abnormal_gaps,
        }

        return LayoutRegressionReport(
            document_id=document_id,
            original_filename=original_filename,
            total_pages_evaluated=len(page_reports),
            flagged_pages=flagged_pages,
            flagged_page_indices=flagged_indices,
            summary=summary,
            pages=page_reports,
        )
    finally:
        if dual_doc is not None:
            dual_doc.close()
        if mono_doc is not None:
            mono_doc.close()
        orig_doc.close()
