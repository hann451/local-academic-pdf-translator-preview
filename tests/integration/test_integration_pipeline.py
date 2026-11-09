"""Integration tests covering Section 11, 14, 15, 16, 18 workflows."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import (
    BlockStatus,
    Glossary,
    GlossaryEntry,
    JobState,
    ProtectionMode,
    ProtectionRegion,
    TranslationJobConfig,
)
from backend.jobs.manager import JobManager, _deduplicate_validation_issues
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged
from backend.translation.adapters.base import SecurityPolicyViolationError
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter
from backend.translation.adapters.mock_adapter import MockPlamoAdapter

pytestmark = pytest.mark.integration


_PROCESSED_STATUSES = {
    BlockStatus.PROTECTED_SKIP,
    BlockStatus.TRANSLATED,
    BlockStatus.MANUALLY_EDITED,
    BlockStatus.FALLBACK_ORIGINAL,
}


def _assert_job_accounting(manager: JobManager, job) -> None:
    blocks = manager.db.list_blocks(job.document_id)
    expected_processed = sum(block.status in _PROCESSED_STATUSES for block in blocks)
    issues = manager.db.list_validation_issues(job.id)
    assert job.processed_blocks == expected_processed
    assert job.processed_blocks <= job.total_blocks
    assert job.warning_count == len(_deduplicate_validation_issues(issues))


def test_end_to_end_mock_translation_preserves_original_and_marks_mock(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Verify full 13-stage pipeline on Japanese+space Windows filename PDF."""
    jp_pdf = generated_pdfs["japanese_space_filename"]
    orig_hash = compute_sha256(jp_pdf)

    mgr = JobManager(config=isolated_config)
    doc = mgr.register_document(jp_pdf, original_filename=jp_pdf.name)
    assert doc.extractable_Sync_status == "ok"
    assert doc.page_count == 1

    job_cfg = TranslationJobConfig(
        use_mock_translator=True,
        form="plain",
        output_mono_pdf=True,
        output_dual_pdf=True,
    )
    job = mgr.create_job(doc.id, job_config=job_cfg)
    completed_job = mgr.run_job(job.id)

    assert completed_job.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    assert completed_job.is_mock_run is True
    assert completed_job.original_hash_verified is True
    assert verify_original_unchanged(jp_pdf, orig_hash)

    artifacts = mgr.db.list_export_artifacts(job.id)
    art_types = {a.artifact_type for a in artifacts}
    assert {"mono_ja_pdf", "dual_bilingual_pdf", "validation_report_json"} <= art_types
    for art in artifacts:
        assert Path(art.file_path).exists()
        assert art.is_mock_translation is True


@pytest.mark.fault_injection
def test_placeholder_failure_retries_and_falls_back_to_original_without_silent_deletion(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 11 critical acceptance test:
    If the model drops `{v1}`, retry up to 2 times (total 3 attempts).
    If still broken, keep original block (`FALLBACK_ORIGINAL`) and mark job `completed_with_warnings`.
    """

    def drop_placeholder_always(source_text: str, attempt: int) -> tuple[str, str] | None:
        if "{v1}" in source_text:
            # Return translation missing {v1}!
            return ("プレースホルダーを欠落させた不正な訳文である [1, 2]。", "stop")
        return None

    faulty_adapter = MockPlamoAdapter(fault_injector=drop_placeholder_always)
    mgr = JobManager(config=isolated_config, translation_adapter=faulty_adapter)
    doc = mgr.register_document(generated_pdfs["single_col_math"])

    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    res_job = mgr.run_job(job.id)

    # Must NOT be marked as clean `COMPLETED`!
    assert res_job.state == JobState.COMPLETED_WITH_WARNINGS
    assert res_job.retry_count >= 2

    blocks = mgr.db.list_blocks(doc.id)
    fallback_blocks = [b for b in blocks if b.status == BlockStatus.FALLBACK_ORIGINAL]
    assert len(fallback_blocks) >= 1
    for fb in fallback_blocks:
        assert fb.translation_text is None
        assert "PLACEHOLDER_MISSING" in fb.warnings

    issues = mgr.db.list_validation_issues(job.id)
    assert any(i.code == "PLACEHOLDER_MISSING" and i.fallback_applied for i in issues)
    assert res_job.warning_count == len(_deduplicate_validation_issues(issues))

    calls_before_resume = {
        fb.masked_source_text or fb.source_text:
        faulty_adapter.call_Count_by_source.get(
            fb.masked_source_text or fb.source_text, 0
        )
        for fb in fallback_blocks
    }
    resumed = mgr.run_job(job.id, resume=True)
    resumed_fallbacks = [
        block
        for block in mgr.db.list_blocks(doc.id)
        if block.status == BlockStatus.FALLBACK_ORIGINAL
    ]
    assert resumed.state == JobState.COMPLETED_WITH_WARNINGS
    assert resumed_fallbacks
    for fallback in resumed_fallbacks:
        source = fallback.masked_source_text or fallback.source_text
        assert faulty_adapter.call_Count_by_source.get(source, 0) == calls_before_resume[source]
    _assert_job_accounting(mgr, resumed)


@pytest.mark.fault_injection
def test_transient_failure_recovers_on_retry(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Verify that if attempt 1 drops `{v1}` but attempt 2 succeeds, block translates cleanly."""

    def fail_only_first_attempt(source_text: str, attempt: int) -> tuple[str, str] | None:
        if "{v1}" in source_text and attempt == 1:
            return ("1回目のみプレースホルダー欠落エラー", "stop")
        return None

    recovering_adapter = MockPlamoAdapter(fault_injector=fail_only_first_attempt)
    mgr = JobManager(config=isolated_config, translation_adapter=recovering_adapter)
    doc = mgr.register_document(generated_pdfs["single_col_math"])

    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    res_job = mgr.run_job(job.id)

    assert res_job.retry_count >= 1
    blocks = mgr.db.list_blocks(doc.id)
    translated_with_ph = [
        b for b in blocks if b.protected_items and b.status == BlockStatus.TRANSLATED
    ]
    assert len(translated_with_ph) >= 1


@pytest.mark.fault_injection
def test_connection_drop_interrupt_and_resume_without_retranslating_completed_blocks(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 14 & 18 acceptance test:
    Simulate server connection drop on the 2nd translatable block, verify `INTERRUPTED` state,
    then resume and confirm already-translated block #1 is NOT re-inferred.
    """
    call_counter = {"total": 0}

    def drop_on_second_call(source_text: str, attempt: int) -> tuple[str, str] | None:
        call_counter["total"] += 1
        if call_counter["total"] == 2:
            return ("", "connection_error")
        return None

    adapter = MockPlamoAdapter(fault_injector=drop_on_second_call)
    mgr = JobManager(config=isolated_config, translation_adapter=adapter)
    doc = mgr.register_document(generated_pdfs["two_col_figures_tables_2p"])

    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    interrupted_job = mgr.run_job(job.id)
    assert interrupted_job.state == JobState.INTERRUPTED

    blocks_mid = mgr.db.list_blocks(doc.id)
    _assert_job_accounting(mgr, interrupted_job)
    already_done = [b for b in blocks_mid if b.status == BlockStatus.TRANSLATED]
    assert len(already_done) == 1
    first_done_id = already_done[0].id
    first_done_calls = adapter.call_Count_by_source.get(
        already_done[0].masked_source_text or already_done[0].source_text, 0
    )
    assert first_done_calls == 1

    # Clear fault injector and resume job
    adapter.fault_injector = None
    resumed_job = mgr.run_job(job.id, resume=True)
    assert resumed_job.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    assert resumed_job.processed_blocks <= resumed_job.total_blocks
    assert resumed_job.processed_blocks == resumed_job.total_blocks
    _assert_job_accounting(mgr, resumed_job)

    # First block must not have been re-sent to the model
    after_calls = adapter.call_Count_by_source.get(
        already_done[0].masked_source_text or already_done[0].source_text, 0
    )
    assert after_calls == 1


def test_targeted_block_retranslation_excludes_target_from_in_progress_count(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    adapter = MockPlamoAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=adapter)
    doc = mgr.register_document(generated_pdfs["two_col_figures_tables_2p"])
    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    completed = mgr.run_job(job.id)
    assert completed.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)

    target = next(
        block
        for block in mgr.db.list_blocks(doc.id)
        if block.status == BlockStatus.TRANSLATED
    )
    observed: dict[str, int] = {}
    original_translate = mgr._translate_and_validate_single_block

    def observe_target_translation(**kwargs):
        block = kwargs["block"]
        if block.id == target.id:
            current_blocks = mgr.db.list_blocks(doc.id)
            expected_before_target = sum(
                item.status in _PROCESSED_STATUSES and item.id != target.id
                for item in current_blocks
            )
            observed["reported_before"] = kwargs["job"].processed_blocks
            observed["expected_before"] = expected_before_target
        return original_translate(**kwargs)

    mgr._translate_and_validate_single_block = observe_target_translation  # type: ignore[method-assign]
    retranslated = mgr.run_job(
        job.id,
        resume=True,
        target_block_ids={target.id},
    )

    assert observed["reported_before"] == observed["expected_before"]
    assert retranslated.state in (JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS)
    _assert_job_accounting(mgr, retranslated)


def test_manual_protection_region_and_manual_edit_and_large_glossary(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    mgr = JobManager(config=isolated_config)
    doc = mgr.register_document(generated_pdfs["single_col_math"])

    # Add a large glossary (120 terms) including one active term
    entries = [GlossaryEntry(source=f"term_{i}", target=f"用語_{i}") for i in range(120)]
    entries.append(GlossaryEntry(source="energy function", target="エネルギー汎関数"))
    glossary = Glossary(id="gls-large", name="Large Domain Glossary", entries=entries)
    mgr.db.save_glossary(glossary)

    # Add manual protection region over top area (Title/Abstract)
    mgr.db.save_protection_region(
        ProtectionRegion(
            id="reg-manual-1",
            document_id=doc.id,
            page_index=0,
            bbox=[60.0, 50.0, 550.0, 90.0],
            mode=ProtectionMode.DO_NOT_TRANSLATE,
        )
    )

    job = mgr.create_job(
        doc.id,
        TranslationJobConfig(use_mock_translator=True, glossary_id="gls-large"),
    )
    mgr.run_job(job.id)

    blocks = mgr.db.list_blocks(doc.id)
    # Title block overlapped by manual DO_NOT_TRANSLATE must be PROTECTED_SKIP
    title_blocks = [b for b in blocks if "Convergence Analysis" in b.source_text]
    assert len(title_blocks) == 1
    assert title_blocks[0].status == BlockStatus.PROTECTED_SKIP

    # Glossary term "エネルギー汎関数" must be reflected in translated body block
    body_blocks = [
        b for b in blocks if "energy function" in b.source_text and b.status == BlockStatus.TRANSLATED
    ]
    assert len(body_blocks) >= 1
    assert "エネルギー汎関数" in (body_blocks[0].translation_text or "")

    # Manual edit must be stored separately from model translation_text
    target_blk = body_blocks[0]
    original_model_out = target_blk.translation_text
    updated_blk = mgr.db.update_block_manual_translation(
        doc.id, target_blk.id, "ユーザーによる手動修正訳文 [1, 2]。"
    )
    assert updated_blk.status == BlockStatus.MANUALLY_EDITED
    assert updated_blk.translation_text == original_model_out
    assert updated_blk.manual_translation_text == "ユーザーによる手動修正訳文 [1, 2]。"
    assert updated_blk.effective_translation == "ユーザーによる手動修正訳文 [1, 2]。"


def test_scanned_and_corrupted_pdfs_rejected_without_silent_ocr(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    mgr = JobManager(config=isolated_config)

    scanned_doc = mgr.register_document(generated_pdfs["scanned_like_no_text"])
    assert scanned_doc.is_scanned_pdf is True
    assert scanned_doc.extractable_Sync_status == "unsupported_scanned"
    assert "OCR" in (scanned_doc.unsupported_reason or "")

    scanned_job = mgr.create_job(scanned_doc.id, TranslationJobConfig(use_mock_translator=True))
    res_scanned = mgr.run_job(scanned_job.id)
    assert res_scanned.state == JobState.FAILED

    corrupted_doc = mgr.register_document(generated_pdfs["corrupted_sample"])
    assert corrupted_doc.is_corrupted is True
    corrupted_job = mgr.create_job(corrupted_doc.id, TranslationJobConfig(use_mock_translator=True))
    res_corrupted = mgr.run_job(corrupted_job.id)
    assert res_corrupted.state == JobState.FAILED


def test_security_blocks_external_translation_host_and_untrusted_origin(
    isolated_config: AppConfig,
) -> None:
    with pytest.raises(SecurityPolicyViolationError):
        LlamaCppPlamoAdapter(
            config=isolated_config,
            base_url="https://api.openai.com/v1",
        )

    app = create_app(config=isolated_config, session_token="secret-test-token")
    client = TestClient(app)

    # Disallowed origin rejected with 403
    bad_origin_resp = client.get(
        "/api/diagnostics", headers={"Origin": "https://malicious.example.com"}
    )
    assert bad_origin_resp.status_code == 403

    # Invalid token rejected with 401
    bad_token_resp = client.get(
        "/api/diagnostics", headers={"X-PaperBridge-Token": "wrong-token"}
    )
    assert bad_token_resp.status_code == 401

    # Valid loopback origin + token succeeds
    ok_resp = client.get(
        "/api/diagnostics",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "X-PaperBridge-Token": "secret-test-token",
        },
    )
    assert ok_resp.status_code == 200
    data = ok_resp.json()
    assert data["loopback_only_enforced"] is True
