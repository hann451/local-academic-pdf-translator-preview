"""Geometry-aware drag-selection splitting engine for mixed formula/prose raw PDF blocks.

Constructs visual lines from PyMuPDF raw lines, spans, and nearby vector drawings,
snaps/rejects user drag rectangles safely without mutating the source PDF, verifies
exact span-ID conservation across `top` / `formula` / `bottom` children, and
matches persisted `BlockSplitSpec` rules against fresh raw extractions strictly by
parent bbox IoU and source-text hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.domain.models import (
    Block,
    BlockKind,
    BlockSplitAppliedStatus,
    BlockSplitPreviewPart,
    BlockSplitPreviewResult,
    BlockSplitRole,
    BlockSplitSpec,
    compute_source_text_hash,
)
from backend.pdf.coordinates import bbox_intersection_ratio, bbox_iou

# --- Explicit snapping, tolerance & matching constants (PDF points / ratios) ---
MIN_DRAG_EXTENT_PT = 4.0
DRAG_MARGIN_TOLERANCE_PT = 6.0
MIN_PROSE_LINE_VERTICAL_OVERLAP_RATIO = 0.45
FORMULA_LINE_VERTICAL_OVERLAP_RATIO = 0.18
SUB_SUPER_VERTICAL_TOLERANCE_PT = 6.5
FRACTION_DRAWING_PROXIMITY_PT = 8.0
EQ_NUMBER_HORIZONTAL_GAP_MAX_PT = 220.0
EQ_NUMBER_VERTICAL_DELTA_MAX_PT = 10.0
ADJACENT_SEAM_CLEARANCE_PT = 0.8
MAX_SEAM_OVERLAP_TRIM_PT = 4.0
PARENT_MATCH_MIN_IOU = 0.85
PARENT_CHANGED_MIN_IOU = 0.50

_STANDALONE_EQ_NUM_RE = re.compile(
    r"^\s*(?:\(\s*\d+(?:\.\d+)*(?:[A-Za-z])?\s*\)"
    r"|（\s*\d+(?:\.\d+)*(?:[A-Za-z])?\s*）"
    r"|\[\s*\d+(?:\.\d+)*(?:[A-Za-z])?\s*\])\s*$"
)
_INLINE_EQ_REF_TAIL_RE = re.compile(
    r"(?:Eq\.?|Eqs\.?|Equation|式|Fig\.?|Figure|Table|Tab\.?)\s*$",
    re.IGNORECASE,
)


@dataclass
class SpanRecord:
    """Single immutable PDF span indexed by its raw block/line/span origin."""

    uid: str
    raw_idx: int
    line_idx: int
    span_idx: int
    bbox: list[float]
    text: str
    size: float
    flags: int
    font: str
    raw_span: dict[str, Any]


@dataclass
class RawLineUnit:
    """Single MuPDF `line` dict enriched with geometry and span records."""

    raw_idx: int
    line_idx: int
    bbox: list[float]
    raw_line: dict[str, Any]
    spans: list[SpanRecord]
    text: str
    max_size: float
    min_size: float
    width: float
    height: float
    mid_y: float
    has_small_script: bool
    is_eq_number_only: bool


@dataclass
class VisualLineGroup:
    """One visual horizontal band grouping host row spans, sub/superscripts, and stacked parts."""

    index: int
    raw_lines: list[RawLineUnit]
    bbox: list[float]
    text: str
    span_uids: list[str]
    mid_y: float
    height: float
    max_size: float
    is_prose_like: bool = False
    has_math_hint: bool = False


@dataclass
class RawPageTextCandidate:
    """Unsplit MuPDF text/image/vector item with stable base block ID and visual lines."""

    base_seq: int
    base_block_id: str
    item_type: Literal["vector_drawing", "raster_image", "text"]
    raw_idx: int | None
    bbox: list[float]
    raw_item: dict[str, Any] | None = None
    drawing_bbox: list[float] | None = None
    visual_lines: list[VisualLineGroup] = field(default_factory=list)
    span_uids: set[str] = field(default_factory=set)
    source_text: str = ""
    source_text_hash: str = ""
    is_standalone_eq_num: bool = False


@dataclass
class SplitExecutionOutcome:
    """Resolved split partition for one raw parent candidate (+ optional absorbed eq-num block)."""

    spec_id: str
    parent_candidate: RawPageTextCandidate
    absorbed_eq_candidate: RawPageTextCandidate | None
    top_lines: list[dict[str, Any]]
    formula_lines: list[dict[str, Any]]
    bottom_lines: list[dict[str, Any]]
    top_bbox: list[float] | None
    formula_bbox: list[float]
    bottom_bbox: list[float] | None
    top_text: str
    formula_text: str
    bottom_text: str
    equation_number_text: str | None
    equation_number_bbox: list[float] | None


def _union_boxes(boxes: list[list[float]]) -> list[float]:
    return [
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    ]


def _clamp_bbox_vertical_overlap(top: float, bottom: float) -> tuple[float, float]:
    return (round(top, 2), round(bottom, 2))


def _vertical_overlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _horizontal_overlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _looks_like_math_text(text: str, raw_lines: list[RawLineUnit]) -> bool:
    for rline in raw_lines:
        for sp in rline.spans:
            fn = sp.font.lower()
            if any(k in fn for k in ("cmmi", "cmsy", "cmex", "math", "symbol", "txsys", "txexs", "txmia")):
                return True
            if sp.flags & 1:
                return True
    stripped = text.strip()
    if any(op in stripped for op in ("=", "+", "−", "×", "÷", "≤", "≥", "≈", "∑", "∫", "√", "α", "β", "γ", "θ", "λ", "μ", "σ", "φ", "ω")):
        return True
    if _STANDALONE_EQ_NUM_RE.search(stripped):
        return True
    return False


def _looks_like_prose_line(text: str, has_math_hint: bool, block_width: float, line_width: float) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    words = [w for w in re.split(r"\s+", stripped) if re.search(r"[A-Za-z]{2,}", w)]
    if len(words) >= 5 and not any(op in stripped for op in ("=", "∑", "∫", "≈", "≤", "≥")):
        return True
    if len(words) >= 4 and not has_math_hint and (line_width >= max(60.0, block_width * 0.45)):
        return True
    return False


def _extract_raw_line_units(
    b_item: dict[str, Any],
    raw_idx: int,
    body_font_size: float,
) -> list[RawLineUnit]:
    units: list[RawLineUnit] = []
    for line_idx, line in enumerate(b_item.get("lines", [])):
        raw_spans = line.get("spans", [])
        if not raw_spans:
            continue
        span_records: list[SpanRecord] = []
        boxes: list[list[float]] = []
        sizes: list[float] = []
        texts: list[str] = []
        for span_idx, sp in enumerate(raw_spans):
            t = str(sp.get("text", "")).replace("\xa0", " ")
            if not t.strip():
                continue
            sb = sp.get("bbox")
            if not sb or len(sb) != 4:
                continue
            sb_f = [float(sb[0]), float(sb[1]), float(sb[2]), float(sb[3])]
            if sb_f[2] <= sb_f[0] or sb_f[3] <= sb_f[1]:
                continue
            sz = float(sp.get("size", 10.0))
            uid = f"r{raw_idx}:l{line_idx}:s{span_idx}"
            span_records.append(
                SpanRecord(
                    uid=uid,
                    raw_idx=raw_idx,
                    line_idx=line_idx,
                    span_idx=span_idx,
                    bbox=sb_f,
                    text=t,
                    size=sz,
                    flags=int(sp.get("flags", 0)),
                    font=str(sp.get("font", "")),
                    raw_span=sp,
                )
            )
            boxes.append(sb_f)
            sizes.append(sz)
            texts.append(t)

        if not span_records:
            continue

        lb = _union_boxes(boxes)
        joined = "".join(texts).strip()
        max_sz = max(sizes)
        min_sz = min(sizes)
        w = max(0.0, lb[2] - lb[0])
        h = max(0.0, lb[3] - lb[1])
        mid_y = (lb[1] + lb[3]) / 2.0
        has_small = (
            max_sz <= body_font_size * 0.85
            or (len(joined) <= 10 and h <= body_font_size * 0.88)
            or all((sp.flags & 1) or sp.size <= body_font_size * 0.85 for sp in span_records)
        )
        units.append(
            RawLineUnit(
                raw_idx=raw_idx,
                line_idx=line_idx,
                bbox=lb,
                raw_line=line,
                spans=span_records,
                text=joined,
                max_size=max_sz,
                min_size=min_sz,
                width=w,
                height=h,
                mid_y=mid_y,
                has_small_script=has_small,
                is_eq_number_only=bool(_STANDALONE_EQ_NUM_RE.match(joined)),
            )
        )
    return units


def build_visual_lines_for_raw_block(
    b_item: dict[str, Any],
    raw_idx: int,
    drawings: list[dict[str, Any]],
    body_font_size: float,
    extract_text_fn: Any,
) -> list[VisualLineGroup]:
    """Cluster MuPDF raw lines in `b_item` into disjoint visual horizontal line bands."""
    units = _extract_raw_line_units(b_item, raw_idx=raw_idx, body_font_size=body_font_size)
    if not units:
        return []

    block_bbox = [float(x) for x in b_item.get("bbox", [0, 0, 0, 0])]
    block_w = max(1.0, block_bbox[2] - block_bbox[0])

    # Step 1: separate primary host rows from floating small sub/superscript / accent fragments.
    hosts: list[list[RawLineUnit]] = []
    floaters: list[RawLineUnit] = []

    host_candidates = [u for u in units if not u.has_small_script]
    if not host_candidates:
        # Entire block is small script (e.g. isolated symbols) -> keep everything as hosts
        host_candidates = list(units)
    else:
        floaters = [u for u in units if u.has_small_script]

    # Sort host candidates top-to-bottom then left-to-right to form horizontal rows
    host_candidates.sort(key=lambda u: (round(u.mid_y, 2), round(u.bbox[0], 2)))
    for u in host_candidates:
        placed = False
        for row in hosts:
            row_mid_y = sum(r.mid_y for r in row) / len(row)
            row_max_sz = max(r.max_size for r in row)
            same_row_tol = max(row_max_sz, u.max_size, body_font_size) * 0.45
            if abs(u.mid_y - row_mid_y) <= same_row_tol:
                row.append(u)
                placed = True
                break
        if not placed:
            hosts.append([u])

    # Step 2: bind each floating script/hat/accent unit to nearest compatible host row
    unassigned_floaters: list[RawLineUnit] = []
    for f in floaters:
        best_row_idx: int | None = None
        best_dist = float("inf")
        for idx, row in enumerate(hosts):
            row_box = _union_boxes([r.bbox for r in row])
            row_mid_y = (row_box[1] + row_box[3]) / 2.0
            # Require horizontal proximity or overlap with the host row
            horiz_gap = max(0.0, row_box[0] - f.bbox[2], f.bbox[0] - row_box[2])
            if horiz_gap > max(24.0, body_font_size * 2.5):
                continue
            v_delta = abs(f.mid_y - row_mid_y)
            edge_dist = max(0.0, row_box[1] - f.bbox[3], f.bbox[1] - row_box[3])
            if edge_dist <= SUB_SUPER_VERTICAL_TOLERANCE_PT and v_delta < best_dist:
                best_dist = v_delta
                best_row_idx = idx
        if best_row_idx is not None:
            hosts[best_row_idx].append(f)
        else:
            unassigned_floaters.append(f)

    for uf in unassigned_floaters:
        placed = False
        for row in hosts:
            row_box = _union_boxes([r.bbox for r in row])
            if abs(uf.mid_y - (row_box[1] + row_box[3]) / 2.0) <= body_font_size * 0.45:
                row.append(uf)
                placed = True
                break
        if not placed:
            hosts.append([uf])

    # Step 3: merge rows bridged vertically/horizontally by formula drawings (fraction bars / tall brackets / radicals)
    block_drawings: list[list[float]] = []
    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        db = [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
        if db[2] <= db[0] or db[3] <= db[1]:
            continue
        if (
            min(db[2], block_bbox[2] + 6.0) - max(db[0], block_bbox[0] - 6.0) > 0
            and min(db[3], block_bbox[3] + 6.0) - max(db[1], block_bbox[1] - 6.0) > 0
        ):
            dw, dh = db[2] - db[0], db[3] - db[1]
            # Ignore huge page frames
            if dw < block_w * 1.4 and dh < max(120.0, (block_bbox[3] - block_bbox[1]) * 0.95):
                block_drawings.append(db)

    if block_drawings and len(hosts) > 1:
        parent = list(range(len(hosts)))

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        row_boxes = [_union_boxes([u.bbox for u in r]) for r in hosts]
        for db in block_drawings:
            touched: list[int] = []
            padded_db = [
                db[0] - 4.0,
                db[1] - FRACTION_DRAWING_PROXIMITY_PT,
                db[2] + 4.0,
                db[3] + FRACTION_DRAWING_PROXIMITY_PT,
            ]
            for r_idx, rb in enumerate(row_boxes):
                if _horizontal_overlap(rb, padded_db) > 2.0 and _vertical_overlap(rb, padded_db) > 0.0:
                    # Do not let a drawing bridge a full prose paragraph line across a gap
                    r_text = " ".join(u.text for u in hosts[r_idx]).strip()
                    r_math = _looks_like_math_text(r_text, hosts[r_idx])
                    if not _looks_like_prose_line(r_text, r_math, block_w, rb[2] - rb[0]):
                        touched.append(r_idx)
            if len(touched) >= 2:
                base = touched[0]
                for other in touched[1:]:
                    _union(base, other)

        merged_map: dict[int, list[RawLineUnit]] = {}
        for r_idx, row in enumerate(hosts):
            merged_map.setdefault(_find(r_idx), []).extend(row)
        hosts = list(merged_map.values())

    # Preserve original MuPDF line order inside each group, and order groups top-to-bottom
    groups_raw: list[tuple[float, list[RawLineUnit]]] = []
    for row in hosts:
        ordered_units = sorted(row, key=lambda u: u.line_idx)
        box = _union_boxes([u.bbox for u in ordered_units])
        groups_raw.append((box[1], ordered_units))
    groups_raw.sort(key=lambda pair: (round(pair[0], 2), min(u.line_idx for u in pair[1])))

    visual_lines: list[VisualLineGroup] = []
    for idx, (_, ordered_units) in enumerate(groups_raw):
        box = _union_boxes([u.bbox for u in ordered_units])
        raw_line_dicts = [u.raw_line for u in ordered_units]
        text_lines, _, _, _ = extract_text_fn(raw_line_dicts)
        joined = " ".join(t.strip() for t in text_lines if t.strip()).strip()
        if not joined:
            joined = " ".join(u.text for u in ordered_units if u.text).strip()
        span_uids = [sp.uid for u in ordered_units for sp in u.spans]
        max_sz = max(u.max_size for u in ordered_units)
        math_hint = _looks_like_math_text(joined, ordered_units)
        prose_like = _looks_like_prose_line(joined, math_hint, block_w, box[2] - box[0])
        visual_lines.append(
            VisualLineGroup(
                index=idx,
                raw_lines=ordered_units,
                bbox=box,
                text=joined,
                span_uids=span_uids,
                mid_y=(box[1] + box[3]) / 2.0,
                height=max(0.0, box[3] - box[1]),
                max_size=max_sz,
                is_prose_like=prose_like,
                has_math_hint=math_hint,
            )
        )
    return visual_lines


def build_raw_page_candidates(
    *,
    document_id: str,
    page_index: int,
    drawing_boxes: list[list[float]],
    raw_blocks: list[dict[str, Any]],
    drawings: list[dict[str, Any]],
    body_font_size: float,
    extract_text_fn: Any,
) -> list[RawPageTextCandidate]:
    """Build deterministic unsplit page candidates preserving exact `block-{seq}` numbering."""
    candidates: list[RawPageTextCandidate] = []
    block_seq = 0

    for d_box in drawing_boxes:
        block_seq += 1
        candidates.append(
            RawPageTextCandidate(
                base_seq=block_seq,
                base_block_id=f"{document_id}:page-{page_index + 1}:block-{block_seq}",
                item_type="vector_drawing",
                raw_idx=None,
                bbox=list(d_box),
                drawing_bbox=list(d_box),
            )
        )

    for raw_idx, b_item in enumerate(raw_blocks):
        b_type = b_item.get("type", 0)
        bbox = [round(float(x), 2) for x in b_item.get("bbox", [0, 0, 0, 0])]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        if b_type == 1:
            block_seq += 1
            candidates.append(
                RawPageTextCandidate(
                    base_seq=block_seq,
                    base_block_id=f"{document_id}:page-{page_index + 1}:block-{block_seq}",
                    item_type="raster_image",
                    raw_idx=raw_idx,
                    bbox=bbox,
                    raw_item=b_item,
                )
            )
            continue

        lines = b_item.get("lines", [])
        if not lines:
            continue
        text_lines, _, _, _ = extract_text_fn(lines)
        full_text = " ".join(t.strip() for t in text_lines if t.strip()).strip()
        if not full_text:
            continue

        block_seq += 1
        vlines = build_visual_lines_for_raw_block(
            b_item=b_item,
            raw_idx=raw_idx,
            drawings=drawings,
            body_font_size=body_font_size,
            extract_text_fn=extract_text_fn,
        )
        span_uids = {uid for vl in vlines for uid in vl.span_uids}
        candidates.append(
            RawPageTextCandidate(
                base_seq=block_seq,
                base_block_id=f"{document_id}:page-{page_index + 1}:block-{block_seq}",
                item_type="text",
                raw_idx=raw_idx,
                bbox=bbox,
                raw_item=b_item,
                visual_lines=vlines,
                span_uids=span_uids,
                source_text=full_text,
                source_text_hash=compute_source_text_hash(full_text),
                is_standalone_eq_num=bool(
                    len(vlines) <= 2 and _STANDALONE_EQ_NUM_RE.match(full_text)
                ),
            )
        )
    return candidates


def verify_split_conservation(
    *,
    parent_span_uids: set[str],
    top_vlines: list[VisualLineGroup],
    formula_vlines: list[VisualLineGroup],
    bottom_vlines: list[VisualLineGroup],
    absorbed_span_uids: set[str] | None = None,
) -> tuple[bool, str | None]:
    """Verify every span UID is conserved exactly once across split children."""
    expected = set(parent_span_uids) | set(absorbed_span_uids or set())
    top_ids = [uid for vl in top_vlines for uid in vl.span_uids]
    formula_ids = [uid for vl in formula_vlines for uid in vl.span_uids]
    bottom_ids = [uid for vl in bottom_vlines for uid in vl.span_uids]
    combined = top_ids + formula_ids + bottom_ids
    if len(combined) != len(set(combined)):
        return False, "分割候補内でテキストspanが重複しています。"
    actual = set(combined)
    if actual != expected:
        return False, "分割後に一部の数式・本文テキストが欠落するため保存できません。"
    if not formula_ids:
        return False, "選択範囲から独立数式テキストを特定できませんでした。"
    if not top_ids and not bottom_ids:
        return False, "ブロック全体が数式として選択され、分離すべき本文が残っていません。"
    return True, None


def _detect_inline_eq_num_on_line(
    vl: VisualLineGroup,
    drag_bbox: list[float],
) -> tuple[SpanRecord | None, bool]:
    """Check if the far-right span of `vl` is an equation number candidate outside `drag_bbox`."""
    spans_ordered = sorted(
        [sp for u in vl.raw_lines for sp in u.spans],
        key=lambda sp: (sp.bbox[0], sp.bbox[1]),
    )
    if len(spans_ordered) < 2:
        return None, False
    last_sp = spans_ordered[-1]
    if not _STANDALONE_EQ_NUM_RE.match(last_sp.text.strip()):
        return None, False
    prefix_text = " ".join(sp.text.strip() for sp in spans_ordered[:-1]).strip()
    if _INLINE_EQ_REF_TAIL_RE.search(prefix_text):
        return None, False
    inside_drag = (
        last_sp.bbox[0] >= drag_bbox[0] - DRAG_MARGIN_TOLERANCE_PT
        and last_sp.bbox[2] <= drag_bbox[2] + DRAG_MARGIN_TOLERANCE_PT
    )
    return last_sp, not inside_drag


def _has_midline_prose_cut(vl: VisualLineGroup, drag_bbox: list[float]) -> bool:
    """Return True if `drag_bbox` cuts horizontally through the middle of prose on `vl`."""
    tol = DRAG_MARGIN_TOLERANCE_PT
    all_spans = sorted(
        [sp for u in vl.raw_lines for sp in u.spans],
        key=lambda sp: sp.bbox[0],
    )
    if not all_spans:
        return False

    inside_spans: list[SpanRecord] = []
    outside_prose_spans: list[SpanRecord] = []
    eq_num_span, _ = _detect_inline_eq_num_on_line(vl, drag_bbox)

    # Detect drag partially slicing across a single wide multi-word prose span (e.g. one PyMuPDF span for whole line)
    for sp in all_spans:
        if eq_num_span is not None and sp.uid == eq_num_span.uid:
            continue
        x0, _, x1, _ = sp.bbox
        sp_w = max(0.5, x1 - x0)
        ov_x = min(x1, drag_bbox[2] + tol) - max(x0, drag_bbox[0] - tol)
        uncovered_w = sp_w - max(0.0, ov_x)
        if ov_x > 1.0 and uncovered_w > 18.0 and (ov_x / sp_w) < 0.72:
            prose_words = re.findall(r"[A-Za-z\u3040-\u30ff\u4e00-\u9faf]{2,}", sp.text)
            if len(prose_words) >= 3:
                return True

    # Check if drag completely omits non-trivial prose words to the left or right of drag_bbox

    for sp in all_spans:
        if eq_num_span is not None and sp.uid == eq_num_span.uid:
            continue
        x0, _, x1, _ = sp.bbox
        ov_x = min(x1, drag_bbox[2] + tol) - max(x0, drag_bbox[0] - tol)
        w = max(0.5, x1 - x0)
        if ov_x >= w * 0.45:
            inside_spans.append(sp)
        else:
            outside_prose_spans.append(sp)

    if not inside_spans:
        return False
    outside_text = " ".join(sp.text.strip() for sp in outside_prose_spans).strip()
    if not outside_text:
        return False
    # Trailing punctuation alone (e.g. `,` or `.` at end of display equation) is not a prose cut
    outside_tokens = re.findall(r"[A-Za-z0-9\u3040-\u30ff\u4e00-\u9faf]+", outside_text)
    if not outside_tokens:
        return False
    # Reject if substantial prose words or multiple left/right tokens sit outside drag horizontally
    outside_words = re.findall(r"[A-Za-z\u3040-\u30ff\u4e00-\u9faf]{2,}", outside_text)
    if len(outside_words) >= 2 or len(outside_text) >= 6:
        return True
    # Also reject if inside is a short inline fragment flanked by alphanumeric tokens
    inside_min_x = min(sp.bbox[0] for sp in inside_spans)
    inside_max_x = max(sp.bbox[2] for sp in inside_spans)
    has_left = any(sp.bbox[2] <= inside_min_x + 1.0 for sp in outside_prose_spans)
    has_right = any(sp.bbox[0] >= inside_max_x - 1.0 for sp in outside_prose_spans)
    if has_left or has_right:
        return True
    return False


def _detect_two_column_mid_x(
    candidates: list[RawPageTextCandidate], page_w: float
) -> float | None:
    mid_x = page_w / 2.0
    narrow_left = sum(
        1
        for c in candidates
        if c.item_type == "text"
        and c.bbox[2] <= mid_x + 15.0
        and (c.bbox[2] - c.bbox[0]) < page_w * 0.48
    )
    narrow_right = sum(
        1
        for c in candidates
        if c.item_type == "text"
        and c.bbox[0] >= mid_x - 15.0
        and (c.bbox[2] - c.bbox[0]) < page_w * 0.48
    )
    return mid_x if (narrow_left >= 1 and narrow_right >= 1) else None


def _find_standalone_eq_number_neighbor(
    candidates: list[RawPageTextCandidate],
    parent_candidate: RawPageTextCandidate,
    formula_bbox: list[float],
    already_hit_candidates: list[RawPageTextCandidate],
) -> RawPageTextCandidate | None:
    # Check if drag already touched a standalone eq-num block neighbor
    for cand in already_hit_candidates:
        if cand.base_block_id != parent_candidate.base_block_id and cand.is_standalone_eq_num:
            cand_mid_y = (cand.bbox[1] + cand.bbox[3]) / 2.0
            f_mid_y = (formula_bbox[1] + formula_bbox[3]) / 2.0
            if abs(cand_mid_y - f_mid_y) <= EQ_NUMBER_VERTICAL_DELTA_MAX_PT + 6.0:
                return cand

    f_mid_y = (formula_bbox[1] + formula_bbox[3]) / 2.0
    best: tuple[float, RawPageTextCandidate] | None = None
    for cand in candidates:
        if cand.base_block_id == parent_candidate.base_block_id:
            continue
        if cand.item_type != "text" or not cand.is_standalone_eq_num:
            continue
        cand_mid_y = (cand.bbox[1] + cand.bbox[3]) / 2.0
        v_delta = abs(cand_mid_y - f_mid_y)
        if v_delta > EQ_NUMBER_VERTICAL_DELTA_MAX_PT:
            continue
        # Expect right-margin (or occasional left-margin) horizontal alignment
        h_gap = max(
            0.0,
            cand.bbox[0] - formula_bbox[2],
            formula_bbox[0] - cand.bbox[2],
        )
        if h_gap > EQ_NUMBER_HORIZONTAL_GAP_MAX_PT:
            continue
        score = v_delta + h_gap * 0.05
        if best is None or score < best[0]:
            best = (score, cand)
    return best[1] if best else None


def _compute_part_bboxes_with_safe_seams(
    *,
    top_vlines: list[VisualLineGroup],
    formula_vlines: list[VisualLineGroup],
    bottom_vlines: list[VisualLineGroup],
    extra_formula_boxes: list[list[float]] | None = None,
) -> tuple[bool, list[float] | None, list[float], list[float] | None, str | None]:
    """Compute part bboxes ensuring >=0.8pt clearance around an unshrunk `formula` bbox."""
    f_boxes = [vl.bbox for vl in formula_vlines] + list(extra_formula_boxes or [])
    formula_bbox = _union_boxes(f_boxes)
    top_bbox = _union_boxes([vl.bbox for vl in top_vlines]) if top_vlines else None
    bottom_bbox = _union_boxes([vl.bbox for vl in bottom_vlines]) if bottom_vlines else None

    # Never shrink `formula_bbox` into its own subscripts/superscripts/drawings:
    # trim only the adjacent `top_bbox` bottom edge / `bottom_bbox` top edge while ensuring
    # the adjacent prose line retains enough vertical extent for clean redaction.
    if top_bbox is not None:
        overlap_top = top_bbox[3] - formula_bbox[1]
        max_trim = max(MAX_SEAM_OVERLAP_TRIM_PT, top_vlines[-1].height * 0.55)
        if overlap_top > max_trim:
            return (
                False,
                None,
                formula_bbox,
                None,
                "上の本文と選択数式の縦方向領域が深く重なっており、安全に分離できません。",
            )
        if overlap_top > -ADJACENT_SEAM_CLEARANCE_PT:
            new_top_y1 = formula_bbox[1] - ADJACENT_SEAM_CLEARANCE_PT
            last_top_kept = new_top_y1 - top_vlines[-1].bbox[1]
            if new_top_y1 <= top_bbox[1] + 2.0 or last_top_kept < max(4.0, top_vlines[-1].height * 0.38):
                return (
                    False,
                    None,
                    formula_bbox,
                    None,
                    "上の本文と数式の間隔が狭すぎるため、本文または数式の一部が欠落する恐れがあります。",
                )
            top_bbox = [top_bbox[0], top_bbox[1], top_bbox[2], round(new_top_y1, 2)]

    if bottom_bbox is not None:
        overlap_bottom = formula_bbox[3] - bottom_bbox[1]
        max_trim = max(MAX_SEAM_OVERLAP_TRIM_PT, bottom_vlines[0].height * 0.55)
        if overlap_bottom > max_trim:
            return (
                False,
                None,
                formula_bbox,
                None,
                "選択数式と下の本文の縦方向領域が深く重なっており、安全に分離できません。",
            )
        if overlap_bottom > -ADJACENT_SEAM_CLEARANCE_PT:
            new_bot_y0 = formula_bbox[3] + ADJACENT_SEAM_CLEARANCE_PT
            first_bot_kept = bottom_vlines[0].bbox[3] - new_bot_y0
            if new_bot_y0 >= bottom_bbox[3] - 2.0 or first_bot_kept < max(4.0, bottom_vlines[0].height * 0.38):
                return (
                    False,
                    None,
                    formula_bbox,
                    None,
                    "選択数式と下の本文の間隔が狭すぎるため、本文または数式の一部が欠落する恐れがあります。",
                )
            bottom_bbox = [bottom_bbox[0], round(new_bot_y0, 2), bottom_bbox[2], bottom_bbox[3]]

    return True, top_bbox, formula_bbox, bottom_bbox, None


def evaluate_drag_split_candidate(
    *,
    document_id: str,
    page_index: int,
    page_w: float,
    candidates: list[RawPageTextCandidate],
    drag_bbox: list[float],
    existing_splits: list[BlockSplitSpec],
    drawings: list[dict[str, Any]],
    extract_text_fn: Any,
    include_equation_number: bool = True,
    manual_edit_block_ids: set[str] | None = None,
) -> tuple[BlockSplitPreviewResult, SplitExecutionOutcome | None]:
    """Evaluate a user drag bbox against fresh unsplit page candidates and return preview + outcome."""
    rx0, ry0, rx1, ry1 = [round(float(v), 2) for v in drag_bbox]
    norm_drag = [min(rx0, rx1), min(ry0, ry1), max(rx0, rx1), max(ry0, ry1)]

    def _reject(code: str, reason: str, guidance: str) -> tuple[BlockSplitPreviewResult, None]:
        return (
            BlockSplitPreviewResult(
                valid=False,
                document_id=document_id,
                page_index=page_index,
                requested_bbox=norm_drag,
                rejection_code=code,
                rejection_reason=reason,
                retry_guidance=guidance,
            ),
            None,
        )

    if (norm_drag[2] - norm_drag[0]) < MIN_DRAG_EXTENT_PT or (norm_drag[3] - norm_drag[1]) < MIN_DRAG_EXTENT_PT:
        return _reject(
            "EMPTY_SELECTION",
            "ドラッグ範囲が小さすぎるため分割対象を特定できません。",
            "保護したい独立数式全体（と必要に応じて右端の式番号）を囲むように十分な大きさでドラッグしてください。",
        )

    # Check multi-column span across page midline
    col_mid_x = _detect_two_column_mid_x(candidates, page_w)
    if col_mid_x is not None and norm_drag[0] < col_mid_x - 18.0 and norm_drag[2] > col_mid_x + 18.0:
        left_hit = any(
            _vertical_overlap(c.bbox, norm_drag) > 1.0 and c.bbox[2] <= col_mid_x + 15.0
            for c in candidates
            if c.item_type == "text"
        )
        right_hit = any(
            _vertical_overlap(c.bbox, norm_drag) > 1.0 and c.bbox[0] >= col_mid_x - 15.0
            for c in candidates
            if c.item_type == "text"
        )
        if left_hit and right_hit:
            return _reject(
                "MULTI_COLUMN_SPAN",
                "複数段組をまたいで選択されているため安全に分割できません。",
                "左段または右段のどちらか片側の数式だけを囲むように選び直してください。",
            )

    # Identify all candidates intersected meaningfully by raw drag (with tiny 1.5pt edge touch grace)
    hit_candidates: list[RawPageTextCandidate] = []
    for cand in candidates:
        if cand.item_type == "vector_drawing":
            # Only consider large non-trivial drawing clusters if no text matched later
            continue
        padded_cand = [
            cand.bbox[0] - 1.5,
            cand.bbox[1] - 1.5,
            cand.bbox[2] + 1.5,
            cand.bbox[3] + 1.5,
        ]
        if _horizontal_overlap(padded_cand, norm_drag) > 0.5 and _vertical_overlap(padded_cand, norm_drag) > 1.5:
            hit_candidates.append(cand)

    if not hit_candidates:
        # Check if user only dragged over vector figure / blank area
        hit_vec = any(
            _horizontal_overlap(c.bbox, norm_drag) > 2.0 and _vertical_overlap(c.bbox, norm_drag) > 2.0
            for c in candidates
            if c.item_type == "vector_drawing"
        )
        if hit_vec:
            return _reject(
                "UNSUPPORTED_BLOCK",
                "図・ベクター要素は文字・数式分離の対象外です。",
                "独立数式と本文が混在しているテキストブロック内の数式範囲をドラッグしてください。",
            )
        return _reject(
            "EMPTY_SELECTION",
            "選択範囲に分割可能な文字・数式が含まれていません。",
            "空白ではなく、数式全体が収まるようにドラッグしてください。",
        )

    non_eq_hits = [c for c in hit_candidates if not c.is_standalone_eq_num]
    if not non_eq_hits:
        return _reject(
            "PARTIAL_CONTENT_LOSS",
            "式番号の小さなブロックだけが選択されており、対応する数式・本文ブロックを含んでいません。",
            "式本体から右端の式番号までを含めるか、混在ブロック側の数式を囲んでドラッグしてください。",
        )

    if any(c.item_type != "text" for c in non_eq_hits):
        return _reject(
            "UNSUPPORTED_BLOCK",
            "画像・図表ブロックを含む範囲は分離対象外です。",
            "数式と本文が混在するテキストブロックのみを範囲に含めてください。",
        )

    if len(non_eq_hits) > 1:
        return _reject(
            "MULTI_BLOCK_SPAN",
            "複数の独立した本文ブロックをまたいで選択しています。",
            "分割したい1つの混在ブロック内にある数式（および右隣の式番号）だけを囲んで選び直してください。",
        )

    parent = non_eq_hits[0]
    for sp in existing_splits:
        if sp.applied_status in (
            "applied",
            "pending_changed",
            "pending_ambiguous",
        ):
            if (
                bbox_iou(parent.bbox, sp.parent_bbox) >= PARENT_MATCH_MIN_IOU
                and parent.source_text_hash == sp.parent_source_hash
            ):
                return _reject(
                    "CONFLICTING_SPLIT",
                    "対象ブロックには既に分割設定が存在します。",
                    "分割し直す場合は、右側の「分割指定一覧」から既存の設定を削除してから再度ドラッグしてください。",
                )

    vlines = parent.visual_lines
    if len(vlines) < 2:
        return _reject(
            "NO_SPLIT_NEEDED",
            "対象ブロックに分離できる複数の行・要素がないため、分割は不要です。",
            "ブロック全体を守りたい場合は、通常の「数式として保護」またはブロック単位の保護切替を使用してください。",
        )

    # Determine which visual lines in `parent` are intentionally covered by `norm_drag`
    selected_indices: list[int] = []
    for idx, vl in enumerate(vlines):
        v_ov = _vertical_overlap(vl.bbox, norm_drag)
        h_ov = _horizontal_overlap(vl.bbox, [norm_drag[0] - DRAG_MARGIN_TOLERANCE_PT, norm_drag[1], norm_drag[2] + DRAG_MARGIN_TOLERANCE_PT, norm_drag[3]])
        if v_ov <= 0.0 or h_ov <= 0.5:
            continue
        v_ratio = v_ov / max(1.0, vl.height)
        threshold = (
            MIN_PROSE_LINE_VERTICAL_OVERLAP_RATIO
            if vl.is_prose_like and not vl.has_math_hint
            else FORMULA_LINE_VERTICAL_OVERLAP_RATIO
        )
        if v_ratio >= threshold:
            selected_indices.append(idx)

    if not selected_indices:
        return _reject(
            "EMPTY_SELECTION",
            "ドラッグ範囲内の行境界を十分に覆っていないため、数式範囲を確定できません。",
            "数式行の高さ全体がしっかり入るように上下の余白を含めてドラッグしてください。",
        )

    s_idx = min(selected_indices)
    e_idx = max(selected_indices)

    # Check if unselected intermediate visual lines exist inside [s_idx .. e_idx]
    if any(i not in selected_indices for i in range(s_idx, e_idx + 1)):
        # If intermediate lines are formula sub-parts, include contiguous block; if prose, reject ambiguity
        inter_prose = any(vlines[i].is_prose_like and not vlines[i].has_math_hint for i in range(s_idx, e_idx + 1) if i not in selected_indices)
        if inter_prose:
            return _reject(
                "AMBIGUOUS_LINE_BOUNDARY",
                "選択範囲の上下間に本文行が挟まっているため、1つの数式領域として切り出せません。",
                "分離したい連続した1組の数式行だけを囲むように範囲を指定してください。",
            )

    # Check every selected visual line for prohibited partial inline cuts
    for i in range(s_idx, e_idx + 1):
        vl = vlines[i]
        if _has_midline_prose_cut(vl, norm_drag):
            return _reject(
                "AMBIGUOUS_LINE_BOUNDARY",
                "同一行の途中に本文と数式候補が混在しており、行内インライン切り出しは安全境界を特定できません。",
                "独立数式行の左右端までを含めて選択してください（行内の一部だけの切り出しは非対応です）。",
            )

    top_vlines = vlines[:s_idx]
    formula_vlines = vlines[s_idx : e_idx + 1]
    bottom_vlines = vlines[e_idx + 1 :]

    if not top_vlines and not bottom_vlines:
        return _reject(
            "NO_SPLIT_NEEDED",
            "ブロック全体が選択されており、分離すべき本文が残っていません。",
            "数式部分だけをドラッグするか、ブロック全体を保護したい場合は既存の手動保護（「数式として保護」）をご利用ください。",
        )

    # Check same-line trailing eq number candidate vs adjacent standalone eq-num block neighbor
    formula_raw_box = _union_boxes([vl.bbox for vl in formula_vlines])
    inline_eq_span: SpanRecord | None = None
    inline_eq_was_outside = False
    for fvl in formula_vlines:
        cand_sp, was_outside = _detect_inline_eq_num_on_line(fvl, norm_drag)
        if cand_sp is not None:
            inline_eq_span = cand_sp
            inline_eq_was_outside = was_outside
            break

    eq_neighbor = _find_standalone_eq_number_neighbor(
        candidates=candidates,
        parent_candidate=parent,
        formula_bbox=formula_raw_box,
        already_hit_candidates=hit_candidates,
    )

    eq_number_text: str | None = None
    eq_number_bbox: list[float] | None = None
    eq_candidate_detected = False
    absorbed_neighbor: RawPageTextCandidate | None = None
    extra_formula_boxes: list[list[float]] = []

    if eq_neighbor is not None:
        eq_candidate_detected = True
        eq_number_text = eq_neighbor.source_text.strip()
        eq_number_bbox = list(eq_neighbor.bbox)
        if include_equation_number:
            absorbed_neighbor = eq_neighbor
            extra_formula_boxes.append(eq_neighbor.bbox)
    elif inline_eq_span is not None:
        eq_number_text = inline_eq_span.text.strip()
        eq_number_bbox = list(inline_eq_span.bbox)
        eq_candidate_detected = inline_eq_was_outside

    # Also include drawing rects strictly inside formula vertical/horizontal neighborhood
    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        db = [round(float(rect.x0), 2), round(float(rect.y0), 2), round(float(rect.x1), 2), round(float(rect.y1), 2)]
        if db[2] <= db[0] or db[3] <= db[1]:
            continue
        # Only compact vector elements overlapping formula vertically & not spilling far into top/bottom prose
        if (db[3] - db[1]) <= max(80.0, (formula_raw_box[3] - formula_raw_box[1]) + 18.0) and (
            _horizontal_overlap(db, [formula_raw_box[0] - 8.0, formula_raw_box[1], formula_raw_box[2] + 8.0, formula_raw_box[3]]) > 2.0
            and _vertical_overlap(db, [formula_raw_box[0], formula_raw_box[1] - 4.0, formula_raw_box[2], formula_raw_box[3] + 4.0]) > 0.0
        ):
            overlaps_top = any(_vertical_overlap(db, tv.bbox) > 2.0 for tv in top_vlines)
            overlaps_bot = any(_vertical_overlap(db, bv.bbox) > 2.0 for bv in bottom_vlines)
            if not overlaps_top and not overlaps_bot:
                extra_formula_boxes.append(db)

    seam_ok, top_bbox, formula_bbox, bottom_bbox, seam_err = _compute_part_bboxes_with_safe_seams(
        top_vlines=top_vlines,
        formula_vlines=formula_vlines,
        bottom_vlines=bottom_vlines,
        extra_formula_boxes=extra_formula_boxes,
    )
    if not seam_ok:
        return _reject(
            "PARTIAL_CONTENT_LOSS",
            seam_err or "本文と数式の境界を安全に分離できません。",
            "数式行だけが収まるようにドラッグ範囲を少し調整してください。",
        )

    formula_vlines_all = list(formula_vlines) + (
        list(absorbed_neighbor.visual_lines) if absorbed_neighbor else []
    )
    absorbed_uids = absorbed_neighbor.span_uids if absorbed_neighbor is not None else set()
    conserved, cons_err = verify_split_conservation(
        parent_span_uids=parent.span_uids,
        top_vlines=top_vlines,
        formula_vlines=formula_vlines_all,
        bottom_vlines=bottom_vlines,
        absorbed_span_uids=absorbed_uids,
    )
    if not conserved:
        return _reject(
            "PARTIAL_CONTENT_LOSS",
            cons_err or "分割後の内容整合性を検証できませんでした。",
            "別の範囲でドラッグし直してください。",
        )

    def _join_vlines_text(vlist: list[VisualLineGroup]) -> tuple[list[dict[str, Any]], str]:
        raw_line_dicts: list[dict[str, Any]] = []
        for vl in vlist:
            for rl in vl.raw_lines:
                raw_line_dicts.append(rl.raw_line)
        t_lines, _, _, _ = extract_text_fn(raw_line_dicts)
        txt = " ".join(t.strip() for t in t_lines if t.strip()).strip()
        if not txt and vlist:
            txt = " ".join(vl.text for vl in vlist if vl.text).strip()
        return raw_line_dicts, txt

    top_raw_lines, top_text = _join_vlines_text(top_vlines)
    formula_raw_lines, formula_text = _join_vlines_text(formula_vlines_all)
    bottom_raw_lines, bottom_text = _join_vlines_text(bottom_vlines)

    parts: list[BlockSplitPreviewPart] = []
    if top_vlines and top_bbox is not None:
        parts.append(
            BlockSplitPreviewPart(
                role="top",
                kind=BlockKind.PARAGRAPH,
                will_protect=False,
                bbox=top_bbox,
                source_text=top_text,
                label_ja="上の本文（翻訳対象）",
            )
        )
    parts.append(
        BlockSplitPreviewPart(
            role="formula",
            kind=BlockKind.ISOLATED_FORMULA,
            will_protect=True,
            bbox=formula_bbox,
            source_text=formula_text,
            label_ja="選択数式（原文保護）",
        )
    )
    if bottom_vlines and bottom_bbox is not None:
        parts.append(
            BlockSplitPreviewPart(
                role="bottom",
                kind=BlockKind.PARAGRAPH,
                will_protect=False,
                bbox=bottom_bbox,
                source_text=bottom_text,
                label_ja="下の本文（翻訳対象）",
            )
        )

    affected_manual_ids = (
        sorted(manual_edit_block_ids & ({parent.base_block_id} | ({absorbed_neighbor.base_block_id} if absorbed_neighbor else set())))
        if manual_edit_block_ids
        else []
    )
    will_invalidate_manual = bool(affected_manual_ids)
    warnings: list[str] = []
    if eq_candidate_detected and eq_number_text:
        if (eq_neighbor is not None and include_equation_number) or inline_eq_span is not None:
            warnings.append(
                f"取り残し防止のため、近傍の式番号候補 {eq_number_text} を数式保護範囲に含めました。"
            )
        else:
            warnings.append(
                f"式番号候補 {eq_number_text} が近傍に検出されました（現在は保護範囲に含めていません）。"
            )
    if will_invalidate_manual:
        warnings.append(
            "対象ブロックに手動編集済みの訳文があります。分割を確定すると再翻訳が必要となり手動訳文は無効化されます。"
        )
    parent_sig = compute_source_text_hash(
        f"{parent.base_block_id}:{parent.source_text_hash}:{parent.bbox}"
    )
    preview = BlockSplitPreviewResult(
        valid=True,
        document_id=document_id,
        page_index=page_index,
        requested_bbox=norm_drag,
        snapped_bbox=formula_bbox,
        equation_number_candidate_bbox=eq_number_bbox,
        equation_number_text=eq_number_text,
        equation_number_auto_included=bool(eq_candidate_detected and ((eq_neighbor is not None and include_equation_number) or inline_eq_span is not None)),
        parent_block_id=parent.base_block_id,
        parent_bbox=list(parent.bbox),
        parent_source_hash=parent.source_text_hash,
        parent_signature=parent_sig,
        parts=parts,
        warnings=warnings,
        will_invalidate_manual_edits=will_invalidate_manual,
        affected_manual_block_ids=affected_manual_ids,
    )

    outcome = SplitExecutionOutcome(
        spec_id="",
        parent_candidate=parent,
        absorbed_eq_candidate=absorbed_neighbor,
        top_lines=top_raw_lines,
        formula_lines=formula_raw_lines,
        bottom_lines=bottom_raw_lines,
        top_bbox=top_bbox,
        formula_bbox=formula_bbox,
        bottom_bbox=bottom_bbox,
        top_text=top_text,
        formula_text=formula_text,
        bottom_text=bottom_text,
        equation_number_text=eq_number_text,
        equation_number_bbox=eq_number_bbox,
    )
    return preview, outcome


def match_split_specs_to_page_candidates(
    *,
    document_id: str,
    page_index: int,
    page_w: float,
    candidates: list[RawPageTextCandidate],
    page_specs: list[BlockSplitSpec],
    drawings: list[dict[str, Any]],
    extract_text_fn: Any,
) -> tuple[
    dict[str, tuple[BlockSplitSpec, SplitExecutionOutcome]],
    set[str],
    list[BlockSplitSpec],
]:
    """Match saved `BlockSplitSpec` records strictly to fresh unsplit `candidates`.

    Returns:
      - `matched_by_parent_id`: parent base_block_id -> (spec, SplitExecutionOutcome)
      - `consumed_eq_block_ids`: standalone eq-number candidate IDs adsorbed into a split
      - `updated_specs`: specs with updated `applied_status` and `status_reason`
    """
    matched_by_parent_id: dict[str, tuple[BlockSplitSpec, SplitExecutionOutcome]] = {}
    consumed_eq_block_ids: set[str] = set()
    updated_specs: list[BlockSplitSpec] = []

    text_candidates = [c for c in candidates if c.item_type == "text"]

    for spec in page_specs:
        exact_matches = [
            c
            for c in text_candidates
            if bbox_iou(c.bbox, spec.parent_bbox) >= PARENT_MATCH_MIN_IOU
            and c.source_text_hash == spec.parent_source_hash
        ]
        if len(exact_matches) == 0:
            near_changed = any(
                bbox_iou(c.bbox, spec.parent_bbox) >= PARENT_CHANGED_MIN_IOU
                for c in text_candidates
            )
            new_status: BlockSplitAppliedStatus = (
                "pending_changed" if near_changed else "pending_missing"
            )
            reason = (
                "親ブロックの内容または解析結果が変化したため、安全のため分割適用を保留しました。"
                if near_changed
                else "保存済み分割設定に対応する親ブロックが元の位置・内容で見つからないため保留しています。"
            )
            updated_specs.append(
                spec.model_copy(
                    update={"applied_status": new_status, "status_reason": reason}
                )
            )
            continue

        if len(exact_matches) > 1:
            updated_specs.append(
                spec.model_copy(
                    update={
                        "applied_status": "pending_ambiguous",
                        "status_reason": "一致する親ブロック候補が複数あるため、誤分割を防ぐ目的で適用を保留しました。",
                    }
                )
            )
            continue

        target_parent = exact_matches[0]
        if target_parent.base_block_id in matched_by_parent_id:
            updated_specs.append(
                spec.model_copy(
                    update={
                        "applied_status": "pending_ambiguous",
                        "status_reason": "同じ親ブロックに対する分割設定が競合しているため保留しました。",
                    }
                )
            )
            continue

        # Evaluate saved drag coordinates cleanly (without rejecting against self in existing_splits)
        other_specs = [s for s in page_specs if s.id != spec.id]
        preview, outcome = evaluate_drag_split_candidate(
            document_id=document_id,
            page_index=page_index,
            page_w=page_w,
            candidates=candidates,
            drag_bbox=spec.requested_bbox,
            existing_splits=other_specs,
            drawings=drawings,
            extract_text_fn=extract_text_fn,
            include_equation_number=spec.include_equation_number,
        )
        if not preview.valid or outcome is None:
            updated_specs.append(
                spec.model_copy(
                    update={
                        "applied_status": "pending_changed",
                        "status_reason": preview.rejection_reason
                        or "再解析時の境界検証を通過できなかったため適用を保留しました。",
                    }
                )
            )
            continue

        outcome.spec_id = spec.id
        matched_by_parent_id[target_parent.base_block_id] = (spec, outcome)
        if outcome.absorbed_eq_candidate is not None:
            consumed_eq_block_ids.add(outcome.absorbed_eq_candidate.base_block_id)
        updated_specs.append(
            spec.model_copy(
                update={
                    "applied_status": "applied",
                    "status_reason": None,
                }
            )
        )

    return matched_by_parent_id, consumed_eq_block_ids, updated_specs
