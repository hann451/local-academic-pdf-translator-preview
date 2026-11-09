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

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    GlossaryEntry,
    JobState,
    ProtectionMode,
    TranslationJobConfig,
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
