"""Conservative detection of isolated TeX/math-italic single-letter variables.

Proposal A (primary protection):
Detects high-confidence standalone single-letter mathematical variables (e.g. `t`, `k`, `m`, `n`)
when all strict checks hold together:
1. Math-italic font family AND italic flag (`flags & 2`) — never broad font name OR italic flag alone.
2. Normal baseline size (`>= max_line_size * 0.86`, not superscript/subscript/hat/overbar).
3. Isolated single-ASCII-letter span (no adjacent script span, no embedded multi-letter word,
   conservatively excluding `'a'` and `'I'` without altering existing subscript math like `a_t`).
4. Surrounding prose/math context checks (rejects bullet labels, abbreviations, headings,
   units after digits, hyphen/slash/URL/DOI tokens, and operator-adjacent algebraic expressions).

Proposal B (auxiliary provenance):
Collects unmasked candidate coordinates (when protection is disabled or skipped by overlap guards)
while deduplicating against `{vN}` placeholders so `MATH_VARIABLE_MISSING` never double-counts.
"""

from __future__ import annotations

import re
from typing import Iterable

from backend.domain.models import (
    BlockKind,
    InlineMathSpan,
    UnmaskedMathVariableCandidate,
)

INLINE_MASK_VERSION_LEGACY = "inline-mask-v1"
INLINE_MASK_VERSION_STANDALONE_VAR = "inline-mask-v2"

# Prose articles/pronouns excluded conservatively from new unscripted single-letter detection.
# These remain out-of-scope for standalone protection here, while existing compound/scripted
# protection (e.g. `a_t`, `I_0`) is kept intact.
CONSERVATIVE_EXCLUDED_PROSE_SINGLE_LETTERS: frozenset[str] = frozenset({"a", "I"})

# Dedicated math-italic font families (explicitly excludes generic italic text fonts like CMTI,
# LMTI, LMRI, Times-Italic, Arial-ItalicMT, Helvetica-Oblique, etc.).
_MATH_ITALIC_FONT_RE = re.compile(
    r"(?:^|[-_+,.])(?:cmmi|cmmib|lmmi|lmmib|txmi|pxmi|mtmi|zplmi|zptmi|eurm)\d*"
    r"|(?:stix|cambria|latinmodern|asana|xits).*math.*italic",
    re.IGNORECASE,
)

# Isolated single ASCII letter inside a span (allows surrounding spaces or light punctuation).
_ISOLATED_SINGLE_VAR_SPAN_RE = re.compile(
    r"^[\s,;:\(\)\[\]]*([A-Za-z])[\s.,;:\)\]]*$"
)
_UNIT_HOMONYM_LETTERS: frozenset[str] = frozenset(
    {"s", "m", "g", "h", "l", "L", "K", "V", "A", "W", "J", "N", "T", "F", "H", "C"}
)
_OPERATOR_LEFT_RE = re.compile(r"[+\-−*/=<>≤≥≈≠×⋅·∈∉⊂⊆∪∩^|∣∥]\s*$")
_OPERATOR_RIGHT_RE = re.compile(r"^\s*[+\-−*/=<>≤≥≈≠×⋅·∈∉⊂⊆∪∩^|∣∥]")

_NON_PROSE_CATEGORIES: frozenset[str] = frozenset(
    {
        "title",
        "author",
        "affiliation",
        "email_or_contact",
        "publishing_metadata",
        "running_header",
        "running_footer",
        "reference_heading",
        "reference",
        "section_heading",
        "formula",
        "figure",
        "table",
    }
)
_NON_PROSE_KINDS: frozenset[BlockKind] = frozenset(
    {
        BlockKind.TITLE,
        BlockKind.HEADER_FOOTER,
        BlockKind.ISOLATED_FORMULA,
        BlockKind.FIGURE,
        BlockKind.TABLE,
        BlockKind.REFERENCE,
        BlockKind.HEADING,
    }
)


def is_block_eligible_for_standalone_math_vars(
    kind: BlockKind, semantic_category: str
) -> bool:
    """Return True when block semantics permit standalone single-letter variable masking."""
    return (
        kind not in _NON_PROSE_KINDS
        and semantic_category not in _NON_PROSE_CATEGORIES
    )


def is_math_italic_variable_span(span: dict, max_line_size: float) -> bool:
    """Require BOTH a dedicated math-italic font name AND italic flag at baseline size."""
    font_name = str(span.get("font", "")).strip()
    if not font_name or not _MATH_ITALIC_FONT_RE.search(font_name):
        return False
    flags = int(span.get("flags", 0))
    if (flags & 2) == 0:
        return False
    sz = float(span.get("size", 10.0))
    if max_line_size > 0 and sz < max_line_size * 0.86:
        return False
    return True


def _is_inside_compact_math_tuple(left_ctx: str, right_ctx: str) -> bool:
    """Return True when cursor lies inside an unprose parenthesized/bracketed arg list like `h(m, n, k, t)`."""
    last_open = -1
    depth = 0
    for idx in range(len(left_ctx) - 1, -1, -1):
        ch = left_ctx[idx]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            if depth == 0:
                last_open = idx
                break
            depth -= 1
    if last_open == -1:
        return False
    close_idx = -1
    depth = 0
    for idx, ch in enumerate(right_ctx):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                close_idx = idx
                break
            depth -= 1
    if close_idx == -1:
        return False
    inner = left_ctx[last_open + 1 :] + "x" + right_ctx[:close_idx]
    # Any English prose word of >= 3 letters inside the brackets signals prose, not a math tuple
    return not bool(re.search(r"\b[A-Za-z]{3,}\b", inner))


def _looks_like_bullet_or_abbrev(
    sym: str, left_ch: str, right_ch: str, left_ctx: str, right_ctx: str
) -> bool:
    if left_ch == "(" and right_ch == ")":
        return True
    if right_ch == ")" and (
        not left_ctx.strip()
        or bool(re.search(r"(?:^|[:;\n])\s*$", left_ctx))
        or (
            bool(re.match(r"^\)\s+[A-Za-z0-9]", right_ctx))
            and not bool(re.search(r"\([^()]*$", left_ctx))
        )
    ):
        return True
    if left_ch == "." or (
        right_ch == "."
        and (
            (len(right_ctx) >= 2 and right_ctx[1].isalpha())
            or bool(re.match(r"^\.\s*\d", right_ctx))
            or (
                sym.isupper()
                and (
                    not left_ctx.strip()
                    or bool(re.search(r"(?:^|[\n:;])\s*$", left_ctx))
                )
            )
        )
    ):
        return True
    if sym.isupper() and re.search(
        r"\b(?:Section|Appendix|Part|Case|Group|Class|Type|Level|Grade|Option|Item|Fig\.?|Table|Eq\.?)\s+$",
        left_ctx,
        re.IGNORECASE,
    ):
        return True
    return False


def extract_standalone_math_variable_spans(
    *,
    lines: list[dict],
    block_text: str,
    span_global_ranges: dict[tuple[int, int], tuple[int, int]],
    line_span_roles: list[list[str]],
    script_base_positions: set[tuple[int, int]],
    existing_script_ranges: Iterable[tuple[int, int]],
    normalize_fn,
    boundary_regex_fn,
    allow_extraction: bool = True,
) -> tuple[list[InlineMathSpan], list[UnmaskedMathVariableCandidate]]:
    """Detect high-confidence standalone math-italic single-letter variables and candidates."""
    if not allow_extraction or not block_text:
        return [], []

    script_ranges = list(existing_script_ranges)
    protected_occ_by_sym: dict[str, set[int]] = {}
    candidates_out: list[UnmaskedMathVariableCandidate] = []
    seen_positions: set[int] = set()

    for line_idx, line in enumerate(lines):
        spans = line.get("spans", [])
        if not spans or line_idx >= len(line_span_roles):
            continue
        roles = line_span_roles[line_idx]
        line_sizes = [
            float(s.get("size", 10.0)) for s in spans if str(s.get("text", "")).strip()
        ]
        if not line_sizes:
            continue
        max_sz = max(line_sizes)

        for s_idx, span in enumerate(spans):
            if s_idx >= len(roles) or roles[s_idx] != "normal":
                continue
            if (line_idx, s_idx) in script_base_positions:
                continue
            if s_idx > 0 and roles[s_idx - 1] == "hat":
                continue
            if not is_math_italic_variable_span(span, max_sz):
                continue

            norm_span_txt = normalize_fn(str(span.get("text", "")))
            span_m = _ISOLATED_SINGLE_VAR_SPAN_RE.match(norm_span_txt)
            if not span_m:
                continue
            sym = span_m.group(1)
            if sym in CONSERVATIVE_EXCLUDED_PROSE_SINGLE_LETTERS:
                continue

            g_rng = span_global_ranges.get((line_idx, s_idx))
            if g_rng is None:
                continue
            gs, ge = g_rng
            if gs < 0 or ge > len(block_text) or gs >= ge:
                continue
            sub_slice = block_text[gs:ge]
            sub_m = _ISOLATED_SINGLE_VAR_SPAN_RE.match(sub_slice)
            if not sub_m or sub_m.group(1) != sym:
                continue
            sym_idx = gs + sub_m.start(1)
            if sym_idx in seen_positions or block_text[sym_idx] != sym:
                continue
            if any(r0 <= sym_idx < r1 for r0, r1 in script_ranges):
                continue

            left_ch = block_text[sym_idx - 1] if sym_idx > 0 else ""
            right_ch = block_text[sym_idx + 1] if sym_idx + 1 < len(block_text) else ""
            if left_ch and re.match(r"[A-Za-z0-9\U0001d400-\U0001d7ff_/@:\\-]", left_ch):
                continue
            if right_ch and re.match(r"[A-Za-z0-9\U0001d400-\U0001d7ff_/@:\\^-]", right_ch):
                continue

            left_ctx = block_text[max(0, sym_idx - 48) : sym_idx]
            right_ctx = block_text[sym_idx + 1 : min(len(block_text), sym_idx + 49)]

            if _looks_like_bullet_or_abbrev(sym, left_ch, right_ch, left_ctx, right_ctx):
                continue
            if sym in _UNIT_HOMONYM_LETTERS and re.search(r"\d\s+$", left_ctx):
                continue
            if _is_inside_compact_math_tuple(left_ctx, right_ctx):
                continue

            is_op_adj = bool(
                _OPERATOR_LEFT_RE.search(left_ctx)
                or _OPERATOR_RIGHT_RE.match(right_ctx)
            )
            has_left_prose = bool(re.search(r"[A-Za-z]{2,}(?:[,;:\s]+)$", left_ctx))
            has_right_prose = bool(re.match(r"^(?:[.,;:\s]+[A-Za-z]{2,}|[.,;:]?$)", right_ctx))
            if not (has_left_prose or has_right_prose):
                continue

            # Resolve exact boundary occurrence index in `block_text`
            pat = boundary_regex_fn(sym)
            matched_occ_idx: int | None = None
            for occ_idx, m in enumerate(pat.finditer(block_text)):
                if m.start() == sym_idx and m.end() == sym_idx + 1:
                    matched_occ_idx = occ_idx
                    break
            if matched_occ_idx is None:
                continue

            seen_positions.add(sym_idx)
            excerpt_s = max(0, sym_idx - 20)
            excerpt_e = min(len(block_text), sym_idx + 21)
            excerpt = block_text[excerpt_s:excerpt_e]
            basis_tag = (
                "math_italic_operator_adjacent"
                if is_op_adj
                else "math_italic_standalone_prose_var"
            )
            candidates_out.append(
                UnmaskedMathVariableCandidate(
                    symbol=sym,
                    source_start=sym_idx,
                    source_end=sym_idx + 1,
                    context_excerpt=excerpt,
                    evidence_basis=basis_tag,
                )
            )
            if not is_op_adj:
                protected_occ_by_sym.setdefault(sym, set()).add(matched_occ_idx)

    spans_out = [
        InlineMathSpan(
            context_text=sym,
            target_text=sym,
            role="standalone_var",
            scale=1.0,
            dy_em=0.0,
            ambiguous=False,
            mask_occurrence_indices=sorted(occs),
        )
        for sym, occs in protected_occ_by_sym.items()
        if occs
    ]
    return spans_out, candidates_out


def collect_unmasked_math_candidates(
    source_text: str,
    orig_to_masked: dict[int, int] | None,
    candidates: list[UnmaskedMathVariableCandidate],
) -> list[UnmaskedMathVariableCandidate]:
    """Keep only candidates whose source character indices remained unmasked (`not {vN}`)."""
    if not candidates or orig_to_masked is None:
        return []
    remaining: list[UnmaskedMathVariableCandidate] = []
    for cand in candidates:
        pos = cand.source_start
        if (
            0 <= pos < len(source_text)
            and source_text[pos : cand.source_end] == cand.symbol
            and pos in orig_to_masked
        ):
            remaining.append(cand)
    return remaining
