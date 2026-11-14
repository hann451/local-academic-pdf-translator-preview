from __future__ import annotations

from backend.domain.models import Block, BlockKind, BlockStatus, IssueSeverity, ValidationIssue
from backend.jobs.manager import (
    _deduplicate_validation_issues,
    _issue_check_scope,
    _reconcile_retypeset_issues_and_warnings,
)


def _issue(
    issue_id: str,
    *,
    code: str = "VISUAL_PRESERVATION_DIFF_DETECTED",
    page_index: int = 0,
    block_id: str | None = "block-synthetic",
    bbox: list[float] | None = None,
    message: str = "Synthetic warning",
    details: dict | None = None,
    fallback_applied: bool = False,
) -> ValidationIssue:
    return ValidationIssue(
        id=issue_id,
        job_id="job-synthetic",
        document_id="doc-synthetic",
        page_index=page_index,
        block_id=block_id,
        bbox=[10.0, 20.0, 30.0, 40.0] if bbox is None else bbox,
        code=code,
        severity=IssueSeverity.WARNING,
        message=message,
        details=details or {},
        fallback_applied=fallback_applied,
    )


def _block(
    block_id: str,
    *,
    page_index: int = 0,
    bbox: list[float] | None = None,
    status: BlockStatus = BlockStatus.FALLBACK_ORIGINAL,
    warnings: list[str] | None = None,
) -> Block:
    return Block(
        id=block_id,
        document_id="doc-synthetic",
        page_index=page_index,
        block_index=0,
        bbox=bbox or [50.0, 100.0, 250.0, 140.0],
        kind=BlockKind.PARAGRAPH,
        source_text="Synthetic source",
        status=status,
        warnings=list(warnings or []),
    )


def test_identical_resume_warnings_are_deduplicated_but_distinct_findings_remain() -> None:
    first = _issue("issue-1")
    duplicate = _issue("issue-2")
    different_box = _issue("issue-3", bbox=[10.0, 20.0, 31.0, 40.0])
    different_code = _issue("issue-4", code="NUMERIC_VALUE_MISMATCH")

    unique = _deduplicate_validation_issues(
        [first, duplicate, different_box, different_code]
    )

    assert [issue.id for issue in unique] == ["issue-1", "issue-3", "issue-4"]


def test_issue_check_scope_classifies_by_code_only_not_message() -> None:
    assert _issue_check_scope("EMPTY_OUTPUT") == "translation_validation"
    assert _issue_check_scope("NUMERIC_VALUE_MISMATCH") == "translation_validation"
    assert _issue_check_scope("TYPESETTING_OVERFLOW_FALLBACK") == "typesetting"
    assert _issue_check_scope("PROTECTED_REGION_OVERLAP_FALLBACK") == "typesetting"
    assert _issue_check_scope("UNTRANSLATED_AFTER_PROTECTION_CHANGE") == "protection_change"
    assert _issue_check_scope("VISUAL_PRESERVATION_DIFF_DETECTED") == "visual_preservation"
    # Even if message mentions typesetting/validation wording, an unknown code remains "other"
    other = _issue(
        "iss-other",
        code="CUSTOM_DOCUMENT_NOTICE",
        message="最小フォントサイズ（8.0pt）でも枠内に収まらない TYPESETTING_OVERFLOW_FALLBACK",
    )
    assert _issue_check_scope(other.code) == "other"


def test_reconcile_preserves_page_doc_and_unevaluated_visual_without_leaking_to_blocks() -> None:
    blk0 = _block("b0", page_index=0, bbox=[50.0, 100.0, 200.0, 140.0], status=BlockStatus.TRANSLATED)
    blk_uneval = _block("b1", page_index=1, bbox=[50.0, 100.0, 200.0, 140.0], status=BlockStatus.TRANSLATED)

    existing = [
        _issue("iss-doc-null", code="CUSTOM_DOC_NOTICE", page_index=0, block_id=None, bbox=[0.0, 0.0, 10.0, 10.0]),
        _issue("iss-vis-p0-old", code="VISUAL_PRESERVATION_DIFF_DETECTED", page_index=0, block_id="b0", bbox=blk0.bbox),
        _issue("iss-vis-p1-keep", code="VISUAL_PRESERVATION_DIFF_DETECTED", page_index=1, block_id="b1", bbox=blk_uneval.bbox),
        _issue("iss-p1-out-of-range", code="TYPESETTING_OVERFLOW_FALLBACK", page_index=1, block_id="b1", bbox=blk_uneval.bbox, fallback_applied=True),
        _issue("iss-b0-custom", code="PREFLIGHT_FONT_SUBSET_NOTE", page_index=0, block_id="b0", bbox=blk0.bbox, message="UNTRANSLATED_AFTER_PROTECTION_CHANGE fake message"),
    ]
    # Page 0 was visually evaluated and passed cleanly (0 new visual issues), while page 0 ran typesetting+validation on b0
    reconciled = _reconcile_retypeset_issues_and_warnings(
        existing_issues=existing,
        new_issues=[],
        working_blocks=[blk0],  # page 1 excluded via page_range_spec
        evaluated_page_indices={0},
        reevaluated_scopes_by_block={"b0": {"translation_validation", "typesetting", "protection_change"}},
        evaluated_visual_pages={0},
        orig_warnings_by_block={"b0": ["PREFLIGHT_FONT_SUBSET_NOTE"]},
    )

    assert [iss.id for iss in reconciled] == [
        "iss-b0-custom",
        "iss-doc-null",
        "iss-p1-out-of-range",
        "iss-vis-p1-keep",
    ]
    # VISUAL_PRESERVATION_DIFF_DETECTED never enters block.warnings; non-evaluated "other" custom notice stays
    assert blk0.warnings == ["PREFLIGHT_FONT_SUBSET_NOTE"]

    # Next: even if page 0 emits a new VISUAL_PRESERVATION_DIFF_DETECTED with block_id=b0, blk0.warnings excludes it
    new_vis = _issue("iss-vis-p0-new", code="VISUAL_PRESERVATION_DIFF_DETECTED", page_index=0, block_id="b0", bbox=blk0.bbox)
    reconciled2 = _reconcile_retypeset_issues_and_warnings(
        existing_issues=reconciled,
        new_issues=[new_vis],
        working_blocks=[blk0],
        evaluated_page_indices={0},
        reevaluated_scopes_by_block={"b0": {"translation_validation", "typesetting", "protection_change"}},
        evaluated_visual_pages={0},
        orig_warnings_by_block={"b0": blk0.warnings},
    )
    assert "iss-vis-p0-new" in [i.id for i in reconciled2]
    assert blk0.warnings == ["PREFLIGHT_FONT_SUBSET_NOTE"]


def test_reconcile_guards_shifted_orphaned_and_protected_skip_blocks() -> None:
    b_shifted = _block("doc:page-1:block-1", page_index=0, bbox=[50.0, 100.0, 200.0, 140.0], status=BlockStatus.TRANSLATED)
    b_prot = _block("doc:page-1:block-2", page_index=0, bbox=[50.0, 200.0, 200.0, 240.0], status=BlockStatus.PROTECTED_SKIP)
    b_fb = _block("doc:page-1:block-3", page_index=0, bbox=[50.0, 300.0, 200.0, 340.0], status=BlockStatus.FALLBACK_ORIGINAL)

    existing = [
        # Same block_id after reparse, completely shifted bbox -> IoU < 0.45 -> must NOT leak onto b_shifted
        _issue("iss-shifted", code="TYPESETTING_OVERFLOW_FALLBACK", page_index=0, block_id=b_shifted.id, bbox=[400.0, 600.0, 520.0, 650.0], fallback_applied=True),
        # Removed block on evaluated page -> must NOT survive as orphan
        _issue("iss-orphan", code="UNTRANSLATED_SOURCE_RETURNED", page_index=0, block_id="doc:page-1:block-99", bbox=[10.0, 10.0, 40.0, 30.0], fallback_applied=True),
        # Protected block had old overflow -> cleared because translation/typesetting/protection scopes are cleared on PROTECTED_SKIP
        _issue("iss-prot-old-of", code="TYPESETTING_OVERFLOW_FALLBACK", page_index=0, block_id=b_prot.id, bbox=b_prot.bbox, fallback_applied=True),
        # Untouched FALLBACK_ORIGINAL block keeps both existing issues cleanly
        _issue("iss-fb-a", code="UNTRANSLATED_SOURCE_RETURNED", page_index=0, block_id=b_fb.id, bbox=b_fb.bbox, fallback_applied=True),
    ]

    reconciled = _reconcile_retypeset_issues_and_warnings(
        existing_issues=existing,
        new_issues=[],
        working_blocks=[b_shifted, b_prot, b_fb],
        evaluated_page_indices={0},
        reevaluated_scopes_by_block={
            b_shifted.id: {"translation_validation", "typesetting"},
            b_prot.id: {"translation_validation", "typesetting", "protection_change"},
        },
        evaluated_visual_pages={0},
        orig_warnings_by_block={
            b_shifted.id: ["TYPESETTING_OVERFLOW_FALLBACK"],
            b_prot.id: ["TYPESETTING_OVERFLOW_FALLBACK"],
            b_fb.id: ["UNTRANSLATED_SOURCE_RETURNED"],
        },
    )

    assert [i.id for i in reconciled] == ["iss-fb-a"]
    assert b_shifted.warnings == []
    assert b_prot.warnings == []
    assert b_fb.warnings == ["UNTRANSLATED_SOURCE_RETURNED"]
