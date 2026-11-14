"""Phase 3 Backend API, Auth Boundary, Streaming, Glossary CRUD, and Zero-LLM Retypeset Tests.

Covers Section 3, 4, 5, 6, 7, 8, 9, 11 of the Phase 3 specification:
- Session token lifecycle (`GET /api/auth/session`, `POST /api/auth/rotate`, `POST /api/auth/revoke`),
  missing/invalid token rejection (`401`), query-param token rejection (`400`), and Origin check (`403`).
- Path traversal and invalid `artifact_type` rejection (`400`/`403`/`404`).
- Authenticated PDF & artifact streaming (`200 OK` and `206 Partial Content` Range requests).
- Pre-translation parsing (`POST /api/documents/{id}/parse`), manual protection region CRUD,
  `protection_source` (`auto` vs `manual` vs `none`), and genuine 6-kind block detection.
- Zero-LLM `retypeset` (`POST /api/jobs/{id}/retypeset`) verified with a spy adapter (`0` LLM calls),
  confirming manual edits appear in the regenerated PDF while `translation_text` remains intact.
- Revision conflict (`409`), validation failure gate (`422`), and staging failure preservation (`500`)
  ensuring previous finalized artifacts are never corrupted on failure.
- Glossary CRUD (`GET`/`POST`/`PUT`/`DELETE /api/glossaries`) and 1,200-entry glossary test.
"""

from __future__ import annotations

from pathlib import Path
import json
import uuid

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    GlossaryEntry,
    IssueSeverity,
    JobState,
    ProtectionMode,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged
from backend.translation.adapters.base import (
    TranslationAdapter,
    TranslationAdapterResponse,
)
from backend.translation.adapters.mock_adapter import MockPlamoAdapter
from backend.translation.prompts.pfml3 import HistoryPair

pytestmark = pytest.mark.integration


class SpyTranslationAdapter(TranslationAdapter):
    """Wraps MockPlamoAdapter and counts every translate_segment invocation."""

    adapter_version = "spy-mock-1.0"
    prompt_version = "pfml3-v1"
    is_mock = True

    def __init__(self) -> None:
        self.inner = MockPlamoAdapter()
        self.call_count = 0

    def check_health(self) -> dict[str, str | bool | None]:
        return self.inner.check_health()

    def translate_segment(
        self,
        source_text: str,
        source_lang: str = "en",
        target_lang: str = "ja",
        form: str = "plain",
        context: str | None = None,
        history: list[HistoryPair] | None = None,
        glossary_entries: list[GlossaryEntry] | None = None,
        context_budget_tokens: int = 8192,
    ) -> TranslationAdapterResponse:
        self.call_count += 1
        return self.inner.translate_segment(
            source_text=source_text,
            source_lang=source_lang,
            target_lang=target_lang,
            form=form,
            context=context,
            history=history,
            glossary_entries=glossary_entries,
            context_budget_tokens=context_budget_tokens,
        )


def test_auth_session_lifecycle_origin_and_query_token_rejection(
    isolated_config: AppConfig,
) -> None:
    app = create_app(config=isolated_config)
    client = TestClient(app)

    # 1. Calling protected API without token must be rejected with 401
    no_tok = client.get("/api/diagnostics")
    assert no_tok.status_code == 401

    # 2. Bootstrap session token via GET /api/auth/session
    sess_resp = client.get(
        "/api/auth/session",
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert sess_resp.status_code == 200
    token1 = sess_resp.json()["token"]
    assert len(token1) >= 24

    # 3. Passing token in URL query string must be rejected with 400
    query_tok = client.get(
        f"/api/diagnostics?token={token1}",
        headers={"X-PaperBridge-Token": token1},
    )
    assert query_tok.status_code == 400

    # 4. Disallowed Origin rejected with 403 even with valid token
    bad_origin = client.get(
        "/api/diagnostics",
        headers={
            "Origin": "http://evil.example.com",
            "X-PaperBridge-Token": token1,
        },
    )
    assert bad_origin.status_code == 403

    # 5. Rotate token -> old token immediately rejected (401), new token accepted (200)
    rot_resp = client.post(
        "/api/auth/rotate",
        headers={"X-PaperBridge-Token": token1},
    )
    assert rot_resp.status_code == 200
    token2 = rot_resp.json()["token"]
    assert token2 != token1

    assert (
        client.get("/api/diagnostics", headers={"X-PaperBridge-Token": token1}).status_code
        == 401
    )
    assert (
        client.get("/api/diagnostics", headers={"X-PaperBridge-Token": token2}).status_code
        == 200
    )

    # 6. Revoke token -> token2 rejected (401), session bootstrap requires reissue=true
    rev_resp = client.post(
        "/api/auth/revoke",
        headers={"X-PaperBridge-Token": token2},
    )
    assert rev_resp.status_code == 200
    assert rev_resp.json()["revoked"] is True
    assert (
        client.get("/api/diagnostics", headers={"X-PaperBridge-Token": token2}).status_code
        == 401
    )
    assert client.get("/api/auth/session").status_code == 401

    reissue_resp = client.get("/api/auth/session?reissue=true")
    assert reissue_resp.status_code == 200
    token3 = reissue_resp.json()["token"]
    assert (
        client.get("/api/diagnostics", headers={"X-PaperBridge-Token": token3}).status_code
        == 200
    )


def test_pdf_streaming_range_and_path_traversal_protection(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    app = create_app(config=isolated_config, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    pdf_path = generated_pdfs["two_col_figures_tables_2p"]
    with open(pdf_path, "rb") as f:
        up_resp = client.post(
            "/api/documents/upload",
            files={"file": ("two_col.pdf", f, "application/pdf")},
            headers=headers,
        )
    assert up_resp.status_code == 200
    doc_id = up_resp.json()["id"]

    # Unauthenticated PDF request rejected with 401
    assert client.get(f"/api/documents/{doc_id}/pdf").status_code == 401

    # Full PDF fetch (200 OK)
    full_resp = client.get(f"/api/documents/{doc_id}/pdf", headers=headers)
    assert full_resp.status_code == 200
    assert full_resp.headers["content-type"] == "application/pdf"
    assert full_resp.content.startswith(b"%PDF-")
    assert len(full_resp.content) == pdf_path.stat().st_size

    # HTTP Range fetch (206 Partial Content) for PDF.js chunked loading
    range_resp = client.get(
        f"/api/documents/{doc_id}/pdf",
        headers={**headers, "Range": "bytes=0-63"},
    )
    assert range_resp.status_code == 206
    assert len(range_resp.content) == 64
    assert range_resp.content == full_resp.content[:64]

    # Invalid ID / path traversal rejected with 400/404
    bad_doc = client.get("/api/documents/..%2F..%2Fetc/pdf", headers=headers)
    assert bad_doc.status_code in (400, 404)
    bad_id = client.get("/api/documents/doc$invalid!/pdf", headers=headers)
    assert bad_id.status_code == 400


def test_parse_blocks_protection_source_and_six_block_kinds(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Verify Screen 2 pre-translation block parsing detects all 6 display kinds
    (paragraph, heading, isolated_formula, figure, table, reference) and tracks
    `protection_source` ('auto' vs 'manual' vs 'none').
    """
    app = create_app(config=isolated_config, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    # Upload 2-page two-column PDF (contains heading, paragraph, figure, table, reference)
    with open(generated_pdfs["two_col_figures_tables_2p"], "rb") as f:
        doc2 = client.post(
            "/api/documents/upload",
            files={"file": ("two_col.pdf", f, "application/pdf")},
            headers=headers,
        ).json()

    # Upload 1-column math PDF (contains isolated_formula)
    with open(generated_pdfs["single_col_math"], "rb") as f:
        doc1 = client.post(
            "/api/documents/upload",
            files={"file": ("math.pdf", f, "application/pdf")},
            headers=headers,
        ).json()

    blocks2 = client.post(f"/api/documents/{doc2['id']}/parse", headers=headers).json()
    blocks1 = client.post(f"/api/documents/{doc1['id']}/parse", headers=headers).json()

    detected_kinds = {b["kind"] for b in blocks2} | {b["kind"] for b in blocks1}
    assert {
        BlockKind.PARAGRAPH.value,
        BlockKind.HEADING.value,
        BlockKind.ISOLATED_FORMULA.value,
        BlockKind.FIGURE.value,
        BlockKind.TABLE.value,
        BlockKind.REFERENCE.value,
    } <= detected_kinds

    # Add a manual protection region on doc1 page 0 and re-parse -> verify protection_source == "manual"
    reg_resp = client.post(
        f"/api/documents/{doc1['id']}/protection-regions",
        json={
            "page_index": 0,
            "bbox": [60.0, 50.0, 550.0, 90.0],
            "mode": ProtectionMode.DO_NOT_TRANSLATE.value,
            "note": "Manual header protection",
        },
        headers=headers,
    )
    assert reg_resp.status_code == 200
    reg_id = reg_resp.json()["id"]

    blocks1_after = client.post(f"/api/documents/{doc1['id']}/parse", headers=headers).json()
    manual_protected = [b for b in blocks1_after if b["protection_source"] == "manual"]
    auto_protected = [b for b in blocks1_after if b["protection_source"] == "auto"]
    translatable = [b for b in blocks1_after if b["protection_source"] == "none"]
    assert len(manual_protected) >= 1
    assert len(auto_protected) >= 1
    assert len(translatable) >= 1

    # Delete manual protection region
    del_resp = client.delete(
        f"/api/documents/{doc1['id']}/protection-regions/{reg_id}",
        headers=headers,
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True


def test_zero_llm_retypeset_reflects_manual_edit_and_preserves_artifacts_on_failure(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 7 & 11 core acceptance test:
    1. Run mock translation job with SpyTranslationAdapter.
    2. Save manual edit on a translated block (preserving `translation_text` untouched).
    3. Call `POST /api/jobs/{job_id}/retypeset`:
       - Verify 0 LLM calls occurred (`spy.call_count` unchanged and `llm_calls_made == 0`).
       - Verify manual translation string is embedded in the regenerated PDF.
       - Verify artifact SHA-256 updated and `retypeset_revision == 1`.
    4. Verify `expected_edit_revision` mismatch returns 409 Conflict.
    5. Verify `fail_stage_for_test=True` returns 500 and preserves rev-1 finalized artifacts intact.
    6. Verify invalid manual edit with `allow_validation_warnings=False` returns 422 without altering rev-1 artifacts.
    """
    spy = SpyTranslationAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    src_pdf = generated_pdfs["single_col_math"]
    orig_sha = compute_sha256(src_pdf)

    with open(src_pdf, "rb") as f:
        doc = client.post(
            "/api/documents/upload",
            files={"file": ("single_col_math.pdf", f, "application/pdf")},
            headers=headers,
        ).json()
    doc_id = doc["id"]

    # 1. Start and complete initial job
    job_resp = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    )
    assert job_resp.status_code == 200
    job = job_resp.json()
    job_id = job["id"]
    assert job["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)

    calls_after_initial_job = spy.call_count
    assert calls_after_initial_job >= 1

    # Download initial mono_ja_pdf artifact and record its hash
    art_list_0 = client.get(f"/api/jobs/{job_id}/artifacts", headers=headers).json()
    mono_art_0 = next(a for a in art_list_0 if a["artifact_type"] == "mono_ja_pdf")
    sha_before_retypeset = mono_art_0["sha256_hash"]

    # 2. Pick a translated block and apply a manual edit
    blocks = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()
    target_blk = next(b for b in blocks if b["status"] == BlockStatus.TRANSLATED.value and b["semantic_category"] == "body")
    original_model_translation = target_blk["translation_text"]
    custom_manual_phrase = "手動編集によるカスタム再組版検証テキスト"

    edit_resp = client.put(
        f"/api/documents/{doc_id}/blocks/{target_blk['id']}/manual-edit",
        json={
            "manual_translation_text": custom_manual_phrase,
            "expected_edit_revision": target_blk["edit_revision"],
        },
        headers=headers,
    )
    assert edit_resp.status_code == 200
    edited_blk = edit_resp.json()
    assert edited_blk["status"] == BlockStatus.MANUALLY_EDITED.value
    assert edited_blk["translation_text"] == original_model_translation
    assert edited_blk["manual_translation_text"] == custom_manual_phrase
    assert edited_blk["edit_revision"] == 1

    # Stale expected_edit_revision on block edit must return 409 Conflict
    conflict_blk_resp = client.put(
        f"/api/documents/{doc_id}/blocks/{target_blk['id']}/manual-edit",
        json={
            "manual_translation_text": "古いリビジョンでの上書き試行",
            "expected_edit_revision": 0,
        },
        headers=headers,
    )
    assert conflict_blk_resp.status_code == 409

    # 3. Call POST /api/jobs/{job_id}/retypeset and verify 0 LLM calls
    retypeset_resp = client.post(
        f"/api/jobs/{job_id}/retypeset",
        json={"expected_edit_revision": 1, "allow_validation_warnings": True},
        headers=headers,
    )
    assert retypeset_resp.status_code == 200
    rt_data = retypeset_resp.json()
    assert rt_data["llm_calls_made"] == 0
    assert spy.call_count == calls_after_initial_job  # Zero additional LLM calls!
    assert rt_data["job"]["retypeset_revision"] == 1
    assert rt_data["job"]["based_on_edit_revision"] == 1
    assert (
        rt_data["job"]["applied_block_revisions"][target_blk["id"]]["source"] == "manual"
    )

    # Verify downloaded mono_ja_pdf has new SHA-256 and contains custom_manual_phrase
    dl_mono = client.get(
        f"/api/jobs/{job_id}/artifacts/mono_ja_pdf/download",
        headers=headers,
    )
    assert dl_mono.status_code == 200
    with pymupdf.open(stream=dl_mono.content, filetype="pdf") as pdf_doc:
        page_text = pdf_doc[0].get_text("text")
        print("DBGTEXT", repr(page_text), target_blk["page_index"], target_blk["id"], target_blk["semantic_category"])
        assert custom_manual_phrase in page_text

    mono_art_1 = next(a for a in rt_data["artifacts"] if a["artifact_type"] == "mono_ja_pdf")
    sha_rev1 = mono_art_1["sha256_hash"]
    assert sha_rev1 != sha_before_retypeset

    # 4. Stale expected_edit_revision on retypeset must return 409 Conflict
    stale_rt = client.post(
        f"/api/jobs/{job_id}/retypeset",
        json={"expected_edit_revision": 0},
        headers=headers,
    )
    assert stale_rt.status_code == 409

    # 5. Simulated failure during retypeset staging must return 500 AND preserve rev-1 artifacts intact
    fail_rt = client.post(
        f"/api/jobs/{job_id}/retypeset",
        json={"fail_stage_for_test": True},
        headers=headers,
    )
    assert fail_rt.status_code == 500
    art_after_fail = client.get(f"/api/jobs/{job_id}/artifacts", headers=headers).json()
    mono_after_fail = next(a for a in art_after_fail if a["artifact_type"] == "mono_ja_pdf")
    assert mono_after_fail["sha256_hash"] == sha_rev1
    assert compute_sha256(mono_after_fail["file_path"]) == sha_rev1

    # 6. Manual edit with missing placeholder + allow_validation_warnings=False returns 422 and preserves rev-1
    ph_blk = next(b for b in blocks if b["protected_items"])
    bad_edit = client.put(
        f"/api/documents/{doc_id}/blocks/{ph_blk['id']}/manual-edit",
        json={"manual_translation_text": "プレースホルダを消してしまった手動訳文。"},
        headers=headers,
    ).json()
    assert "PLACEHOLDER_MISSING" in bad_edit["manual_validation_errors"]

    reject_rt = client.post(
        f"/api/jobs/{job_id}/retypeset",
        json={"allow_validation_warnings": False},
        headers=headers,
    )
    assert reject_rt.status_code == 422
    art_after_422 = client.get(f"/api/jobs/{job_id}/artifacts", headers=headers).json()
    mono_after_422 = next(a for a in art_after_422 if a["artifact_type"] == "mono_ja_pdf")
    assert mono_after_422["sha256_hash"] == sha_rev1
    assert verify_original_unchanged(src_pdf, orig_sha)


def test_glossary_crud_and_1200_entries_scaling(
    isolated_config: AppConfig,
) -> None:
    """Section 8 & 11 acceptance test:
    Full Glossary CRUD + 1,200 entries creation/retrieval/update/deletion.
    """
    app = create_app(config=isolated_config, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    # Create glossary with 1,200 entries
    entries_1200 = [
        {"source": f"scientific_term_{i}", "target": f"学術専門用語_{i}", "notes": f"note_{i}"}
        for i in range(1200)
    ]
    create_resp = client.post(
        "/api/glossaries",
        json={"name": "1200 Terms Benchmark Glossary", "entries": entries_1200},
        headers=headers,
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    gls_id = created["id"]
    assert created["version"] == 1
    assert len(created["entries"]) == 1200

    # List glossaries
    list_resp = client.get("/api/glossaries", headers=headers)
    assert list_resp.status_code == 200
    assert any(g["id"] == gls_id for g in list_resp.json())

    # Get glossary by ID
    get_resp = client.get(f"/api/glossaries/{gls_id}", headers=headers)
    assert get_resp.status_code == 200
    assert len(get_resp.json()["entries"]) == 1200

    # Update glossary -> version increments to 2
    entries_updated = entries_1200[:10] + [
        {"source": "variational inference", "target": "変分推論", "notes": "updated"}
    ]
    upd_resp = client.put(
        f"/api/glossaries/{gls_id}",
        json={"name": "Updated Glossary", "entries": entries_updated},
        headers=headers,
    )
    assert upd_resp.status_code == 200
    updated = upd_resp.json()
    assert updated["version"] == 2
    assert updated["name"] == "Updated Glossary"
    assert len(updated["entries"]) == 11

    # Delete glossary
    del_resp = client.delete(f"/api/glossaries/{gls_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True
    assert client.get(f"/api/glossaries/{gls_id}", headers=headers).status_code == 404


def test_static_frontend_dist_serving_and_local_pdfjs_worker(
    isolated_config: AppConfig,
) -> None:
    """Section 4 & 10 acceptance test:
    Verify FastAPI static serving delivers `frontend/dist/index.html`, JS/CSS bundles,
    and local PDF.js worker (`pdf.worker.min-*.mjs`) without any external CDN references.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    dist_dir = repo_root / "frontend" / "dist"
    assert (dist_dir / "index.html").exists(), "Run `npm run build` in frontend/ first"

    app = create_app(config=isolated_config, frontend_dist_dir=dist_dir)
    client = TestClient(app)

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    html = root_resp.text
    assert "PaperBridge Local" in html
    assert "Content-Security-Policy" in html
    # Ensure no external CDN script/style URLs exist in index.html
    assert "https://cdn." not in html
    assert "https://unpkg.com" not in html
    assert "https://cdnjs." not in html

    # Verify bundled local PDF.js worker is served under /assets/
    worker_files = list((dist_dir / "assets").glob("pdf.worker*.mjs"))
    assert len(worker_files) == 1
    worker_resp = client.get(f"/assets/{worker_files[0].name}")
    assert worker_resp.status_code == 200
    assert len(worker_resp.content) > 100_000


def _create_retypeset_issues_pdf(pdf_path: Path) -> None:
    """Create a deterministic 1-page PDF with separated paragraphs and narrow `(a)` subcaption."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # Use Cambria / Times so inline math sub/super glyphs and normal English parse cleanly
    page.insert_text((72, 80), "Alpha inline math paragraph keeps x", fontsize=10.5, fontname="helv")
    page.insert_text((241.5, 77.0), "2", fontsize=7.0, fontname="helv")
    page.insert_text((246.5, 80), "and y", fontsize=10.5, fontname="helv")
    page.insert_text((273.0, 82.0), "i", fontsize=7.0, fontname="helv")
    page.insert_text((276.0, 80), "values inside normal prose.", fontsize=10.5, fontname="helv")

    page.insert_text((72, 160), "Beta existing overflow fallback block.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 240), "Gamma existing overlap fallback block.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 320), "Delta existing untranslated source error block.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 400), "Epsilon numeric mismatch 42 and overlap block.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 480), "(a)", fontsize=7.0, fontname="helv")
    doc.save(str(pdf_path))
    doc.close()


def _find_block_containing(blocks, substr: str):
    for blk in blocks:
        if substr in blk.source_text:
            return blk
    raise AssertionError(f"Expected block containing {substr!r}, got {[b.source_text for b in blocks]}")


def test_http_retypeset_preserves_existing_issues_adds_new_and_is_idempotent(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Covers Requirements A, B, C, E + non-re-evaluated & page-level issues + 0 LLM calls:
    - Existing FALLBACK_ORIGINAL warnings (TYPESETTING_OVERFLOW_FALLBACK,
      TRANSLATED_BLOCK_OVERLAP_FALLBACK, UNTRANSLATED_SOURCE_RETURNED[error],
      NUMERIC_VALUE_MISMATCH + TRANSLATED_BLOCK_OVERLAP_FALLBACK) are preserved.
    - New overflow fallback from newly re-evaluated TRANSLATED block is added.
    - Non-re-evaluated block/page issues (`block_id=None` and custom non-retypeset code) survive.
    - Inline math in TRANSLATED block remains intact; 0 LLM calls occur.
    - Running retypeset 2x/3x neither loses nor duplicates issues; blocks, `/issues`,
      `job.warning_count`, `state`, and `validation_report_json` stay consistent.
    """
    pdf_path = tmp_path / "retypeset_issue_preservation.pdf"
    _create_retypeset_issues_pdf(pdf_path)

    spy = SpyTranslationAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    doc = mgr.register_document(pdf_path, original_filename=pdf_path.name)
    parsed = mgr.parse_document_blocks(doc.id, TranslationJobConfig(use_mock_translator=True))

    b_math = _find_block_containing(parsed, "Alpha inline math")
    b_overflow = _find_block_containing(parsed, "Beta existing overflow")
    b_overlap = _find_block_containing(parsed, "Gamma existing overlap")
    b_untrans = _find_block_containing(parsed, "Delta existing untranslated")
    b_combo = _find_block_containing(parsed, "Epsilon numeric mismatch")
    b_narrow = _find_block_containing(parsed, "(a)")

    # Configure realistic Stage-1-like saved block states before retypeset
    b_math.status = BlockStatus.TRANSLATED
    b_math.translation_text = "Alpha 段落では x2 と yi を保持する。"
    b_math.translation_provenance = "llm_inference"
    b_math.warnings = ["CUSTOM_PREFLIGHT_STYLE_NOTICE"]

    b_overflow.status = BlockStatus.FALLBACK_ORIGINAL
    b_overflow.translation_text = "非常に長い既存の組版オーバーフロー訳文である。"
    b_overflow.translation_provenance = "fallback_original"
    b_overflow.warnings = ["TYPESETTING_OVERFLOW_FALLBACK"]

    b_overlap.status = BlockStatus.FALLBACK_ORIGINAL
    b_overlap.translation_text = "既存の重複ブロック訳文。"
    b_overlap.translation_provenance = "fallback_original"
    b_overlap.warnings = ["TRANSLATED_BLOCK_OVERLAP_FALLBACK"]

    b_untrans.status = BlockStatus.FALLBACK_ORIGINAL
    b_untrans.translation_text = None
    b_untrans.translation_provenance = "fallback_original"
    b_untrans.warnings = ["UNTRANSLATED_SOURCE_RETURNED"]

    b_combo.status = BlockStatus.FALLBACK_ORIGINAL
    b_combo.translation_text = "数値不一致 99 の重複ブロック。"
    b_combo.translation_provenance = "fallback_original"
    b_combo.warnings = ["NUMERIC_VALUE_MISMATCH", "TRANSLATED_BLOCK_OVERLAP_FALLBACK"]

    # Previously translated narrow subcaption `(a)` that overflows cleanly under exact bbox fit check
    b_narrow.status = BlockStatus.TRANSLATED
    b_narrow.translation_text = "(あ) 狭いサブキャプションの長文訳"
    b_narrow.translation_provenance = "llm_inference"
    b_narrow.warnings = []

    mgr.db.save_blocks(doc.id, parsed)

    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    initial_issues = [
        ValidationIssue(
            id="iss-custom-blk",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_math.page_index,
            block_id=b_math.id,
            bbox=b_math.bbox,
            code="CUSTOM_PREFLIGHT_STYLE_NOTICE",
            severity=IssueSeverity.WARNING,
            message="再組版で再評価されないスタイル注記。",
            fallback_applied=False,
        ),
        ValidationIssue(
            id="iss-page-audit",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=None,
            bbox=None,
            code="PAGE_FONT_AUDIT_WARNING",
            severity=IssueSeverity.WARNING,
            message="ページ単位の埋め込みフォント警告。",
            fallback_applied=False,
        ),
        ValidationIssue(
            id="iss-overflow",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_overflow.page_index,
            block_id=b_overflow.id,
            bbox=b_overflow.bbox,
            code="TYPESETTING_OVERFLOW_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="枠内収容不可のため原文保持。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-overlap",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_overlap.page_index,
            block_id=b_overlap.id,
            bbox=b_overlap.bbox,
            code="TRANSLATED_BLOCK_OVERLAP_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="重複回避のため原文保持。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-untrans",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_untrans.page_index,
            block_id=b_untrans.id,
            bbox=b_untrans.bbox,
            code="UNTRANSLATED_SOURCE_RETURNED",
            severity=IssueSeverity.ERROR,
            message="翻訳されず原文がそのまま返却されました。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-num",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_combo.page_index,
            block_id=b_combo.id,
            bbox=b_combo.bbox,
            code="NUMERIC_VALUE_MISMATCH",
            severity=IssueSeverity.WARNING,
            message="原文中の数値が変化しています: ['42']",
            fallback_applied=False,
        ),
        ValidationIssue(
            id="iss-combo-ov",
            job_id=job.id,
            document_id=doc.id,
            page_index=b_combo.page_index,
            block_id=b_combo.id,
            bbox=b_combo.bbox,
            code="TRANSLATED_BLOCK_OVERLAP_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="重複回避のため原文保持。",
            fallback_applied=True,
        ),
    ]
    mgr.db.replace_validation_issues_for_job(job.id, initial_issues)
    job.transition_to(JobState.PREFLIGHT)
    job.transition_to(JobState.PARSING)
    job.transition_to(JobState.TRANSLATING)
    job.transition_to(JobState.VALIDATING)
    job.transition_to(JobState.TYPESETTING)
    job.warning_count = len(initial_issues)
    job.transition_to(JobState.COMPLETED_WITH_WARNINGS)
    mgr.db.save_job(job)

    calls_before = spy.call_count

    # Run HTTP retypeset #1, #2, #3 cleanly and compare
    expected_signatures = {
        ("iss-custom-blk", b_math.id, "CUSTOM_PREFLIGHT_STYLE_NOTICE", "warning"),
        ("iss-page-audit", None, "PAGE_FONT_AUDIT_WARNING", "warning"),
        ("iss-overflow", b_overflow.id, "TYPESETTING_OVERFLOW_FALLBACK", "warning"),
        ("iss-overlap", b_overlap.id, "TRANSLATED_BLOCK_OVERLAP_FALLBACK", "warning"),
        ("iss-untrans", b_untrans.id, "UNTRANSLATED_SOURCE_RETURNED", "error"),
        ("iss-num", b_combo.id, "NUMERIC_VALUE_MISMATCH", "warning"),
        ("iss-combo-ov", b_combo.id, "TRANSLATED_BLOCK_OVERLAP_FALLBACK", "warning"),
    }

    for rev in (1, 2, 3):
        rt_resp = client.post(
            f"/api/jobs/{job.id}/retypeset",
            json={"allow_validation_warnings": True},
            headers=headers,
        )
        assert rt_resp.status_code == 200, rt_resp.text
        rt = rt_resp.json()
        assert rt["llm_calls_made"] == 0
        assert spy.call_count == calls_before
        assert rt["job"]["retypeset_revision"] == rev
        assert rt["job"]["state"] == JobState.COMPLETED_WITH_WARNINGS.value

        api_job = client.get(f"/api/jobs/{job.id}", headers=headers).json()
        api_issues = client.get(f"/api/jobs/{job.id}/issues", headers=headers).json()
        api_blocks = {
            b["id"]: b
            for b in client.get(f"/api/documents/{doc.id}/blocks", headers=headers).json()
        }
        dl_rep = json.loads(
            client.get(
                f"/api/jobs/{job.id}/artifacts/validation_report_json/download",
                headers=headers,
            ).content.decode("utf-8")
        )

        # Exact consistency across response, DB /issues, job.warning_count, and report.json
        assert len(api_issues) == 8, f"rev {rev}: got {[i['code'] for i in api_issues]}"
        assert api_job["warning_count"] == 8
        assert len(rt["issues"]) == 8
        assert dl_rep["validation_issues_count"] == 8
        assert [i["id"] for i in api_issues] == [i["id"] for i in rt["issues"]]
        assert [i["id"] for i in api_issues] == [i["id"] for i in dl_rep["validation_issues"]]

        # Existing 7 preserved verbatim (with original IDs) + 1 new on b_narrow
        got_sigs = {
            (i["id"], i["block_id"], i["code"], i["severity"]) for i in api_issues
        }
        assert expected_signatures <= got_sigs
        new_narrow = [i for i in api_issues if i["block_id"] == b_narrow.id]
        assert len(new_narrow) == 1
        assert new_narrow[0]["code"] == "TYPESETTING_OVERFLOW_FALLBACK"
        assert new_narrow[0]["fallback_applied"] is True

        # Check block statuses & warnings arrays
        assert api_blocks[b_math.id]["status"] == BlockStatus.TRANSLATED.value
        assert api_blocks[b_math.id]["warnings"] == ["CUSTOM_PREFLIGHT_STYLE_NOTICE"]
        assert api_blocks[b_overflow.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
        assert api_blocks[b_overflow.id]["warnings"] == ["TYPESETTING_OVERFLOW_FALLBACK"]
        assert api_blocks[b_overlap.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
        assert api_blocks[b_overlap.id]["warnings"] == ["TRANSLATED_BLOCK_OVERLAP_FALLBACK"]
        assert api_blocks[b_untrans.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
        assert api_blocks[b_untrans.id]["warnings"] == ["UNTRANSLATED_SOURCE_RETURNED"]
        assert api_blocks[b_combo.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
        assert set(api_blocks[b_combo.id]["warnings"]) == {
            "NUMERIC_VALUE_MISMATCH",
            "TRANSLATED_BLOCK_OVERLAP_FALLBACK",
        }
        assert api_blocks[b_narrow.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
        assert api_blocks[b_narrow.id]["warnings"] == ["TYPESETTING_OVERFLOW_FALLBACK"]

        # Verify mono PDF still preserves inline math subscript/superscript on Alpha block
        dl_mono = client.get(
            f"/api/jobs/{job.id}/artifacts/mono_ja_pdf/download", headers=headers
        )
        assert dl_mono.status_code == 200
        with pymupdf.open(stream=dl_mono.content, filetype="pdf") as out_pdf:
            rect = pymupdf.Rect(b_math.bbox)
            spans = [
                sp
                for b in out_pdf[0].get_text("dict", clip=rect).get("blocks", [])
                if b.get("type", 0) == 0
                for ln in b.get("lines", [])
                for sp in ln.get("spans", [])
            ]
            assert any(sp["text"] == "2" and sp["size"] < 8.5 for sp in spans)
            assert any(sp["text"] == "i" and sp["size"] < 8.5 for sp in spans)
            # Delta original English must remain in mono PDF untouched
            delta_txt = out_pdf[0].get_text("text", clip=pymupdf.Rect(b_untrans.bbox))
            assert "Delta" in delta_txt


def test_http_retypeset_clears_resolved_issues_on_manual_edit_and_keeps_unresolved(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Covers Requirements D, E:
    - Manual edit fixing UNTRANSLATED_SOURCE_RETURNED removes old error issue and updates job count.
    - Manual edit that still has unresolved NUMERIC_VALUE_MISMATCH keeps/updates only the
      unresolved warning while removing resolved TRANSLATED_BLOCK_OVERLAP_FALLBACK.
    - Fixing remaining blocks cleanly removes remaining issues and transitions job to `completed`.
    """
    pdf_path = tmp_path / "retypeset_resolve_issues.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=612, height=792)
    page.insert_text((72, 80), "First block untranslated failure source 10.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 160), "Second block overlap and value 42 check.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 240), "Third block overflow fallback check.", fontsize=10.0, fontname="helv")
    doc_pdf.save(str(pdf_path))
    doc_pdf.close()

    spy = SpyTranslationAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    doc = mgr.register_document(pdf_path, original_filename=pdf_path.name)
    parsed = mgr.parse_document_blocks(doc.id, TranslationJobConfig(use_mock_translator=True))
    b1 = _find_block_containing(parsed, "First block")
    b2 = _find_block_containing(parsed, "Second block")
    b3 = _find_block_containing(parsed, "Third block")

    b1.status = BlockStatus.FALLBACK_ORIGINAL
    b1.translation_text = None
    b1.warnings = ["UNTRANSLATED_SOURCE_RETURNED"]

    b2.status = BlockStatus.FALLBACK_ORIGINAL
    b2.translation_text = "二番目の値99。"
    b2.warnings = ["NUMERIC_VALUE_MISMATCH", "TRANSLATED_BLOCK_OVERLAP_FALLBACK"]

    b3.status = BlockStatus.FALLBACK_ORIGINAL
    b3.translation_text = "非常に長い三番目の訳文。"
    b3.warnings = ["TYPESETTING_OVERFLOW_FALLBACK"]

    mgr.db.save_blocks(doc.id, parsed)
    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    init_issues = [
        ValidationIssue(
            id="iss-b1-err",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b1.id,
            bbox=b1.bbox,
            code="UNTRANSLATED_SOURCE_RETURNED",
            severity=IssueSeverity.ERROR,
            message="原文がそのまま返却されました。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-b2-num",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b2.id,
            bbox=b2.bbox,
            code="NUMERIC_VALUE_MISMATCH",
            severity=IssueSeverity.WARNING,
            message="数値が不一致です: ['42']",
            fallback_applied=False,
        ),
        ValidationIssue(
            id="iss-b2-ov",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b2.id,
            bbox=b2.bbox,
            code="TRANSLATED_BLOCK_OVERLAP_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="重複により原文保持。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-b3-of",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b3.id,
            bbox=b3.bbox,
            code="TYPESETTING_OVERFLOW_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="組版オーバーフロー。",
            fallback_applied=True,
        ),
    ]
    mgr.db.replace_validation_issues_for_job(job.id, init_issues)
    job.transition_to(JobState.PREFLIGHT)
    job.transition_to(JobState.PARSING)
    job.transition_to(JobState.TRANSLATING)
    job.transition_to(JobState.VALIDATING)
    job.transition_to(JobState.TYPESETTING)
    job.warning_count = 4
    job.transition_to(JobState.COMPLETED_WITH_WARNINGS)
    mgr.db.save_job(job)

    # Phase 1: Fix b1 completely, edit b2 (fits cleanly, STILL wrong number 999!), leave b3 untouched
    assert client.put(
        f"/api/documents/{doc.id}/blocks/{b1.id}/manual-edit",
        json={"manual_translation_text": "最初のブロックは 10 である。", "expected_edit_revision": 0},
        headers=headers,
    ).status_code == 200
    assert client.put(
        f"/api/documents/{doc.id}/blocks/{b2.id}/manual-edit",
        json={"manual_translation_text": "二番目の値 999。", "expected_edit_revision": 0},
        headers=headers,
    ).status_code == 200

    rt1 = client.post(
        f"/api/jobs/{job.id}/retypeset",
        json={"allow_validation_warnings": True},
        headers=headers,
    )
    assert rt1.status_code == 200
    assert rt1.json()["llm_calls_made"] == 0
    assert rt1.json()["job"]["state"] == JobState.COMPLETED_WITH_WARNINGS.value
    assert rt1.json()["job"]["warning_count"] == 2

    issues_1 = client.get(f"/api/jobs/{job.id}/issues", headers=headers).json()
    blocks_1 = {
        b["id"]: b
        for b in client.get(f"/api/documents/{doc.id}/blocks", headers=headers).json()
    }
    assert blocks_1[b1.id]["status"] == BlockStatus.MANUALLY_EDITED.value
    assert blocks_1[b1.id]["warnings"] == []
    assert blocks_1[b2.id]["status"] == BlockStatus.MANUALLY_EDITED.value
    assert blocks_1[b2.id]["warnings"] == ["NUMERIC_VALUE_MISMATCH"]
    assert blocks_1[b3.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
    assert blocks_1[b3.id]["warnings"] == ["TYPESETTING_OVERFLOW_FALLBACK"]
    assert {(i["block_id"], i["code"]) for i in issues_1} == {
        (b2.id, "NUMERIC_VALUE_MISMATCH"),
        (b3.id, "TYPESETTING_OVERFLOW_FALLBACK"),
    }

    # Phase 2: Fix b2's number (42) and give b3 a short manual translation -> 0 issues & completed!
    assert client.put(
        f"/api/documents/{doc.id}/blocks/{b2.id}/manual-edit",
        json={"manual_translation_text": "二番目の値 42。", "expected_edit_revision": 1},
        headers=headers,
    ).status_code == 200
    assert client.put(
        f"/api/documents/{doc.id}/blocks/{b3.id}/manual-edit",
        json={"manual_translation_text": "三番目の短い訳。", "expected_edit_revision": 0},
        headers=headers,
    ).status_code == 200

    rt2 = client.post(
        f"/api/jobs/{job.id}/retypeset",
        json={"allow_validation_warnings": False},
        headers=headers,
    )
    assert rt2.status_code == 200
    assert rt2.json()["llm_calls_made"] == 0
    assert rt2.json()["job"]["state"] == JobState.COMPLETED.value
    assert rt2.json()["job"]["warning_count"] == 0
    assert rt2.json()["issues"] == []
    assert client.get(f"/api/jobs/{job.id}/issues", headers=headers).json() == []
    dl_rep2 = json.loads(
        client.get(
            f"/api/jobs/{job.id}/artifacts/validation_report_json/download",
            headers=headers,
        ).content.decode("utf-8")
    )
    assert dl_rep2["validation_issues_count"] == 0
    assert dl_rep2["validation_issues"] == []
    blocks_2 = client.get(f"/api/documents/{doc.id}/blocks", headers=headers).json()
    assert all(b["warnings"] == [] for b in blocks_2)


def test_http_retypeset_protection_change_and_unrelated_block_guard(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify:
    - Changing another block's protection does NOT overwrite existing UNTRANSLATED_SOURCE_RETURNED
      on an unrelated FALLBACK_ORIGINAL block.
    - Explicitly switching an existing FALLBACK_ORIGINAL block to `force_protect` clears its
      fallback issues cleanly.
    - Stale issues whose `block_id` shifted/disappeared after reparse do NOT leak onto an
      unrelated block.
    """
    pdf_path = tmp_path / "retypeset_prot_guard.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=612, height=792)
    page.insert_text((72, 80), "Sample Research Paper Title", fontsize=14.0, fontname="helv")
    page.insert_text((72, 140), "1. Introduction", fontsize=11.0, fontname="helv")
    page.insert_text((72, 200), "Alpha first paragraph block discusses the primary baseline model behavior in detail across multiple experiments.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 280), "Beta second block fallback source contains detailed quantitative results for the second baseline configuration.", fontsize=10.0, fontname="helv")
    page.insert_text((72, 360), "Gamma third block overflow describes the third baseline setup for evaluation.", fontsize=10.0, fontname="helv")
    doc_pdf.save(str(pdf_path))
    doc_pdf.close()

    spy = SpyTranslationAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="test-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "test-tok"}

    doc = mgr.register_document(pdf_path, original_filename=pdf_path.name)
    parsed = mgr.parse_document_blocks(doc.id, TranslationJobConfig(use_mock_translator=True))
    intro = _find_block_containing(parsed, "1. Introduction")
    b1 = _find_block_containing(parsed, "Alpha")
    b2 = _find_block_containing(parsed, "Beta")
    b3 = _find_block_containing(parsed, "Gamma")

    intro.status = BlockStatus.TRANSLATED
    intro.translation_text = "1. はじめに"
    b1.status = BlockStatus.TRANSLATED
    b1.translation_text = "アルファ第一段落は、複数の実験にわたる主要なベースラインモデルの挙動を詳細に議論する。"
    b2.status = BlockStatus.FALLBACK_ORIGINAL
    b2.translation_text = None
    b2.warnings = ["UNTRANSLATED_SOURCE_RETURNED"]
    b3.status = BlockStatus.FALLBACK_ORIGINAL
    b3.translation_text = "ガンマ長文オーバーフロー。"
    b3.warnings = ["TYPESETTING_OVERFLOW_FALLBACK"]
    mgr.db.save_blocks(doc.id, parsed)

    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=True))
    init_issues = [
        ValidationIssue(
            id="iss-b2-untrans",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b2.id,
            bbox=b2.bbox,
            code="UNTRANSLATED_SOURCE_RETURNED",
            severity=IssueSeverity.ERROR,
            message="原文がそのまま返却されました。",
            fallback_applied=True,
        ),
        ValidationIssue(
            id="iss-b3-of",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b3.id,
            bbox=b3.bbox,
            code="TYPESETTING_OVERFLOW_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="組版オーバーフロー。",
            fallback_applied=True,
        ),
        # Stale orphaned issue referencing b1.id with completely different old bbox (shifted after reparse)
        ValidationIssue(
            id="iss-stale-shifted",
            job_id=job.id,
            document_id=doc.id,
            page_index=0,
            block_id=b1.id,
            bbox=[400.0, 600.0, 520.0, 650.0],
            code="TYPESETTING_OVERFLOW_FALLBACK",
            severity=IssueSeverity.WARNING,
            message="古いブロックへの無関係な保持理由。",
            fallback_applied=True,
        ),
    ]
    mgr.db.replace_validation_issues_for_job(job.id, init_issues)
    job.transition_to(JobState.PREFLIGHT)
    job.transition_to(JobState.PARSING)
    job.transition_to(JobState.TRANSLATING)
    job.transition_to(JobState.VALIDATING)
    job.transition_to(JobState.TYPESETTING)
    job.warning_count = 3
    job.transition_to(JobState.COMPLETED_WITH_WARNINGS)
    mgr.db.save_job(job)

    # Protect b3 (`force_protect`) -> bumps doc.protection_revision, leaving b2 untouched!
    prot_resp = client.put(
        f"/api/documents/{doc.id}/blocks/{b3.id}/protection",
        json={"user_protection_override": "force_protect"},
        headers=headers,
    )
    assert prot_resp.status_code == 200

    rt = client.post(
        f"/api/jobs/{job.id}/retypeset",
        json={"allow_validation_warnings": True},
        headers=headers,
    )
    assert rt.status_code == 200
    assert rt.json()["llm_calls_made"] == 0

    issues = client.get(f"/api/jobs/{job.id}/issues", headers=headers).json()
    blocks = {
        b["id"]: b
        for b in client.get(f"/api/documents/{doc.id}/blocks", headers=headers).json()
    }
    # b2 must retain UNTRANSLATED_SOURCE_RETURNED (NOT overwritten with UNTRANSLATED_AFTER_PROTECTION_CHANGE)
    assert blocks[b2.id]["status"] == BlockStatus.FALLBACK_ORIGINAL.value
    assert blocks[b2.id]["warnings"] == ["UNTRANSLATED_SOURCE_RETURNED"]
    # b3 is now PROTECTED_SKIP and cleared of its old overflow fallback issue
    assert blocks[b3.id]["status"] == BlockStatus.PROTECTED_SKIP.value
    assert blocks[b3.id]["warnings"] == []
    # b1 never inherits stale shifted fallback reason
    assert blocks[b1.id]["warnings"] == []
    assert [(i["id"], i["block_id"], i["code"]) for i in issues] == [
        ("iss-b2-untrans", b2.id, "UNTRANSLATED_SOURCE_RETURNED")
    ]
    assert rt.json()["job"]["warning_count"] == 1
