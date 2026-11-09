"""Conservative, context-only detection of prose at layout boundaries."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    ContinuationCandidate,
)

_CLOSING_QUOTES_AND_BRACKETS = "\"'”’»›)]}"
_ABBREVIATION_END_RE = re.compile(
    r"(?:\b(?:et\s+al|e\.g|i\.e|fig|eq|dr|prof|no|vs|cf|approx)\.)$",
    re.IGNORECASE,
)
_CONTINUATION_START_RE = re.compile(
    r"^[a-z]"
)
_LIST_START_RE = re.compile(
    r"^(?:\(?\d{1,3}\)?[.)]|\(?[a-z]\)?[.)]|[ivxlcdm]+\.)\s+|^[•●▪◦‣–—-]\s+",
    re.IGNORECASE,
)
_OPEN_GRAMMAR_END_RE = re.compile(
    r"(?:\b(?:and|or|but|because|which|that|when|while|where|with|as|to|of|for|from|by|in|on|at|under|over|into|through|if|than|such|the|a|an|is|are|was|were|has|have|had|can|will|may|most|each|between|among|including|based|compared|relative|proportional|equal|defined|given)|[,;:]|[-\u2010\u2011])$",
    re.IGNORECASE,
)
_VARIABLE_START_RE = re.compile(
    r"^(?:[A-Z]{2,}(?:_[A-Za-z0-9]+)*\b|[A-Z](?:_[A-Za-z0-9]+|[′’']|[=<>≤≥]))"
)
_PLACEHOLDER_RE = re.compile(r"\{v\d+\}")


@dataclass(frozen=True)
class _CandidatePair:
    previous_block_id: str
    next_block_id: str
    relation: str
    confidence: str
    reasons: tuple[str, ...]
    use_as_context: bool


def _is_body_paragraph(block: Block) -> bool:
    return (
        block.kind == BlockKind.PARAGRAPH
        and block.semantic_category in {"body", "abstract"}
        and block.status != BlockStatus.PROTECTED_SKIP
        and block.status != BlockStatus.MANUALLY_EDITED
        and block.manual_translation_text is None
        and block.user_protection_override is None
    )


def _is_flow_content(block: Block) -> bool:
    return block.kind != BlockKind.HEADER_FOOTER


def _ordered(blocks: list[Block]) -> list[Block]:
    return sorted(
        blocks,
        key=lambda block: (
            block.reading_order,
            round(block.bbox[1], 1),
            round(block.bbox[0], 1),
        ),
    )


def _page_is_two_column(blocks: list[Block]) -> bool:
    columns = {block.column_index for block in blocks if _is_flow_content(block)}
    return 1 in columns and 2 in columns


def _has_full_width_barrier(blocks: list[Block]) -> bool:
    spanning = [
        block
        for block in blocks
        if block.column_index == 0 and _is_flow_content(block)
    ]
    column_content = [block for block in blocks if block.column_index in {1, 2}]

    for block in spanning:
        if block.semantic_category != "title":
            return True
        before = any(item.bbox[3] <= block.bbox[1] for item in column_content)
        after = any(item.bbox[1] >= block.bbox[3] for item in column_content)
        if before and after:
            return True
    return False


def _boundary_blocks(blocks: list[Block], column: int | None, first: bool) -> Block | None:
    content = [block for block in blocks if _is_flow_content(block)]
    if column is not None:
        content = [block for block in content if block.column_index == column]
    ordered = _ordered(content)
    if not ordered:
        return None
    return ordered[0] if first else ordered[-1]


def _ends_at_sentence_boundary(text: str) -> bool:
    ending = text.rstrip()
    while ending and ending[-1] in _CLOSING_QUOTES_AND_BRACKETS:
        ending = ending[:-1].rstrip()
    if not ending or ending[-1] not in ".?!":
        return False
    if ending.endswith(".") and _ABBREVIATION_END_RE.search(ending):
        return False
    return True


def _starts_with_continuation(text: str) -> bool:
    text = text.lstrip()
    return not _LIST_START_RE.match(text) and bool(_CONTINUATION_START_RE.match(text))


def _is_indented_paragraph_start(block: Block, peers: list[Block]) -> bool:
    peer_starts = [
        peer.bbox[0]
        for peer in peers
        if peer.id != block.id
        and peer.reading_order > block.reading_order
        and _is_body_paragraph(peer)
        and abs(peer.font_size_pt - block.font_size_pt) <= 1.0
    ]
    return bool(peer_starts) and block.bbox[0] - min(peer_starts) >= 8.0


def _classify_pair(
    previous: Block,
    following: Block,
    relation: str,
    compare_indentation: bool,
    following_column_blocks: list[Block],
) -> _CandidatePair:
    reasons = [f"adjacent:{relation}"]
    previous_text = previous.source_text.rstrip()
    following_text = following.source_text.lstrip()
    terminal = _ends_at_sentence_boundary(previous_text)
    starts_continuation = _starts_with_continuation(following_text)
    starts_with_variable = bool(_VARIABLE_START_RE.match(following_text))
    hyphenated = previous_text.endswith(("-", "\u2010", "\u2011")) and bool(
        re.match(r"^[a-z]", following_text)
    )
    open_grammar = hyphenated or bool(_OPEN_GRAMMAR_END_RE.search(previous_text))
    similar_font = abs(previous.font_size_pt - following.font_size_pt) <= 1.0
    indented_start = _is_indented_paragraph_start(following, following_column_blocks)
    aligned_indent = not indented_start and (
        not compare_indentation or abs(previous.bbox[0] - following.bbox[0]) <= 24.0
    )

    if not terminal:
        reasons.append("no_terminal_punctuation")
    else:
        reasons.append("sentence_terminator")
    if starts_continuation:
        reasons.append("lowercase_or_continuation_start")
    else:
        reasons.append("independent_or_unclear_start")
    if hyphenated:
        reasons.append("possible_line_end_hyphen")
    if open_grammar:
        reasons.append("open_grammatical_construction")
    if starts_with_variable:
        reasons.append("variable_or_acronym_start")
    if indented_start:
        reasons.append("indented_paragraph_start")
    if similar_font:
        reasons.append("similar_font_size")
    if compare_indentation and aligned_indent:
        reasons.append("aligned_indent")

    high_confidence = (
        (starts_continuation or starts_with_variable)
        and open_grammar
        and not terminal
        and similar_font
        and aligned_indent
    )
    return _CandidatePair(
        previous_block_id=previous.id,
        next_block_id=following.id,
        relation=relation,
        confidence="high" if high_confidence else "review_candidate",
        reasons=tuple(reasons),
        use_as_context=high_confidence,
    )


def _candidate_pairs(blocks: list[Block]) -> list[_CandidatePair]:
    pages: dict[int, list[Block]] = defaultdict(list)
    for block in blocks:
        pages[block.page_index].append(block)

    pairs: list[_CandidatePair] = []
    for page_index in sorted(pages):
        page_blocks = pages[page_index]
        if not _page_is_two_column(page_blocks) or _has_full_width_barrier(page_blocks):
            continue
        left_tail = _boundary_blocks(page_blocks, column=1, first=False)
        right_head = _boundary_blocks(page_blocks, column=2, first=True)
        if left_tail is not None and right_head is not None:
            if _is_body_paragraph(left_tail) and _is_body_paragraph(right_head):
                pairs.append(
                    _classify_pair(
                        left_tail,
                        right_head,
                        "same_page_cross_column",
                        compare_indentation=False,
                        following_column_blocks=[
                            block for block in page_blocks if block.column_index == 2
                        ],
                    )
                )

    for page_index in sorted(pages):
        next_index = page_index + 1
        if next_index not in pages:
            continue
        current = pages[page_index]
        following_page = pages[next_index]
        current_two_column = _page_is_two_column(current)
        next_two_column = _page_is_two_column(following_page)
        if current_two_column != next_two_column:
            continue

        if current_two_column:
            if _has_full_width_barrier(current) or _has_full_width_barrier(following_page):
                continue
            previous = _boundary_blocks(current, column=2, first=False)
            following = _boundary_blocks(following_page, column=1, first=True)
            relation = "adjacent_pages_two_column"
            compare_indentation = False
        else:
            previous = _boundary_blocks(current, column=None, first=False)
            following = _boundary_blocks(following_page, column=None, first=True)
            relation = "adjacent_pages_one_column"
            compare_indentation = True

        if (
            previous is not None
            and following is not None
            and _is_body_paragraph(previous)
            and _is_body_paragraph(following)
        ):
            pairs.append(
                _classify_pair(
                    previous,
                    following,
                    relation,
                    compare_indentation=compare_indentation,
                    following_column_blocks=[
                        block
                        for block in following_page
                        if block.column_index == following.column_index
                    ],
                )
            )
    return pairs


def analyze_continuation_candidates(blocks: list[Block]) -> list[_CandidatePair]:
    """Return layout-boundary candidates; only high-confidence pairs may inform context."""
    return _candidate_pairs(blocks)


def attach_continuation_candidates(blocks: list[Block]) -> None:
    by_id = {block.id: block for block in blocks}
    for block in blocks:
        block.continuation_candidates = []

    for pair in _candidate_pairs(blocks):
        previous = by_id[pair.previous_block_id]
        following = by_id[pair.next_block_id]
        use_as_context = pair.use_as_context
        common = {
            "relation": pair.relation,
            "confidence": pair.confidence,
            "reasons": list(pair.reasons),
            "use_as_context": use_as_context,
        }
        previous.continuation_candidates.append(
            ContinuationCandidate(
                counterpart_block_id=following.id,
                direction="next",
                **common,
            )
        )
        following.continuation_candidates.append(
            ContinuationCandidate(
                counterpart_block_id=previous.id,
                direction="previous",
                **common,
            )
        )


def build_continuation_contexts(blocks: list[Block]) -> dict[str, str]:
    """Build bounded, masked neighboring context without joining physical blocks."""
    by_id = {block.id: block for block in blocks}
    context_parts: dict[str, list[str]] = defaultdict(list)

    for block in blocks:
        for candidate in block.continuation_candidates:
            if not candidate.use_as_context or candidate.confidence != "high":
                continue
            counterpart = by_id.get(candidate.counterpart_block_id)
            if counterpart is None:
                continue
            text = counterpart.masked_source_text or counterpart.source_text
            text = _PLACEHOLDER_RE.sub("[protected item]", text).strip()
            if len(text) > 320:
                text = (
                    "…" + text[-320:]
                    if candidate.direction == "previous"
                    else text[:320] + "…"
                )
            label = "preceding fragment" if candidate.direction == "previous" else "following fragment"
            context_parts[block.id].append(f"{label}: {text}")

    return {
        block_id: (
            "Adjacent-block context only; translate only the current block. "
            + " ".join(parts)
        )
        for block_id, parts in context_parts.items()
    }
