"""Job Manager and 13-Stage Translation Pipeline Orchestrator (Section 6, 11, 14).

Handles:
- State machine transitions (`created -> preflight -> parsing -> awaiting_review -> translating -> validating -> typesetting -> completed / completed_with_warnings`, plus `failed / canceled / interrupted`).
- Block-by-block persistence for crash recovery and resumption without re-translating completed blocks.
- Up to 2 retries + sentence-level re-splitting on validation failure.
- Strict fallback-to-original when validation or typesetting fails (never silently deleting placeholders).
- Separation of temporary working files from finalized export artifacts.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

from typing import Any

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    BlockKind,
    BlockStatus,
    Document,
    ExportArtifact,
    GlossaryEntry,
    IssueSeverity,
    Job,
    JobState,
    ProtectionRegion,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.pdf.adapters.base import PdfEngineAdapter
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.pdf.coordinates import bbox_iou, parse_page_range_spec
from backend.pdf.validation.preflight import (
    compute_sha256,
    inspect_pdf_preflight,
    verify_original_unchanged,
)
from backend.storage.cache import compute_translation_cache_key
from backend.storage.database import DatabaseRepository
from backend.translation.adapters.base import (
    TranslationAdapter,
    TranslationConnectionError,
)
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter
from backend.translation.adapters.mock_adapter import MockPlamoAdapter
from backend.translation.continuation import (
    attach_continuation_candidates,
    build_continuation_contexts,
)
from backend.translation.prompts.pfml3 import (
    HistoryPair,
    split_paragraph_preserving_placeholders,
)
from backend.translation.validation.validator import validate_translation_output


def _deduplicate_validation_issues(
    issues: list[ValidationIssue],
) -> list[ValidationIssue]:
    unique: dict[tuple[Any, ...], ValidationIssue] = {}
    for issue in issues:
        identity = (
            issue.job_id,
            issue.document_id,
            issue.page_index,
            issue.block_id,
            tuple(issue.bbox) if issue.bbox is not None else None,
            issue.code,
            issue.severity.value,
            issue.fallback_applied,
            issue.message,
            json.dumps(issue.details, ensure_ascii=False, sort_keys=True, default=str),
        )
        unique.setdefault(identity, issue)
    return list(unique.values())


class JobManager:
    def __init__(
        self,
        config: AppConfig | None = None,
        db: DatabaseRepository | None = None,
        pdf_adapter: PdfEngineAdapter | None = None,
        translation_adapter: TranslationAdapter | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.config.ensure_directories()
        self.db = db or DatabaseRepository(self.config.db_path)
        self.pdf_adapter = pdf_adapter or NativePreservingAdapter(self.config)
        self.default_translation_adapter = translation_adapter
        self._cancel_flags: dict[str, threading.Event] = {}
        self._interrupt_flags: dict[str, threading.Event] = {}
        self._active_lock = threading.Lock()
        self._active_jobs: set[str] = set()

    def is_job_active(self, job_id: str) -> bool:
        with self._active_lock:
            return job_id in self._active_jobs

    def register_document(
        self,
        source_pdf_path: str | Path,
        original_filename: str | None = None,
    ) -> Document:
        """Copy uploaded PDF to a path-traversal-safe internal path while verifying original hash."""
        src = Path(source_pdf_path).resolve()
        orig_sha = compute_sha256(src)
        doc_id = f"doc-{uuid.uuid4().hex[:12]}"
        safe_dest = (self.config.runtime_dir / "uploads" / f"{doc_id}.pdf").resolve()
        safe_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, safe_dest)

        doc = inspect_pdf_preflight(
            pdf_path=safe_dest,
            original_filename=original_filename or src.name,
            document_id=doc_id,
            max_file_mb=self.config.max_upload_mb,
            max_pages=self.config.max_pages,
        )
        # Ensure original source file was not touched
        if not verify_original_unchanged(src, orig_sha):
            raise RuntimeError("Original source PDF hash changed during registration!")
        self.db.save_document(doc)
        return doc

    def create_job(
        self,
        document_id: str,
        job_config: TranslationJobConfig | None = None,
    ) -> Job:
        doc = self.db.get_document(document_id)
        if not doc:
            raise KeyError(f"Document not found: {document_id}")
        cfg = job_config or TranslationJobConfig()
        job = Job(
            id=f"job-{uuid.uuid4().hex[:12]}",
            document_id=document_id,
            state=JobState.CREATED,
            current_stage="created",
            config=cfg,
            original_sha256=doc.sha256_hash,
            is_mock_run=cfg.use_mock_translator,
        )
        self.db.save_job(job)
        self._cancel_flags[job.id] = threading.Event()
        self._interrupt_flags[job.id] = threading.Event()
        return job

    def request_cancel(self, job_id: str) -> Job | None:
        if job_id not in self._cancel_flags:
            self._cancel_flags[job_id] = threading.Event()
        self._cancel_flags[job_id].set()

        job = self.db.get_job(job_id)
        if not job:
            return None
        job.cancel_requested = True
        if not self.is_job_active(job_id):
            if job.state != JobState.CANCELED and job.can_transition_to(JobState.CANCELED):
                job.transition_to(JobState.CANCELED, "ユーザーにより安全な区切りでキャンセルされました")
        self.db.save_job(job)
        return job

    def request_simulate_interrupt(self, job_id: str) -> None:
        if job_id not in self._interrupt_flags:
            self._interrupt_flags[job_id] = threading.Event()
        self._interrupt_flags[job_id].set()

    def _check_cancel_or_interrupt(self, job: Job) -> bool:
        if self._cancel_flags.get(job.id) and self._cancel_flags[job.id].is_set():
            job.cancel_requested = True
            if job.state != JobState.CANCELED:
                job.transition_to(JobState.CANCELED, "ユーザーにより安全な区切りでキャンセルされました")
            self.db.save_job(job)
            return True
        if self._interrupt_flags.get(job.id) and self._interrupt_flags[job.id].is_set():
            job.transition_to(JobState.INTERRUPTED, "中断されました（再開可能）")
            self.db.save_job(job)
            return True
        return False

    def _merge_existing_block_state(
        self,
        parsed_blocks: list[Block],
        existing_blocks: list[Block],
        manual_regions: list[ProtectionRegion],
        cfg: TranslationJobConfig,
        reset_user_overrides: bool = False,
        target_block_ids: set[str] | None = None,
    ) -> None:
        if not existing_blocks:
            return
        old_by_id = {b.id: b for b in existing_blocks}
        old_by_page: dict[int, list[Block]] = {}
        for b in existing_blocks:
            old_by_page.setdefault(b.page_index, []).append(b)
        manual_by_page: dict[int, list[ProtectionRegion]] = {}
        for r in manual_regions:
            manual_by_page.setdefault(r.page_index, []).append(r)

        for pb in parsed_blocks:
            old_b = old_by_id.get(pb.id)
            if old_b is None or bbox_iou(pb.bbox, old_b.bbox) < 0.45:
                # Match by page_index + high bbox overlap if block numbering shifted
                candidates = old_by_page.get(pb.page_index, [])
                best_cand = None
                best_iou = 0.0
                for cand in candidates:
                    iou = bbox_iou(pb.bbox, cand.bbox)
                    if iou > best_iou:
                        best_iou = iou
                        best_cand = cand
                if best_cand is not None and best_iou >= 0.65:
                    old_b = best_cand
                elif old_b is not None and bbox_iou(pb.bbox, old_b.bbox) < 0.25:
                    old_b = None

            if old_b is None:
                continue

            pb.edit_revision = old_b.edit_revision
            pb.manual_validation_errors = list(old_b.manual_validation_errors)
            pb.manual_validation_warnings = list(old_b.manual_validation_warnings)

            if not reset_user_overrides and old_b.user_protection_override in (
                "force_translate",
                "force_protect",
            ):
                pb.user_protection_override = old_b.user_protection_override
                if isinstance(self.pdf_adapter, NativePreservingAdapter):
                    self.pdf_adapter._apply_protection_and_masking(
                        pb, manual_by_page.get(pb.page_index, []), cfg
                    )

            if (
                pb.status != BlockStatus.PROTECTED_SKIP
                and (target_block_ids is None or pb.id not in target_block_ids)
                and old_b.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED)
            ):
                pb.translation_text = old_b.translation_text
                pb.manual_translation_text = old_b.manual_translation_text
                pb.status = old_b.status
                pb.translation_provenance = (
                    "manual_edit"
                    if old_b.status == BlockStatus.MANUALLY_EDITED
                    else (old_b.translation_provenance if old_b.translation_provenance != "none" else "reused_block")
                )
            else:
                pb.translation_text = old_b.translation_text
                pb.manual_translation_text = old_b.manual_translation_text

    def parse_document_blocks(
        self,
        document_id: str,
        job_config: TranslationJobConfig | None = None,
        reset_user_overrides: bool = False,
    ) -> list[Block]:
        """Parse and extract blocks for Screen 2 pre-translation protection region editing."""
        doc = self.db.get_document(document_id)
        if not doc:
            raise KeyError(f"Document not found: {document_id}")
        if (
            doc.is_corrupted
            or doc.is_encrypted
            or doc.is_scanned_pdf
            or doc.extractable_Sync_status != "ok"
        ):
            return []
        cfg = job_config or TranslationJobConfig()
        page_indices = parse_page_range_spec(cfg.page_range_spec, doc.page_count)
        manual_regions = self.db.list_protection_regions(doc.id)
        existing_blocks = self.db.list_blocks(doc.id)
        parsed_blocks = self.pdf_adapter.parse_and_extract_blocks(
            document=doc,
            page_indices=page_indices,
            manual_regions=manual_regions,
            job_config=cfg,
        )
        self._merge_existing_block_state(
            parsed_blocks=parsed_blocks,
            existing_blocks=existing_blocks,
            manual_regions=manual_regions,
            cfg=cfg,
            reset_user_overrides=reset_user_overrides,
        )
        self.db.save_blocks(doc.id, parsed_blocks, replace_all_for_pages=True)
        return parsed_blocks

    def update_block_protection(
        self,
        document_id: str,
        block_id: str,
        user_protection_override: str | None,
        job_config: TranslationJobConfig | None = None,
    ) -> Block:
        """Explicitly set or clear a user block-level protection override ('force_translate' | 'force_protect' | None)."""
        if user_protection_override not in ("force_translate", "force_protect", None):
            raise ValueError(
                f"Invalid user_protection_override: {user_protection_override}. Expected 'force_translate', 'force_protect', or None."
            )
        doc = self.db.get_document(document_id)
        if not doc:
            raise KeyError(f"Document not found: {document_id}")
        blocks = self.db.list_blocks(document_id)
        target = next((b for b in blocks if b.id == block_id), None)
        if not target:
            raise KeyError(f"Block not found: {block_id}")

        cfg = job_config or TranslationJobConfig()
        manual_regions = self.db.list_protection_regions(document_id, page_index=target.page_index)
        saved_trans = target.translation_text
        saved_manual = target.manual_translation_text

        target.user_protection_override = user_protection_override
        if isinstance(self.pdf_adapter, NativePreservingAdapter):
            self.pdf_adapter._apply_protection_and_masking(target, manual_regions, cfg)

        target.translation_text = saved_trans
        target.manual_translation_text = saved_manual
        if target.status != BlockStatus.PROTECTED_SKIP:
            if saved_manual is not None:
                target.status = BlockStatus.MANUALLY_EDITED
                target.translation_provenance = "manual_edit"
            elif saved_trans is not None:
                target.status = BlockStatus.TRANSLATED
            else:
                target.status = BlockStatus.PENDING
                target.translation_provenance = "none"

        self.db.save_blocks(document_id, [target])
        self.db.increment_protection_revision(document_id)
        return target

    def reapply_auto_protection(
        self,
        document_id: str,
        reset_user_overrides: bool = True,
        clear_manual_regions: bool = False,
        job_config: TranslationJobConfig | None = None,
    ) -> dict[str, Any]:
        """Re-apply initial academic non-translation auto-detection to an existing document
        without modifying already finalized job artifacts.
        """
        doc = self.db.get_document(document_id)
        if not doc:
            raise KeyError(f"Document not found: {document_id}")
        cleared_regions = 0
        if clear_manual_regions:
            cleared_regions = self.db.clear_protection_regions_for_document(document_id)
        updated_blocks = self.parse_document_blocks(
            document_id=document_id,
            job_config=job_config,
            reset_user_overrides=reset_user_overrides,
        )
        new_rev = self.db.increment_protection_revision(document_id)
        doc_after = self.db.get_document(document_id) or doc
        auto_protected = [
            b
            for b in updated_blocks
            if b.status == BlockStatus.PROTECTED_SKIP and b.protection_source == "auto"
        ]
        manual_protected = [
            b
            for b in updated_blocks
            if b.status == BlockStatus.PROTECTED_SKIP and b.protection_source == "manual"
        ]
        translatable = [b for b in updated_blocks if b.status != BlockStatus.PROTECTED_SKIP]
        category_counts: dict[str, int] = {}
        for b in auto_protected:
            category_counts[b.semantic_category] = category_counts.get(b.semantic_category, 0) + 1
        return {
            "document": doc_after,
            "blocks": updated_blocks,
            "protection_revision": new_rev,
            "cleared_manual_regions": cleared_regions,
            "auto_protected_count": len(auto_protected),
            "manual_protected_count": len(manual_protected),
            "translatable_count": len(translatable),
            "auto_protected_by_category": category_counts,
        }

    def _resolve_translator(self, cfg: TranslationJobConfig) -> TranslationAdapter:
        if self.default_translation_adapter is not None:
            return self.default_translation_adapter
        if cfg.use_mock_translator:
            return MockPlamoAdapter(model_name=cfg.model_name, quantization=cfg.quantization)
        return LlamaCppPlamoAdapter(
            config=self.config,
            model_alias=cfg.model_name,
            quantization=cfg.quantization,
        )

    def run_job(
        self,
        job_id: str,
        resume: bool = False,
        target_block_ids: set[str] | None = None,
    ) -> Job:
        """Execute or resume a job through all 13 stages."""
        with self._active_lock:
            if job_id in self._active_jobs:
                raise RuntimeError(f"JobConflict: Job '{job_id}' is already running.")
            self._active_jobs.add(job_id)

        try:
            job = self.db.get_job(job_id)
            if not job:
                raise KeyError(f"Job not found: {job_id}")
            doc = self.db.get_document(job.document_id)
            if not doc:
                raise KeyError(f"Document not found: {job.document_id}")

            if resume:
                job.cancel_requested = False
                if job.id in self._cancel_flags:
                    self._cancel_flags[job.id].clear()
                if job.id in self._interrupt_flags:
                    self._interrupt_flags[job.id].clear()
            elif job.cancel_requested or (
                self._cancel_flags.get(job.id) and self._cancel_flags[job.id].is_set()
            ):
                if job.state != JobState.CANCELED and job.can_transition_to(JobState.CANCELED):
                    job.transition_to(
                        JobState.CANCELED, "ユーザーにより安全な区切りでキャンセルされました"
                    )
                job.cancel_requested = True
                self.db.save_job(job)
                return job

            # Stage 1 - 3: Preflight verification
            if job.state in (JobState.CREATED, JobState.INTERRUPTED, JobState.CANCELED, JobState.FAILED) and not resume:
                job.transition_to(JobState.PREFLIGHT, "Stage 1-3: ファイル・ページ・抽出可否検証")
                self.db.save_job(job)

            if not verify_original_unchanged(doc.safe_storage_path, doc.sha256_hash):
                job.original_hash_verified = False
                job.error_message = "原本PDFのハッシュ不一致が検出されました。"
                job.transition_to(JobState.FAILED, "原本ハッシュ検証エラー")
                self.db.save_job(job)
                return job

            if doc.is_corrupted or doc.is_encrypted or doc.is_scanned_pdf or doc.extractable_Sync_status != "ok":
                job.error_message = doc.unsupported_reason or "非対応または破損したPDFです。"
                if job.can_transition_to(JobState.FAILED):
                    job.transition_to(JobState.FAILED, "Preflight 非対応検出")
                self.db.save_job(job)
                return job

            page_indices = parse_page_range_spec(job.config.page_range_spec, doc.page_count)
            job.total_pages = len(page_indices)
            self.db.save_job(job)

            if self._check_cancel_or_interrupt(job):
                return job

            # Stage 4 - 7: Parsing, Reading Order, Protection Regions, Translation Units
            existing_blocks = self.db.list_blocks(doc.id)
            manual_regions = self.db.list_protection_regions(doc.id)

            if not existing_blocks or not resume:
                if job.can_transition_to(JobState.PARSING):
                    job.transition_to(JobState.PARSING, "Stage 4-7: 構造・数式解析および保護領域決定")
                    self.db.save_job(job)

                parsed_blocks = self.pdf_adapter.parse_and_extract_blocks(
                    document=doc,
                    page_indices=page_indices,
                    manual_regions=manual_regions,
                    job_config=job.config,
                )
                # Preserve existing translations/manual edits and user overrides
                self._merge_existing_block_state(
                    parsed_blocks=parsed_blocks,
                    existing_blocks=existing_blocks,
                    manual_regions=manual_regions,
                    cfg=job.config,
                    reset_user_overrides=False,
                    target_block_ids=target_block_ids,
                )
                self.db.save_blocks(doc.id, parsed_blocks, replace_all_for_pages=True)
                blocks = parsed_blocks
            else:
                blocks = [b for b in existing_blocks if b.page_index in set(page_indices)]

            job.total_blocks = len(blocks)
            attach_continuation_candidates(blocks)
            self.db.save_blocks(doc.id, blocks)
            continuation_contexts = build_continuation_contexts(blocks)
            self.db.save_job(job)

            if job.config.pause_for_review and not resume:
                job.transition_to(JobState.AWAITING_REVIEW, "保護領域・解析結果のユーザー確認待ち")
                self.db.save_job(job)
                return job

            issues: list[ValidationIssue] = self.db.list_validation_issues(job.id) if resume else []
            newly_inferenced_blocks = 0
            llm_api_calls_made = 0
            cache_hit_blocks = 0
            reused_existing_blocks = 0
            adapter_name = "roundtrip_noop"
            engine_process = "none (roundtrip preservation)"
            endpoint_url = "none"
            model_file_used = job.config.model_name
            compute_backend = "not_applicable"

            # Stage 8 & 9: Translation & Validation (skipped in roundtrip-only mode)
            if not job.config.only_roundtrip_preservation:
                translator = self._resolve_translator(job.config)
                job.is_mock_run = translator.is_mock
                if translator.is_mock:
                    adapter_name = "mock"
                    engine_process = "MockPlamoAdapter (in-process)"
                    endpoint_url = "in-process://mock"
                    model_file_used = f"{job.config.model_name} (mock)"
                    compute_backend = "mock (no GPU/CPU LLM)"
                else:
                    adapter_name = "plamo_llama_cpp"
                    endpoint_url = getattr(translator, "base_url", self.config.llama_base_url)
                    engine_process = "unverified_from_api"
                    compute_backend = "unverified_from_api"
                    try:
                        from backend.diagnostics import _probe_llama_server_process_and_vulkan

                        pid, vk_status = _probe_llama_server_process_and_vulkan(endpoint_url)
                        compute_backend = vk_status
                        if pid is not None:
                            engine_process = f"llama-server.exe (PID {pid})"
                    except Exception:
                        pass
                    try:
                        check_fn = getattr(translator, "check_health", None)
                        if callable(check_fn):
                            health = check_fn()
                            if isinstance(health, dict):
                                if isinstance(health.get("model_file"), str) and health["model_file"]:
                                    model_file_used = str(health["model_file"])
                                if isinstance(health.get("build_info"), str) and health["build_info"]:
                                    engine_process = f"{engine_process} [{health['build_info']}]"
                    except Exception:
                        pass

                job.transition_to(JobState.TRANSLATING, "Stage 8: PLaMo PFML3 段落翻訳中")
                self.db.save_job(job)

                glossary_entries: list[GlossaryEntry] = []
                if job.config.glossary_id:
                    gl = self.db.get_glossary(job.config.glossary_id)
                    if gl:
                        glossary_entries = gl.entries
                        job.glossary_version_used = gl.version

                recent_heading: str | None = None
                history_window: list[HistoryPair] = []
                processed_statuses = {
                    BlockStatus.PROTECTED_SKIP,
                    BlockStatus.TRANSLATED,
                    BlockStatus.MANUALLY_EDITED,
                    BlockStatus.FALLBACK_ORIGINAL,
                }

                def processed_block_count() -> int:
                    return sum(
                        1
                        for item in blocks
                        if item.status in processed_statuses
                        and not (target_block_ids and item.id in target_block_ids)
                    )

                job.processed_blocks = processed_block_count()
                self.db.save_job(job)

                for blk in blocks:
                    if self._check_cancel_or_interrupt(job):
                        return job

                    if blk.kind in (BlockKind.TITLE, BlockKind.HEADING):
                        recent_heading = blk.source_text

                    # Skip blocks that are protected or already translated (unless targeted for partial re-translation)
                    if blk.status == BlockStatus.PROTECTED_SKIP:
                        blk.translation_provenance = "protected_skip"
                        continue
                    if blk.status == BlockStatus.MANUALLY_EDITED and (
                        target_block_ids is None or blk.id not in target_block_ids
                    ):
                        blk.translation_provenance = "manual_edit"
                        continue
                    if blk.status == BlockStatus.FALLBACK_ORIGINAL and (
                        target_block_ids is None or blk.id not in target_block_ids
                    ):
                        blk.translation_provenance = "fallback_original"
                        continue
                    if (
                        blk.status == BlockStatus.TRANSLATED
                        and blk.translation_text
                        and (target_block_ids is None or blk.id not in target_block_ids)
                    ):
                        blk.translation_provenance = "reused_block"
                        reused_existing_blocks += 1
                        history_window.append(
                            HistoryPair(
                                source_text=blk.masked_source_text or blk.source_text,
                                target_text=blk.translation_text,
                            )
                        )
                        history_window = history_window[-2:]
                        continue

                    context_parts = []
                    if recent_heading:
                        context_parts.append(f"Section heading: {recent_heading}")
                    if blk.id in continuation_contexts:
                        context_parts.append(continuation_contexts[blk.id])
                    context_hint = "\n".join(context_parts) or None

                    block_source_outcome, block_llm_calls = self._translate_and_validate_single_block(
                        job=job,
                        doc=doc,
                        block=blk,
                        translator=translator,
                        glossary_entries=glossary_entries,
                        context_hint=context_hint,
                        history_window=history_window,
                        issues=issues,
                    )
                    llm_api_calls_made += block_llm_calls
                    if block_source_outcome == "cache_hit":
                        cache_hit_blocks += 1
                    elif block_source_outcome == "llm_inference":
                        newly_inferenced_blocks += 1
                    self.db.save_blocks(doc.id, [blk])
                    job.processed_blocks = sum(
                        1
                        for b in blocks
                        if b.status in processed_statuses
                        and not (target_block_ids and b.id in target_block_ids)
                    )
                    job.warning_count = len(_deduplicate_validation_issues(issues))
                    self.db.save_job(job)

                    if blk.status == BlockStatus.TRANSLATED and blk.translation_text:
                        history_window.append(
                            HistoryPair(
                                source_text=blk.masked_source_text or blk.source_text,
                                target_text=blk.translation_text,
                            )
                        )
                        history_window = history_window[-2:]

                job.transition_to(JobState.VALIDATING, "Stage 9: 訳文および保護記号の整合性検証")
                self.db.save_job(job)

            if self._check_cancel_or_interrupt(job):
                return job

            # Stage 10 - 13: Typesetting, Preservation Verification, Final Export, Report
            job.transition_to(JobState.TYPESETTING, "Stage 10-12: 日本語再配置・数式保持検証・PDF出力")
            self.db.save_job(job)

            work_dir = self.config.runtime_dir / "working" / job.id
            final_dir = self.config.runtime_dir / "outputs" / job.id
            work_dir.mkdir(parents=True, exist_ok=True)
            final_dir.mkdir(parents=True, exist_ok=True)

            work_mono = work_dir / f"{doc.id}.ja.pdf" if job.config.output_mono_pdf else None
            work_dual = work_dir / f"{doc.id}.dual.pdf" if job.config.output_dual_pdf else None

            blocks, typeset_issues = self.pdf_adapter.typeset_and_export(
                document=doc,
                blocks=blocks,
                page_indices=page_indices,
                mono_output_path=work_mono,
                dual_output_path=work_dual,
                job_id=job.id,
                job_config=job.config,
                manual_regions=manual_regions,
            )
            issues.extend(typeset_issues)
            self.db.save_blocks(doc.id, blocks)

            # Stage 11: Verify visual preservation of protected formulas and figures
            visual_reports = []
            if work_mono and work_mono.exists():
                v_reps = self.pdf_adapter.verify_preservation(
                    document=doc,
                    output_pdf_path=work_mono,
                    blocks=blocks,
                    page_indices=page_indices,
                    manual_regions=manual_regions,
                    is_roundtrip_only=job.config.only_roundtrip_preservation,
                )
                for rep in v_reps:
                    visual_reports.append(
                        {
                            "page_index": rep.page_index,
                            "display_page": rep.page_index + 1,
                            "all_protected_regions_preserved": rep.all_protected_regions_preserved,
                            "non_translated_background_diff_ratio": round(
                                rep.non_translated_background_diff_ratio, 6
                            ),
                            "full_page_diff_ratio": round(rep.full_page_diff_ratio, 6),
                            "semantic_accuracy_guaranteed": rep.semantic_accuracy_guaranteed,
                            "regions": [
                                {
                                    "region_id": r.region_id,
                                    "bbox_pt": r.bbox_pt,
                                    "max_channel_diff": r.max_channel_diff,
                                    "mean_abs_diff": round(r.mean_abs_diff, 6),
                                    "differing_pixel_ratio": round(r.differing_pixel_ratio, 6),
                                    "passed": r.passed,
                                    "note": r.note,
                                }
                                for r in rep.protected_region_results
                            ],
                        }
                    )
                    if not rep.all_protected_regions_preserved:
                        failing_regs = [
                            {
                                "region_id": r.region_id,
                                "bbox_pt": r.bbox_pt,
                                "differing_pixel_ratio": round(r.differing_pixel_ratio, 6),
                                "max_channel_diff": r.max_channel_diff,
                            }
                            for r in rep.protected_region_results
                            if not r.passed
                        ]
                        first_reg_id = failing_regs[0]["region_id"] if failing_regs else None
                        first_bbox = failing_regs[0]["bbox_pt"] if failing_regs else None
                        issues.append(
                            ValidationIssue(
                                id=f"iss-{uuid.uuid4().hex[:10]}",
                                job_id=job.id,
                                document_id=doc.id,
                                page_index=rep.page_index,
                                block_id=first_reg_id if (first_reg_id and ":block-" in str(first_reg_id)) else None,
                                bbox=first_bbox,
                                code="VISUAL_PRESERVATION_DIFF_DETECTED",
                                severity=IssueSeverity.WARNING,
                                message=(
                                    f"ページ {rep.page_index + 1} の保護領域（{first_reg_id or '背景'}）に視覚差分が検出されました。"
                                ),
                                details={
                                    "background_diff_ratio": round(rep.non_translated_background_diff_ratio, 6),
                                    "failing_regions": failing_regs,
                                },
                            )
                        )

            # Stage 12: Promote working files to finalized export artifacts
            if work_mono and work_mono.exists():
                final_mono = final_dir / f"{doc.id}.ja.pdf"
                shutil.copy2(work_mono, final_mono)
                art_type = "roundtrip_pdf" if job.config.only_roundtrip_preservation else "mono_ja_pdf"
                self.db.save_export_artifact(
                    ExportArtifact(
                        id=f"art-{uuid.uuid4().hex[:10]}",
                        job_id=job.id,
                        document_id=doc.id,
                        artifact_type=art_type,
                        file_path=str(final_mono),
                        sha256_hash=compute_sha256(final_mono),
                        file_size_bytes=final_mono.stat().st_size,
                        is_finalized=True,
                        is_mock_translation=job.is_mock_run,
                    )
                )

            if work_dual and work_dual.exists():
                final_dual = final_dir / f"{doc.id}.dual.pdf"
                shutil.copy2(work_dual, final_dual)
                self.db.save_export_artifact(
                    ExportArtifact(
                        id=f"art-{uuid.uuid4().hex[:10]}",
                        job_id=job.id,
                        document_id=doc.id,
                        artifact_type="dual_bilingual_pdf",
                        file_path=str(final_dual),
                        sha256_hash=compute_sha256(final_dual),
                        file_size_bytes=final_dual.stat().st_size,
                        is_finalized=True,
                        is_mock_translation=job.is_mock_run,
                    )
                )

            # Stage 13: Generate detailed JSON validation report
            original_intact = verify_original_unchanged(doc.safe_storage_path, doc.sha256_hash)
            job.original_hash_verified = original_intact
            if not original_intact:
                raise RuntimeError("CRITICAL: Original PDF hash changed!")

            doc_latest = self.db.get_document(doc.id) or doc
            job.based_on_edit_revision = doc_latest.edit_revision
            job.based_on_protection_revision = doc_latest.protection_revision
            job.applied_block_revisions = {
                b.id: {
                    "source": (
                        "manual"
                        if b.manual_translation_text is not None
                        else ("model" if b.translation_text is not None else "original")
                    ),
                    "provenance": b.translation_provenance,
                    "edit_revision": b.edit_revision,
                    "status": b.status.value,
                }
                for b in blocks
            }
            manual_translation_blocks = sum(
                1 for b in blocks if b.status == BlockStatus.MANUALLY_EDITED
            )
            protected_skip_blocks = sum(
                1 for b in blocks if b.status == BlockStatus.PROTECTED_SKIP
            )
            fallback_original_blocks = sum(
                1 for b in blocks if b.status == BlockStatus.FALLBACK_ORIGINAL
            )
            job.execution_provenance = {
                "adapter_name": adapter_name,
                "engine_process": engine_process,
                "endpoint_url": endpoint_url,
                "model_name": job.config.model_name,
                "model_file": model_file_used,
                "quantization": job.config.quantization,
                "compute_backend": compute_backend,
                "is_mock": job.is_mock_run,
                "only_roundtrip_preservation": job.config.only_roundtrip_preservation,
                "protection_policy_version": "v2.0-academic-auto-protect",
                "total_blocks": len(blocks),
                "newly_inferenced_blocks": newly_inferenced_blocks,
                "llm_api_calls_made": llm_api_calls_made,
                "cache_hit_blocks": cache_hit_blocks,
                "reused_existing_blocks": reused_existing_blocks,
                "manual_translation_blocks": manual_translation_blocks,
                "protected_skip_blocks": protected_skip_blocks,
                "fallback_original_blocks": fallback_original_blocks,
            }

            issues = _deduplicate_validation_issues(issues)
            job.processed_blocks = sum(
                1
                for block in blocks
                if block.status
                in (
                    BlockStatus.PROTECTED_SKIP,
                    BlockStatus.TRANSLATED,
                    BlockStatus.MANUALLY_EDITED,
                    BlockStatus.FALLBACK_ORIGINAL,
                )
            )
            self.db.replace_validation_issues_for_job(job.id, issues)
            report_path = final_dir / f"{doc.id}.report.json"
            report_payload = {
                "job_id": job.id,
                "document_id": doc.id,
                "original_filename": doc.original_filename,
                "original_sha256": doc.sha256_hash,
                "original_hash_unchanged": original_intact,
                "is_mock_translation": job.is_mock_run,
                "only_roundtrip_preservation": job.config.only_roundtrip_preservation,
                "retypeset_revision": job.retypeset_revision,
                "based_on_edit_revision": job.based_on_edit_revision,
                "based_on_protection_revision": job.based_on_protection_revision,
                "execution_provenance": job.execution_provenance,
                "applied_block_revisions": job.applied_block_revisions,
                "total_pages_processed": len(page_indices),
                "total_blocks": len(blocks),
                "translated_blocks": sum(
                    1 for b in blocks if b.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED)
                ),
                "protected_blocks": protected_skip_blocks,
                "fallback_original_blocks": fallback_original_blocks,
                "validation_issues_count": len(issues),
                "validation_issues": [i.model_dump() for i in issues],
                "visual_preservation_reports": visual_reports,
            }
            report_path.write_text(
                json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.db.save_export_artifact(
                ExportArtifact(
                    id=f"art-{uuid.uuid4().hex[:10]}",
                    job_id=job.id,
                    document_id=doc.id,
                    artifact_type="validation_report_json",
                    file_path=str(report_path),
                    sha256_hash=compute_sha256(report_path),
                    file_size_bytes=report_path.stat().st_size,
                    is_finalized=True,
                    is_mock_translation=job.is_mock_run,
                )
            )

            job.processed_pages = len(page_indices)
            job.warning_count = len(issues)
            has_fallbacks = any(b.status == BlockStatus.FALLBACK_ORIGINAL for b in blocks)

            if issues or has_fallbacks:
                job.transition_to(
                    JobState.COMPLETED_WITH_WARNINGS,
                    f"完了（要確認警告 {len(issues)} 件・原文保持フォールバックあり）",
                )
            else:
                job.transition_to(JobState.COMPLETED, "全工程正常完了")

            self.db.save_job(job)
            return job

        except TranslationConnectionError as exc:
            job.error_message = f"推論サーバー接続中断（再開可能）: {exc}"
            if job.can_transition_to(JobState.INTERRUPTED):
                job.transition_to(JobState.INTERRUPTED, "推論サーバー切断により一時停止")
            self.db.save_job(job)
            return job
        except Exception as exc:
            job.error_message = f"{type(exc).__name__}: {exc}"
            if job.can_transition_to(JobState.FAILED):
                job.transition_to(JobState.FAILED, f"エラー発生: {type(exc).__name__}")
            self.db.save_job(job)
            raise
        finally:
            with self._active_lock:
                self._active_jobs.discard(job_id)

    def retypeset_job(
        self,
        job_id: str,
        expected_edit_revision: int | None = None,
        allow_validation_warnings: bool = True,
        fail_stage_for_test: bool = False,
    ) -> dict[str, Any]:
        """Re-run Stages 10-13 (Typesetting, Preservation Verification, Artifact Export, JSON Report)
        using existing translations and manual edits WITHOUT calling the LLM (`llm_calls_made = 0`).
        Preserves previous finalized artifacts untouched if any validation or typesetting error occurs.
        """
        with self._active_lock:
            if job_id in self._active_jobs:
                raise RuntimeError(f"JobConflict: Job '{job_id}' is already running or retypesetting.")
            self._active_jobs.add(job_id)

        staging_dir: Path | None = None
        try:
            job = self.db.get_job(job_id)
            if not job:
                raise KeyError(f"Job not found: {job_id}")
            doc = self.db.get_document(job.document_id)
            if not doc:
                raise KeyError(f"Document not found: {job.document_id}")

            if (
                expected_edit_revision is not None
                and doc.edit_revision != expected_edit_revision
            ):
                raise ValueError(
                    f"RevisionConflict: Document '{doc.id}' expected edit_revision={expected_edit_revision}, actual={doc.edit_revision}"
                )

            if not verify_original_unchanged(doc.safe_storage_path, doc.sha256_hash):
                raise RuntimeError("CRITICAL: Original PDF SHA-256 mismatch detected before retypeset!")

            page_indices = parse_page_range_spec(job.config.page_range_spec, doc.page_count)
            page_index_set = set(page_indices)
            manual_regions = self.db.list_protection_regions(doc.id)
            manual_by_page: dict[int, list[ProtectionRegion]] = {}
            for r in manual_regions:
                manual_by_page.setdefault(r.page_index, []).append(r)

            stored_blocks = [
                b for b in self.db.list_blocks(doc.id) if b.page_index in page_index_set
            ]
            # Work on deep copies so DB blocks are not mutated if staging fails
            working_blocks = [b.model_copy(deep=True) for b in stored_blocks]
            issues: list[ValidationIssue] = []
            requires_reparse_or_retranslate = False

            protection_changed = doc.protection_revision != job.based_on_protection_revision

            for blk in working_blocks:
                blk.warnings = []
                saved_trans = blk.translation_text
                saved_manual = blk.manual_translation_text

                if protection_changed and isinstance(self.pdf_adapter, NativePreservingAdapter):
                    self.pdf_adapter._apply_protection_and_masking(
                        blk,
                        manual_by_page.get(blk.page_index, []),
                        job.config,
                    )
                    blk.translation_text = saved_trans
                    blk.manual_translation_text = saved_manual
                    if blk.status != BlockStatus.PROTECTED_SKIP:
                        if saved_manual is not None:
                            blk.status = BlockStatus.MANUALLY_EDITED
                        elif saved_trans is not None:
                            blk.status = BlockStatus.TRANSLATED
                        else:
                            requires_reparse_or_retranslate = True
                            blk.status = BlockStatus.FALLBACK_ORIGINAL
                            blk.warnings.append("UNTRANSLATED_AFTER_PROTECTION_CHANGE")
                            issues.append(
                                ValidationIssue(
                                    id=f"iss-{uuid.uuid4().hex[:10]}",
                                    job_id=job.id,
                                    document_id=doc.id,
                                    page_index=blk.page_index,
                                    block_id=blk.id,
                                    bbox=blk.bbox,
                                    code="UNTRANSLATED_AFTER_PROTECTION_CHANGE",
                                    severity=IssueSeverity.WARNING,
                                    message="保護領域の変更により翻訳対象となったブロックに訳文が存在しないため、LLM再推論を行わず原文を保持しました。",
                                    fallback_applied=True,
                                )
                            )

                if blk.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED):
                    eff = blk.effective_translation
                    if not eff:
                        blk.status = BlockStatus.FALLBACK_ORIGINAL
                        continue
                    val_res = validate_translation_output(
                        source_text=blk.source_text,
                        masked_source_text=blk.masked_source_text or blk.source_text,
                        translated_text=eff,
                        protected_items=blk.protected_items,
                        finish_reason="stop",
                    )
                    if blk.status == BlockStatus.MANUALLY_EDITED:
                        blk.manual_validation_errors = list(val_res.error_codes)
                        blk.manual_validation_warnings = list(val_res.warning_codes)

                    if not val_res.is_valid or val_res.findings:
                        if not allow_validation_warnings:
                            codes = [f.code for f in val_res.findings]
                            raise ValueError(
                                f"RetypesetValidationFailed: Block '{blk.id}' has validation findings {codes}"
                            )
                        for f in val_res.findings:
                            blk.warnings.append(f.code)
                            issues.append(
                                ValidationIssue(
                                    id=f"iss-{uuid.uuid4().hex[:10]}",
                                    job_id=job.id,
                                    document_id=doc.id,
                                    page_index=blk.page_index,
                                    block_id=blk.id,
                                    bbox=blk.bbox,
                                    code=f.code,
                                    severity=f.severity,
                                    message=f.message,
                                    details=f.details,
                                    fallback_applied=(not val_res.is_valid and blk.status != BlockStatus.MANUALLY_EDITED),
                                )
                            )
                        if not val_res.is_valid and blk.status != BlockStatus.MANUALLY_EDITED:
                            blk.status = BlockStatus.FALLBACK_ORIGINAL

            next_rev = job.retypeset_revision + 1
            staging_dir = (
                self.config.runtime_dir / "working" / f"{job.id}_retypeset_rev{next_rev}"
            )
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            if fail_stage_for_test:
                raise RuntimeError("Simulated typesetting failure during retypeset staging")

            staging_mono = (
                staging_dir / f"{doc.id}.ja.pdf" if job.config.output_mono_pdf else None
            )
            staging_dual = (
                staging_dir / f"{doc.id}.dual.pdf" if job.config.output_dual_pdf else None
            )

            working_blocks, typeset_issues = self.pdf_adapter.typeset_and_export(
                document=doc,
                blocks=working_blocks,
                page_indices=page_indices,
                mono_output_path=staging_mono,
                dual_output_path=staging_dual,
                job_id=job.id,
                job_config=job.config,
                manual_regions=manual_regions,
            )
            issues.extend(typeset_issues)

            visual_reports = []
            if staging_mono and staging_mono.exists():
                v_reps = self.pdf_adapter.verify_preservation(
                    document=doc,
                    output_pdf_path=staging_mono,
                    blocks=working_blocks,
                    page_indices=page_indices,
                    manual_regions=manual_regions,
                    is_roundtrip_only=job.config.only_roundtrip_preservation,
                )
                for rep in v_reps:
                    visual_reports.append(
                        {
                            "page_index": rep.page_index,
                            "display_page": rep.page_index + 1,
                            "all_protected_regions_preserved": rep.all_protected_regions_preserved,
                            "non_translated_background_diff_ratio": round(
                                rep.non_translated_background_diff_ratio, 6
                            ),
                            "full_page_diff_ratio": round(rep.full_page_diff_ratio, 6),
                            "semantic_accuracy_guaranteed": rep.semantic_accuracy_guaranteed,
                            "regions": [
                                {
                                    "region_id": r.region_id,
                                    "bbox_pt": r.bbox_pt,
                                    "max_channel_diff": r.max_channel_diff,
                                    "mean_abs_diff": round(r.mean_abs_diff, 6),
                                    "differing_pixel_ratio": round(r.differing_pixel_ratio, 6),
                                    "passed": r.passed,
                                    "note": r.note,
                                }
                                for r in rep.protected_region_results
                            ],
                        }
                    )
                    if not rep.all_protected_regions_preserved:
                        failing_regs = [
                            {
                                "region_id": r.region_id,
                                "bbox_pt": r.bbox_pt,
                                "differing_pixel_ratio": round(r.differing_pixel_ratio, 6),
                                "max_channel_diff": r.max_channel_diff,
                            }
                            for r in rep.protected_region_results
                            if not r.passed
                        ]
                        first_reg_id = failing_regs[0]["region_id"] if failing_regs else None
                        first_bbox = failing_regs[0]["bbox_pt"] if failing_regs else None
                        issues.append(
                            ValidationIssue(
                                id=f"iss-{uuid.uuid4().hex[:10]}",
                                job_id=job.id,
                                document_id=doc.id,
                                page_index=rep.page_index,
                                block_id=first_reg_id
                                if (first_reg_id and ":block-" in str(first_reg_id))
                                else None,
                                bbox=first_bbox,
                                code="VISUAL_PRESERVATION_DIFF_DETECTED",
                                severity=IssueSeverity.WARNING,
                                message=f"ページ {rep.page_index + 1} の保護領域（{first_reg_id or '背景'}）に視覚差分が検出されました。",
                                details={
                                    "background_diff_ratio": round(
                                        rep.non_translated_background_diff_ratio, 6
                                    ),
                                    "failing_regions": failing_regs,
                                },
                            )
                        )

            original_intact = verify_original_unchanged(doc.safe_storage_path, doc.sha256_hash)
            if not original_intact:
                raise RuntimeError("CRITICAL: Original PDF hash changed during retypeset!")

            applied_revisions = {
                b.id: {
                    "source": (
                        "manual"
                        if b.manual_translation_text is not None
                        else ("model" if b.translation_text is not None else "original")
                    ),
                    "edit_revision": b.edit_revision,
                    "status": b.status.value,
                }
                for b in working_blocks
            }

            prov = dict(job.execution_provenance or {})
            prov["last_retypeset_revision"] = next_rev
            prov["last_retypeset_llm_calls_made"] = 0
            prov["manual_translation_blocks"] = sum(
                1 for b in working_blocks if b.status == BlockStatus.MANUALLY_EDITED
            )
            prov["protected_skip_blocks"] = sum(
                1 for b in working_blocks if b.status == BlockStatus.PROTECTED_SKIP
            )
            prov["fallback_original_blocks"] = sum(
                1 for b in working_blocks if b.status == BlockStatus.FALLBACK_ORIGINAL
            )
            job.execution_provenance = prov

            staging_report = staging_dir / f"{doc.id}.report.json"
            report_payload = {
                "job_id": job.id,
                "document_id": doc.id,
                "original_filename": doc.original_filename,
                "original_sha256": doc.sha256_hash,
                "original_hash_unchanged": original_intact,
                "is_mock_translation": job.is_mock_run,
                "only_roundtrip_preservation": job.config.only_roundtrip_preservation,
                "retypeset_revision": next_rev,
                "based_on_edit_revision": doc.edit_revision,
                "based_on_protection_revision": doc.protection_revision,
                "applied_block_revisions": applied_revisions,
                "execution_provenance": job.execution_provenance,
                "llm_calls_made": 0,
                "total_pages_processed": len(page_indices),
                "total_blocks": len(working_blocks),
                "translated_blocks": sum(
                    1
                    for b in working_blocks
                    if b.status in (BlockStatus.TRANSLATED, BlockStatus.MANUALLY_EDITED)
                ),
                "protected_blocks": sum(
                    1 for b in working_blocks if b.status == BlockStatus.PROTECTED_SKIP
                ),
                "fallback_original_blocks": sum(
                    1 for b in working_blocks if b.status == BlockStatus.FALLBACK_ORIGINAL
                ),
                "validation_issues_count": len(issues),
                "validation_issues": [i.model_dump() for i in issues],
                "visual_preservation_reports": visual_reports,
            }
            staging_report.write_text(
                json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # Staging succeeded completely -> atomically promote artifacts to outputs/{job.id}/
            final_dir = self.config.runtime_dir / "outputs" / job.id
            final_dir.mkdir(parents=True, exist_ok=True)
            new_artifacts: list[ExportArtifact] = []

            if staging_mono and staging_mono.exists():
                final_mono = final_dir / f"{doc.id}.ja.pdf"
                shutil.copy2(staging_mono, final_mono)
                art_type = (
                    "roundtrip_pdf"
                    if job.config.only_roundtrip_preservation
                    else "mono_ja_pdf"
                )
                new_artifacts.append(
                    ExportArtifact(
                        id=f"art-{uuid.uuid4().hex[:10]}",
                        job_id=job.id,
                        document_id=doc.id,
                        artifact_type=art_type,
                        file_path=str(final_mono),
                        sha256_hash=compute_sha256(final_mono),
                        file_size_bytes=final_mono.stat().st_size,
                        is_finalized=True,
                        is_mock_translation=job.is_mock_run,
                    )
                )

            if staging_dual and staging_dual.exists():
                final_dual = final_dir / f"{doc.id}.dual.pdf"
                shutil.copy2(staging_dual, final_dual)
                new_artifacts.append(
                    ExportArtifact(
                        id=f"art-{uuid.uuid4().hex[:10]}",
                        job_id=job.id,
                        document_id=doc.id,
                        artifact_type="dual_bilingual_pdf",
                        file_path=str(final_dual),
                        sha256_hash=compute_sha256(final_dual),
                        file_size_bytes=final_dual.stat().st_size,
                        is_finalized=True,
                        is_mock_translation=job.is_mock_run,
                    )
                )

            final_report = final_dir / f"{doc.id}.report.json"
            shutil.copy2(staging_report, final_report)
            new_artifacts.append(
                ExportArtifact(
                    id=f"art-{uuid.uuid4().hex[:10]}",
                    job_id=job.id,
                    document_id=doc.id,
                    artifact_type="validation_report_json",
                    file_path=str(final_report),
                    sha256_hash=compute_sha256(final_report),
                    file_size_bytes=final_report.stat().st_size,
                    is_finalized=True,
                    is_mock_translation=job.is_mock_run,
                )
            )

            self.db.save_blocks(doc.id, working_blocks)
            self.db.replace_validation_issues_for_job(job.id, issues)
            self.db.replace_export_artifacts_for_job(job.id, new_artifacts)

            if job.can_transition_to(JobState.TYPESETTING):
                job.transition_to(JobState.TYPESETTING, "Stage 10-12: 手動修正のPDF再組版（LLM呼び出しなし）")

            job.retypeset_revision = next_rev
            job.based_on_edit_revision = doc.edit_revision
            job.based_on_protection_revision = doc.protection_revision
            job.applied_block_revisions = applied_revisions
            job.original_hash_verified = original_intact
            job.warning_count = len(issues)

            has_fallbacks = any(
                b.status == BlockStatus.FALLBACK_ORIGINAL for b in working_blocks
            )
            if issues or has_fallbacks:
                job.transition_to(
                    JobState.COMPLETED_WITH_WARNINGS,
                    f"再組版完了（rev {next_rev}・要確認警告 {len(issues)} 件）",
                )
            else:
                job.transition_to(
                    JobState.COMPLETED,
                    f"再組版完了（rev {next_rev}・LLM呼び出し0回）",
                )

            self.db.save_job(job)
            return {
                "job": job,
                "artifacts": new_artifacts,
                "issues": issues,
                "requires_reparse_or_retranslate": requires_reparse_or_retranslate,
                "llm_calls_made": 0,
            }
        finally:
            if staging_dir and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            with self._active_lock:
                self._active_jobs.discard(job_id)

    def _translate_and_validate_single_block(
        self,
        job: Job,
        doc: Document,
        block: Block,
        translator: TranslationAdapter,
        glossary_entries: list[GlossaryEntry],
        context_hint: str | None,
        history_window: list[HistoryPair],
        issues: list[ValidationIssue],
    ) -> tuple[str, int]:
        """Translate a single block with cache lookup, 10-point validation,
        up to 2 retries (including sentence-level re-splitting), and strict
        fallback-to-original if still invalid.
        """
        llm_calls = 0

        def _counted(**kw: Any) -> Any:
            nonlocal llm_calls
            llm_calls += 1
            return translator.translate_segment(**kw)

        masked_src = block.masked_source_text or block.source_text
        cache_key = compute_translation_cache_key(
            masked_source_text=masked_src,
            model_name=job.config.model_name,
            quantization=job.config.quantization,
            prompt_version=translator.prompt_version,
            source_lang=job.config.source_lang,
            target_lang=job.config.target_lang,
            form=job.config.form,
            glossary_entries=glossary_entries,
            context=context_hint,
            history=history_window,
            protected_items=block.protected_items,
            adapter_version=translator.adapter_version,
        )

        cached = self.db.get_cached_translation(cache_key)
        if cached is not None:
            val_res = validate_translation_output(
                source_text=block.source_text,
                masked_source_text=masked_src,
                translated_text=cached["model_output_text"],
                protected_items=block.protected_items,
                finish_reason="stop",
            )
            if val_res.is_valid:
                block.translation_text = cached["model_output_text"]
                block.status = BlockStatus.TRANSLATED
                block.translation_provenance = "cache_hit"
                return "cache_hit", 0

        max_attempts = 3  # Initial attempt + up to 2 retries (Section 11)
        last_val_res = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                job.retry_count += 1

            # On retry 2+, if resplit was recommended or paragraph is long, translate sentence by sentence
            use_sentence_split = (
                attempt >= 2
                and (
                    (last_val_res is not None and last_val_res.recommend_resplit)
                    or len(masked_src) > 450
                )
            )

            try:
                if use_sentence_split:
                    sentences = split_paragraph_preserving_placeholders(
                        masked_src, max_segment_chars=250
                    )
                    seg_outputs: list[str] = []
                    combined_finish = "stop"
                    for s_seg in sentences:
                        resp = _counted(
                            source_text=s_seg,
                            source_lang=job.config.source_lang,
                            target_lang=job.config.target_lang,
                            form=job.config.form,
                            context=context_hint if attempt == 1 else None,
                            history=None,
                            glossary_entries=glossary_entries,
                            context_budget_tokens=job.config.context_length,
                        )
                        seg_outputs.append(resp.translated_text)
                        if resp.finish_reason == "length":
                            combined_finish = "length"
                    candidate_text = "".join(seg_outputs)
                    finish_reason = combined_finish
                else:
                    try:
                        resp = _counted(
                            source_text=masked_src,
                            source_lang=job.config.source_lang,
                            target_lang=job.config.target_lang,
                            form=job.config.form,
                            context=context_hint if attempt == 1 else None,
                            history=history_window if attempt == 1 else None,
                            glossary_entries=glossary_entries,
                            context_budget_tokens=job.config.context_length,
                        )
                        candidate_text = resp.translated_text
                        finish_reason = resp.finish_reason
                    except ValueError:
                        # Single paragraph exceeded token budget -> split into sentences
                        sentences = split_paragraph_preserving_placeholders(
                            masked_src, max_segment_chars=300
                        )
                        seg_outputs = []
                        combined_finish = "stop"
                        for s_seg in sentences:
                            r_seg = _counted(
                                source_text=s_seg,
                                source_lang=job.config.source_lang,
                                target_lang=job.config.target_lang,
                                form=job.config.form,
                                context=None,
                                history=None,
                                glossary_entries=glossary_entries,
                                context_budget_tokens=job.config.context_length,
                            )
                            seg_outputs.append(r_seg.translated_text)
                            if r_seg.finish_reason == "length":
                                combined_finish = "length"
                        candidate_text = "".join(seg_outputs)
                        finish_reason = combined_finish
            except TranslationConnectionError:
                raise

            val_res = validate_translation_output(
                source_text=block.source_text,
                masked_source_text=masked_src,
                translated_text=candidate_text,
                protected_items=block.protected_items,
                finish_reason=finish_reason,
            )
            last_val_res = val_res

            if val_res.is_valid:
                block.translation_text = candidate_text
                block.status = BlockStatus.TRANSLATED
                self.db.put_cached_translation(
                    cache_key=cache_key,
                    model_output_text=candidate_text,
                    model_name=job.config.model_name,
                    quantization=job.config.quantization,
                    is_mock=translator.is_mock,
                )
                # Record any non-fatal warnings (e.g., numeric formatting notice)
                for w_find in val_res.findings:
                    block.warnings.append(w_find.code)
                    issues.append(
                        ValidationIssue(
                            id=f"iss-{uuid.uuid4().hex[:10]}",
                            job_id=job.id,
                            document_id=doc.id,
                            page_index=block.page_index,
                            block_id=block.id,
                            bbox=block.bbox,
                            code=w_find.code,
                            severity=w_find.severity,
                            message=w_find.message,
                            details=w_find.details,
                            fallback_applied=False,
                        )
                    )
                block.translation_provenance = "llm_inference"
                return "llm_inference", llm_calls

        # All attempts (initial + 2 retries) failed validation!
        # Section 11: Never delete placeholders to fake repair. Keep original source and record issue.
        block.translation_text = None
        block.status = BlockStatus.FALLBACK_ORIGINAL
        if last_val_res:
            for f in last_val_res.findings:
                block.warnings.append(f.code)
                issues.append(
                    ValidationIssue(
                        id=f"iss-{uuid.uuid4().hex[:10]}",
                        job_id=job.id,
                        document_id=doc.id,
                        page_index=block.page_index,
                        block_id=block.id,
                        bbox=block.bbox,
                        code=f.code,
                        severity=f.severity,
                        message=f"{f.message}（最大再試行回数超過のため原文を保持しました）",
                        details=f.details,
                        fallback_applied=True,
                    )
                )
        block.translation_provenance = "fallback_original"
        return "fallback_original", llm_calls
