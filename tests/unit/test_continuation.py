from __future__ import annotations

import pytest

from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    ProtectedItem,
    ProtectedItemKind,
)
from backend.translation.continuation import (
    analyze_continuation_candidates,
    attach_continuation_candidates,
    build_continuation_contexts,
)


def _block(
    block_id: str,
    *,
    page: int,
    column: int,
    order: int,
    text: str,
    kind: BlockKind = BlockKind.PARAGRAPH,
    category: str = "body",
    status: BlockStatus = BlockStatus.PENDING,
    x: float = 50.0,
    font_size: float = 10.0,
) -> Block:
    return Block(
        id=block_id,
        page_index=page,
        bbox=[x, 100.0 + order * 10, x + 220.0, 110.0 + order * 10],
        column_index=column,
        reading_order=order,
        source_text=text,
        masked_source_text=text,
        kind=kind,
        semantic_category=category,
        status=status,
        font_size_pt=font_size,
    )


def test_context_uses_neutral_formula_marker_and_preserves_per_block_placeholders() -> None:
    previous = _block(
        "p1-left",
        page=0,
        column=1,
        order=1,
        text="The estimate {v1} remains stable when",
    )
    previous.protected_items = [
        ProtectedItem(
            placeholder="{v1}",
            kind=ProtectedItemKind.INLINE_FORMULA,
            original_text="E_n = sum_i x_i / n",
        )
    ]
    following = _block(
        "p1-right",
        page=0,
        column=2,
        order=2,
        text="the sample size increases.",
    )
    attach_continuation_candidates([previous, following])

    contexts = build_continuation_contexts([previous, following])

    assert previous.continuation_candidates[0].confidence == "high"
    assert previous.continuation_candidates[0].use_as_context is True
    assert "{v1}" not in contexts[following.id]
    assert "sum_i" not in contexts[following.id]
    assert "[protected item]" in contexts[following.id]
    assert "preceding fragment" in contexts[following.id]
    assert previous.id != following.id


def test_punctuation_and_abbreviations_are_combined_with_other_evidence() -> None:
    abbreviation = _block(
        "abbrev-left",
        page=0,
        column=1,
        order=1,
        text="The estimate by Smith et al.",
    )
    continuation = _block(
        "abbrev-right",
        page=0,
        column=2,
        order=2,
        text="and colleagues remains consistent.",
    )
    candidates = analyze_continuation_candidates([abbreviation, continuation])
    assert candidates[0].confidence == "review_candidate"
    assert candidates[0].use_as_context is False

    sentence = _block(
        "sentence-left",
        page=0,
        column=1,
        order=1,
        text="The estimated rate is 0.5.",
    )
    independent = _block(
        "sentence-right",
        page=0,
        column=2,
        order=2,
        text="A separate analysis follows.",
        x=320.0,
    )
    candidates = analyze_continuation_candidates([sentence, independent])
    assert candidates[0].confidence == "review_candidate"
    assert candidates[0].use_as_context is False
    assert "sentence_terminator" in candidates[0].reasons

    decimal = _block(
        "decimal-left",
        page=0,
        column=1,
        order=1,
        text="The threshold was 0.5",
    )
    after_decimal = _block(
        "decimal-right",
        page=0,
        column=2,
        order=2,
        text="and the result remained stable.",
        x=320.0,
    )
    candidates = analyze_continuation_candidates([decimal, after_decimal])
    assert candidates[0].confidence == "review_candidate"
    assert "no_terminal_punctuation" in candidates[0].reasons
    assert "open_grammatical_construction" not in candidates[0].reasons

    quoted_sentence = _block(
        "quote-left",
        page=0,
        column=1,
        order=1,
        text="The result was “stable.”",
    )
    after_quote = _block(
        "quote-right",
        page=0,
        column=2,
        order=2,
        text="and a separate experiment follows.",
        x=320.0,
    )
    candidates = analyze_continuation_candidates([quoted_sentence, after_quote])
    assert candidates[0].confidence == "review_candidate"
    assert "sentence_terminator" in candidates[0].reasons


def test_hyphenated_boundary_is_context_only_and_does_not_mutate_source() -> None:
    previous = _block(
        "hyphen-left",
        page=0,
        column=1,
        order=1,
        text="The inter-",
    )
    following = _block(
        "hyphen-right",
        page=0,
        column=2,
        order=2,
        text="national comparison is complete.",
        x=320.0,
    )
    original_texts = (previous.source_text, following.source_text)

    candidates = analyze_continuation_candidates([previous, following])

    assert candidates[0].confidence == "high"
    assert "possible_line_end_hyphen" in candidates[0].reasons
    assert (previous.source_text, following.source_text) == original_texts


def test_unpunctuated_new_paragraph_and_enumerated_list_are_not_high_confidence() -> None:
    previous = _block(
        "paragraph-end",
        page=0,
        column=1,
        order=1,
        text="The first group contains the observations",
    )
    next_paragraph = _block(
        "paragraph-start",
        page=0,
        column=2,
        order=2,
        text="and the second group contains controls.",
        x=320.0,
    )
    candidate = analyze_continuation_candidates([previous, next_paragraph])[0]
    assert candidate.confidence == "review_candidate"
    assert candidate.use_as_context is False
    assert "open_grammatical_construction" not in candidate.reasons

    enumerated = _block(
        "list-start",
        page=0,
        column=2,
        order=2,
        text="(b) the second experiment.",
        x=320.0,
    )
    candidate = analyze_continuation_candidates([previous, enumerated])[0]
    assert candidate.confidence == "review_candidate"
    assert candidate.use_as_context is False


def test_misclassified_small_font_footnote_is_not_high_confidence() -> None:
    previous = _block(
        "body-end",
        page=0,
        column=1,
        order=1,
        text="The method depends on",
    )
    footnote = _block(
        "small-footnote",
        page=0,
        column=2,
        order=2,
        text="the samples collected in the auxiliary trial.",
        x=320.0,
        font_size=8.0,
    )
    candidate = analyze_continuation_candidates([previous, footnote])[0]
    assert candidate.confidence == "review_candidate"
    assert candidate.use_as_context is False
    assert "similar_font_size" not in candidate.reasons


def test_indented_new_column_paragraph_is_not_high_confidence() -> None:
    previous = _block(
        "left-end",
        page=0,
        column=1,
        order=1,
        text="The model is conditioned on",
    )
    indented = _block(
        "right-indented",
        page=0,
        column=2,
        order=2,
        text="the independent paragraph starts here.",
        x=340.0,
    )
    following_peer = _block(
        "right-peer",
        page=0,
        column=2,
        order=3,
        text="A later paragraph uses the column margin.",
        x=320.0,
    )
    candidate = analyze_continuation_candidates(
        [previous, indented, following_peer]
    )[0]
    assert candidate.confidence == "review_candidate"
    assert candidate.use_as_context is False
    assert "indented_paragraph_start" in candidate.reasons


@pytest.mark.parametrize(
    "following_text",
    [
        "CSI, RSSI, and X_i are measured.",
        "RSSI is the response variable.",
        "X_i = X_{i+1} under the constraint.",
    ],
)
def test_uppercase_acronym_or_variable_can_continue_an_open_construction(
    following_text: str,
) -> None:
    previous = _block(
        "variable-context",
        page=0,
        column=1,
        order=1,
        text="The measurement is conditioned on",
    )
    following = _block(
        "variable-start",
        page=0,
        column=2,
        order=2,
        text=following_text,
        x=320.0,
    )
    candidate = analyze_continuation_candidates([previous, following])[0]
    assert candidate.confidence == "high"
    assert candidate.use_as_context is True
    assert "variable_or_acronym_start" in candidate.reasons


@pytest.mark.parametrize(
    ("kind", "category", "status"),
    [
        (BlockKind.FIGURE, "figure", BlockStatus.PROTECTED_SKIP),
        (BlockKind.ISOLATED_FORMULA, "formula", BlockStatus.PROTECTED_SKIP),
    ],
)
def test_full_width_figure_or_formula_excludes_page_continuation_candidates(
    kind: BlockKind,
    category: str,
    status: BlockStatus,
) -> None:
    left = _block(
        "left-end",
        page=0,
        column=1,
        order=1,
        text="The result depends on",
    )
    right = _block(
        "right-start",
        page=0,
        column=2,
        order=2,
        text="the next condition.",
        x=320.0,
    )
    spanning = _block(
        "full-width-object",
        page=0,
        column=0,
        order=3,
        text="[protected full-width object]",
        kind=kind,
        category=category,
        status=status,
        x=20.0,
    )
    spanning.bbox = [20.0, 250.0, 590.0, 300.0]

    assert analyze_continuation_candidates([left, right, spanning]) == []


def test_heading_protected_block_full_width_content_and_page_gap_block_links() -> None:
    left = _block("left", page=0, column=1, order=1, text="The result continues when")
    right = _block("right", page=0, column=2, order=2, text="the next condition holds.")
    caption = _block(
        "caption",
        page=0,
        column=1,
        order=3,
        text="Figure 1: summary",
        kind=BlockKind.CAPTION,
        category="caption",
    )
    assert analyze_continuation_candidates([left, right, caption]) == []

    protected = _block(
        "protected",
        page=0,
        column=1,
        order=1,
        text="The result continues when",
        kind=BlockKind.ISOLATED_FORMULA,
        category="formula",
        status=BlockStatus.PROTECTED_SKIP,
    )
    assert analyze_continuation_candidates([protected, right]) == []

    page_zero = _block("page-zero", page=0, column=0, order=0, text="The result continues when")
    page_two = _block("page-two", page=2, column=0, order=0, text="the next condition holds.")
    assert analyze_continuation_candidates([page_zero, page_two]) == []

    spanning_heading = _block(
        "spanning-heading",
        page=0,
        column=0,
        order=0,
        text="A new section",
        kind=BlockKind.HEADING,
        category="section_heading",
    )
    assert analyze_continuation_candidates([left, right, spanning_heading]) == []

    full_width_title = _block(
        "full-width-title",
        page=0,
        column=0,
        order=15,
        text="A full-width section title",
        kind=BlockKind.TITLE,
        category="title",
    )
    full_width_title.bbox = [20.0, 250.0, 590.0, 270.0]
    below_title = _block(
        "below-title",
        page=0,
        column=1,
        order=20,
        text="A later paragraph follows.",
    )
    assert analyze_continuation_candidates(
        [left, right, full_width_title, below_title]
    ) == []


@pytest.mark.parametrize(
    ("kind", "category"),
    [
        (BlockKind.HEADING, "section_heading"),
        (BlockKind.CAPTION, "caption"),
        (BlockKind.REFERENCE, "reference"),
        (BlockKind.PARAGRAPH, "list"),
    ],
)
def test_non_body_boundary_blocks_do_not_become_continuation_targets(
    kind: BlockKind,
    category: str,
) -> None:
    left = _block("left", page=0, column=1, order=1, text="The result continues when")
    right = _block(
        "right",
        page=0,
        column=2,
        order=2,
        text="a non-body block starts here.",
        kind=kind,
        category=category,
        x=320.0,
    )
    assert analyze_continuation_candidates([left, right]) == []


def test_manual_edits_are_not_used_as_automatic_context() -> None:
    edited = _block(
        "edited-left",
        page=0,
        column=1,
        order=1,
        text="The result continues when",
        status=BlockStatus.MANUALLY_EDITED,
    )
    following = _block(
        "right",
        page=0,
        column=2,
        order=2,
        text="the next condition holds.",
        x=320.0,
    )
    assert analyze_continuation_candidates([edited, following]) == []
