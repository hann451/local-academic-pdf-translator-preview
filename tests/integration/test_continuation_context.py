from __future__ import annotations

from pathlib import Path

from backend.config import AppConfig
from backend.domain.models import BlockKind, JobState, TranslationJobConfig
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged
from backend.translation.adapters.mock_adapter import MockPlamoAdapter


class _RecordingMockAdapter(MockPlamoAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[tuple[str, str | None]] = []

    def translate_segment(self, source_text: str, **kwargs):
        self.contexts.append((source_text, kwargs.get("context")))
        return super().translate_segment(source_text=source_text, **kwargs)


def test_synthetic_continuation_boundaries_are_recorded_without_merging_blocks(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    manager = JobManager(config=isolated_config)
    pdf_path = generated_pdfs["continuation_edges"]
    original_hash = compute_sha256(pdf_path)
    document = manager.register_document(pdf_path)
    blocks = manager.parse_document_blocks(document.id)

    def find_block(marker: str):
        return next(block for block in blocks if marker in block.source_text)

    left_end = find_block("The estimator")
    right_start = find_block("the sample size")
    right_end = find_block("The resulting bound")
    next_page_left = find_block("and converges")
    one_column_end = find_block("The single-column estimate")
    one_column_next = find_block("the noise variance")

    assert left_end.page_index == right_start.page_index == 0
    assert left_end.column_index == 1
    assert right_start.column_index == 2
    assert right_end.page_index == 0
    assert next_page_left.page_index == 1
    assert one_column_end.page_index == 2
    assert one_column_next.page_index == 3

    assert any(
        candidate.counterpart_block_id == right_start.id
        and candidate.confidence == "high"
        for candidate in left_end.continuation_candidates
    )
    assert any(
        candidate.counterpart_block_id == next_page_left.id
        and candidate.confidence == "high"
        for candidate in right_end.continuation_candidates
    )
    assert any(
        candidate.counterpart_block_id == one_column_next.id
        and candidate.confidence == "high"
        for candidate in one_column_end.continuation_candidates
    )
    page_boundary_layout_change = next(
        block for block in blocks if "This independent paragraph" in block.source_text
    )
    one_column_first = next(
        block for block in blocks if "A one-column page begins" in block.source_text
    )
    assert not any(
        candidate.counterpart_block_id == one_column_first.id
        for candidate in page_boundary_layout_change.continuation_candidates
    )

    assert left_end.id != right_start.id
    assert left_end.bbox != right_start.bbox
    assert left_end.bbox[2] <= right_start.bbox[0]
    assert left_end.masked_source_text != left_end.source_text
    assert any(item.placeholder == "{v1}" for item in left_end.protected_items)
    assert any(item.kind.value == "equation_number" for item in right_start.protected_items)
    assert right_start.protected_items[0].placeholder == "{v1}"
    assert all(block.kind != BlockKind.ISOLATED_FORMULA for block in (left_end, right_start))
    assert verify_original_unchanged(pdf_path, original_hash)


def test_high_confidence_context_is_sent_to_mock_without_combining_or_redrawing_blocks(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    adapter = _RecordingMockAdapter()
    manager = JobManager(config=isolated_config, translation_adapter=adapter)
    document = manager.register_document(generated_pdfs["continuation_edges"])
    job = manager.create_job(
        document.id,
        TranslationJobConfig(use_mock_translator=True, output_dual_pdf=False),
    )

    completed = manager.run_job(job.id)

    assert completed.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    left_source, left_context = next(
        (source, context)
        for source, context in adapter.contexts
        if "The estimator" in source
    )
    right_source, right_context = next(
        (source, context)
        for source, context in adapter.contexts
        if "the sample size" in source
    )
    assert "{v1}" in left_source
    assert "following fragment" in (left_context or "")
    assert "preceding fragment" in (right_context or "")
    assert "sum_i" not in (right_context or "")
    assert "[protected item]" in (right_context or "")
    assert verify_original_unchanged(
        generated_pdfs["continuation_edges"],
        document.sha256_hash,
    )


def test_lower_confidence_candidate_is_recorded_but_not_approved_as_context() -> None:
    from backend.domain.models import Block, BlockStatus
    from backend.translation.continuation import analyze_continuation_candidates

    first = Block(
        id="synthetic:p1:left",
        page_index=0,
        bbox=[40, 650, 270, 680],
        column_index=1,
        reading_order=1,
        source_text="The measured value is 0.5.",
        masked_source_text="The measured value is 0.5.",
        semantic_category="body",
        status=BlockStatus.PENDING,
    )
    second = Block(
        id="synthetic:p1:right",
        page_index=0,
        bbox=[320, 100, 550, 130],
        column_index=2,
        reading_order=2,
        source_text="A separate result follows.",
        masked_source_text="A separate result follows.",
        semantic_category="body",
        status=BlockStatus.PENDING,
    )

    candidates = analyze_continuation_candidates([first, second])

    assert len(candidates) == 1
    assert candidates[0].confidence == "review_candidate"
    assert candidates[0].use_as_context is False
    assert candidates[0].reasons
    assert "independent_or_unclear_start" in candidates[0].reasons
