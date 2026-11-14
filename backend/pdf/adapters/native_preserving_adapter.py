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
import statistics
import uuid
from pathlib import Path
from typing import Literal

import pymupdf

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    Document,
    InlineMathSpan,
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
_KNOWN_MATH_FUNCS = (
    r"(?:arcsin|arccos|arctan|sinh|cosh|tanh|sin|cos|tan|cot|sec|csc"
    r"|log|ln|lg|exp|lim|sup|inf|max|min|arg\s*max|arg\s*min"
    r"|det|dim|ker|rank|diag|sgn|sign|erf|cov|var|Prob|Pr"
    r"|MSE|MAE|RMSE|BCE|KL|IoU|sqrt)"
)
_SMP_OR_GREEK_OR_OP = r"[\U0001d400-\U0001d7ff\u0370-\u03ff∇∂∑∏∐∫∬∭∮√]"
_HAT_CHARS = r"[\u02c6\u0302]"
_VS_CHARS = r"\ufe00\ufe01"
_SUBSUP_TAIL = (
    r"(?:\s*[_^]\s*(?:\{[^}]+\}|[0-9A-Za-z\u0370-\u03ff\U0001d400-\U0001d7ff+\-()∗]+))*"
)
_PROSE_SHORT_WORDS = (
    r"(?:in|of|on|to|at|by|or|as|is|it|we|be|do|no|so|up|us|an|if|he|me|my|go|am)"
)
# Disallow 3+ letter plain ASCII prose words inside math function args unless they are known math functions
_NON_PROSE_ARG = (
    r"\((?!(?:(?!"
    + _KNOWN_MATH_FUNCS
    + r"\b)[^()\n])*\b(?!(?:"
    + _KNOWN_MATH_FUNCS
    + r")\b)[A-Za-z]{3,}\b)[^()\n]+\)"
)
_MATH_FUNC_CALL = (
    r"(?<![A-Za-z0-9\U0001d400-\U0001d7ff])(?:"
    + _KNOWN_MATH_FUNCS
    + r"\s*"
    + _NON_PROSE_ARG
    + r"|(?:"
    + _HAT_CHARS
    + r"\s*)?"
    + _SMP_OR_GREEK_OR_OP
    + r"[\U0001d400-\U0001d7ff\u0370-\u03ff∇∂∑∏∐∫∬∭∮√A-Za-z0-9′″'\u2217∗"
    + _VS_CHARS
    + r"]*"
    + _SUBSUP_TAIL
    + _NON_PROSE_ARG
    + r"|(?!(?:"
    + _PROSE_SHORT_WORDS
    + r")\b)[A-Za-z]{1,2}(?:[′″']+)+"
    + _SUBSUP_TAIL
    + r"\([^()\n]+\)"
    + r"|(?!(?:"
    + _PROSE_SHORT_WORDS
    + r")\b)[A-Za-z]{1,2}"
    + _SUBSUP_TAIL
    + _NON_PROSE_ARG
    + r")"
)
_MATH_SET_ATOM = r"\{(?!v\d+\})[^}\n]*" + _SMP_OR_GREEK_OR_OP + r"[^}\n]*\}"
_MATH_DIM_ATOM = r"\b\d+(?:\.\d+)?(?:\s*[×±]\s*\d+(?:\.\d+)?)+\b"
_MATH_SCI_ATOM = (
    r"(?<![A-Za-z\U0001d400-\U0001d7ff])\d+(?:\.\d+)?(?:e|E|\U0001d452)[+\-−]?\d+\b"
)
_MATH_SYMBOL_ATOM = (
    r"(?:[−\-]\s*)?(?:"
    + _HAT_CHARS
    + r"\s*)?"
    + _SMP_OR_GREEK_OR_OP
    + r"(?:\s*[_^]\s*(?:\{[^}]+\}|[0-9A-Za-z\u0370-\u03ff\U0001d400-\U0001d7ff+\-()∗]+)|[\U0001d400-\U0001d7ff\u0370-\u03ff∇∂∑∏∐∫∬∭∮√0-9′″'\u2217∗"
    + _VS_CHARS
    + r"]|(?<=[\u2217∗])\s+\d+)*"
)
_MATH_ASCII_SUBSUP_ATOM = (
    r"(?<![A-Za-z0-9\U0001d400-\U0001d7ff])[A-Za-z0-9]+(?:[′″']+)?(?:\s*[_^]\s*(?:\{[^}]+\}|[0-9A-Za-z\u0370-\u03ff\U0001d400-\U0001d7ff+\-()∗]+))+"
    r"|(?<![A-Za-z0-9\U0001d400-\U0001d7ff])[A-Za-z]+(?:[′″']+)(?:[0-9A-Za-z_^{}\(\)]*)"
)
_MATH_ATOM = (
    r"(?:"
    + _MATH_SET_ATOM
    + r"|"
    + _MATH_FUNC_CALL
    + r"|"
    + _MATH_DIM_ATOM
    + r"|"
    + _MATH_SCI_ATOM
    + r"|"
    + _MATH_SYMBOL_ATOM
    + r"|"
    + _MATH_ASCII_SUBSUP_ATOM
    + r"|[\u2200-\u22ff\u2190-\u21ff]+[\ufe00\ufe01]?[A-Za-z\u0370-\u03ff\U0001d400-\U0001d7ff0-9_^{}\(\)′″']*"
    + r")"
)
_MATH_RHS_ATOM = (
    r"(?:"
    + _MATH_ATOM
    + r"|"
    + _NON_PROSE_ARG
    + r"|[0-9]+(?:\.[0-9]+)?(?:e[+\-]?\d+)?"
    + r"|(?<![A-Za-z\U0001d400-\U0001d7ff])[A-Za-z0-9_^{}\(\)′″']+(?![A-Za-z])"
    + r")"
)
_INLINE_MATH_EXPR_RE = re.compile(
    r"(?:"
    r"\$[^$]+\$"
    r"|"
    + _MATH_ATOM
    + r"(?:\s*[=<>≤≥≈≠±+\-−*/×⋅·∈∉⊂⊆∪∩]\s*"
    + _MATH_RHS_ATOM
    + r")*"
    r"|(?<![A-Za-z0-9\U0001d400-\U0001d7ff])[A-Za-z\u0370-\u03ff\U0001d400-\U0001d7ff][A-Za-z0-9\u0370-\u03ff\U0001d400-\U0001d7ff_^{}′″']*\s*[=≈≠≤≥]\s*"
    + _MATH_RHS_ATOM
    + r"(?:\s*[+\-−*/×⋅·]\s*"
    + _MATH_RHS_ATOM
    + r")*"
    r")"
)
_EQUATION_NUM_ONLY_RE = re.compile(r"^\s*\(\d+(?:\.\d+)*[a-z]?\)\s*$")
_UNRESOLVED_PH_CHECK_RE = re.compile(
    r"(?:\{\s*v\s*\d+[A-Za-z0-9_]*\s*\}|［\s*v\s*\d+[A-Za-z0-9_]*\s*］|｛\s*v\s*\d+[A-Za-z0-9_]*\s*｝|\[\s*v\s*\d+[A-Za-z0-9_]*\s*\])",
    re.IGNORECASE,
)
# Known TeX extension font (e.g. txexs) operator + Variation Selector-1/2 pairs where
# U+FE00/U+FE01 selects operator display/text size in TeX but has no standalone PDF glyph.
_KNOWN_TEX_OPERATOR_VS_RE = re.compile(r"([√∑∏∐∫∬∭∮⋀⋁⋂⋃])[\ufe00\ufe01]")
_SMP_TOUNICODE_HEX_RE = re.compile(rb"<([0-9a-fA-F]{5,6})>")
_BFCHAR_SECTION_RE = re.compile(rb"(beginbfchar\b)(.*?)(\bendbfchar)", re.DOTALL)
_BFRANGE_SECTION_RE = re.compile(rb"(beginbfrange\b)(.*?)(\bendbfrange)", re.DOTALL)
_BFCHAR_PAIR_RE = re.compile(rb"(<[0-9A-Fa-f]+>)(\s*)(<[0-9A-Fa-f]+>)")
_BFRANGE_TRIPLE_RE = re.compile(
    rb"(<[0-9A-Fa-f]+>)(\s*)(<[0-9A-Fa-f]+>)(\s*)(<[0-9A-Fa-f]+>|\[[^\]]*\])",
    re.DOTALL,
)
_HEX_TOKEN_RE = re.compile(rb"<([0-9A-Fa-f]+)>")


def normalize_for_pdf_rendering(text: str) -> str:
    """Normalize text strictly for PDF glyph measurement and rendering by stripping
    only known TeX operator variation selectors (e.g. √+U+FE01, ∑+U+FE01) and converting
    non-breaking spaces (`\\xa0`) to standard spaces, while keeping any unknown/unsupported
    variation selector intact so pre-redaction glyph checks can safely fall back.
    """
    if not text:
        return ""
    return _KNOWN_TEX_OPERATOR_VS_RE.sub(r"\1", text.replace("\xa0", " "))


def _is_math_preferred_codepoint(cp: int) -> bool:
    """Return True for Unicode codepoints that should prefer the math fallback font
    over the Japanese text font when available (avoids CJK ambiguous-width glyphs
    and shared-GID ToUnicode collisions such as U+2212/U+FF0D or U+02C6/U+0302 in YuGothR).
    """
    return (
        (0x1D400 <= cp <= 0x1D7FF)  # Mathematical Alphanumeric Symbols (SMP)
        or (0x2200 <= cp <= 0x22FF)  # Mathematical Operators (−, √, ∑, ∏, ∫, ≤, ≥, ≠, ≈)
        or (0x2A00 <= cp <= 0x2AFF)  # Supplemental Mathematical Operators
        or (0x27C0 <= cp <= 0x27EF)  # Miscellaneous Mathematical Symbols-A
        or (0x2980 <= cp <= 0x29FF)  # Miscellaneous Mathematical Symbols-B
        or (0x2190 <= cp <= 0x21FF)  # Arrows
        or (0x2100 <= cp <= 0x214F)  # Letterlike Symbols
        or (0x2070 <= cp <= 0x209F)  # Superscripts and Subscripts
        or (0x0370 <= cp <= 0x03FF)  # Greek and Coptic (Φ, π, φ, etc.)
        or (0x0300 <= cp <= 0x036F)  # Combining Diacritical Marks
        or (0x20D0 <= cp <= 0x20FF)  # Combining Diacritical Marks for Symbols
        or cp in (0x02C6, 0x02DC, 0x2032, 0x2033, 0x2034, 0x00B1, 0x00D7, 0x00F7)
    )


def _select_char_font(
    ch: str,
    ja_font_tag: str,
    ja_font: pymupdf.Font,
    math_font_tag: str | None = None,
    math_font: pymupdf.Font | None = None,
) -> tuple[str, pymupdf.Font] | None:
    """Select the exact `(font_tag, pymupdf.Font)` for a single character `ch`, used
    identically across glyph verification, width measurement, and PDF rendering.
    Returns `None` if neither `ja_font` nor `math_font` has a glyph for `ch`.
    """
    cp = ord(ch)
    if cp <= 0x20 or ch.isspace():
        return (ja_font_tag, ja_font)
    if _is_math_preferred_codepoint(cp):
        if math_font is not None and math_font_tag and math_font.has_glyph(cp) != 0:
            return (math_font_tag, math_font)
        if ja_font.has_glyph(cp) != 0:
            return (ja_font_tag, ja_font)
        return None
    if ja_font.has_glyph(cp) != 0:
        return (ja_font_tag, ja_font)
    if math_font is not None and math_font_tag and math_font.has_glyph(cp) != 0:
        return (math_font_tag, math_font)
    return None


def _segment_line_into_font_runs(
    line_str: str,
    ja_font_tag: str,
    ja_font: pymupdf.Font,
    math_font_tag: str | None = None,
    math_font: pymupdf.Font | None = None,
) -> list[tuple[str, pymupdf.Font, str]]:
    """Split a single normalized line string into contiguous `(font_tag, font_obj, run_text)` runs."""
    runs: list[tuple[str, pymupdf.Font, str]] = []
    for ch in line_str:
        sel = _select_char_font(ch, ja_font_tag, ja_font, math_font_tag, math_font)
        tag, fobj = sel if sel is not None else (ja_font_tag, ja_font)
        if runs and runs[-1][0] == tag:
            runs[-1] = (tag, fobj, runs[-1][2] + ch)
        else:
            runs.append((tag, fobj, ch))
    return runs


def _merge_horizontal_visual_lines(raw_lines: list[dict]) -> list[dict]:
    """Merge consecutive PyMuPDF line dicts that belong to the same horizontal visual line
    (e.g. when a superscript+subscript stack like `𝑃^∗_2` or a radical `√(𝑎^2 + 𝑏^2)` caused
    PyMuPDF to split one line into consecutive horizontal segments).
    """
    merged: list[dict] = []
    for ln in raw_lines:
        spans = [s for s in ln.get("spans", []) if str(s.get("text", ""))]
        if not spans:
            continue
        raw_bbox = ln.get("bbox")
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            lb = [float(x) for x in raw_bbox]
        else:
            s_boxes = [
                s["bbox"]
                for s in spans
                if isinstance(s.get("bbox"), (list, tuple)) and len(s["bbox"]) == 4
            ]
            lb = (
                [
                    min(float(b[0]) for b in s_boxes),
                    min(float(b[1]) for b in s_boxes),
                    max(float(b[2]) for b in s_boxes),
                    max(float(b[3]) for b in s_boxes),
                ]
                if s_boxes
                else [0.0, 0.0, 0.0, 0.0]
            )
        if merged:
            prev = merged[-1]
            pb = prev["bbox"]
            y_ov = min(pb[3], lb[3]) - max(pb[1], lb[1])
            if y_ov >= 4.0 and lb[0] >= pb[2] - 6.0:
                prev["spans"].extend(spans)
                prev["bbox"] = [
                    min(pb[0], lb[0]),
                    min(pb[1], lb[1]),
                    max(pb[2], lb[2]),
                    max(pb[3], lb[3]),
                ]
                continue
        merged.append({"bbox": lb, "spans": list(spans)})
    return merged


def extract_inline_math_spans(
    raw_lines: list[dict],
    page_drawings: list[dict] | None = None,
) -> list[InlineMathSpan]:
    """Extract structured `InlineMathSpan` annotations (superscripts, subscripts, circumflex hats
    over base symbols, and radical vinculum overbars) strictly from the PDF's span geometry and
    vector line drawings. Never guesses superscripts/subscripts from plain text.
    """
    lines = _merge_horizontal_visual_lines(raw_lines)
    spans_out: list[InlineMathSpan] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def _add(item: InlineMathSpan) -> None:
        key = (item.role, item.context_text, item.target_text)
        if key not in seen_keys:
            seen_keys.add(key)
            spans_out.append(item)

    for line in lines:
        spans = line["spans"]
        line_sizes = [
            float(s.get("size", 10.0)) for s in spans if str(s.get("text", "")).strip()
        ]
        if not line_sizes:
            continue
        max_sz = max(line_sizes)
        base_origins = [
            float(s["origin"][1])
            for s in spans
            if isinstance(s.get("origin"), (list, tuple))
            and len(s["origin"]) >= 2
            and float(s.get("size", 10.0)) >= max_sz * 0.86
            and str(s.get("text", "")).strip() != "ˆ"
        ]
        if base_origins:
            baseline_y = statistics.median(base_origins)
        else:
            first_orig = spans[0].get("origin")
            baseline_y = (
                float(first_orig[1])
                if isinstance(first_orig, (list, tuple)) and len(first_orig) >= 2
                else 0.0
            )

        script_base_by_idx: dict[int, tuple[str, Literal["super", "sub"]]] = {}

        for idx, s in enumerate(spans):
            raw_t = normalize_for_pdf_rendering(str(s.get("text", "")))
            st = raw_t.strip()
            if not st:
                continue
            s_bbox = s.get("bbox")
            if not (isinstance(s_bbox, (list, tuple)) and len(s_bbox) == 4):
                continue

            # 1. Circumflex hat over immediately following math symbol
            if st == "ˆ" and idx + 1 < len(spans):
                ns = spans[idx + 1]
                nt = normalize_for_pdf_rendering(str(ns.get("text", ""))).strip()
                ns_bbox = ns.get("bbox")
                s_orig = s.get("origin")
                ns_orig = ns.get("origin")
                has_horiz_overlap = (
                    isinstance(ns_bbox, (list, tuple))
                    and len(ns_bbox) == 4
                    and float(ns_bbox[0]) < float(s_bbox[2])
                )
                has_raised_origin = (
                    isinstance(s_orig, (list, tuple))
                    and len(s_orig) >= 2
                    and float(s_orig[1]) < baseline_y - 0.8
                )
                if nt and (has_horiz_overlap or has_raised_origin):
                    dy_em = -0.23
                    if (
                        isinstance(s_orig, (list, tuple))
                        and len(s_orig) >= 2
                        and isinstance(ns_orig, (list, tuple))
                        and len(ns_orig) >= 2
                        and max_sz > 0
                    ):
                        raw_dy = (float(s_orig[1]) - float(ns_orig[1])) / max_sz
                        dy_em = round(min(-0.15, max(-0.35, raw_dy)), 3)
                    _add(
                        InlineMathSpan(
                            context_text=f"ˆ{nt[0]}",
                            target_text="ˆ",
                            role="hat",
                            scale=1.0,
                            dy_em=dy_em,
                        )
                    )
                    continue

            # 2. Radical vinculum overbar (`√` + horizontal line matching radicand extent)
            if "√" in st and page_drawings:
                rx0, ry0, rx1, ry1 = (
                    float(s_bbox[0]),
                    float(s_bbox[1]),
                    float(s_bbox[2]),
                    float(s_bbox[3]),
                )
                rad_suffix = raw_t.split("√", 1)[1] if "√" in raw_t else ""
                for d in page_drawings:
                    r = d.get("rect")
                    if r is None:
                        continue
                    dx0, dy0, dx1, dy1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                    if abs(dy1 - dy0) <= 1.2 and (dx1 - dx0) >= 3.0:
                        starts_at_radical = (
                            abs(dx0 - rx1) <= 3.0
                            if not rad_suffix.strip()
                            else (rx0 <= dx0 <= rx1 + 3.0)
                        )
                        if starts_at_radical and (ry0 - 3.0 <= dy0 <= ry1 + 2.0):
                            acc = rad_suffix
                            for j in range(idx + 1, len(spans)):
                                jt = normalize_for_pdf_rendering(str(spans[j].get("text", "")))
                                j_bbox = spans[j].get("bbox")
                                if not (isinstance(j_bbox, (list, tuple)) and len(j_bbox) == 4):
                                    break
                                jx1 = float(j_bbox[2])
                                acc += jt
                                if abs(jx1 - dx1) <= 3.0 or (
                                    jt.rstrip().endswith((").", "),", ");", "):"))
                                    and abs(jx1 - dx1) <= 6.0
                                ):
                                    rad_t = acc.rstrip().rstrip(".,;:")
                                    if rad_t:
                                        _add(
                                            InlineMathSpan(
                                                context_text=f"√{rad_t}",
                                                target_text=rad_t,
                                                role="radicand",
                                                scale=1.0,
                                                dy_em=0.0,
                                                overbar=True,
                                                orig_overbar_bbox=[
                                                    round(dx0, 2),
                                                    round(dy0, 2),
                                                    round(dx1, 2),
                                                    round(dy1, 2),
                                                ],
                                            )
                                        )
                                    break

            # 3. Superscript / Subscript span attached to preceding base token
            sz = float(s.get("size", 10.0))
            s_orig = s.get("origin")
            if not (isinstance(s_orig, (list, tuple)) and len(s_orig) >= 2):
                continue
            oy = float(s_orig[1])
            is_sup = st != "ˆ" and (sz < max_sz * 0.86 and oy < baseline_y - max_sz * 0.10)
            is_sub = (
                st != "ˆ"
                and not is_sup
                and (sz < max_sz * 0.86 and oy > baseline_y + max_sz * 0.08)
            )
            if (is_sup or is_sub) and idx > 0:
                ps_idx = idx - 1
                ps = spans[ps_idx]
                crossed_ws = False
                if ps_idx >= 1 and not normalize_for_pdf_rendering(str(ps.get("text", ""))).strip():
                    ws_raw = str(ps.get("text", ""))
                    if len(ws_raw) <= 2:
                        ps_idx -= 1
                        ps = spans[ps_idx]
                        crossed_ws = True
                ps_bbox = ps.get("bbox")
                if not (isinstance(ps_bbox, (list, tuple)) and len(ps_bbox) == 4):
                    continue
                dx = float(s_bbox[0]) - float(ps_bbox[2])
                max_dx = max(2.0, max_sz * 0.40) if crossed_ws else 2.0
                if dx <= max_dx:
                    pt = normalize_for_pdf_rendering(str(ps.get("text", "")))
                    role_lit: Literal["super", "sub"] = "super" if is_sup else "sub"
                    dy_val = -0.29 if is_sup else 0.18
                    prev_script = script_base_by_idx.get(ps_idx)
                    if prev_script is not None and prev_script[1] != role_lit:
                        base_tok = prev_script[0] + pt.strip()
                        _add(
                            InlineMathSpan(
                                context_text=f"{base_tok} {st}",
                                target_text=st,
                                role=role_lit,
                                scale=0.78,
                                dy_em=dy_val,
                            )
                        )
                        _add(
                            InlineMathSpan(
                                context_text=f"{base_tok}{st}",
                                target_text=st,
                                role=role_lit,
                                scale=0.78,
                                dy_em=dy_val,
                            )
                        )
                    elif pt.strip() == "∗" and ps_idx >= 1:
                        pps_idx = ps_idx - 1
                        if pps_idx >= 1 and not normalize_for_pdf_rendering(str(spans[pps_idx].get("text", ""))).strip():
                            pps_idx -= 1
                        ppt = normalize_for_pdf_rendering(str(spans[pps_idx].get("text", "")))
                        p_parts = [
                            tok for tok in re.split(r"[\s(\[{,=+\-−*/√]+", ppt) if tok
                        ]
                        p_tok = p_parts[-1] if p_parts else ""
                        if p_tok:
                            base_tok = p_tok + "∗"
                            _add(
                                InlineMathSpan(
                                    context_text=f"{base_tok} {st}",
                                    target_text=st,
                                    role=role_lit,
                                    scale=0.78,
                                    dy_em=dy_val,
                                )
                            )
                            _add(
                                InlineMathSpan(
                                    context_text=f"{base_tok}{st}",
                                    target_text=st,
                                    role=role_lit,
                                    scale=0.78,
                                    dy_em=dy_val,
                                )
                            )
                    else:
                        pt_parts = [
                            tok for tok in re.split(r"[\s(\[{,=+\-−*/√]+", pt) if tok
                        ]
                        base_tok = pt_parts[-1] if pt_parts else ""
                        if is_sub and base_tok.isdigit():
                            continue
                        if base_tok:
                            script_base_by_idx[idx] = (base_tok, role_lit)
                            _add(
                                InlineMathSpan(
                                    context_text=f"{base_tok}{st}",
                                    target_text=st,
                                    role=role_lit,
                                    scale=0.78,
                                    dy_em=dy_val,
                                )
                            )

    # Detect any super/sub InlineMathSpan whose `context_text` appears multiple times
    # in the same block with conflicting structures (e.g. both super and sub, sub/super and
    # normal plain text, or different split target_text like `a1^2` vs `a_{12}`).
    # Instead of silently dropping spans into flat plain text, mark conflicting spans `ambiguous=True`
    # so `typeset_and_export` safely falls back to original PDF with an explicit warning before redaction.
    if spans_out:
        variants_by_ctx: dict[str, set[tuple[str, str]]] = {}
        for item in spans_out:
            if item.role in ("super", "sub"):
                variants_by_ctx.setdefault(item.context_text, set()).add(
                    (item.role, item.target_text)
                )
        if variants_by_ctx:
            normal_chunks: list[str] = []
            for line in lines:
                spans = line["spans"]
                line_sizes = [
                    float(s.get("size", 10.0))
                    for s in spans
                    if str(s.get("text", "")).strip()
                ]
                if not line_sizes:
                    continue
                max_sz = max(line_sizes)
                base_origins = [
                    float(s["origin"][1])
                    for s in spans
                    if isinstance(s.get("origin"), (list, tuple))
                    and len(s["origin"]) >= 2
                    and float(s.get("size", 10.0)) >= max_sz * 0.86
                    and str(s.get("text", "")).strip() != "ˆ"
                ]
                baseline_y = (
                    statistics.median(base_origins)
                    if base_origins
                    else (
                        float(spans[0]["origin"][1])
                        if isinstance(spans[0].get("origin"), (list, tuple))
                        and len(spans[0]["origin"]) >= 2
                        else 0.0
                    )
                )
                cur_normal: list[str] = []
                for s in spans:
                    raw_t = normalize_for_pdf_rendering(str(s.get("text", "")))
                    st = raw_t.strip()
                    if not st:
                        continue
                    sz = float(s.get("size", 10.0))
                    s_orig = s.get("origin")
                    oy = (
                        float(s_orig[1])
                        if (isinstance(s_orig, (list, tuple)) and len(s_orig) >= 2)
                        else baseline_y
                    )
                    is_sup = st != "ˆ" and (
                        sz < max_sz * 0.86 and oy < baseline_y - max_sz * 0.10
                    )
                    is_sub = (
                        st != "ˆ"
                        and not is_sup
                        and (sz < max_sz * 0.86 and oy > baseline_y + max_sz * 0.08)
                    )
                    if is_sup or is_sub or st == "ˆ":
                        if cur_normal:
                            normal_chunks.append("".join(cur_normal))
                            cur_normal = []
                    else:
                        cur_normal.append(raw_t)
                if cur_normal:
                    normal_chunks.append("".join(cur_normal))

            _WORD_MATH_CH_RE = re.compile(r"[A-Za-z0-9\U0001d400-\U0001d7ff]")
            for ctx, vset in variants_by_ctx.items():
                left_guard = (
                    r"(?<![A-Za-z0-9\U0001d400-\U0001d7ff])"
                    if ctx and _WORD_MATH_CH_RE.match(ctx[0])
                    else ""
                )
                right_guard = (
                    r"(?![A-Za-z0-9\U0001d400-\U0001d7ff])"
                    if ctx and _WORD_MATH_CH_RE.match(ctx[-1])
                    else ""
                )
                pat = re.compile(left_guard + re.escape(ctx) + right_guard)
                for chunk in normal_chunks:
                    if pat.search(chunk):
                        vset.add(("normal", ctx))
                        break

            final_spans: list[InlineMathSpan] = []
            seen_ambiguous: set[tuple[str, str]] = set()
            for item in spans_out:
                if item.role in ("super", "sub"):
                    if len(variants_by_ctx.get(item.context_text, set())) > 1:
                        key = (item.context_text, item.target_text)
                        if key not in seen_ambiguous:
                            seen_ambiguous.add(key)
                            final_spans.append(item.model_copy(update={"ambiguous": True}))
                        continue
                final_spans.append(item)
            spans_out = final_spans

    return spans_out


def _is_math_ident_continuation_char(ch: str) -> bool:
    """Return True if `ch` can continue a Latin/Greek/numeric math token.

    Used by `_build_styled_line_plan()` trailing boundary checks so that
    ASCII/math/Greek alphanumerics (e.g., `20`, `abc`, `𝑙𝑠`, `β`) suppress
    partial `InlineMathSpan` suffix matches, whereas directly following
    Japanese particles or words (e.g., `は`, `の`, `および`, `個`) do not.
    """
    if not ch or not ch.isalnum():
        return False
    if ch.isascii():
        return True
    cp = ord(ch)
    return (
        cp in (0x00B2, 0x00B3, 0x00B9)
        or (0x00C0 <= cp <= 0x024F)
        or (0x0370 <= cp <= 0x03FF)
        or (0x2070 <= cp <= 0x209F)
        or (0x2100 <= cp <= 0x214F)
        or (0x1D400 <= cp <= 0x1D7FF)
    )


def _build_styled_line_plan(
    line_str: str,
    font_size_pt: float,
    ja_font_tag: str,
    ja_font: pymupdf.Font | None,
    math_font_tag: str | None = None,
    math_font: pymupdf.Font | None = None,
    inline_math_spans: list[InlineMathSpan] | None = None,
) -> tuple[
    list[tuple[str, pymupdf.Font | None, str, float, float, float]],
    list[tuple[float, float, float, float]],
    float,
    float,
    float,
]:
    """Compute exact styled glyph runs, radical overbar segments, total horizontal width,
    and vertical ascent/descent extents (in `em` units) for `line_str`.
    """
    n = len(line_str)
    if n == 0:
        return [], [], 0.0, 0.78, 0.22

    char_role = ["normal"] * n
    char_scale = [1.0] * n
    char_dy_em = [0.0] * n
    overbar_ranges: list[tuple[int, int]] = []

    if inline_math_spans:
        # Process longer contexts first (e.g. `𝑃abci` before `𝑃abc`) so stacked
        # subscript characters are already classified when shorter super spans check boundaries.
        ordered_spans = sorted(
            inline_math_spans,
            key=lambda m: (len(m.context_text), 1 if m.role == "sub" else 0),
            reverse=True,
        )
        for mspan in ordered_spans:
            if mspan.ambiguous:
                continue
            ctx = mspan.context_text
            tgt = mspan.target_text
            if not ctx or not tgt:
                continue
            start = 0
            while True:
                pos = line_str.find(ctx, start)
                if pos == -1:
                    break
                start = pos + 1
                if mspan.role == "hat" and ctx.startswith(tgt):
                    t0 = pos
                    t1 = pos + len(tgt)
                    for k in range(t0, t1):
                        char_role[k] = "hat"
                        char_scale[k] = mspan.scale
                        char_dy_em[k] = mspan.dy_em
                elif mspan.role in ("super", "sub") and ctx.endswith(tgt):
                    t1 = pos + len(ctx)
                    t0 = t1 - len(tgt)
                    if (
                        pos > 0
                        and line_str[pos].isascii()
                        and line_str[pos].isalpha()
                        and line_str[pos - 1].isascii()
                        and line_str[pos - 1].isalnum()
                    ):
                        continue
                    if (
                        t1 < n
                        and _is_math_ident_continuation_char(line_str[t1 - 1])
                        and _is_math_ident_continuation_char(line_str[t1])
                        and char_role[t1] == "normal"
                    ):
                        continue
                    for k in range(t0, t1):
                        char_role[k] = mspan.role
                        char_scale[k] = mspan.scale
                        char_dy_em[k] = mspan.dy_em
                elif mspan.role == "radicand" and mspan.overbar and ctx.endswith(tgt):
                    t1 = pos + len(ctx)
                    t0 = t1 - len(tgt)
                    if (t0, t1) not in overbar_ranges:
                        overbar_ranges.append((t0, t1))

    def _measure_char(ch: str, fs: float) -> tuple[str, pymupdf.Font | None, float]:
        if ja_font is not None:
            sel = _select_char_font(ch, ja_font_tag, ja_font, math_font_tag, math_font)
            tag, fobj = sel if sel is not None else (ja_font_tag, ja_font)
            return tag, fobj, float(fobj.text_length(ch, fontsize=fs))
        w = fs * 0.56 if (ord(ch) < 128 or (0x1D400 <= ord(ch) <= 0x1D7FF)) else fs * 1.02
        return ja_font_tag, None, w

    styled_runs: list[tuple[str, pymupdf.Font | None, str, float, float, float]] = []
    char_x0 = [0.0] * n
    char_x1 = [0.0] * n
    cursor_x = 0.0
    last_run_end_x = 0.0
    max_rendered_x1 = 0.0
    sup_group_x0: float | None = None
    sup_group_x1 = 0.0
    sub_group_x1 = 0.0
    in_stacked_sub = False

    for i in range(n):
        ch = line_str[i]
        role = char_role[i]
        # Collapse artificial space between stacked superscript and subscript (`𝑃^∗_3`, `𝑃^{abc}_{i}`)
        if (
            ch == " "
            and i > 0
            and i + 1 < n
            and char_role[i - 1] == "super"
            and char_role[i + 1] == "sub"
        ):
            char_x0[i] = cursor_x
            char_x1[i] = cursor_x
            continue

        if role == "hat" and i + 1 < n:
            hat_fs = font_size_pt * char_scale[i]
            hat_tag, hat_fobj, hat_w = _measure_char(ch, hat_fs)
            base_fs = font_size_pt * char_scale[i + 1]
            _, _, base_w = _measure_char(line_str[i + 1], base_fs)
            hat_x = cursor_x + max(0.0, (base_w - hat_w) * 0.5)
            hat_y = font_size_pt * char_dy_em[i]
            styled_runs.append((hat_tag, hat_fobj, ch, hat_x, hat_y, hat_fs))
            last_run_end_x = hat_x + hat_w
            max_rendered_x1 = max(max_rendered_x1, last_run_end_x)
            char_x0[i] = cursor_x
            char_x1[i] = cursor_x + base_w
            sup_group_x0 = None
            in_stacked_sub = False
            continue

        if role == "super":
            if i == 0 or char_role[i - 1] != "super":
                sup_group_x0 = cursor_x
                sup_group_x1 = cursor_x
            in_stacked_sub = False
        elif role == "sub":
            is_sub_start = i == 0 or char_role[i - 1] != "sub"
            preceded_by_super = (
                (i > 0 and char_role[i - 1] == "super")
                or (i >= 2 and line_str[i - 1] == " " and char_role[i - 2] == "super")
            )
            if is_sub_start and preceded_by_super and sup_group_x0 is not None:
                in_stacked_sub = True
                sub_group_x1 = sup_group_x0
                cursor_x = sup_group_x0
        else:
            in_stacked_sub = False
            sup_group_x0 = None

        c_fs = font_size_pt * char_scale[i]
        c_y = font_size_pt * char_dy_em[i]
        c_tag, c_fobj, c_w = _measure_char(ch, c_fs)
        c_x0 = cursor_x
        char_x0[i] = c_x0
        cursor_x += c_w
        char_x1[i] = cursor_x
        if role == "super":
            sup_group_x1 = max(sup_group_x1, cursor_x)
        if in_stacked_sub:
            sub_group_x1 = max(sub_group_x1, cursor_x)

        can_merge = (
            bool(styled_runs)
            and (i == 0 or char_role[i - 1] != "hat")
            and not (role == "sub" and in_stacked_sub and (i == 0 or char_role[i - 1] != "sub"))
            and styled_runs[-1][0] == c_tag
            and abs(styled_runs[-1][4] - c_y) < 1e-4
            and abs(styled_runs[-1][5] - c_fs) < 1e-4
            and abs(last_run_end_x - c_x0) < 1e-4
        )
        if can_merge:
            p_tag, p_fobj, p_txt, p_x, p_y, p_fs = styled_runs[-1]
            new_txt = p_txt + ch
            styled_runs[-1] = (p_tag, p_fobj, new_txt, p_x, p_y, p_fs)
            if c_fobj is not None:
                actual_w = float(c_fobj.text_length(new_txt, fontsize=c_fs))
                last_run_end_x = p_x + actual_w
                cursor_x = max(cursor_x, last_run_end_x)
                char_x1[i] = cursor_x
            else:
                last_run_end_x = cursor_x
        else:
            styled_runs.append((c_tag, c_fobj, ch, c_x0, c_y, c_fs))
            last_run_end_x = cursor_x

        max_rendered_x1 = max(max_rendered_x1, cursor_x, last_run_end_x)

        # At the end of a stacked sub/super group, advance cursor_x past the wider group
        if in_stacked_sub and (i + 1 >= n or char_role[i + 1] != "sub"):
            cursor_x = max(sup_group_x1, sub_group_x1)
            max_rendered_x1 = max(max_rendered_x1, cursor_x)
            in_stacked_sub = False
            sup_group_x0 = None

    overbars: list[tuple[float, float, float, float]] = []
    for t0, t1 in overbar_ranges:
        if 0 <= t0 < t1 <= n:
            bx0 = char_x0[t0]
            bx1 = char_x1[t1 - 1]
            by_off = -font_size_pt * 0.88
            bw = max(0.45, font_size_pt * 0.055)
            overbars.append((bx0, bx1, by_off, bw))
            max_rendered_x1 = max(max_rendered_x1, bx1)

    top_em = 0.78
    if any(r in ("super", "hat") for r in char_role):
        top_em = max(top_em, 0.86)
    if overbars:
        top_em = max(top_em, 0.90)
    bot_em = 0.22
    if any(r == "sub" for r in char_role):
        bot_em = max(bot_em, 0.36)

    return styled_runs, overbars, max_rendered_x1, top_em, bot_em


def _subtract_protected_boxes(
    rect_coords: list[float],
    protected_boxes: list[list[float]],
    margin: float = 0.25,
) -> list[tuple[float, float, float, float]]:
    """Return non-overlapping sub-rectangles of `rect_coords` strictly outside every
    box in `protected_boxes` (inflated by `margin`). Used so whiteout rects such as
    `orig_overbar_bbox` never paint over protected text or vector graphics.
    """
    rects: list[tuple[float, float, float, float]] = [
        (float(rect_coords[0]), float(rect_coords[1]), float(rect_coords[2]), float(rect_coords[3]))
    ]
    for pb in protected_boxes:
        cx0 = float(pb[0]) - margin
        cy0 = float(pb[1]) - margin
        cx1 = float(pb[2]) + margin
        cy1 = float(pb[3]) + margin
        next_rects: list[tuple[float, float, float, float]] = []
        for rx0, ry0, rx1, ry1 in rects:
            ix0 = max(rx0, cx0)
            iy0 = max(ry0, cy0)
            ix1 = min(rx1, cx1)
            iy1 = min(ry1, cy1)
            if ix0 >= ix1 or iy0 >= iy1:
                next_rects.append((rx0, ry0, rx1, ry1))
                continue
            if ry0 < iy0:
                next_rects.append((rx0, ry0, rx1, iy0))
            if iy1 < ry1:
                next_rects.append((rx0, iy1, rx1, ry1))
            if rx0 < ix0:
                next_rects.append((rx0, iy0, ix0, iy1))
            if ix1 < rx1:
                next_rects.append((ix1, iy0, rx1, iy1))
        rects = [
            (x0, y0, x1, y1)
            for (x0, y0, x1, y1) in next_rects
            if (x1 - x0) > 0.1 and (y1 - y0) > 0.1
        ]
        if not rects:
            break
    return rects


def _rewrite_tounicode_cmap_smp_bytes(raw: bytes) -> bytes:
    """Rewrite 5- or 6-hex-digit SMP Unicode scalar values (`<10000>`..`<10FFFF>`) emitted by
    MuPDF 1.27.x inside `beginbfchar ... endbfchar` and `beginbfrange ... endbfrange` sections
    of a `/ToUnicode` CMap stream into PDF-compliant 8-digit UTF-16BE surrogate pairs (`<D8xxDCxx>`).

    Guarantees:
    - 4-digit BMP targets (`<0041>`, `<3042>`) are never modified.
    - 8-digit valid UTF-16BE surrogate pairs (`<D835DC43>`) or BMP ligatures are never modified.
    - Source glyph codes (`<src>` in `bfchar` and `<src_lo> <src_hi>` in `bfrange`) are never modified.
    - Unsupported CMap sections (`begincidchar`, `begincidrange`, headers/footers) are never modified.
    - Idempotent: applying this function two or more times produces the exact same bytes.
    """
    if not raw or (b"beginbfchar" not in raw and b"beginbfrange" not in raw):
        return raw

    def _fix_dst_hex_token(m: re.Match) -> bytes:
        hex_bytes = m.group(1)
        if len(hex_bytes) in (5, 6):
            cp = int(hex_bytes, 16)
            if 0x10000 <= cp <= 0x10FFFF:
                u16 = chr(cp).encode("utf-16-be").hex().upper().encode("ascii")
                return b"<" + u16 + b">"
        return m.group(0)

    def _fix_bfchar_section(sec_match: re.Match) -> bytes:
        prefix, body, suffix = sec_match.group(1), sec_match.group(2), sec_match.group(3)
        fixed_body = _BFCHAR_PAIR_RE.sub(
            lambda pm: pm.group(1) + pm.group(2) + _HEX_TOKEN_RE.sub(_fix_dst_hex_token, pm.group(3)),
            body,
        )
        return prefix + fixed_body + suffix

    def _fix_bfrange_section(sec_match: re.Match) -> bytes:
        prefix, body, suffix = sec_match.group(1), sec_match.group(2), sec_match.group(3)
        fixed_body = _BFRANGE_TRIPLE_RE.sub(
            lambda rm: (
                rm.group(1)
                + rm.group(2)
                + rm.group(3)
                + rm.group(4)
                + _HEX_TOKEN_RE.sub(_fix_dst_hex_token, rm.group(5))
            ),
            body,
        )
        return prefix + fixed_body + suffix

    out = _BFCHAR_SECTION_RE.sub(_fix_bfchar_section, raw)
    out = _BFRANGE_SECTION_RE.sub(_fix_bfrange_section, out)
    return out


def _collect_document_tounicode_xrefs(doc: pymupdf.Document) -> set[int]:
    """Return the set of all `/ToUnicode` stream xrefs currently referenced anywhere in `doc`."""
    tu_xrefs: set[int] = set()
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if "/ToUnicode " in obj:
            try:
                tu_xref = int(obj.split("/ToUnicode ")[1].split()[0])
                tu_xrefs.add(tu_xref)
            except Exception:
                continue
    return tu_xrefs


def _fix_embedded_tounicode_smp(
    doc: pymupdf.Document,
    target_font_tags: tuple[str, ...] = ("F_JA", "F_MATH"),
    protected_tounicode_xrefs: set[int] | None = None,
) -> int:
    """Fix MuPDF's 5-digit hex `<1xxxx>` entries for SMP codepoints (`U+10000`..`U+10FFFF`)
    in embedded `/ToUnicode` CMap streams of newly inserted translation/math fonts (`F_JA`, `F_MATH`)
    into valid PDF 8-digit UTF-16BE surrogate pairs (`<D8xxDCxx>`), without touching any original
    PDF font CMap streams. Returns the number of `/ToUnicode` streams updated.
    """
    target_tag_set = set(target_font_tags)
    candidate_font_xrefs: set[int] = set()
    for p_idx in range(len(doc)):
        try:
            for f_entry in doc.get_page_fonts(p_idx):
                # f_entry = (xref, ext, type, basefont, name, encoding, ...)
                if len(f_entry) >= 5 and f_entry[4] in target_tag_set:
                    candidate_font_xrefs.add(int(f_entry[0]))
        except Exception:
            continue

    seen_tu_xrefs: set[int] = set()
    updated_count = 0
    for font_xref in sorted(candidate_font_xrefs):
        try:
            obj = doc.xref_object(font_xref)
        except Exception:
            continue
        if "/ToUnicode " not in obj:
            continue
        try:
            tu_xref = int(obj.split("/ToUnicode ")[1].split()[0])
            if tu_xref in seen_tu_xrefs:
                continue
            seen_tu_xrefs.add(tu_xref)
            if protected_tounicode_xrefs and tu_xref in protected_tounicode_xrefs:
                continue
            raw = doc.xref_stream(tu_xref)
            if raw:
                fixed = _rewrite_tounicode_cmap_smp_bytes(raw)
                if fixed != raw:
                    doc.update_stream(tu_xref, fixed)
                    updated_count += 1
        except Exception:
            continue
    return updated_count


def _find_unresolved_placeholders(restored_text: str, source_text: str) -> list[str]:
    """Return any placeholder-like tokens remaining in restored_text that were not
    literally present in the original source_text.
    """
    found = _UNRESOLVED_PH_CHECK_RE.findall(restored_text)
    if not found:
        return []
    src_tokens = _UNRESOLVED_PH_CHECK_RE.findall(source_text or "")
    unresolved: list[str] = []
    remaining_src = list(src_tokens)
    for tok in found:
        if tok in remaining_src:
            remaining_src.remove(tok)
        else:
            unresolved.append(tok)
    return unresolved


def _find_unrenderable_chars(
    text: str,
    ja_font_tag: str,
    ja_font: pymupdf.Font,
    math_font_tag: str | None = None,
    math_font: pymupdf.Font | None = None,
) -> list[str]:
    """Return a list of unique characters (formatted as 'ch(U+XXXX)') in `normalize_for_pdf_rendering(text)`
    that have no glyph (`has_glyph == 0`) in the active fonts.
    """
    norm_text = normalize_for_pdf_rendering(text)
    missing: list[str] = []
    seen: set[int] = set()
    for ch in norm_text:
        cp = ord(ch)
        if cp <= 0x20 or ch.isspace() or cp in seen:
            continue
        if _select_char_font(ch, ja_font_tag, ja_font, math_font_tag, math_font) is None:
            seen.add(cp)
            missing.append(f"{ch}(U+{cp:04X})")
    return missing

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
                    inline_spans = extract_inline_math_spans(lines, drawings)
                    blk = Block(
                        id=f"{document.id}:page-{p_idx + 1}:block-{block_seq}",
                        page_index=p_idx,
                        bbox=bbox,
                        kind=kind,
                        source_text=full_text,
                        inline_math_spans=inline_spans,
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
            prev_span: dict | None = None

            for s in spans:
                t = str(s.get("text", "")).replace("\xa0", " ")
                if not t:
                    continue
                sz = float(s.get("size", 10.0))
                font_name = str(s.get("font", "")).lower()
                flags = int(s.get("flags", 0))
                sizes.append(sz)

                if any(k in font_name for k in ("cmmi", "cmsy", "cmex", "math", "symbol", "txsys", "txexs", "txmia")):
                    has_math_font = True
                if (flags & 1) or (sz < max_line_size * 0.78 and len(t.strip()) <= 4):
                    has_sub_super = True

                if prev_span is not None:
                    prev_t = str(prev_span.get("text", "")).replace("\xa0", " ")
                    prev_bbox = prev_span.get("bbox")
                    cur_bbox = s.get("bbox")
                    if (
                        prev_t
                        and not prev_t[-1].isspace()
                        and not t[0].isspace()
                        and isinstance(prev_bbox, (list, tuple))
                        and len(prev_bbox) == 4
                        and isinstance(cur_bbox, (list, tuple))
                        and len(cur_bbox) == 4
                    ):
                        dx = float(cur_bbox[0]) - float(prev_bbox[2])
                        if dx >= max(1.8, max_line_size * 0.22):
                            has_prose_boundary = bool(
                                re.search(r"(?:[A-Za-z]{2,}|\b[aI])$", prev_t)
                                or re.match(r"^\(?([A-Za-z]{2,}|[aI])\b", t)
                            )
                            if (
                                has_prose_boundary
                                and t[0] not in ".,;:!?)]}’”'"
                                and prev_t[-1] not in "([{‘“"
                            ):
                                line_parts.append(" ")

                line_parts.append(t)
                prev_span = s

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
                # Join prime/sub/superscript directly onto the last line of prev while preserving span objects
                first_cur_line = cur_txt_lines[0].strip() if cur_txt_lines else ""
                if (
                    len(cur_txt_lines) >= 2
                    and re.match(r"^[′″']+$", first_cur_line)
                    and re.match(r"^[A-Za-z0-9(\[]", cur_txt_lines[1].strip())
                ):
                    head_lines = cur_lines[:2]
                    remaining_cur_lines = cur_lines[2:]
                else:
                    head_lines = cur_lines[:1]
                    remaining_cur_lines = cur_lines[1:]

                if prev_lines[-1].get("spans"):
                    prev_lines[-1]["spans"][-1]["text"] = str(
                        prev_lines[-1]["spans"][-1].get("text", "")
                    ).rstrip()
                    for hl in head_lines:
                        hl_spans = [dict(s) for s in hl.get("spans", []) if str(s.get("text", ""))]
                        if hl_spans:
                            hl_spans[0]["text"] = str(hl_spans[0].get("text", "")).lstrip()
                            prev_lines[-1]["spans"].extend(hl_spans)
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
                    if not sep:
                        for cl in cur_lines:
                            cl_spans = [
                                dict(s) for s in cl.get("spans", []) if str(s.get("text", ""))
                            ]
                            prev_lines[-1]["spans"].extend(cl_spans)
                    else:
                        prev_lines[-1]["spans"][-1]["text"] = (
                            str(prev_lines[-1]["spans"][-1].get("text", "")).rstrip()
                            + sep
                            + cur_text
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
        if font_path and font_path.exists():
            ja_font_tag = "F_JA"
            ja_font_obj = pymupdf.Font(fontfile=str(font_path))
        else:
            ja_font_tag = "japan-s"
            ja_font_obj = pymupdf.Font("japan-s")

        math_font_path = getattr(self.config, "font_math_path", None)
        if math_font_path and Path(math_font_path).exists():
            math_font_tag: str | None = "F_MATH"
            math_font_obj: pymupdf.Font | None = pymupdf.Font(fontfile=str(math_font_path))
        else:
            math_font_tag = None
            math_font_obj = None

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
            orig_tounicode_xrefs = _collect_document_tounicode_xrefs(out_doc)
            target_set = set(page_indices)

            for p_idx in sorted(target_set):
                if p_idx < 0 or p_idx >= len(out_doc):
                    continue
                mpage = out_doc[p_idx]
                raw_page_blocks = blocks_by_page.get(p_idx, [])
                # Sort deterministically by geometry/id so protection decisions and rendering
                # never depend on the caller's input list ordering.
                page_blocks = sorted(
                    raw_page_blocks,
                    key=lambda b: (
                        round(float(b.bbox[1]), 3),
                        round(float(b.bbox[0]), 3),
                        round(float(b.bbox[3]), 3),
                        round(float(b.bbox[2]), 3),
                        b.id,
                    ),
                )
                page_manual = manual_by_page.get(p_idx, [])

                # Pre-check 0: Detect translatable block pairs whose bounding boxes deeply overlap
                # (e.g. when PyMuPDF split a single paragraph mid-line across an inline math stack
                # or stacked fraction like 1/10). Fall back both colliding blocks to original before
                # redaction so they never render overlapping Japanese text or distort split fractions.
                candidate_trans_blocks = [
                    b
                    for b in page_blocks
                    if b.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED)
                    and b.effective_translation
                ]
                colliding_block_ids: dict[str, list[str]] = {}
                for i_b, b1 in enumerate(candidate_trans_blocks):
                    for b2 in candidate_trans_blocks[i_b + 1 :]:
                        x_ov = min(float(b1.bbox[2]), float(b2.bbox[2])) - max(
                            float(b1.bbox[0]), float(b2.bbox[0])
                        )
                        y_ov = min(float(b1.bbox[3]), float(b2.bbox[3])) - max(
                            float(b1.bbox[1]), float(b2.bbox[1])
                        )
                        if x_ov > 5.0 and y_ov >= 4.5:
                            ir1 = bbox_intersection_ratio(b1.bbox, b2.bbox)
                            ir2 = bbox_intersection_ratio(b2.bbox, b1.bbox)
                            if max(ir1, ir2) >= 0.20 or bbox_iou(b1.bbox, b2.bbox) >= 0.08:
                                colliding_block_ids.setdefault(b1.id, []).append(b2.id)
                                colliding_block_ids.setdefault(b2.id, []).append(b1.id)

                for b_col in candidate_trans_blocks:
                    if b_col.id in colliding_block_ids:
                        peer_ids = sorted(colliding_block_ids[b_col.id])
                        b_col.status = BlockStatus.FALLBACK_ORIGINAL
                        b_col.translation_provenance = "fallback_original"
                        if "TRANSLATED_BLOCK_OVERLAP_FALLBACK" not in b_col.warnings:
                            b_col.warnings.append("TRANSLATED_BLOCK_OVERLAP_FALLBACK")
                        issues.append(
                            ValidationIssue(
                                id=f"iss-{uuid.uuid4().hex[:10]}",
                                job_id=job_id,
                                document_id=document.id,
                                page_index=p_idx,
                                block_id=b_col.id,
                                bbox=b_col.bbox,
                                code="TRANSLATED_BLOCK_OVERLAP_FALLBACK",
                                severity=IssueSeverity.WARNING,
                                message=(
                                    f"隣接する翻訳ブロック（{peer_ids}）と矩形が大きく重複しているため、"
                                    "行の重なりや分割数式の崩れを防ぎ原文要素を保持しました。"
                                ),
                                details={"colliding_block_ids": peer_ids},
                                fallback_applied=True,
                            )
                        )

                # Collect all initial protected bounding boxes on this page
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

                def _record_fallback(
                    blk_obj: Block,
                    code: str,
                    message: str,
                    details: dict | None = None,
                ) -> None:
                    blk_obj.status = BlockStatus.FALLBACK_ORIGINAL
                    blk_obj.translation_provenance = "fallback_original"
                    if code not in blk_obj.warnings:
                        blk_obj.warnings.append(code)
                    protected_boxes.append(blk_obj.bbox)
                    issues.append(
                        ValidationIssue(
                            id=f"iss-{uuid.uuid4().hex[:10]}",
                            job_id=job_id,
                            document_id=document.id,
                            page_index=p_idx,
                            block_id=blk_obj.id,
                            bbox=blk_obj.bbox,
                            code=code,
                            severity=IssueSeverity.WARNING,
                            message=message,
                            details=details or {},
                            fallback_applied=True,
                        )
                    )

                # Phase 1: Intrinsic per-block checks (order-independent) before spatial convergence
                provisional_items: list[tuple[Block, float, list[str], list[InlineMathSpan]]] = []
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

                    # Pre-redaction check 1: Never allow unresolved placeholders into final PDF
                    unresolved_phs = _find_unresolved_placeholders(restored_text, blk.source_text)
                    if unresolved_phs:
                        _record_fallback(
                            blk,
                            "UNRESOLVED_PLACEHOLDER_FALLBACK",
                            (
                                f"未解決の保護プレースホルダー（{unresolved_phs}）が残存しているため、"
                                "不完全な出力を避けて原文要素を保持しました。"
                            ),
                            {"unresolved_placeholders": unresolved_phs},
                        )
                        continue

                    # Pre-redaction check 2: Verify all characters have glyphs in active rendering fonts
                    missing_glyphs = _find_unrenderable_chars(
                        restored_text,
                        ja_font_tag,
                        ja_font_obj,
                        math_font_tag,
                        math_font_obj,
                    )
                    if missing_glyphs:
                        _record_fallback(
                            blk,
                            "UNRENDERABLE_GLYPH_FALLBACK",
                            (
                                f"利用可能なフォントにグリフが存在しない文字（{missing_glyphs}）が含まれるため、"
                                "文字欠け（.notdef）を防ぎ原文要素を保持しました。"
                            ),
                            {"missing_glyphs": missing_glyphs},
                        )
                        continue

                    # Normalize known TeX operator variation selectors before width measurement & rendering
                    render_ready_text = normalize_for_pdf_rendering(restored_text)

                    # Refresh structured inline math spans from original PDF page if not yet populated
                    if not blk.inline_math_spans:
                        try:
                            src_page = src_doc[p_idx]
                            clip_dict = src_page.get_text("dict", clip=pymupdf.Rect(blk.bbox))
                            raw_clip_lines = [
                                ln
                                for b_item in clip_dict.get("blocks", [])
                                if b_item.get("type", 0) == 0
                                for ln in b_item.get("lines", [])
                            ]
                            if raw_clip_lines:
                                blk.inline_math_spans = extract_inline_math_spans(
                                    _merge_horizontal_visual_lines(raw_clip_lines),
                                    src_page.get_drawings(),
                                )
                        except Exception:
                            pass

                    # For MANUALLY_EDITED blocks, only apply InlineMathSpan items that belong to
                    # protected `{vN}` placeholders still present in the manual translation text
                    # (or legacy unmasked math already in `blk.translation_text`), and never apply
                    # source inline_math_spans onto new plain text typed outside `{vN}`.
                    if blk.status == BlockStatus.MANUALLY_EDITED:
                        all_prot_texts = [
                            normalize_for_pdf_rendering(it.original_text)
                            for it in blk.protected_items
                        ]
                        kept_orig_texts = [
                            normalize_for_pdf_rendering(it.original_text)
                            for it in blk.protected_items
                            if it.placeholder in effective_txt
                        ]
                        stripped_manual = normalize_for_pdf_rendering(
                            re.sub(
                                r"\{v\d+\}",
                                " ",
                                effective_txt,
                            )
                        )
                        norm_orig_trans = normalize_for_pdf_rendering(blk.translation_text or "")
                        active_math_spans = [
                            ms
                            for ms in blk.inline_math_spans
                            if (
                                any(ms.context_text in orig_t for orig_t in kept_orig_texts)
                                and ms.context_text not in stripped_manual
                            )
                            or (
                                not any(ms.context_text in orig_t for orig_t in all_prot_texts)
                                and bool(norm_orig_trans)
                                and ms.context_text in norm_orig_trans
                            )
                        ]
                    else:
                        active_math_spans = list(blk.inline_math_spans)

                    # Pre-redaction check 3: Refuse to flatten conflicting super/sub/normal math roles
                    ambiguous_ctxs = sorted(
                        {ms.context_text for ms in active_math_spans if ms.ambiguous}
                    )
                    if ambiguous_ctxs:
                        _record_fallback(
                            blk,
                            "AMBIGUOUS_INLINE_MATH_FALLBACK",
                            (
                                f"同一ブロック内で上付き・下付き・通常表記が競合する数式表現（{ambiguous_ctxs}）があり、"
                                "意味を損なわず安全に組版できないため原文要素を保持しました。"
                            ),
                            {"ambiguous_contexts": ambiguous_ctxs},
                        )
                        continue

                    # Fit Japanese lines within bbox respecting min_font_size_pt (Section 12)
                    fitted_size, wrapped_lines, fits = fit_japanese_text_in_bbox(
                        text=render_ready_text,
                        bbox=blk.bbox,
                        initial_font_size=min(blk.font_size_pt, 11.0),
                        min_font_size=job_config.min_font_size_pt,
                        ja_font=ja_font_obj,
                        math_font=math_font_obj,
                        inline_math_spans=active_math_spans,
                    )
                    if not fits:
                        # Section 12: Never shrink below min_font_size_pt, never delete text,
                        # never move figures/formulas; keep original and emit overflow warning.
                        _record_fallback(
                            blk,
                            "TYPESETTING_OVERFLOW_FALLBACK",
                            (
                                f"最小フォントサイズ（{job_config.min_font_size_pt}pt）でも枠内に収まらないため、"
                                "訳文の切り捨てや数式への重なりを避けて原文要素を保持しました（要手動確認）。"
                            ),
                            {
                                "min_font_size_pt": job_config.min_font_size_pt,
                                "restored_text": restored_text,
                            },
                        )
                        continue

                    blk.fitted_font_size_pt = fitted_size
                    provisional_items.append((blk, fitted_size, wrapped_lines, active_math_spans))

                # Phase 2: Fixed-point convergence so all approved blocks & overbar clear rects
                # are re-evaluated whenever `protected_boxes` grows, guaranteeing order independence.
                while provisional_items:
                    newly_rejected = False
                    next_provisional: list[
                        tuple[Block, float, list[str], list[InlineMathSpan]]
                    ] = []
                    for item in provisional_items:
                        blk, fitted_size, wrapped_lines, active_math_spans = item
                        overbar_boxes = [
                            [
                                float(ms.orig_overbar_bbox[0]),
                                float(ms.orig_overbar_bbox[1]),
                                float(ms.orig_overbar_bbox[2]),
                                float(ms.orig_overbar_bbox[3]),
                            ]
                            for ms in active_math_spans
                            if ms.orig_overbar_bbox and len(ms.orig_overbar_bbox) == 4
                        ]
                        check_boxes = [blk.bbox] + overbar_boxes
                        overlapping_prot = any(
                            bbox_iou(cb, pb) > 0.02
                            or bbox_intersection_ratio(pb, cb) > 0.05
                            or bbox_intersection_ratio(cb, pb) > 0.05
                            for cb in check_boxes
                            for pb in protected_boxes
                        )
                        if overlapping_prot:
                            _record_fallback(
                                blk,
                                "PROTECTED_REGION_OVERLAP_FALLBACK",
                                "翻訳領域が保護対象（数式・図）と近接・重複するため、数式保護を優先して原文要素を保持しました。",
                            )
                            newly_rejected = True
                        else:
                            next_provisional.append(item)
                    provisional_items = next_provisional
                    if not newly_rejected:
                        break

                approved_typeset_items = provisional_items

                if approved_typeset_items:
                    # Redact ONLY the approved translated text bounding boxes while preserving 100% of images & vector line art
                    for blk, _, _, _ in approved_typeset_items:
                        rx0, ry0, rx1, ry1 = (
                            float(blk.bbox[0]),
                            float(blk.bbox[1]),
                            float(blk.bbox[2]),
                            float(blk.bbox[3]),
                        )
                        # Ensure redaction rect never clips the border of any adjacent protected box
                        # (top, bottom, left, right) so adjacent FALLBACK_ORIGINAL / PROTECTED_SKIP
                        # glyphs are never erased by MuPDF's apply_redactions.
                        for pb in protected_boxes:
                            px0, py0, px1, py1 = float(pb[0]), float(pb[1]), float(pb[2]), float(pb[3])
                            if min(rx1, px1) - max(rx0, px0) > 0:
                                if py0 >= ry0 and py0 <= ry1 + 1.2 and py0 > (ry0 + ry1) / 2.0:
                                    ry1 = min(ry1, py0 - 1.0)
                                elif py1 <= ry1 and py1 >= ry0 - 1.2 and py1 < (ry0 + ry1) / 2.0:
                                    ry0 = max(ry0, py1 + 1.0)
                            if min(ry1, py1) - max(ry0, py0) > 0:
                                if px0 >= rx0 and px0 <= rx1 + 1.2 and px0 > (rx0 + rx1) / 2.0:
                                    rx1 = min(rx1, px0 - 1.0)
                                elif px1 <= rx1 and px1 >= rx0 - 1.2 and px1 < (rx0 + rx1) / 2.0:
                                    rx0 = max(rx0, px1 + 1.0)
                        if rx1 > rx0 and ry1 > ry0:
                            rect = pymupdf.Rect(rx0, ry0, rx1, ry1)
                            mpage.add_redact_annot(rect, fill=(1, 1, 1))
                    mpage.apply_redactions(
                        images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    )

                    # Clear any inline radical vinculum bar from the original English position
                    # for blocks that are being re-typeset, subtracting all protected_boxes
                    # so protected text or vector graphics adjacent to the overbar are never erased.
                    for blk, _, _, active_math_spans in approved_typeset_items:
                        for mspan in active_math_spans:
                            if mspan.orig_overbar_bbox and len(mspan.orig_overbar_bbox) == 4:
                                ox0, oy0, ox1, oy1 = (
                                    float(mspan.orig_overbar_bbox[0]),
                                    float(mspan.orig_overbar_bbox[1]),
                                    float(mspan.orig_overbar_bbox[2]),
                                    float(mspan.orig_overbar_bbox[3]),
                                )
                                white_rects = _subtract_protected_boxes(
                                    [ox0 - 0.5, oy0 - 1.0, ox1 + 0.5, oy1 + 1.0],
                                    protected_boxes,
                                    margin=0.25,
                                )
                                for wx0, wy0, wx1, wy1 in white_rects:
                                    mpage.draw_rect(
                                        pymupdf.Rect(wx0, wy0, wx1, wy1),
                                        color=None,
                                        fill=(1, 1, 1),
                                        overlay=True,
                                    )

                    if font_path and font_path.exists():
                        mpage.insert_font(fontname=ja_font_tag, fontfile=str(font_path))
                    else:
                        mpage.insert_font(fontname=ja_font_tag, fontbuffer=None)
                    if math_font_tag and math_font_path and Path(math_font_path).exists():
                        mpage.insert_font(fontname=math_font_tag, fontfile=str(math_font_path))

                    for blk, fitted_size, wrapped_lines, active_math_spans in approved_typeset_items:
                        line_height = fitted_size * 1.32
                        cursor_y = blk.bbox[1] + fitted_size * 0.95
                        base_x = float(blk.bbox[0])
                        for line_str in wrapped_lines:
                            styled_runs, overbars, _, _, _ = _build_styled_line_plan(
                                line_str=line_str,
                                font_size_pt=fitted_size,
                                ja_font_tag=ja_font_tag,
                                ja_font=ja_font_obj,
                                math_font_tag=math_font_tag,
                                math_font=math_font_obj,
                                inline_math_spans=active_math_spans,
                            )
                            for r_tag, _r_font, r_text, x_off, y_off, r_fs in styled_runs:
                                mpage.insert_text(
                                    pymupdf.Point(base_x + x_off, cursor_y + y_off),
                                    r_text,
                                    fontsize=r_fs,
                                    fontname=r_tag,
                                    color=(0, 0, 0),
                                    overlay=True,
                                )
                            for bx0, bx1, by_off, bw in overbars:
                                mpage.draw_line(
                                    pymupdf.Point(base_x + bx0, cursor_y + by_off),
                                    pymupdf.Point(base_x + bx1, cursor_y + by_off),
                                    color=(0, 0, 0),
                                    width=bw,
                                    overlay=True,
                                )
                            cursor_y += line_height

            _fix_embedded_tounicode_smp(
                out_doc,
                target_font_tags=("F_JA", "F_MATH"),
                protected_tounicode_xrefs=orig_tounicode_xrefs,
            )

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


TYPESETTING_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "TRANSLATED_BLOCK_OVERLAP_FALLBACK",
        "UNRESOLVED_PLACEHOLDER_FALLBACK",
        "UNRENDERABLE_GLYPH_FALLBACK",
        "AMBIGUOUS_INLINE_MATH_FALLBACK",
        "TYPESETTING_OVERFLOW_FALLBACK",
        "PROTECTED_REGION_OVERLAP_FALLBACK",
    }
)


# ----------------------------------------------------------------------
# Helper functions for Inline Formula Masking & Kinsoku Japanese Fitting
# ----------------------------------------------------------------------
def mask_inline_protected_elements(text: str) -> tuple[str, list[ProtectedItem]]:
    """Detect inline formulas, Greek/math symbols, sub/superscripts, and URLs/DOIs,
    replacing them with `{v1}`, `{v2}`, ... while leaving `[1]` citation brackets visible
    (or protected) for validation. Also safely protects any pre-existing literal `{vN}`
    or Private Use Area (PUA) characters in the source text so they cannot collide with
    generated placeholders or internal sentinels.
    """
    pending_items: list[tuple[ProtectedItemKind, str]] = []
    existing_cps = {ord(ch) for ch in text if ord(ch) >= 0xE000}
    base_cp = 0xE100
    for candidate_base in list(range(0xE100, 0xF800, 256)) + list(range(0xF0000, 0xFF000, 256)):
        if not any(candidate_base <= cp < candidate_base + 256 for cp in existing_cps):
            base_cp = candidate_base
            break
    sentinel_re = re.compile(f"[{chr(base_cp)}-{chr(base_cp + 255)}]")

    def _alloc_sentinel(kind: ProtectedItemKind, orig_text: str) -> str:
        idx = len(pending_items)
        pending_items.append((kind, orig_text))
        return chr(base_cp + idx)

    # Protect any pre-existing literal `{v1}`, `{v2}`, ... in source text first
    working = re.sub(
        r"\{v\d+\}",
        lambda m: _alloc_sentinel(ProtectedItemKind.INLINE_FORMULA, m.group(0)),
        text,
    )

    def _replace(match: re.Match, kind: ProtectedItemKind) -> str:
        raw_match = match.group(0)
        if sentinel_re.search(raw_match):
            return raw_match

        # Strip trailing sentence punctuation or unbalanced closing brackets/parens/braces
        orig = raw_match
        trailing = ""
        while orig and (
            orig[-1] in ".,;:"
            or (orig[-1] == ")" and orig.count(")") > orig.count("("))
            or (orig[-1] == "]" and orig.count("]") > orig.count("["))
            or (orig[-1] == "}" and orig.count("}") > orig.count("{"))
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
        sentinel = _alloc_sentinel(kind, orig)
        return f"{sentinel}{trailing}"

    working = _URL_DOI_RE.sub(lambda m: _replace(m, ProtectedItemKind.URL_OR_DOI), working)
    working = _EQUATION_REFERENCE_RE.sub(
        lambda m: _replace(m, ProtectedItemKind.EQUATION_NUMBER), working
    )
    working = _INLINE_MATH_EXPR_RE.sub(
        lambda m: _replace(m, ProtectedItemKind.INLINE_FORMULA), working
    )

    items: list[ProtectedItem] = []
    counter = 0

    def _finalize_sentinel(match: re.Match) -> str:
        nonlocal counter
        idx = ord(match.group(0)) - base_cp
        kind, orig_text = pending_items[idx]
        counter += 1
        ph = f"{{v{counter}}}"
        items.append(
            ProtectedItem(
                placeholder=ph,
                kind=kind,
                original_text=orig_text,
            )
        )
        return ph

    final_masked = sentinel_re.sub(_finalize_sentinel, working)
    return final_masked, items


def unmask_inline_protected_elements(
    translated_text: str, protected_items: list[ProtectedItem]
) -> str:
    """Restore `{v1}`, `{v2}` placeholders back to their exact original math/symbol strings
    in a single simultaneous pass so literal `{vN}` values never chain-expand.
    """
    if not translated_text or not protected_items:
        return translated_text
    ph_map = {item.placeholder: item.original_text for item in protected_items}
    pattern = re.compile("|".join(re.escape(ph) for ph in sorted(ph_map, key=len, reverse=True)))
    return pattern.sub(lambda m: ph_map[m.group(0)], translated_text)


def _measure_rendered_line_width(
    line_str: str,
    font_size_pt: float,
    ja_font: pymupdf.Font | None = None,
    math_font: pymupdf.Font | None = None,
    inline_math_spans: list[InlineMathSpan] | None = None,
) -> float:
    """Measure the exact rendered horizontal extent of a line matching `_build_styled_line_plan`."""
    if not line_str:
        return 0.0
    if inline_math_spans or ja_font is not None:
        styled_runs, _, plan_w, _, _ = _build_styled_line_plan(
            line_str=line_str,
            font_size_pt=font_size_pt,
            ja_font_tag="F_JA",
            ja_font=ja_font,
            math_font_tag="F_MATH" if math_font is not None else None,
            math_font=math_font,
            inline_math_spans=inline_math_spans,
        )
        max_x1 = plan_w
        for _, r_fobj, r_txt, r_x0, _, r_fs in styled_runs:
            if r_fobj is not None:
                max_x1 = max(max_x1, r_x0 + float(r_fobj.text_length(r_txt, fontsize=r_fs)))
        return max_x1
    return sum(
        font_size_pt * 0.56
        if (ord(c) < 128 or (0x1D400 <= ord(c) <= 0x1D7FF))
        else font_size_pt * 1.02
        for c in line_str
    )


def wrap_japanese_kinsoku(
    text: str,
    max_width_pt: float,
    font_size_pt: float,
    ja_font: pymupdf.Font | None = None,
    math_font: pymupdf.Font | None = None,
    inline_math_spans: list[InlineMathSpan] | None = None,
) -> list[str]:
    """Wrap mixed Japanese/English/math text into lines respecting `max_width_pt`
    and Kinsoku Shori (prohibited line-start and line-end characters) without
    right-edge horizontal overhang.
    """
    if not text:
        return []
    norm_text = normalize_for_pdf_rendering(text)
    lines: list[str] = []

    def _measure_str(s: str) -> float:
        return _measure_rendered_line_width(
            line_str=s,
            font_size_pt=font_size_pt,
            ja_font=ja_font,
            math_font=math_font,
            inline_math_spans=inline_math_spans,
        )

    # Tokenize so URLs, space-containing inline math spans (e.g. `𝑃abc i`), radical
    # expressions `√(...)`, or contiguous math sequences stay together without splitting.
    span_ctx_alts: list[str] = []
    if inline_math_spans:
        ws_ctx_set = sorted(
            {
                normalize_for_pdf_rendering(m.context_text)
                for m in inline_math_spans
                if not m.ambiguous
                and m.context_text
                and " " in normalize_for_pdf_rendering(m.context_text)
            },
            key=len,
            reverse=True,
        )
        span_ctx_alts = [re.escape(c) for c in ws_ctx_set]
    base_token_pattern = (
        r"https?://[^\s\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\uff00-\uffef]+"
        r"|[A-Za-z0-9\U0001d400-\U0001d7ff\u0370-\u03ff\u02c6=+\-−∗]*√\([^()\n]+\)[.,;:]?"
        r"|[A-Za-z0-9\U0001d400-\U0001d7ff\u0370-\u03ff\u2070-\u209f\u2190-\u21ff\u2200-\u22ff\u00b7\u00d7\u00f7\u27e8\u27e9\u02c6_^\(\)\[\]{}=<>≤≥≈≠±+\-−∗√∑∏∫*/.,:;′″']+"
        r"|\s+|."
    )
    token_pattern = "|".join(span_ctx_alts + [base_token_pattern]) if span_ctx_alts else base_token_pattern
    tokens = re.findall(token_pattern, norm_text)
    cur_tokens: list[str] = []

    for tok in tokens:
        if tok == "\n":
            cur_joined = "".join(cur_tokens).rstrip()
            if cur_joined:
                lines.append(cur_joined)
            cur_tokens = []
            continue
        if not cur_tokens and tok.isspace():
            continue

        cand_line = "".join(cur_tokens) + tok
        cand_w = _measure_str(cand_line)
        if cur_tokens and (cand_w > max_width_pt):
            cur_joined = "".join(cur_tokens)
            # Kinsoku line-start check (追い出し without exceeding max_width_pt):
            # Pull the trailing non-space token onto the next line with `tok`.
            if len(tok) == 1 and tok in KINSOKU_LINE_START_PROHIBITED:
                if (
                    len(cur_tokens) >= 2
                    and not cur_tokens[-1].isspace()
                    and cur_tokens[-1] not in KINSOKU_LINE_END_PROHIBITED
                    and cur_tokens[-1] not in KINSOKU_LINE_START_PROHIBITED
                ):
                    prefix = "".join(cur_tokens[:-1]).rstrip()
                    if prefix and _measure_str(prefix) <= max_width_pt + 1e-3:
                        lines.append(prefix)
                        cur_tokens = [cur_tokens[-1], tok]
                        continue
            # Kinsoku line-end check: if cur_tokens ends with an opening bracket, push it to next line
            if len(cur_tokens) >= 2 and cur_tokens[-1] in KINSOKU_LINE_END_PROHIBITED:
                carry = cur_tokens[-1]
                prefix = "".join(cur_tokens[:-1]).rstrip()
                if prefix:
                    lines.append(prefix)
                cur_tokens = [carry] if tok.isspace() else [carry, tok]
                continue

            if cur_joined.rstrip():
                lines.append(cur_joined.rstrip())
            cur_tokens = [] if tok.isspace() else [tok]
        else:
            cur_tokens.append(tok)

    cur_joined = "".join(cur_tokens).rstrip()
    if cur_joined:
        lines.append(cur_joined)

    return lines


def fit_japanese_text_in_bbox(
    text: str,
    bbox: list[float],
    initial_font_size: float = 10.0,
    min_font_size: float = 8.0,
    ja_font: pymupdf.Font | None = None,
    math_font: pymupdf.Font | None = None,
    inline_math_spans: list[InlineMathSpan] | None = None,
) -> tuple[float, list[str], bool]:
    """Find the largest font size in `[min_font_size, initial_font_size]` (step 0.5pt)
    such that the wrapped text (including any superscript/hat/radical top ascent,
    subscript bottom descent, and every wrapped line's horizontal extent) fits
    inside `bbox` width and height.
    Returns `(fitted_font_size, lines, fits_in_box)`.
    """
    raw_w = float(bbox[2]) - float(bbox[0])
    raw_h = float(bbox[3]) - float(bbox[1])
    if raw_w <= 0.0 or raw_h <= 0.0:
        return round(min_font_size, 2), [], False
    box_w = raw_w
    box_h = raw_h
    start_sz = max(min_font_size, initial_font_size)

    candidate_sizes: list[float] = []
    sz = start_sz
    while sz >= min_font_size - 1e-6:
        candidate_sizes.append(max(min_font_size, sz))
        sz -= 0.5
    if candidate_sizes and candidate_sizes[-1] > min_font_size + 1e-6:
        candidate_sizes.append(min_font_size)

    for sz in candidate_sizes:
        lines = wrap_japanese_kinsoku(
            text,
            box_w,
            sz,
            ja_font=ja_font,
            math_font=math_font,
            inline_math_spans=inline_math_spans,
        )
        line_h = sz * 1.32
        extra_bottom = 0.0
        if lines and inline_math_spans:
            _, _, _, _, last_bot_em = _build_styled_line_plan(
                line_str=lines[-1],
                font_size_pt=sz,
                ja_font_tag="F_JA",
                ja_font=ja_font,
                math_font_tag="F_MATH" if math_font is not None else None,
                math_font=math_font,
                inline_math_spans=inline_math_spans,
            )
            if last_bot_em > 0.25:
                extra_bottom = (last_bot_em - 0.22) * sz
        required_h = len(lines) * line_h + extra_bottom
        vertical_fits = required_h <= box_h + sz * 0.45
        horizontal_fits = all(
            _measure_rendered_line_width(
                line_str=line,
                font_size_pt=sz,
                ja_font=ja_font,
                math_font=math_font,
                inline_math_spans=inline_math_spans,
            )
            <= box_w + 1e-3
            for line in lines
        )
        if vertical_fits and horizontal_fits:
            return round(sz, 2), lines, True

    # Even at min_font_size it overflows vertically or horizontally
    min_lines = wrap_japanese_kinsoku(
        text,
        box_w,
        min_font_size,
        ja_font=ja_font,
        math_font=math_font,
        inline_math_spans=inline_math_spans,
    )
    return round(min_font_size, 2), min_lines, False
