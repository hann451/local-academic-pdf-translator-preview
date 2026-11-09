"""Visual and Formula Preservation Round-Trip Tests (Phase 1 Core Acceptance Criteria).

Verifies:
1. Non-translation round-trip reconstruction preserves original PDF hash (100% untouched)
   and achieves 0.0 pixel difference across all pages and formula/figure regions.
2. Translated PDF output preserves isolated equations (integrals, sums, fractions, matrices,
   equation numbers) and vector figures with 0.0 pixel corruption inside protected boxes.
3.Rotated / CropBox pages are handled without corrupting the original file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import AppConfig
from backend.domain.models import BlockKind, BlockStatus, JobState, TranslationJobConfig
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged

pytestmark = pytest.mark.visual


def test_phase1_non_translation_roundtrip_preserves_all_formulas_and_figures(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Phase 1 core milestone:
    Non-translation parse & round-trip export on 1-column math and 2-column figure/table PDFs.
    """
    mgr = JobManager(config=isolated_config)

    for key in ("single_col_math", "two_col_figures_tables_2p", "rotated_and_cropbox"):
        pdf_path = generated_pdfs[key]
        initial_sha = compute_sha256(pdf_path)

        doc = mgr.register_document(pdf_path)
        job = mgr.create_job(
            doc.id,
            TranslationJobConfig(
                only_roundtrip_preservation=True,
                output_mono_pdf=True,
                output_dual_pdf=True,
            ),
        )
        finished = mgr.run_job(job.id)
        assert finished.state == JobState.COMPLETED
        assert finished.original_hash_verified is True
        assert verify_original_unchanged(pdf_path, initial_sha)

        artifacts = mgr.db.list_export_artifacts(job.id)
        roundtrip_arts = [a for a in artifacts if a.artifact_type == "roundtrip_pdf"]
        assert len(roundtrip_arts) == 1

        blocks = mgr.db.list_blocks(doc.id)
        reports = mgr.pdf_adapter.verify_preservation(
            document=doc,
            output_pdf_path=Path(roundtrip_arts[0].file_path),
            blocks=blocks,
            page_indices=list(range(doc.page_count)),
            is_roundtrip_only=True,
        )
        for rep in reports:
            assert rep.all_protected_regions_preserved is True
            assert rep.non_translated_background_diff_ratio == 0.0
            assert rep.semantic_accuracy_guaranteed is False


def test_translated_pdf_visual_preservation_of_isolated_equations_and_figures(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Verify that after translating prose paragraphs into Japanese, all isolated
    equations (integral, sum, matrix) and vector figures remain visually identical
    to the original PDF (0.0 differing pixel ratio in protected boxes).
    """
    mgr = JobManager(config=isolated_config)
    pdf_path = generated_pdfs["single_col_math"]
    orig_sha = compute_sha256(pdf_path)

    doc = mgr.register_document(pdf_path)
    job = mgr.create_job(
        doc.id,
        TranslationJobConfig(
            use_mock_translator=True,
            output_mono_pdf=True,
            output_dual_pdf=True,
        ),
    )
    finished = mgr.run_job(job.id)
    assert finished.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    assert verify_original_unchanged(pdf_path, orig_sha)

    blocks = mgr.db.list_blocks(doc.id)
    formula_blocks = [b for b in blocks if b.kind == BlockKind.ISOLATED_FORMULA]
    figure_blocks = [b for b in blocks if b.kind == BlockKind.FIGURE]
    assert len(formula_blocks) >= 2
    assert len(figure_blocks) >= 1
    for pb in formula_blocks + figure_blocks:
        assert pb.status == BlockStatus.PROTECTED_SKIP

    artifacts = mgr.db.list_export_artifacts(job.id)
    mono_art = next(a for a in artifacts if a.artifact_type == "mono_ja_pdf")

    reports = mgr.pdf_adapter.verify_preservation(
        document=doc,
        output_pdf_path=Path(mono_art.file_path),
        blocks=blocks,
        page_indices=[0],
        is_roundtrip_only=False,
    )
    assert len(reports) == 1
    rep0 = reports[0]
    assert rep0.all_protected_regions_preserved is True
    for r_res in rep0.protected_region_results:
        assert r_res.passed is True
        assert r_res.differing_pixel_ratio == 0.0


def test_spatial_english_fraction_is_preserved_and_surrounding_prose_translates(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Protect a stacked English fraction without suppressing adjacent body translation."""
    mgr = JobManager(config=isolated_config)
    pdf_path = generated_pdfs["english_fraction_equation"]
    original_hash = compute_sha256(pdf_path)

    doc = mgr.register_document(pdf_path)
    job = mgr.create_job(
        doc.id,
        TranslationJobConfig(
            use_mock_translator=True,
            output_mono_pdf=True,
            output_dual_pdf=False,
        ),
    )
    finished = mgr.run_job(job.id)
    assert finished.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    assert verify_original_unchanged(pdf_path, original_hash)

    blocks = mgr.db.list_blocks(doc.id)
    fraction_blocks = [
        block
        for block in blocks
        if block.protection_rule_id == "RULE_SPATIAL_FRACTION_EQUATION"
    ]
    assert len(fraction_blocks) == 1
    fraction = fraction_blocks[0]
    assert fraction.kind == BlockKind.ISOLATED_FORMULA
    assert fraction.status == BlockStatus.PROTECTED_SKIP
    assert all(term in fraction.source_text for term in (
        "Detection Count Ratio",
        "Detected Intrusions",
        "Actual Intrusions",
        "(7)",
    ))
    assert fraction.bbox[0] <= 200
    assert fraction.bbox[2] >= 310

    surrounding_prose = [
        block
        for block in blocks
        if block.semantic_category == "body"
        and (
            "energy function converges" in block.source_text.lower()
            or "loss function converges" in block.source_text.lower()
        )
    ]
    assert len(surrounding_prose) == 2
    assert all(block.status == BlockStatus.TRANSLATED for block in surrounding_prose)
    assert all(block.translation_text != block.source_text for block in surrounding_prose)

    artifacts = mgr.db.list_export_artifacts(job.id)
    mono_artifact = next(
        artifact for artifact in artifacts if artifact.artifact_type == "mono_ja_pdf"
    )
    reports = mgr.pdf_adapter.verify_preservation(
        document=doc,
        output_pdf_path=Path(mono_artifact.file_path),
        blocks=blocks,
        page_indices=[0],
        is_roundtrip_only=False,
    )
    protected_fraction = [
        result
        for report in reports
        for result in report.protected_region_results
        if result.region_id == fraction.id
    ]
    assert len(protected_fraction) == 1
    assert protected_fraction[0].passed is True
    assert protected_fraction[0].differing_pixel_ratio == 0.0
