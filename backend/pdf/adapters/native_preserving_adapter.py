"""Native Preserving PDF Engine Adapter (Section 6, 7, 8, 12).

Implements all 13 PDF processing stages natively using PyMuPDF and OFL Japanese fonts
(`NotoSerifJP-VF.ttf` / `NotoSansJP-VF.ttf`), without requiring any unauthorized external
asset download.

Key guarantees:
1. Original PDF is never modified (verified by SHA-256 before and after).
2. Non-translated elements (isolated equations, figures, tables, references, manual
   protection regions) remain untouched native PDF vector/glyph objects.
3. Inline equations, Greek letters, sub/superscripts, equation numbers, citations, and
   URLs/DOIs are masked as `{v1}`, `{v2}`, ... in `Block.masked_source_text` and tracked
   in `Block.protected_items`.
4. Japanese typesetting applies Kinsoku Shori (line-start/line-end prohibition rules),
   never shrinks below `min_font_size_pt` (default 8.0pt), never overlaps protected
   elements, and never deletes overflowing translations—instead falling back to original
   for that block and emitting a `TYPESETTING_OVERFLOW_FALLBACK` warning.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import pymupdf

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    Document,
    IssueSeverity,
    ProtectedItem,
    ProtectedItemKind,
    ProtectionMode,
    ProtectionRegion,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.pdf.adapters.base import PdfEngineAdapter
from backend.pdf.coordinates import bbox_intersection_ratio, bbox_iou
from backend.pdf.validation.preflight import verify_original_unchanged
from backend.pdf.validation.visual_diff import (
    PageVisualComparisonReport,
    compare_protected_regions_visually,
)
from backend.translation.continuation import attach_continuation_candidates

# Kinsoku Shori character sets (Section 12)
KINSOKU_LINE_START_PROHIBITED = set(
    "、。，．・：；？！゛゜´｀¨＾￣＿ヽヾゝゞ〃仝々〆〇ー—‐／＼〜‖｜…‥’”）〕］｝〉》」』】°′″℃％‰)]},.:;?!"
)
KINSOKU_LINE_END_PROHIBITED = set("‘“（〔［｛〈《「『【([{$￥£€")

# Patterns for inline math, Greek symbols, sub/superscripts, citations, URLs/DOIs
_URL_DOI_RE = re.compile(
    r"(?:https?://\S+|doi:\s*10\.\d{4,9}/[-._;()/:A-Za-z0-9]+|10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)",
    re.IGNORECASE,
)
_EQUATION_REFERENCE_RE = re.compile(
    r"\b(?:Eq(?:uation)?\.?\s*)\(\d+(?:\.\d+)*[a-z]?\)",
    re.IGNORECASE,
)
_MATH_ATOM = (
    r"(?:"
    r"[A-Za-zα-ωΑ-Ω∇∂∑∏∫√]+(?:[′″']+)?(?:\s*[_^]\s*(?:\{[^}]+\}|[0-9A-Za-zα-ωΑ-Ω+\-()]+))*\([^()\n]+\)"
    r"|[A-Za-zα-ωΑ-Ω0-9]+(?:[′″']+)?(?:\s*[_^]\s*(?:\{[^}]+\}|[0-9A-Za-zα-ωΑ-Ω+\-()]+))+"
    r"|[A-Za-zα-ωΑ-Ω]+(?:[′″']+)(?:[0-9A-Za-zα-ωΑ-Ω_^{}\(\)]*)"
    r"|(?:\b(?:sin|cos|tan|log|ln|exp|lim|sup|inf|max|min|arg\s*max|arg\s*min)\s*\([^()\n]+\))"
    r"|[\u0370-\u03ff\u2200-\u22ff\u2190-\u21ff]+[A-Za-zα-ωΑ-Ω0-9_^{}\(\)′″']*"
    r")"
)
_MATH_RHS_ATOM = (
    r"(?:"
    + _MATH_ATOM
    + r"|[0-9]+(?:\.[0-9]+)?(?:e[+\-]?\d+)?"
    + r"|[A-Za-z0-9_^{}\(\)′″']+"
    + r")"
)
_INLINE_MATH_EXPR_RE = re.compile(
    r"(?:"
    r"\$[^$]+\$"
    r"|"
    + _MATH_ATOM
    + r"(?:\s*[=<>≤≥≈≠±+\-*/∈∉⊂⊆∪∩]\s*"
    + _MATH_RHS_ATOM
    + r")*"
    r"|\b[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω_^{}′″']*\s*[=≈≠≤≥]\s*"
    + _MATH_RHS_ATOM
    + r"(?:\s*[+\-*/]\s*"
    + _MATH_RHS_ATOM
    + r")*"
    r")"
)
_EQUATION_NUM_ONLY_RE = re.compile(r"^\s*\(\d+(?:\.\d+)*[a-z]?\)\s*$")
_ISOLATED_FORMULA_LINE_RE = re.compile(
    r"^\s*(?:[A-Za-zα-ωΑ-Ω0-9_^\(\)\[\]{}+*/.,\s|′″'-]*[=<>≤≥≈≠±∑∏∫∂∇∞∈]+\s*[A-Za-zα-ωΑ-Ω0-9_^\(\)\[\]{}+*/.,\s|′″'-]+)\s*(?:\(\d+(?:\.\d+)*\))?\s*$"
)
_HEADING_RE = re.compile(
    r"^(?:Abstract|Introduction|Related Work|Background|Method(?:ology|s)?|Experiments?|Experimental Results|Results|Discussion|Conclusion(?:s)?|References|Bibliography|Acknowledgments?|Appendix(?:\s+[A-Z0-9]+)?|\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9\ufb00-\ufb06\s,:;()'’/–-]{2,75})$"
)
_CAPTION_RE = re.compile(r"^(?:Figure|Fig\.|Table|Tab\.|Algorithm)\s*\d+\s*[:.]", re.IGNORECASE)
_REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.?\s+)?(?:References|Bibliography|Literature Cited)\s*$",
    re.IGNORECASE,
)
_POST_REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:Appendix|Appendices|Supplementary(?:\s+Material)?|Biography|Author\s+Biographies)\b",
    re.IGNORECASE,
)
_REFERENCE_ITEM_RE = re.compile(r"^\s*(?:\[\d+\]|\d+\.\s+[A-Z])")
_EMAIL_RE = re.compile(
    r"(?:\{[A-Za-z0-9._,+\-]+\}|[A-Za-z0-9._%+\-]+)@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    re.IGNORECASE,
)
_ORCID_RE = re.compile(r"\b(?:orcid\.org/)?\d{4}-\d{4}-\d{4}-\d{3}[0-9X]\b", re.IGNORECASE)
_PUBLISHING_META_RE = re.compile(
    r"(?:©|c\s*⃝|Copyright\s+|Springer\s+International\s+Publishing|IEEE\b.*?\b\d{4}\b|ACM\s+ISBN|LNCS\s+\d+|DOI:\s*10\.\d{4,9}/|https?://doi\.org/10\.\d{4,9}/|arXiv:\d{4}\.\d{4,5}|ISSN\s+\d{4}-\d{3}[\dX])",
    re.IGNORECASE,
)
_AFFILIATION_KEYWORD_RE = re.compile(
    r"\b(?:University|Universit[aä]t|Universit[eé]|Institute|Institution|College|Department|Dept\.|School|Laboratory|Lab\.|Faculty|Center|Centre|Academy|Hospital|Corporation|Corp\.|Inc\.|Ltd\.|GmbH|National|Research|Science|Engineering|Technology|Shenzhen|Beijing|Shanghai|Tokyo|China|USA|Japan|UK|Germany|Canada|France|Italy|Korea|Singapore|Australia)\b",
    re.IGNORECASE,
)
_ABSTRACT_START_RE = re.compile(r"^\s*Abstract(?:[\.\s:—-]|$)", re.IGNORECASE)
_KEYWORDS_START_RE = re.compile(r"^\s*(?:Keywords?|Index\s+Terms)(?:[\.\s:—-]|$)", re.IGNORECASE)
_ACK_START_RE = re.compile(r"^\s*Acknowledge?ments?(?:[\.\s:—-]|$)", re.IGNORECASE)
_INTRO_HEADING_RE = re.compile(
    r"^\s*(?:1\.?\s+|I\.?\s+)?Introduction\s*$", re.IGNORECASE
)
_RUNNING_HEADER_PATTERN_RE = re.compile(
    r"(?:^\s*\d{1,4}\s+[A-Z]\.\s*[A-Za-z\-]+|et\s+al\.\s*$|^\s*[A-Z]\.\s*[A-Za-z\-]+\s+et\s+al\.|\s+\d{1,4}\s*$|^\s*\d{1,4}\s*$)"
)


class NativePreservingAdapter(PdfEngineAdapter):
    engine_name: str = "native_preserving_pymupdf"
    engine_version: str = "0.1.0"

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()

    # ------------------------------------------------------------------
    # Stages 4 - 7: Parsing, Reading Order, Protection, Inline Masking
    # ------------------------------------------------------------------
    def parse_and_extract_blocks(
        self,
        document: Document,
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        job_config: TranslationJobConfig | None = None,
    ) -> list[Block]:
        cfg = job_config or TranslationJobConfig()
        manual_by_page: dict[int, list[ProtectionRegion]] = {}
        for reg in manual_regions or []:
            manual_by_page.setdefault(reg.page_index, []).append(reg)

        mupdf_doc = pymupdf.open(document.safe_storage_path)
        all_blocks: list[Block] = []

        try:
            body_font_size = self._estimate_document_body_font_size(mupdf_doc, page_indices)
            in_references_section = False

            for p_idx in page_indices:
                if p_idx < 0 or p_idx >= len(mupdf_doc):
                    continue
                mpage = mupdf_doc[p_idx]
                page_w = float(mpage.cropbox.width)
                page_h = float(mpage.cropbox.height)
                page_manual = manual_by_page.get(p_idx, [])

                raw_dict = mpage.get_text("dict")
                raw_blocks = self._preprocess_page_raw_blocks(
                    raw_dict.get("blocks", []),
                    p_idx=p_idx,
                    page_w=page_w,
                )
                drawings = mpage.get_drawings()
                raw_blocks = self._group_spatial_fraction_equations(raw_blocks, drawings)

                page_candidates: list[Block] = []
                block_seq = 0

                frontmatter_end_y = (
                    self._find_page1_frontmatter_boundary_y(raw_blocks, page_h, body_font_size)
                    if p_idx == 0
                    else 0.0
                )

                # Extract vector drawing clusters as figure/table protection candidates
                drawing_boxes = self._cluster_vector_drawings(drawings, page_w, page_h)
                for d_box in drawing_boxes:
                    block_seq += 1
                    d_blk = Block(
                        id=f"{document.id}:page-{p_idx + 1}:block-{block_seq}",
                        page_index=p_idx,
                        bbox=d_box,
                        kind=BlockKind.FIGURE,
                        source_text="[Vector Figure / Diagram]",
                        masked_source_text="[Vector Figure / Diagram]",
                        status=BlockStatus.PROTECTED_SKIP,
                        protection_source="auto",
                        semantic_category="figure",
                        protection_reason="自動：図・ベクター要素",
                        protection_rule_id="RULE_FIGURE_VECTOR",
                        protection_confidence="high",
                    )
                    self._apply_protection_and_masking(d_blk, page_manual, cfg)
                    page_candidates.append(d_blk)

                for b_item in raw_blocks:
                    b_type = b_item.get("type", 0)
                    bbox = [round(float(x), 2) for x in b_item.get("bbox", [0, 0, 0, 0])]
                    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                        continue

                    if b_type == 1:
                        # Raster image block -> protect as figure
                        block_seq += 1
                        img_blk = Block(
                            id=f"{document.id}:page-{p_idx + 1}:block-{block_seq}",
                            page_index=p_idx,
                            bbox=bbox,
                            kind=BlockKind.FIGURE,
                            source_text="[Raster Image]",
                            masked_source_text="[Raster Image]",
                            status=BlockStatus.PROTECTED_SKIP,
                            protection_source="auto",
                            semantic_category="figure",
                            protection_reason="自動：画像・図表要素",
                            protection_rule_id="RULE_FIGURE_RASTER",
                            protection_confidence="high",
                        )
                        self._apply_protection_and_masking(img_blk, page_manual, cfg)
                        page_candidates.append(img_blk)
                        continue

                    lines = b_item.get("lines", [])
                    if not lines:
                        continue

                    text_lines, avg_font_size, has_math_font, has_sub_super = (
                        self._extract_lines_and_style(lines)
                    )
                    full_text = " ".join(t.strip() for t in text_lines if t.strip()).strip()
                    if not full_text:
                        continue

                    # Stage 4: Classify block kind & academic semantic category
                    (
                        kind,
                        semantic_cat,
                        prot_reason,
                        prot_rule_id,
                        prot_conf,
                        is_uncertain,
                        in_references_section,
                    ) = self._classify_academic_block(
                        text=full_text,
                        line_count=len(text_lines),
                        p_idx=p_idx,
                        bbox=bbox,
                        page_w=page_w,
                        page_h=page_h,
                        avg_font_size=avg_font_size,
                        body_font_size=body_font_size,
                        frontmatter_end_y=frontmatter_end_y,
                        has_math_font=has_math_font,
                        has_sub_super=has_sub_super,
                        drawing_boxes=drawing_boxes,
                        in_references_section=in_references_section,
                    )
                    if b_item.get("_spatial_fraction_equation"):
                        kind = BlockKind.ISOLATED_FORMULA
                        semantic_cat = "formula"
                        prot_reason = "自動：分数線・上下テキスト・式番号の幾何構造"
                        prot_rule_id = "RULE_SPATIAL_FRACTION_EQUATION"
                        prot_conf = "high"
                        is_uncertain = False

                    block_seq += 1
                    blk = Block(
                        id=f"{document.id}:page-{p_idx + 1}:block-{block_seq}",
                        page_index=p_idx,
                        bbox=bbox,
                        kind=kind,
                        source_text=full_text,
                        font_size_pt=round(avg_font_size, 2),
                        is_uncertain_region=is_uncertain,
                        semantic_category=semantic_cat,
                        protection_reason=prot_reason,
                        protection_rule_id=prot_rule_id,
                        protection_confidence=prot_conf,
                    )

                    # Stage 6: Apply protection rules & manual region overrides
                    self._apply_protection_and_masking(blk, page_manual, cfg)
                    page_candidates.append(blk)

                # Reclassify vector drawing boxes and internal cells adjacent to Table captions as TABLE
                table_captions = [
                    b
                    for b in page_candidates
                    if b.kind == BlockKind.CAPTION
                    and re.match(r"^(?:Table|Tab\.)\s*\d+\s*[:.]", b.source_text.strip(), re.IGNORECASE)
                ]
                if table_captions:
                    table_boxes: list[list[float]] = []
                    for cap in table_captions:
                        cx0, cy0, cx1, cy1 = cap.bbox
                        for cand in page_candidates:
                            if cand.id == cap.id:
                                continue
                            bx0, by0, bx1, by1 = cand.bbox
                            horiz_overlap = min(cx1, bx1) - max(cx0, bx0)
                            vert_dist = min(abs(cy0 - by1), abs(by0 - cy1))
                            if (
                                cand.kind == BlockKind.FIGURE
                                and cand.source_text == "[Vector Figure / Diagram]"
                                and horiz_overlap > 20.0
                                and vert_dist <= 45.0
                            ):
                                cand.kind = BlockKind.TABLE
                                cand.semantic_category = "table"
                                cand.protection_reason = "自動：表グリッド要素"
                                cand.protection_rule_id = "RULE_TABLE_GRID"
                                cand.source_text = "[Table Grid]"
                                cand.masked_source_text = "[Table Grid]"
                                self._apply_protection_and_masking(cand, page_manual, cfg)
                                table_boxes.append(cand.bbox)
                    if table_boxes:
                        for cand in page_candidates:
                            if cand.kind == BlockKind.FIGURE and cand.source_text != "[Table Grid]":
                                if any(bbox_intersection_ratio(cand.bbox, tb) >= 0.65 for tb in table_boxes):
                                    cand.kind = BlockKind.TABLE
                                    cand.semantic_category = "table"
                                    cand.protection_reason = "自動：表内部セル"
                                    cand.protection_rule_id = "RULE_TABLE_CELL"
                                    self._apply_protection_and_masking(cand, page_manual, cfg)

                # Sort reading order (Stage 5)
                ordered_page_blocks = self._assign_reading_order(page_candidates, page_w)
                all_blocks.extend(ordered_page_blocks)

            attach_continuation_candidates(all_blocks)
            return all_blocks
        finally:
            mupdf_doc.close()

    def _estimate_document_body_font_size(
        self, mupdf_doc: pymupdf.Document, page_indices: list[int]
    ) -> float:
        prose_sizes: list[float] = []
        for p_idx in page_indices[:5]:
            if p_idx < 0 or p_idx >= len(mupdf_doc):
                continue
            raw_dict = mupdf_doc[p_idx].get_text("dict")
            for b in raw_dict.get("blocks", []):
                if b.get("type") != 0:
                    continue
                lines = b.get("lines", [])
                if len(lines) < 3:
                    continue
                _, avg_sz, _, _ = self._extract_lines_and_style(lines)
                if 7.5 <= avg_sz <= 12.5:
                    prose_sizes.append(avg_sz)
        if not prose_sizes:
            return 10.0
        prose_sizes.sort()
        return prose_sizes[len(prose_sizes) // 2]

    def _cluster_vector_drawings(
        self, drawings: list[dict], page_w: float, page_h: float
    ) -> list[list[float]]:
        boxes: list[list[float]] = []
        for d in drawings:
            r = d.get("rect")
            if not r:
                continue
            x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
            w, h = x1 - x0, y1 - y0
            # Ignore tiny underlines or full-page border frames
            if (w < 20 and h < 12) or (w > page_w * 0.95 and h > page_h * 0.95):
                continue
            boxes.append([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])

        # Merge overlapping drawing boxes
        merged = True
        while merged and len(boxes) > 1:
            merged = False
            new_boxes: list[list[float]] = []
            used = [False] * len(boxes)
            for i in range(len(boxes)):
                if used[i]:
                    continue
                cur = list(boxes[i])
                for j in range(i + 1, len(boxes)):
                    if used[j]:
                        continue
                    if (
                        bbox_iou(cur, boxes[j]) > 0.01
                        or bbox_intersection_ratio(boxes[j], cur) > 0.1
                        or self._boxes_close(cur, boxes[j], margin=8.0)
                    ):
                        cur = [
                            min(cur[0], boxes[j][0]),
                            min(cur[1], boxes[j][1]),
                            max(cur[2], boxes[j][2]),
                            max(cur[3], boxes[j][3]),
                        ]
                        used[j] = True
                        merged = True
                new_boxes.append(cur)
            boxes = new_boxes

        return [b for b in boxes if (b[2] - b[0]) >= 30 and (b[3] - b[1]) >= 18]

    @staticmethod
    def _group_spatial_fraction_equations(
        raw_blocks: list[dict], drawings: list[dict]
    ) -> list[dict]:
        """Group equation text only when a fraction bar and equation label establish layout."""
        span_rows: list[dict] = []
        for block_index, item in enumerate(raw_blocks):
            if item.get("type", 0) != 0:
                continue
            for line_index, line in enumerate(item.get("lines", [])):
                baselines: list[dict] = []
                for span_index, span in enumerate(line.get("spans", [])):
                    span_bbox = [float(value) for value in span.get("bbox", [0, 0, 0, 0])]
                    if len(span_bbox) != 4 or not str(span.get("text", "")):
                        continue
                    center_y = (span_bbox[1] + span_bbox[3]) / 2.0
                    row = next(
                        (
                            candidate
                            for candidate in baselines
                            if abs(candidate["center_y"] - center_y) <= 1.5
                        ),
                        None,
                    )
                    if row is None:
                        row = {"center_y": center_y, "spans": []}
                        baselines.append(row)
                    row["spans"].append(
                        (block_index, line_index, span_index, span, span_bbox)
                    )

                for row in baselines:
                    row_spans = sorted(row["spans"], key=lambda item: item[4][0])
                    row_boxes = [item[4] for item in row_spans]
                    span_rows.append(
                        {
                            "block_index": block_index,
                            "text": "".join(str(item[3].get("text", "")) for item in row_spans).strip(),
                            "bbox": [
                                min(box[0] for box in row_boxes),
                                min(box[1] for box in row_boxes),
                                max(box[2] for box in row_boxes),
                                max(box[3] for box in row_boxes),
                            ],
                            "spans": row_spans,
                        }
                    )

        groups: list[dict] = []
        used_spans: set[tuple[int, int, int]] = set()
        for drawing in drawings:
            for operation in drawing.get("items", []):
                if len(operation) < 2 or operation[0] != "l":
                    continue
                if len(operation) >= 3:
                    start, end = operation[1], operation[2]
                else:
                    start, end = operation[1]
                x0, x1 = sorted((float(start.x), float(end.x)))
                y0, y1 = sorted((float(start.y), float(end.y)))
                if x1 - x0 < 30.0 or y1 - y0 > 1.5:
                    continue
                bar_y = (y0 + y1) / 2.0

                above: list[tuple[dict, float]] = []
                below: list[tuple[dict, float]] = []
                labels: list[dict] = []
                left_sides: list[dict] = []
                for row in span_rows:
                    if any(
                        (block_index, line_index, span_index) in used_spans
                        for block_index, line_index, span_index, _, _ in row["spans"]
                    ):
                        continue
                    text = row["text"]
                    bbox = row["bbox"]
                    center_y = (bbox[1] + bbox[3]) / 2.0
                    if re.search(r"\(\s*\d+(?:\.\d+)*[a-z]?\s*\)\s*$", text, re.IGNORECASE):
                        if (
                            abs(center_y - bar_y) <= 12.0
                            and abs((bbox[0] + bbox[2]) / 2.0 - (x0 + x1) / 2.0) <= 110.0
                        ):
                            labels.append(row)

                    overlap = min(x1, bbox[2]) - max(x0, bbox[0])
                    required_overlap = min(24.0, max(4.0, (bbox[2] - bbox[0]) * 0.35))
                    if overlap >= required_overlap:
                        if bbox[3] <= bar_y + 1.0 and bar_y - bbox[3] <= 24.0:
                            if re.search(r"[A-Za-z]{2,}", text):
                                above.append((row, bar_y - bbox[3]))
                        if bbox[1] >= bar_y - 1.0 and bbox[1] - bar_y <= 24.0:
                            if re.search(r"[A-Za-z]{2,}", text):
                                below.append((row, bbox[1] - bar_y))

                    if (
                        bbox[2] <= x0 + 5.0
                        and abs(center_y - bar_y) <= 8.0
                        and len(re.findall(r"[A-Za-z]{2,}", text)) >= 2
                        and re.search(r"=\s*$", text)
                    ):
                        left_sides.append(row)

                if not above or not below or not labels or not left_sides:
                    continue
                numerator, _ = min(above, key=lambda candidate: candidate[1])
                denominator, _ = min(below, key=lambda candidate: candidate[1])
                numerator_center_x = (numerator["bbox"][0] + numerator["bbox"][2]) / 2.0
                denominator_center_x = (denominator["bbox"][0] + denominator["bbox"][2]) / 2.0
                if abs(numerator_center_x - denominator_center_x) > max(
                    18.0, (x1 - x0) * 0.25
                ):
                    continue
                lhs = min(
                    left_sides,
                    key=lambda candidate: abs(
                        (candidate["bbox"][1] + candidate["bbox"][3]) / 2.0 - bar_y
                    ),
                )
                equation_number = min(
                    labels,
                    key=lambda candidate: abs(
                        (candidate["bbox"][1] + candidate["bbox"][3]) / 2.0 - bar_y
                    ),
                )
                selected_rows = []
                selected_keys: set[tuple[int, int, int]] = set()
                for row in (lhs, numerator, denominator, equation_number):
                    row_keys = {
                        (block_index, line_index, span_index)
                        for block_index, line_index, span_index, _, _ in row["spans"]
                    }
                    if row_keys & selected_keys:
                        continue
                    selected_rows.append(row)
                    selected_keys.update(row_keys)
                if selected_keys & used_spans:
                    continue

                member_lines = [
                    {
                        "bbox": row["bbox"],
                        "spans": [span for _, _, _, span, _ in row["spans"]],
                    }
                    for row in selected_rows
                ]
                member_boxes = [row["bbox"] for row in selected_rows]
                union_bbox = [
                    min(x0 - 1.0, *(bbox[0] for bbox in member_boxes)),
                    min(y0 - 1.0, *(bbox[1] for bbox in member_boxes)),
                    max(x1 + 1.0, *(bbox[2] for bbox in member_boxes)),
                    max(y1 + 1.0, *(bbox[3] for bbox in member_boxes)),
                ]
                grouped_block = {
                    "type": 0,
                    "bbox": union_bbox,
                    "lines": member_lines,
                    "_spatial_fraction_equation": True,
                }
                groups.append(
                    {
                        "start": min(key[0] for key in selected_keys),
                        "keys": selected_keys,
                        "block": grouped_block,
                    }
                )
                used_spans.update(selected_keys)

        if not groups:
            return raw_blocks

        groups_by_start: dict[int, list[dict]] = {}
        selected_by_block: dict[int, set[tuple[int, int, int]]] = {}
        for group in groups:
            groups_by_start.setdefault(group["start"], []).append(group["block"])
            for block_index, line_index, span_index in group["keys"]:
                selected_by_block.setdefault(block_index, set()).add(
                    (block_index, line_index, span_index)
                )

        result: list[dict] = []
        for index, item in enumerate(raw_blocks):
            result.extend(groups_by_start.get(index, []))
            selected = selected_by_block.get(index, set())
            if not selected:
                result.append(item)
                continue

            remaining_lines = []
            remaining_boxes = []
            for line_index, line in enumerate(item.get("lines", [])):
                spans = [
                    span
                    for span_index, span in enumerate(line.get("spans", []))
                    if (index, line_index, span_index) not in selected
                ]
                if not spans:
                    continue
                line_boxes = [
                    [float(value) for value in span.get("bbox", [0, 0, 0, 0])]
                    for span in spans
                ]
                remaining_boxes.extend(line_boxes)
                remaining_lines.append(
                    {
                        **line,
                        "bbox": [
                            min(box[0] for box in line_boxes),
                            min(box[1] for box in line_boxes),
                            max(box[2] for box in line_boxes),
                            max(box[3] for box in line_boxes),
                        ],
                        "spans": spans,
                    }
                )
            if remaining_lines:
                result.append(
                    {
                        **item,
                        "bbox": [
                            min(box[0] for box in remaining_boxes),
                            min(box[1] for box in remaining_boxes),
                            max(box[2] for box in remaining_boxes),
                            max(box[3] for box in remaining_boxes),
                        ],
                        "lines": remaining_lines,
                    }
                )
        return result

    @staticmethod
    def _boxes_close(a: list[float], b: list[float], margin: float = 8.0) -> bool:
        expanded_a = [a[0] - margin, a[1] - margin, a[2] + margin, a[3] + margin]
        return bbox_iou(expanded_a, b) > 0.0

    def _extract_lines_and_style(
        self, lines: list[dict]
    ) -> tuple[list[str], float, bool, bool]:
        text_lines: list[str] = []
        sizes: list[float] = []
        has_math_font = False
        has_sub_super = False

        for line in lines:
            spans = line.get("spans", [])
            if not spans:
                continue
            line_sizes = [float(s.get("size", 10.0)) for s in spans if s.get("text", "").strip()]
            max_line_size = max(line_sizes) if line_sizes else 10.0
            line_parts: list[str] = []

            for s in spans:
                t = str(s.get("text", ""))
                if not t:
                    continue
                sz = float(s.get("size", 10.0))
                font_name = str(s.get("font", "")).lower()
                flags = int(s.get("flags", 0))
                sizes.append(sz)

                if any(k in font_name for k in ("cmmi", "cmsy", "cmex", "math", "symbol")):
                    has_math_font = True
                if (flags & 1) or (sz < max_line_size * 0.78 and len(t.strip()) <= 4):
                    has_sub_super = True

                line_parts.append(t)

            joined_line = "".join(line_parts)
            if joined_line.strip():
                text_lines.append(joined_line)

        avg_size = sum(sizes) / len(sizes) if sizes else 10.0
        return text_lines, avg_size, has_math_font, has_sub_super

    def _preprocess_page_raw_blocks(
        self, raw_blocks: list[dict], p_idx: int, page_w: float
    ) -> list[dict]:
        """1. On Page 1 (p_idx == 0), split any single raw block that mixes front-matter lines
              with an `Abstract` or `Keywords` line at the exact line boundary.
           2. Across all pages, coalesce vertically overlapping prose blocks that were split
              by PyMuPDF due to inline prime symbols (`N` + `′′(f)`) or inline fraction
              denominators (`H = y` + `x.`).
        """
        split_blocks: list[dict] = []
        for b in raw_blocks:
            if b.get("type", 0) != 0:
                split_blocks.append(b)
                continue
            lines = b.get("lines", [])
            if p_idx == 0 and len(lines) >= 2:
                # Check if an interior line begins `Abstract.` or `Keywords:`
                split_indices: list[int] = []
                for idx, ln in enumerate(lines):
                    if idx == 0:
                        continue
                    ln_txt = "".join(str(s.get("text", "")) for s in ln.get("spans", [])).strip()
                    if _ABSTRACT_START_RE.match(ln_txt) or _KEYWORDS_START_RE.match(ln_txt):
                        split_indices.append(idx)
                if split_indices:
                    boundaries = [0] + split_indices + [len(lines)]
                    for s_i, e_i in zip(boundaries[:-1], boundaries[1:]):
                        sub_lines = lines[s_i:e_i]
                        if not sub_lines:
                            continue
                        sub_bbox = [
                            min(float(l["bbox"][0]) for l in sub_lines),
                            min(float(l["bbox"][1]) for l in sub_lines),
                            max(float(l["bbox"][2]) for l in sub_lines),
                            max(float(l["bbox"][3]) for l in sub_lines),
                        ]
                        split_blocks.append({"type": 0, "bbox": sub_bbox, "lines": sub_lines})
                    continue
            split_blocks.append(b)

        # Pass 2: Coalesce vertically split prose paragraphs
        coalesced: list[dict] = []
        for b in split_blocks:
            if b.get("type", 0) != 0 or not coalesced or coalesced[-1].get("type", 0) != 0:
                coalesced.append(b)
                continue

            prev = coalesced[-1]
            prev_lines = prev.get("lines", [])
            cur_lines = b.get("lines", [])
            if not prev_lines or not cur_lines:
                coalesced.append(b)
                continue

            prev_txt_lines, _, _, _ = self._extract_lines_and_style(prev_lines)
            cur_txt_lines, _, _, _ = self._extract_lines_and_style(cur_lines)
            prev_text = " ".join(t.strip() for t in prev_txt_lines if t.strip()).strip()
            cur_text = " ".join(t.strip() for t in cur_txt_lines if t.strip()).strip()
            if not prev_text or not cur_text:
                coalesced.append(b)
                continue

            p_bbox = [float(x) for x in prev.get("bbox", [0, 0, 0, 0])]
            c_bbox = [float(x) for x in b.get("bbox", [0, 0, 0, 0])]
            y_overlap = min(p_bbox[3], c_bbox[3]) - max(p_bbox[1], c_bbox[1])
            x_overlap = min(p_bbox[2], c_bbox[2]) - max(p_bbox[0], c_bbox[0])

            prev_prose_words = re.findall(r"[A-Za-z]{3,}", prev_text)
            prev_ends_sentence = bool(re.search(r"[.?!:;]\s*$", prev_text))

            # Case A: Prose block split at inline prime/superscript (`N` + `′′(f)` or `N` + `′′ D(f)`)
            if (
                len(prev_prose_words) >= 4
                and not prev_ends_sentence
                and y_overlap >= 3.0
                and x_overlap >= 15.0
                and re.match(r"^[′″'_^\(\)]", cur_text)
            ):
                # Join prime/sub/superscript directly onto the last span of prev without space
                first_cur_line = cur_txt_lines[0].strip() if cur_txt_lines else ""
                if (
                    len(cur_txt_lines) >= 2
                    and re.match(r"^[′″']+$", first_cur_line)
                    and re.match(r"^[A-Za-z0-9(\[]", cur_txt_lines[1].strip())
                ):
                    merged_head = first_cur_line + cur_txt_lines[1].strip()
                    remaining_cur_lines = cur_lines[2:]
                else:
                    merged_head = first_cur_line
                    remaining_cur_lines = cur_lines[1:]

                if prev_lines[-1].get("spans"):
                    prev_lines[-1]["spans"][-1]["text"] = (
                        str(prev_lines[-1]["spans"][-1].get("text", "")).rstrip() + merged_head
                    )
                prev["lines"] = prev_lines + remaining_cur_lines
                prev["bbox"] = [
                    min(p_bbox[0], c_bbox[0]),
                    min(p_bbox[1], c_bbox[1]),
                    max(p_bbox[2], c_bbox[2]),
                    max(p_bbox[3], c_bbox[3]),
                ]
                continue

            # Case B: Prose block with an embedded inline fraction denominator at the end (`H = y` + `x.`)
            if (
                len(prev_prose_words) >= 4
                and not prev_ends_sentence
                and len(cur_text) <= 8
                and (
                    bbox_intersection_ratio(c_bbox, p_bbox) >= 0.45
                    or (y_overlap >= 4.0 and c_bbox[0] >= p_bbox[0] and c_bbox[2] <= p_bbox[2] + 6.0)
                )
            ):
                sep = "/" if re.search(r"=\s*[A-Za-z0-9α-ωΑ-Ω]+\s*$", prev_text) else ""
                if prev_lines[-1].get("spans"):
                    prev_lines[-1]["spans"][-1]["text"] = (
                        str(prev_lines[-1]["spans"][-1].get("text", "")).rstrip() + sep + cur_text
                    )
                prev["bbox"] = [
                    min(p_bbox[0], c_bbox[0]),
                    min(p_bbox[1], c_bbox[1]),
                    max(p_bbox[2], c_bbox[2]),
                    max(p_bbox[3], c_bbox[3]),
                ]
                continue

            coalesced.append(b)

        return coalesced

    def _find_page1_frontmatter_boundary_y(
        self, raw_blocks: list[dict], page_h: float, body_font_size: float
    ) -> float:
        """Find the vertical Y coordinate on Page 1 where Abstract, Keywords, or Section 1 starts."""
        for b in raw_blocks:
            if b.get("type", 0) != 0:
                continue
            lines = b.get("lines", [])
            if not lines:
                continue
            text_lines, avg_sz, _, _ = self._extract_lines_and_style(lines)
            full_text = " ".join(t.strip() for t in text_lines if t.strip()).strip()
            if not full_text:
                continue
            y0 = float(b.get("bbox", [0, 0, 0, 0])[1])
            if (
                _ABSTRACT_START_RE.match(full_text)
                or _KEYWORDS_START_RE.match(full_text)
                or _INTRO_HEADING_RE.match(full_text)
                or re.match(r"^\s*1(?:\.0)?\.?\s+[A-Z]", full_text)
            ):
                return y0
        return page_h * 0.30

    def _classify_academic_block(
        self,
        text: str,
        line_count: int,
        p_idx: int,
        bbox: list[float],
        page_w: float,
        page_h: float,
        avg_font_size: float,
        body_font_size: float,
        frontmatter_end_y: float,
        has_math_font: bool,
        has_sub_super: bool,
        drawing_boxes: list[list[float]],
        in_references_section: bool,
    ) -> tuple[BlockKind, str, str | None, str | None, str, bool, bool]:
        """Classify block into (BlockKind, semantic_category, protection_reason,
        protection_rule_id, protection_confidence, is_uncertain, next_in_references_section).
        """
        stripped = text.strip()
        is_large_page1_title = (
            p_idx == 0
            and avg_font_size >= max(12.2, body_font_size + 1.8)
            and bbox[1] < page_h * 0.28
            and len(stripped) < 220
        )

        # 1. Running Header / Footer check (top 7.5% or bottom 7% of page)
        if not is_large_page1_title and len(stripped) <= 130 and line_count <= 2:
            if bbox[1] < page_h * 0.075 and bbox[3] < page_h * 0.095:
                if p_idx >= 1 or _RUNNING_HEADER_PATTERN_RE.search(stripped) or avg_font_size <= body_font_size - 0.4:
                    return (
                        BlockKind.HEADER_FOOTER,
                        "running_header",
                        "自動：ランニングヘッダ・ページ番号",
                        "RULE_RUNNING_HEADER_FOOTER",
                        "high",
                        False,
                        in_references_section,
                    )
            if bbox[3] > page_h * 0.935 and bbox[1] > page_h * 0.90 and not _PUBLISHING_META_RE.search(stripped):
                return (
                    BlockKind.HEADER_FOOTER,
                    "running_footer",
                    "自動：ランニングフッタ・ページ番号",
                    "RULE_RUNNING_HEADER_FOOTER",
                    "high",
                    False,
                    in_references_section,
                )

        # 2. Publishing Metadata / Copyright / DOI notice on Page 1 (or bottom footnote)
        if not in_references_section and _PUBLISHING_META_RE.search(stripped):
            if (
                (p_idx == 0 and (bbox[1] > page_h * 0.75 or bbox[3] < page_h * 0.14 or avg_font_size <= body_font_size - 1.0))
                or (avg_font_size <= body_font_size - 1.2 and bbox[1] > page_h * 0.82)
            ) and len(stripped) <= 360:
                return (
                    BlockKind.HEADER_FOOTER,
                    "publishing_metadata",
                    "自動：DOI・出版・著作権表記",
                    "RULE_PUBLISHING_METADATA",
                    "high",
                    False,
                    in_references_section,
                )

        # 3. Inside a vector drawing / table box -> classify as FIGURE
        for d_box in drawing_boxes:
            if bbox_intersection_ratio(bbox, d_box) >= 0.65:
                if not _CAPTION_RE.match(stripped):
                    return (
                        BlockKind.FIGURE,
                        "figure",
                        "自動：図表内部テキスト",
                        "RULE_FIGURE_INTERNAL_TEXT",
                        "high",
                        False,
                        in_references_section,
                    )

        # 4. Caption check
        if _CAPTION_RE.match(stripped):
            return (
                BlockKind.CAPTION,
                "caption",
                None,
                None,
                "high",
                False,
                in_references_section,
            )

        # 5. References section heading & multi-page continuation
        if _REFERENCE_HEADING_RE.match(stripped) and len(stripped) <= 40:
            return (
                BlockKind.HEADING,
                "reference_heading",
                "自動：参考文献見出し",
                "RULE_REFERENCES_HEADING",
                "high",
                False,
                True,
            )

        if in_references_section:
            if _POST_REFERENCE_HEADING_RE.match(stripped) and (
                avg_font_size >= body_font_size + 0.8 or len(stripped) <= 70
            ):
                in_references_section = False
            else:
                return (
                    BlockKind.REFERENCE,
                    "reference",
                    "自動：参考文献リスト",
                    "RULE_REFERENCES_ENTRY",
                    "high",
                    False,
                    True,
                )

        if _REFERENCE_ITEM_RE.match(stripped) and (
            re.search(r"\b(?:19\d\d|20\d\d)\b", stripped)
            or re.search(r"\b(?:pp\.|vol\.|IEEE|ACM|Springer|Journal|Proc\.|Trans\.)\b", stripped, re.IGNORECASE)
        ):
            return (
                BlockKind.REFERENCE,
                "reference",
                "自動：参考文献リスト",
                "RULE_REFERENCES_ENTRY",
                "high",
                False,
                in_references_section,
            )

        # 6. Page 1 Front-Matter (Title, Authors, Affiliations, Emails/ORCID) before Abstract/Section 1
        if is_large_page1_title:
            return (
                BlockKind.TITLE,
                "title",
                "自動：論文タイトル",
                "RULE_FRONTMATTER_TITLE",
                "high",
                False,
                in_references_section,
            )

        if (
            p_idx == 0
            and bbox[1] < frontmatter_end_y - 1.0
            and not _ABSTRACT_START_RE.match(stripped)
            and not _KEYWORDS_START_RE.match(stripped)
            and not _INTRO_HEADING_RE.match(stripped)
            and not _HEADING_RE.match(stripped)
        ):
            has_email_or_orcid = bool(_EMAIL_RE.search(stripped) or _ORCID_RE.search(stripped))
            has_affiliation_kw = bool(_AFFILIATION_KEYWORD_RE.search(stripped))
            if has_email_or_orcid and not has_affiliation_kw:
                return (
                    BlockKind.HEADER_FOOTER,
                    "email_or_contact",
                    "自動：メールアドレス・識別情報",
                    "RULE_FRONTMATTER_EMAIL",
                    "high",
                    False,
                    in_references_section,
                )
            if has_affiliation_kw:
                return (
                    BlockKind.HEADER_FOOTER,
                    "affiliation",
                    "自動：所属・連絡先",
                    "RULE_FRONTMATTER_AFFILIATION",
                    "high",
                    False,
                    in_references_section,
                )
            # Author name line check: capitalized personal names, commas, 'and', superscripts/(B)
            prose_verbs = re.findall(
                r"\b(?:is|are|was|were|be|have|has|propose|present|introduce|show|demonstrate|study|investigate|develop)\b",
                stripped,
                re.IGNORECASE,
            )
            has_person_name = bool(
                re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+", stripped)
            )
            if has_person_name and not prose_verbs and len(stripped) <= 250 and line_count <= 4:
                return (
                    BlockKind.HEADER_FOOTER,
                    "author",
                    "自動：著者名",
                    "RULE_FRONTMATTER_AUTHOR",
                    "high",
                    False,
                    in_references_section,
                )
            # Short ambiguous front-matter line above Abstract -> protect as review_candidate
            if len(stripped) <= 200 and line_count <= 3 and not prose_verbs:
                return (
                    BlockKind.HEADER_FOOTER,
                    "affiliation",
                    "自動：フロントマター（要確認候補）",
                    "RULE_FRONTMATTER_CANDIDATE",
                    "review_candidate",
                    True,
                    in_references_section,
                )

        # Also protect standalone email/ORCID blocks anywhere on Page 1
        if p_idx == 0 and (_EMAIL_RE.search(stripped) or _ORCID_RE.search(stripped)) and len(stripped) <= 160:
            if not _AFFILIATION_KEYWORD_RE.search(stripped):
                return (
                    BlockKind.HEADER_FOOTER,
                    "email_or_contact",
                    "自動：メールアドレス・識別情報",
                    "RULE_FRONTMATTER_EMAIL",
                    "high",
                    False,
                    in_references_section,
                )

        # 7. Isolated equation check (excluding emails and prose sentences)
        math_Tokens = len(
            re.findall(r"[=<>≤≥≈≠±∑∏∫∂∇∞∈∉⊂⊆∪∩∀∃α-ωΑ-Ω_^\(\)\[\]{}+*/|′″'-]", stripped)
        )
        alpha_words = re.findall(r"[A-Za-z]{4,}", stripped)
        prose_clause_words = re.findall(
            r"\b(?:where|which|that|with|from|into|given|by|is|are|can|be|obtained|represents|denotes|follows)\b",
            stripped,
            re.IGNORECASE,
        )
        if _EQUATION_NUM_ONLY_RE.match(stripped):
            return (
                BlockKind.ISOLATED_FORMULA,
                "formula",
                "自動：独立数式・式番号",
                "RULE_ISOLATED_FORMULA",
                "high",
                False,
                in_references_section,
            )
        if (
            len(alpha_words) <= 2
            and len(prose_clause_words) < 2
            and not _EMAIL_RE.search(stripped)
            and _ISOLATED_FORMULA_LINE_RE.match(stripped)
        ):
            return (
                BlockKind.ISOLATED_FORMULA,
                "formula",
                "自動：独立数式",
                "RULE_ISOLATED_FORMULA",
                "high",
                False,
                in_references_section,
            )

        # High symbol density check for multi-part display equations (e.g. fractions, integrals, matrices)
        if (
            len(alpha_words) <= 2
            and len(prose_clause_words) < 2
            and not _EMAIL_RE.search(stripped)
            and (math_Tokens >= max(3, int(len(stripped) * 0.22)) or has_math_font)
            and len(stripped) < 180
        ):
            return (
                BlockKind.ISOLATED_FORMULA,
                "formula",
                "自動：独立数式",
                "RULE_ISOLATED_FORMULA",
                "high",
                False,
                in_references_section,
            )

        # 8. Abstract / Keywords / Acknowledgements / Section Heading / Body Paragraph
        if _ABSTRACT_START_RE.match(stripped) and len(stripped) > 12:
            is_uncertain = has_math_font or (has_sub_super and math_Tokens >= 6)
            return (
                BlockKind.PARAGRAPH,
                "abstract",
                None,
                None,
                "high",
                is_uncertain,
                in_references_section,
            )
        if _KEYWORDS_START_RE.match(stripped):
            return (
                BlockKind.PARAGRAPH,
                "keywords",
                None,
                None,
                "high",
                False,
                in_references_section,
            )
        if _ACK_START_RE.match(stripped) and len(stripped) > 20:
            return (
                BlockKind.PARAGRAPH,
                "acknowledgements",
                None,
                None,
                "high",
                False,
                in_references_section,
            )

        if _HEADING_RE.match(stripped) or (
            avg_font_size >= max(11.5, body_font_size + 1.5) and len(stripped) < 110
        ):
            return (
                BlockKind.HEADING,
                "section_heading",
                None,
                None,
                "high",
                False,
                in_references_section,
            )

        is_uncertain = has_math_font or (has_sub_super and math_Tokens >= 6)
        return (
            BlockKind.PARAGRAPH,
            "body",
            None,
            None,
            "high",
            is_uncertain,
            in_references_section,
        )

    def _apply_protection_and_masking(
        self,
        block: Block,
        page_manual_regions: list[ProtectionRegion],
        cfg: TranslationJobConfig,
    ) -> None:
        # 0. Explicit block-level user override (`force_translate` or `force_protect`) takes top precedence
        if block.user_protection_override == "force_protect":
            block.status = BlockStatus.PROTECTED_SKIP
            block.protection_source = "manual"
            block.protection_reason = "手動指定：非翻訳保護（ブロック指定）"
            block.masked_source_text = block.source_text
            block.translation_provenance = "protected_skip"
            return

        manual_override_translate = block.user_protection_override == "force_translate"
        if manual_override_translate:
            block.protection_source = "manual"
            block.protection_reason = "手動解除：翻訳対象に変更"

        # 1. Check if a manual protection region rectangle overrides this block (Stage 6)
        if not manual_override_translate:
            for reg in page_manual_regions:
                overlap = bbox_intersection_ratio(block.bbox, reg.bbox)
                iou = bbox_iou(block.bbox, reg.bbox)
                if overlap >= 0.35 or iou >= 0.25:
                    if reg.mode == ProtectionMode.DO_NOT_TRANSLATE:
                        block.status = BlockStatus.PROTECTED_SKIP
                        block.protection_source = "manual"
                        block.protection_reason = f"手動：非翻訳矩形 ({reg.note or '指定領域'})"
                        block.masked_source_text = block.source_text
                        block.translation_provenance = "protected_skip"
                        return
                    if reg.mode == ProtectionMode.PROTECT_AS_FORMULA:
                        block.kind = BlockKind.ISOLATED_FORMULA
                        block.semantic_category = "formula"
                        block.status = BlockStatus.PROTECTED_SKIP
                        block.protection_source = "manual"
                        block.protection_reason = "手動：数式として保護"
                        block.masked_source_text = block.source_text
                        block.translation_provenance = "protected_skip"
                        return
                    if reg.mode == ProtectionMode.PROTECT_AS_FIGURE:
                        block.kind = BlockKind.FIGURE
                        block.semantic_category = "figure"
                        block.status = BlockStatus.PROTECTED_SKIP
                        block.protection_source = "manual"
                        block.protection_reason = "手動：図表として保護"
                        block.masked_source_text = block.source_text
                        block.translation_provenance = "protected_skip"
                        return
                    if reg.mode == ProtectionMode.TRANSLATE_AS_BODY:
                        block.kind = BlockKind.PARAGRAPH
                        block.status = BlockStatus.PENDING
                        block.protection_source = "manual"
                        block.protection_reason = "手動：本文として翻訳"
                        manual_override_translate = True
                        break

        # 2. Automatic non-translation rules based on kind, semantic_category & config (Section 3, 4, 7, 13.C)
        if not manual_override_translate:
            auto_protected_categories = {
                "title",
                "author",
                "affiliation",
                "email_or_contact",
                "publishing_metadata",
                "running_header",
                "running_footer",
            }
            if block.semantic_category in auto_protected_categories or block.kind in (
                BlockKind.TITLE,
                BlockKind.ISOLATED_FORMULA,
                BlockKind.HEADER_FOOTER,
            ):
                block.status = BlockStatus.PROTECTED_SKIP
                block.protection_source = "auto"
                if not block.protection_reason:
                    block.protection_reason = (
                        "自動：論文タイトル"
                        if block.kind == BlockKind.TITLE
                        else (
                            "自動：独立数式"
                            if block.kind == BlockKind.ISOLATED_FORMULA
                            else "自動：ヘッダ・フッタ・メタ情報"
                        )
                    )
                block.masked_source_text = block.source_text
                block.translation_provenance = "protected_skip"
                return
            if block.kind == BlockKind.FIGURE and not cfg.translate_figures:
                block.status = BlockStatus.PROTECTED_SKIP
                block.protection_source = "auto"
                if not block.protection_reason:
                    block.protection_reason = "自動：図表要素"
                block.masked_source_text = block.source_text
                block.translation_provenance = "protected_skip"
                return
            if block.kind == BlockKind.TABLE and not cfg.translate_tables:
                block.status = BlockStatus.PROTECTED_SKIP
                block.protection_source = "auto"
                if not block.protection_reason:
                    block.protection_reason = "自動：表要素"
                block.masked_source_text = block.source_text
                block.translation_provenance = "protected_skip"
                return
            if (
                block.kind == BlockKind.REFERENCE
                or block.semantic_category in ("reference_heading", "reference")
            ) and not cfg.translate_references:
                block.status = BlockStatus.PROTECTED_SKIP
                block.protection_source = "auto"
                if not block.protection_reason:
                    block.protection_reason = (
                        "自動：参考文献見出し"
                        if block.semantic_category == "reference_heading"
                        else "自動：参考文献リスト"
                    )
                block.masked_source_text = block.source_text
                block.translation_provenance = "protected_skip"
                return
            if block.kind == BlockKind.CAPTION and not cfg.translate_captions:
                block.status = BlockStatus.PROTECTED_SKIP
                block.protection_source = "auto"
                block.protection_reason = "自動：キャプション（非翻訳設定）"
                block.masked_source_text = block.source_text
                block.translation_provenance = "protected_skip"
                return

        # 3. Stage 7: Inline formula, symbol, URL/DOI, and equation-ref masking (`{v1}`, `{v2}`, ...)
        masked, items = mask_inline_protected_elements(block.source_text)
        block.masked_source_text = masked
        block.protected_items = items
        block.status = BlockStatus.PENDING
        if not manual_override_translate:
            block.protection_source = "none"
            block.protection_reason = None

    def _assign_reading_order(self, blocks: list[Block], page_w: float) -> list[Block]:
        """Stage 5: Determine reading order supporting 1-column and 2-column layouts."""
        if not blocks:
            return []

        mid_x = page_w / 2.0
        spanning_top: list[Block] = []
        left_col: list[Block] = []
        right_col: list[Block] = []
        spanning_bottom: list[Block] = []

        # Check if page actually has two-column text blocks
        narrow_left = sum(
            1
            for b in blocks
            if b.kind in (BlockKind.PARAGRAPH, BlockKind.HEADING)
            and b.bbox[2] <= mid_x + 15.0
            and (b.bbox[2] - b.bbox[0]) < page_w * 0.48
        )
        narrow_right = sum(
            1
            for b in blocks
            if b.kind in (BlockKind.PARAGRAPH, BlockKind.HEADING)
            and b.bbox[0] >= mid_x - 15.0
            and (b.bbox[2] - b.bbox[0]) < page_w * 0.48
        )
        is_two_column = narrow_left >= 1 and narrow_right >= 1

        if not is_two_column:
            ordered = sorted(blocks, key=lambda b: (round(b.bbox[1], 1), round(b.bbox[0], 1)))
            for idx, blk in enumerate(ordered):
                blk.reading_order = idx
                blk.column_index = 0
            return ordered

        for b in blocks:
            w = b.bbox[2] - b.bbox[0]
            if w >= page_w * 0.55 or (b.bbox[0] < mid_x - 20.0 and b.bbox[2] > mid_x + 20.0):
                b.column_index = 0
                if b.bbox[1] < 220.0:
                    spanning_top.append(b)
                else:
                    spanning_bottom.append(b)
            elif (b.bbox[0] + b.bbox[2]) / 2.0 <= mid_x:
                b.column_index = 1
                left_col.append(b)
            else:
                b.column_index = 2
                right_col.append(b)

        key_fn = lambda b: (round(b.bbox[1], 1), round(b.bbox[0], 1))
        ordered = (
            sorted(spanning_top, key=key_fn)
            + sorted(left_col, key=key_fn)
            + sorted(right_col, key=key_fn)
            + sorted(spanning_bottom, key=key_fn)
        )
        for idx, blk in enumerate(ordered):
            blk.reading_order = idx
        return ordered

    # ------------------------------------------------------------------
    # Stages 10 & 12: Japanese Typesetting & PDF Export
    # ------------------------------------------------------------------
    def typeset_and_export(
        self,
        document: Document,
        blocks: list[Block],
        page_indices: list[int],
        mono_output_path: Path | None,
        dual_output_path: Path | None,
        job_id: str,
        job_config: TranslationJobConfig,
        manual_regions: list[ProtectionRegion] | None = None,
    ) -> tuple[list[Block], list[ValidationIssue]]:
        issues: list[ValidationIssue] = []

        # Verify original PDF hash before starting
        if not verify_original_unchanged(document.safe_storage_path, document.sha256_hash):
            raise RuntimeError("CRITICAL: Original PDF SHA-256 mismatch detected before typesetting!")

        # If roundtrip preservation mode (Phase 1 non-translation roundtrip test):
        if job_config.only_roundtrip_preservation:
            if mono_output_path:
                self._export_roundtrip_preserving_pdf(
                    Path(document.safe_storage_path), mono_output_path, page_indices
                )
            if dual_output_path:
                self._export_dual_bilingual_pdf(
                    Path(document.safe_storage_path),
                    mono_output_path or Path(document.safe_storage_path),
                    dual_output_path,
                    page_indices,
                )
            if not verify_original_unchanged(document.safe_storage_path, document.sha256_hash):
                raise RuntimeError("CRITICAL: Original PDF SHA-256 changed after roundtrip export!")
            return blocks, issues

        font_path = self.config.font_serif_path or self.config.font_sans_path
        blocks_by_page: dict[int, list[Block]] = {}
        for b in blocks:
            blocks_by_page.setdefault(b.page_index, []).append(b)

        manual_by_page: dict[int, list[ProtectionRegion]] = {}
        for r in manual_regions or []:
            manual_by_page.setdefault(r.page_index, []).append(r)

        src_doc = pymupdf.open(document.safe_storage_path)
        out_doc = pymupdf.open()

        try:
            out_doc.insert_pdf(src_doc)
            target_set = set(page_indices)

            for p_idx in sorted(target_set):
                if p_idx < 0 or p_idx >= len(out_doc):
                    continue
                mpage = out_doc[p_idx]
                page_blocks = blocks_by_page.get(p_idx, [])
                page_manual = manual_by_page.get(p_idx, [])

                # Collect all protected bounding boxes on this page so we NEVER draw over them
                protected_boxes: list[list[float]] = [
                    b.bbox
                    for b in page_blocks
                    if b.status in (BlockStatus.PROTECTED_SKIP, BlockStatus.FALLBACK_ORIGINAL)
                    or b.kind in (BlockKind.ISOLATED_FORMULA, BlockKind.FIGURE, BlockKind.TABLE)
                ]
                for reg in page_manual:
                    if reg.mode in (
                        ProtectionMode.DO_NOT_TRANSLATE,
                        ProtectionMode.PROTECT_AS_FORMULA,
                        ProtectionMode.PROTECT_AS_FIGURE,
                    ):
                        protected_boxes.append(reg.bbox)

                approved_typeset_items: list[tuple[Block, float, list[str]]] = []

                for blk in page_blocks:
                    if blk.status not in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED):
                        continue
                    effective_txt = blk.effective_translation
                    if not effective_txt:
                        continue

                    # Restore `{v1}`, `{v2}` inline formulas/symbols into final display string
                    restored_text = unmask_inline_protected_elements(
                        effective_txt, blk.protected_items
                    )

                    # Check overlap with protected boxes (both IoU and sub-box containment ratio)
                    overlapping_prot = [
                        pb
                        for pb in protected_boxes
                        if bbox_iou(blk.bbox, pb) > 0.02
                        or bbox_intersection_ratio(pb, blk.bbox) > 0.05
                        or bbox_intersection_ratio(blk.bbox, pb) > 0.05
                    ]
                    if overlapping_prot:
                        # Do not risk overwriting a protected formula/figure! Keep original PDF element
                        blk.status = BlockStatus.FALLBACK_ORIGINAL
                        blk.translation_provenance = "fallback_original"
                        blk.warnings.append("PROTECTED_REGION_OVERLAP_FALLBACK")
                        protected_boxes.append(blk.bbox)
                        issues.append(
                            ValidationIssue(
                                id=f"iss-{uuid.uuid4().hex[:10]}",
                                job_id=job_id,
                                document_id=document.id,
                                page_index=p_idx,
                                block_id=blk.id,
                                bbox=blk.bbox,
                                code="PROTECTED_REGION_OVERLAP_FALLBACK",
                                severity=IssueSeverity.WARNING,
                                message="翻訳領域が保護対象（数式・図）と近接・重複するため、数式保護を優先して原文要素を保持しました。",
                                fallback_applied=True,
                            )
                        )
                        continue

                    # Fit Japanese lines within bbox respecting min_font_size_pt (Section 12)
                    fitted_size, wrapped_lines, fits = fit_japanese_text_in_bbox(
                        text=restored_text,
                        bbox=blk.bbox,
                        initial_font_size=min(blk.font_size_pt, 11.0),
                        min_font_size=job_config.min_font_size_pt,
                    )
                    if not fits:
                        # Section 12: Never shrink below min_font_size_pt, never delete text,
                        # never move figures/formulas; keep original and emit overflow warning.
                        blk.status = BlockStatus.FALLBACK_ORIGINAL
                        blk.translation_provenance = "fallback_original"
                        blk.warnings.append("TYPESETTING_OVERFLOW_FALLBACK")
                        protected_boxes.append(blk.bbox)
                        issues.append(
                            ValidationIssue(
                                id=f"iss-{uuid.uuid4().hex[:10]}",
                                job_id=job_id,
                                document_id=document.id,
                                page_index=p_idx,
                                block_id=blk.id,
                                bbox=blk.bbox,
                                code="TYPESETTING_OVERFLOW_FALLBACK",
                                severity=IssueSeverity.WARNING,
                                message=(
                                    f"最小フォントサイズ（{job_config.min_font_size_pt}pt）でも枠内に収まらないため、"
                                    "訳文の切り捨てや数式への重なりを避けて原文要素を保持しました（要手動確認）。"
                                ),
                                details={
                                    "min_font_size_pt": job_config.min_font_size_pt,
                                    "restored_text": restored_text,
                                },
                                fallback_applied=True,
                            )
                        )
                        continue

                    blk.fitted_font_size_pt = fitted_size
                    approved_typeset_items.append((blk, fitted_size, wrapped_lines))

                if approved_typeset_items:
                    # Redact ONLY the approved translated text bounding boxes while preserving 100% of images & vector line art
                    for blk, _, _ in approved_typeset_items:
                        rx0, ry0, rx1, ry1 = (
                            float(blk.bbox[0]),
                            float(blk.bbox[1]),
                            float(blk.bbox[2]),
                            float(blk.bbox[3]),
                        )
                        # Ensure redaction rect never clips the border of any adjacent protected box
                        for pb in protected_boxes:
                            px0, py0, px1, py1 = float(pb[0]), float(pb[1]), float(pb[2]), float(pb[3])
                            if min(rx1, px1) - max(rx0, px0) > 0:
                                if py0 >= ry0 and py0 <= ry1 + 1.0 and py0 > (ry0 + ry1) / 2.0:
                                    ry1 = min(ry1, py0 - 0.5)
                                elif py1 <= ry1 and py1 >= ry0 - 1.0 and py1 < (ry0 + ry1) / 2.0:
                                    ry0 = max(ry0, py1 + 0.5)
                        if rx1 > rx0 and ry1 > ry0:
                            rect = pymupdf.Rect(rx0, ry0, rx1, ry1)
                            mpage.add_redact_annot(rect, fill=(1, 1, 1))
                    mpage.apply_redactions(
                        images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    )

                    font_name_tag = "F_JA"
                    if font_path and font_path.exists():
                        mpage.insert_font(fontname=font_name_tag, fontfile=str(font_path))
                    else:
                        font_name_tag = "japan-s"
                        mpage.insert_font(fontname=font_name_tag, fontbuffer=None)

                    for blk, fitted_size, wrapped_lines in approved_typeset_items:
                        line_height = fitted_size * 1.32
                        cursor_y = blk.bbox[1] + fitted_size * 0.95
                        for line_str in wrapped_lines:
                            mpage.insert_text(
                                pymupdf.Point(blk.bbox[0], cursor_y),
                                line_str,
                                fontsize=fitted_size,
                                fontname=font_name_tag,
                                color=(0, 0, 0),
                                overlay=True,
                            )
                            cursor_y += line_height

            if mono_output_path:
                mono_output_path.parent.mkdir(parents=True, exist_ok=True)
                temp_mono = mono_output_path.with_suffix(".tmp.pdf")
                out_doc.save(str(temp_mono), garbage=2, deflate=True)
                temp_mono.replace(mono_output_path)

            if dual_output_path:
                # If mono_output_path was not requested, save a temporary mono to build dual
                mono_ref = mono_output_path
                temp_ref = None
                if mono_ref is None:
                    temp_ref = dual_output_path.with_suffix(".mono_tmp.pdf")
                    out_doc.save(str(temp_ref), garbage=2, deflate=True)
                    mono_ref = temp_ref
                try:
                    self._export_dual_bilingual_pdf(
                        Path(document.safe_storage_path),
                        mono_ref,
                        dual_output_path,
                        page_indices,
                    )
                finally:
                    if temp_ref and temp_ref.exists():
                        temp_ref.unlink(missing_ok=True)

        finally:
            out_doc.close()
            src_doc.close()

        # Final immutability check on original PDF
        if not verify_original_unchanged(document.safe_storage_path, document.sha256_hash):
            raise RuntimeError("CRITICAL: Original PDF SHA-256 changed after typesetting!")

        return blocks, issues

    def _export_roundtrip_preserving_pdf(
        self,
        original_path: Path,
        output_path: Path,
        page_indices: list[int],
    ) -> None:
        """Non-translation roundtrip reconstruction preserving all vector/math objects."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".tmp.pdf")
        src = pymupdf.open(str(original_path))
        dst = pymupdf.open()
        try:
            for idx in page_indices:
                if 0 <= idx < len(src):
                    dst.insert_pdf(src, from_page=idx, to_page=idx)
            dst.save(str(temp_path), garbage=1, deflate=True)
        finally:
            dst.close()
            src.close()
        temp_path.replace(output_path)

    def _export_dual_bilingual_pdf(
        self,
        original_path: Path,
        translated_path: Path,
        dual_output_path: Path,
        page_indices: list[int],
    ) -> None:
        """Create side-by-side bilingual PDF (Original on Left, Japanese on Right)
        preserving exact page correspondence and vector quality.
        """
        dual_output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dual = dual_output_path.with_suffix(".tmp.pdf")
        orig_doc = pymupdf.open(str(original_path))
        trans_doc = pymupdf.open(str(translated_path))
        dual_doc = pymupdf.open()

        try:
            for out_idx, p_idx in enumerate(page_indices):
                if p_idx < 0 or p_idx >= len(orig_doc):
                    continue
                orig_page = orig_doc[p_idx]
                trans_page_idx = p_idx if len(trans_doc) == len(orig_doc) else out_idx
                w = float(orig_page.rect.width)
                h = float(orig_page.rect.height)

                new_page = dual_doc.new_page(width=w * 2.0, height=h)
                left_rect = pymupdf.Rect(0, 0, w, h)
                right_rect = pymupdf.Rect(w, 0, w * 2.0, h)
                new_page.show_pdf_page(left_rect, orig_doc, p_idx)
                if 0 <= trans_page_idx < len(trans_doc):
                    new_page.show_pdf_page(right_rect, trans_doc, trans_page_idx)
            dual_doc.save(str(temp_dual), garbage=2, deflate=True)
        finally:
            dual_doc.close()
            trans_doc.close()
            orig_doc.close()

        temp_dual.replace(dual_output_path)

    # ------------------------------------------------------------------
    # Stage 11: Preservation Verification
    # ------------------------------------------------------------------
    def verify_preservation(
        self,
        document: Document,
        output_pdf_path: Path,
        blocks: list[Block],
        page_indices: list[int],
        manual_regions: list[ProtectionRegion] | None = None,
        is_roundtrip_only: bool = False,
    ) -> list[PageVisualComparisonReport]:
        if not verify_original_unchanged(document.safe_storage_path, document.sha256_hash):
            raise RuntimeError("Original PDF SHA-256 was altered!")

        reports: list[PageVisualComparisonReport] = []
        blocks_by_page: dict[int, list[Block]] = {}
        for b in blocks:
            blocks_by_page.setdefault(b.page_index, []).append(b)
        manual_by_page: dict[int, list[ProtectionRegion]] = {}
        for r in manual_regions or []:
            manual_by_page.setdefault(r.page_index, []).append(r)

        out_doc = pymupdf.open(str(output_pdf_path))
        try:
            total_out_pages = len(out_doc)
        finally:
            out_doc.close()

        for seq_idx, p_idx in enumerate(page_indices):
            p_blocks = blocks_by_page.get(p_idx, [])
            p_manual = manual_by_page.get(p_idx, [])

            protected_list: list[tuple[str, list[float]]] = []
            translated_bboxes: list[list[float]] = []

            for b in p_blocks:
                if is_roundtrip_only:
                    protected_list.append((b.id, b.bbox))
                elif b.status in (BlockStatus.PROTECTED_SKIP, BlockStatus.FALLBACK_ORIGINAL) or b.kind in (
                    BlockKind.ISOLATED_FORMULA,
                    BlockKind.FIGURE,
                    BlockKind.TABLE,
                ):
                    protected_list.append((b.id, b.bbox))
                elif b.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED):
                    translated_bboxes.append(b.bbox)

            for r in p_manual:
                if r.mode != ProtectionMode.TRANSLATE_AS_BODY:
                    protected_list.append((r.id, r.bbox))

            out_page_idx = p_idx if total_out_pages == document.page_count else seq_idx
            rep = compare_protected_regions_visually(
                original_pdf_path=document.safe_storage_path,
                output_pdf_path=output_pdf_path,
                page_index=p_idx,
                protected_regions=protected_list,
                translated_bboxes=[] if is_roundtrip_only else translated_bboxes,
                output_page_index=out_page_idx,
            )
            reports.append(rep)

        return reports


# ----------------------------------------------------------------------
# Helper functions for Inline Formula Masking & Kinsoku Japanese Fitting
# ----------------------------------------------------------------------
def mask_inline_protected_elements(text: str) -> tuple[str, list[ProtectedItem]]:
    """Detect inline formulas, Greek/math symbols, sub/superscripts, and URLs/DOIs,
    replacing them with `{v1}`, `{v2}`, ... while leaving `[1]` citation brackets visible
    (or protected) for validation.
    """
    items: list[ProtectedItem] = []
    counter = 0

    def _replace(match: re.Match, kind: ProtectedItemKind) -> str:
        nonlocal counter
        raw_match = match.group(0)
        if re.search(r"\{v\d+\}", raw_match):
            return raw_match

        # Strip trailing sentence punctuation or unbalanced closing brackets/parens
        orig = raw_match
        trailing = ""
        while orig and (
            orig[-1] in ".,;:"
            or (orig[-1] == ")" and orig.count(")") > orig.count("("))
            or (orig[-1] == "]" and orig.count("]") > orig.count("["))
        ):
            trailing = orig[-1] + trailing
            orig = orig[:-1]

        orig_stripped = orig.rstrip()
        if len(orig_stripped) < len(orig):
            trailing = orig[len(orig_stripped) :] + trailing
            orig = orig_stripped

        # Skip single plain ASCII letters or numbers
        if not orig or re.match(r"^[A-Za-z0-9]+$", orig):
            return raw_match
        counter += 1
        ph = f"{{v{counter}}}"
        items.append(
            ProtectedItem(
                placeholder=ph,
                kind=kind,
                original_text=orig,
            )
        )
        return f"{ph}{trailing}"

    working = _URL_DOI_RE.sub(lambda m: _replace(m, ProtectedItemKind.URL_OR_DOI), text)
    working = _EQUATION_REFERENCE_RE.sub(
        lambda m: _replace(m, ProtectedItemKind.EQUATION_NUMBER), working
    )
    working = _INLINE_MATH_EXPR_RE.sub(
        lambda m: _replace(m, ProtectedItemKind.INLINE_FORMULA), working
    )
    return working, items


def unmask_inline_protected_elements(
    translated_text: str, protected_items: list[ProtectedItem]
) -> str:
    """Restore `{v1}`, `{v2}` placeholders back to their exact original math/symbol strings."""
    restored = translated_text
    for item in protected_items:
        restored = restored.replace(item.placeholder, item.original_text)
    return restored


def wrap_japanese_kinsoku(text: str, max_width_pt: float, font_size_pt: float) -> list[str]:
    """Wrap mixed Japanese/English/math text into lines respecting `max_width_pt`
    and Kinsoku Shori (prohibited line-start and line-end characters).
    """
    if not text:
        return []
    lines: list[str] = []

    def _char_width(ch: str) -> float:
        if ord(ch) < 128:
            return font_size_pt * 0.56
        return font_size_pt * 1.02

    # Tokenize so `{vN}` or ASCII words stay together when possible
    tokens = re.findall(r"[A-Za-z0-9_^\(\)\[\]{}=<>≤≥≈≠±+\-*/.,:;]+|\s+|.", text)
    cur_line = ""
    cur_w = 0.0

    for tok in tokens:
        if tok == "\n":
            if cur_line.strip():
                lines.append(cur_line.rstrip())
            cur_line = ""
            cur_w = 0.0
            continue
        if not cur_line and tok.isspace():
            continue

        tok_w = sum(_char_width(c) for c in tok)
        if cur_line and (cur_w + tok_w > max_width_pt):
            # Kinsoku line-start check: if `tok` starts with a prohibited line-start char
            # and we have only 1 char, allow slight overhang or pull last char from cur_line
            if len(tok) == 1 and tok in KINSOKU_LINE_START_PROHIBITED and cur_w + tok_w <= max_width_pt + font_size_pt * 1.1:
                cur_line += tok
                lines.append(cur_line.rstrip())
                cur_line = ""
                cur_w = 0.0
                continue
            # Kinsoku line-end check: if cur_line ends with an opening bracket, push it to next line
            if len(cur_line) >= 2 and cur_line[-1] in KINSOKU_LINE_END_PROHIBITED:
                carry = cur_line[-1]
                lines.append(cur_line[:-1].rstrip())
                cur_line = carry + tok.lstrip()
                cur_w = sum(_char_width(c) for c in cur_line)
                continue

            lines.append(cur_line.rstrip())
            cur_line = tok.lstrip()
            cur_w = sum(_char_width(c) for c in cur_line)
        else:
            cur_line += tok
            cur_w += tok_w

    if cur_line.strip():
        lines.append(cur_line.rstrip())

    return lines


def fit_japanese_text_in_bbox(
    text: str,
    bbox: list[float],
    initial_font_size: float = 10.0,
    min_font_size: float = 8.0,
) -> tuple[float, list[str], bool]:
    """Find the largest font size in `[min_font_size, initial_font_size]` (step 0.5pt)
    such that the wrapped text fits inside `bbox` width and height.
    Returns `(fitted_font_size, lines, fits_in_box)`.
    """
    box_w = max(10.0, bbox[2] - bbox[0])
    box_h = max(8.0, bbox[3] - bbox[1])
    start_sz = max(min_font_size, initial_font_size)

    sz = start_sz
    while sz >= min_font_size - 1e-6:
        lines = wrap_japanese_kinsoku(text, box_w, sz)
        line_h = sz * 1.32
        required_h = len(lines) * line_h
        if required_h <= box_h + sz * 0.45:
            return round(sz, 2), lines, True
        sz -= 0.5

    # Even at min_font_size it overflows
    min_lines = wrap_japanese_kinsoku(text, box_w, min_font_size)
    return round(min_font_size, 2), min_lines, False
