from __future__ import annotations

from backend.domain.models import IssueSeverity, ValidationIssue
from backend.jobs.manager import _deduplicate_validation_issues


def _issue(
    issue_id: str,
    *,
    code: str = "VISUAL_PRESERVATION_DIFF_DETECTED",
    page_index: int = 0,
    bbox: list[float] | None = None,
    details: dict | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        id=issue_id,
        job_id="job-synthetic",
        document_id="doc-synthetic",
        page_index=page_index,
        block_id="block-synthetic",
        bbox=bbox or [10.0, 20.0, 30.0, 40.0],
        code=code,
        severity=IssueSeverity.WARNING,
        message="Synthetic warning",
        details=details or {},
        fallback_applied=False,
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
