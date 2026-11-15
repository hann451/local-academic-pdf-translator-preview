"""Integration tests for model API endpoints, downloads/cancel, active-job locks, and mismatch guards."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import TranslationJobConfig
from backend.jobs.manager import JobManager
from backend.models.registry import ModelDownloadManager
from tests.unit.test_model_registry_and_catalog import build_synthetic_gguf, build_temp_catalog

pytestmark = pytest.mark.integration


def _auth_headers(token: str = "test-tok") -> dict[str, str]:
    return {"X-PaperBridge-Token": token}


def test_models_api_browse_register_select_delete_and_mismatch_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"API_08B")
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"API_2B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    cfg = AppConfig(runtime_dir=tmp_path / "runtime", model_catalog_path=cat_path)
    cfg.ensure_directories()
    app = create_app(config=cfg, session_token="test-tok")
    client = TestClient(app)

    ext_dir = tmp_path / "外部 モデル (日本語 パス)"
    ext_dir.mkdir(parents=True)
    f08 = ext_dir / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    f08.write_bytes(bytes_08b)
    f2 = ext_dir / "plamo-3-translate-2b-Q4_K_M.gguf"
    f2.write_bytes(bytes_2b)

    # 1. Mock Windows OpenFileDialog via subprocess.run
    dialog_results = [str(f08), ""]

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        out = dialog_results.pop(0) if dialog_results else ""
        return subprocess.CompletedProcess(args=["powershell.exe"], returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("backend.api.app.subprocess.run", fake_subprocess_run)

    b1 = client.post("/api/models/browse-local-file", headers=_auth_headers())
    assert b1.status_code == 200
    assert b1.json() == {"canceled": False, "file_path": str(f08)}

    b2 = client.post("/api/models/browse-local-file", headers=_auth_headers())
    assert b2.status_code == 200
    assert b2.json() == {"canceled": True, "file_path": None}

    # 2. Register 0.8B (reference default) and 2B (copy)
    r08 = client.post(
        "/api/models/register-local",
        headers=_auth_headers(),
        json={
            "file_path": str(f08),
            "register_mode": "reference",
            "license_accepted": True,
            "set_active": True,
        },
    )
    assert r08.status_code == 200
    reg08 = r08.json()
    assert reg08["source_type"] == "reference"
    assert reg08["catalog_model_id"] == "plamo-3-translate-0.8b-q4_k_m"

    r2 = client.post(
        "/api/models/register-local",
        headers=_auth_headers(),
        json={
            "file_path": str(f2),
            "register_mode": "copy",
            "license_accepted": True,
            "set_active": False,
        },
    )
    assert r2.status_code == 200
    reg2 = r2.json()
    assert reg2["source_type"] == "managed_copy"

    # Reject unknown GGUF cleanly with 400
    unknown_gguf = ext_dir / "unknown.gguf"
    unknown_gguf.write_bytes(build_synthetic_gguf(architecture="llama", model_name="custom-llama"))
    bad_reg = client.post(
        "/api/models/register-local",
        headers=_auth_headers(),
        json={"file_path": str(unknown_gguf), "license_accepted": True},
    )
    assert bad_reg.status_code == 400

    # 3. Probe server running 0.8B while switching UI active model to 2B -> mismatch reported!
    monkeypatch.setattr(
        "backend.api.app.LlamaCppPlamoAdapter.check_health",
        lambda self: {
            "status": "connected",
            "model_name": "plamo-3-translate-0.8b-q4_k_m",
            "model_path_full": str(f08),
            "model_file": f08.name,
        },
    )
    sel2 = client.post(
        f"/api/models/{reg2['registration_id']}/select?probe_llama=true",
        headers=_auth_headers(),
    )
    assert sel2.status_code == 200
    body2 = sel2.json()
    assert body2["active_model_id"] == reg2["registration_id"]
    assert body2["server_match"]["match_status"] == "mismatch"
    assert body2["server_match"]["is_matched"] is False

    # 4. Overview & delete reference entry leaves external GGUF untouched
    ov = client.get("/api/models?probe_llama=true", headers=_auth_headers())
    assert ov.status_code == 200
    assert ov.json()["active_model_id"] == reg2["registration_id"]
    assert ov.json()["server_match"]["match_status"] == "mismatch"

    del08 = client.delete(f"/api/models/{reg08['registration_id']}", headers=_auth_headers())
    assert del08.status_code == 200
    assert del08.json()["deleted"] is True
    assert f08.is_file()


def test_model_download_success_cancel_cleanup_and_sha_failure(
    tmp_path: Path,
) -> None:
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"DL_08B")
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"DL_2B")
    url_08b = "https://example.test/models/plamo-0.8b.gguf"
    url_corrupt = "https://example.test/models/corrupt-0.8b.gguf"
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b, "download_url": url_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    cfg = AppConfig(runtime_dir=tmp_path / "rt_dl", model_catalog_path=cat_path)
    cfg.ensure_directories()
    mgr = JobManager(config=cfg)

    # Pre-register 2B as existing active model
    f2 = tmp_path / "existing_2b.gguf"
    f2.write_bytes(bytes_2b)
    prior_reg = mgr.model_registry.register_local_model(
        file_path=f2,
        license_accepted=True,
        set_active=True,
    )

    chunk_gate = threading.Event()

    class ControlledStream(httpx.SyncByteStream):
        def __init__(self, payload: bytes, *, slow: bool = False) -> None:
            self.payload = payload
            self.slow = slow

        def __iter__(self):
            mid = max(1, len(self.payload) // 2)
            yield self.payload[:mid]
            if self.slow:
                chunk_gate.wait(timeout=5.0)
            yield self.payload[mid:]

    slow_mode = {"enabled": False, "corrupt": False}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        if slow_mode["corrupt"]:
            bad = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"TAMPER")
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(bad))},
                stream=ControlledStream(bad, slow=False),
            )
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(bytes_08b))},
            stream=ControlledStream(bytes_08b, slow=slow_mode["enabled"]),
        )

    dl_mgr = ModelDownloadManager(
        registry=mgr.model_registry,
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(transport_handler)),
    )
    app = create_app(config=cfg, job_manager=mgr, model_download_manager=dl_mgr, session_token="test-tok")
    client = TestClient(app)

    # 1. Start background download & cancel mid-stream -> .downloading removed & prior 2B preserved
    slow_mode["enabled"] = True
    started = client.post(
        "/api/models/downloads",
        headers=_auth_headers(),
        json={"catalog_model_id": "plamo-3-translate-0.8b-q4_k_m", "license_accepted": True, "background": True},
    )
    assert started.status_code == 200
    dl_id = started.json()["download_id"]
    # Wait briefly for first chunk to hit disk before canceling
    for _ in range(40):
        st = client.get(f"/api/models/downloads/{dl_id}", headers=_auth_headers()).json()
        if st["bytes_downloaded"] > 0:
            break
        time.sleep(0.02)

    cancel_resp = client.post(f"/api/models/downloads/{dl_id}/cancel", headers=_auth_headers())
    assert cancel_resp.status_code == 200
    chunk_gate.set()

    for _ in range(60):
        cur = client.get(f"/api/models/downloads/{dl_id}", headers=_auth_headers()).json()
        if cur["status"] in ("canceled", "failed", "completed"):
            break
        time.sleep(0.02)
    assert cur["status"] == "canceled"
    assert not list(mgr.model_registry.managed_models_dir.rglob("*.downloading"))
    active_now, _ = mgr.model_registry.get_active_model()
    assert active_now is not None and active_now.registration_id == prior_reg.registration_id

    # 2. Failed SHA-256 download deletes temp file & preserves prior active model
    slow_mode["enabled"] = False
    slow_mode["corrupt"] = True
    failed_dl = client.post(
        "/api/models/downloads",
        headers=_auth_headers(),
        json={"catalog_model_id": "plamo-3-translate-0.8b-q4_k_m", "license_accepted": True, "background": False},
    )
    assert failed_dl.status_code == 200
    assert failed_dl.json()["status"] == "failed"
    assert not list(mgr.model_registry.managed_models_dir.rglob("*.downloading"))
    active_after_fail, _ = mgr.model_registry.get_active_model()
    assert active_after_fail is not None and active_after_fail.registration_id == prior_reg.registration_id

    # 3. Successful synchronous download verifies SHA-256 and activates 0.8B
    slow_mode["corrupt"] = False
    ok_dl = client.post(
        "/api/models/downloads",
        headers=_auth_headers(),
        json={"catalog_model_id": "plamo-3-translate-0.8b-q4_k_m", "license_accepted": True, "background": False},
    )
    assert ok_dl.status_code == 200
    ok_body = ok_dl.json()
    assert ok_body["status"] == "completed"
    dest = Path(ok_body["destination_path"])
    assert dest.is_file()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == hashlib.sha256(bytes_08b).hexdigest()
    active_after_ok, _ = mgr.model_registry.get_active_model()
    assert active_after_ok is not None and active_after_ok.catalog_model_id == "plamo-3-translate-0.8b-q4_k_m"


def test_active_job_locks_switching_and_real_preflight_blocks_server_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    generated_pdfs: dict[str, Path],
    tmp_path: Path,
) -> None:
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"LOCK_08B")
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"LOCK_2B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    cfg = AppConfig(runtime_dir=tmp_path / "rt_guard", model_catalog_path=cat_path)
    cfg.ensure_directories()
    app = create_app(config=cfg, session_token="test-tok")
    mgr: JobManager = app.state.job_manager
    client = TestClient(app)

    f08 = tmp_path / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    f08.write_bytes(bytes_08b)
    f2 = tmp_path / "plamo-3-translate-2b-Q4_K_M.gguf"
    f2.write_bytes(bytes_2b)

    reg08 = mgr.model_registry.register_local_model(file_path=f08, license_accepted=True, set_active=True)
    reg2 = mgr.model_registry.register_local_model(file_path=f2, license_accepted=True, set_active=False)

    # Upload sample PDF
    with open(generated_pdfs["single_col_math"], "rb") as f:
        up = client.post(
            "/api/documents/upload",
            files={"file": ("sample.pdf", f, "application/pdf")},
            headers=_auth_headers(),
        )
    assert up.status_code == 200
    doc_id = up.json()["id"]

    # 1. While a translation job is active, switching / register(set_active) / delete active -> 409 Conflict
    with mgr._active_lock:
        mgr._active_jobs.add("job-busy-test")
    try:
        ov_busy = client.get("/api/models", headers=_auth_headers()).json()
        assert ov_busy["has_active_translation_job"] is True

        sel_conflict = client.post(f"/api/models/{reg2.registration_id}/select", headers=_auth_headers())
        assert sel_conflict.status_code == 409

        reg_conflict = client.post(
            "/api/models/register-local",
            headers=_auth_headers(),
            json={"file_path": str(f2), "license_accepted": True, "set_active": True},
        )
        assert reg_conflict.status_code == 409

        del_conflict = client.delete(f"/api/models/{reg08.registration_id}", headers=_auth_headers())
        assert del_conflict.status_code == 409
    finally:
        with mgr._active_lock:
            mgr._active_jobs.discard("job-busy-test")

    # 2. Real-mode start/resume/retranslate MUST block with 409 when running llama-server has 2B while UI selected 0.8B
    monkeypatch.setattr(
        "backend.jobs.manager.LlamaCppPlamoAdapter.check_health",
        lambda self: {
            "status": "connected",
            "model_name": "plamo-3-translate-2b-q4_k_m",
            "model_path_full": str(f2),
            "model_file": f2.name,
        },
    )

    real_cfg = TranslationJobConfig(
        model_id="plamo-3-translate-0.8b-q4_k_m",
        model_name="plamo-3-translate-0.8b-Q4_K_M",
        use_mock_translator=False,
    ).model_dump()

    blocked_start = client.post(
        f"/api/documents/{doc_id}/jobs",
        headers=_auth_headers(),
        json=real_cfg,
    )
    assert blocked_start.status_code == 409
    assert "ModelMismatchConflict" in blocked_start.json()["detail"]

    # Create a completed/interrupted mock-created or pre-existing job targeted at 0.8B and verify resume/retranslate also block
    real_job = mgr.create_job(
        doc_id,
        TranslationJobConfig(
            model_id="plamo-3-translate-0.8b-q4_k_m",
            model_name="plamo-3-translate-0.8b-Q4_K_M",
            use_mock_translator=False,
        ),
    )
    blocked_resume = client.post(f"/api/jobs/{real_job.id}/resume", headers=_auth_headers())
    assert blocked_resume.status_code == 409
    assert "ModelMismatchConflict" in blocked_resume.json()["detail"]

    blocked_retrans = client.post(
        f"/api/jobs/{real_job.id}/retranslate-blocks",
        headers=_auth_headers(),
        json={"block_ids": ["b1"]},
    )
    assert blocked_retrans.status_code == 409
    assert "ModelMismatchConflict" in blocked_retrans.json()["detail"]

    # 3. Explicitly running mock job completes & provenance records model identity cleanly without claiming real inference
    mock_job = client.post(
        f"/api/documents/{doc_id}/jobs",
        headers=_auth_headers(),
        json={
            "model_id": "plamo-3-translate-0.8b-q4_k_m",
            "use_mock_translator": True,
        },
    )
    assert mock_job.status_code == 200
    prov = mock_job.json()["execution_provenance"]
    assert prov["is_mock"] is True
    assert prov["model_id"] == "plamo-3-translate-0.8b-q4_k_m"
    assert prov["model_display_name"] == "PLaMo 3 Translate 0.8B (Q4_K_M)"


def test_four_stage_verification_progression_across_api_and_jobs(
    monkeypatch: pytest.MonkeyPatch,
    generated_pdfs: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Verify 4-stage verification progression end-to-end across /api/models, /select, and /jobs."""
    from backend.translation.adapters.base import TranslationAdapterResponse

    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"PROG_2B")
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"PROG_08B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
        ],
    )
    cfg = AppConfig(runtime_dir=tmp_path / "rt_prog", model_catalog_path=cat_path)
    cfg.ensure_directories()
    mgr = JobManager(config=cfg)

    def transport_handler(request: httpx.Request) -> httpx.Response:
        payload = bytes_2b if "2b" in str(request.url).lower() else bytes_08b
        return httpx.Response(200, headers={"Content-Length": str(len(payload))}, content=payload)

    dl_mgr = ModelDownloadManager(
        registry=mgr.model_registry,
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(transport_handler)),
    )
    app = create_app(config=cfg, job_manager=mgr, model_download_manager=dl_mgr, session_token="test-tok")
    client = TestClient(app)

    server_health: dict[str, Any] = {"status": "server_unreachable"}
    monkeypatch.setattr("backend.api.app.LlamaCppPlamoAdapter.check_health", lambda self: dict(server_health))
    monkeypatch.setattr("backend.jobs.manager.LlamaCppPlamoAdapter.check_health", lambda self: dict(server_health))

    def fake_translate_segment(self: Any, source_text: str, **kwargs: Any) -> TranslationAdapterResponse:
        return TranslationAdapterResponse(
            translated_text=f"テスト翻訳: {source_text}",
            raw_output=f"テスト翻訳: {source_text}",
            prompt_used=source_text,
            model_name="plamo-3-translate-2b-q4_k_m",
            quantization="Q4_K_M",
            finish_reason="stop",
            is_mock=False,
            prompt_tokens=16,
            completion_tokens=16,
        )

    monkeypatch.setattr("backend.jobs.manager.LlamaCppPlamoAdapter.translate_segment", fake_translate_segment)

    # 1. Downloaded 2B starts honestly at file_verified (never pre-stamped launch/translation verified)
    dl_resp = client.post(
        "/api/models/downloads",
        headers=_auth_headers(),
        json={"catalog_model_id": "plamo-3-translate-2b-q4_k_m", "license_accepted": True, "background": False},
    )
    assert dl_resp.status_code == 200
    assert dl_resp.json()["status"] == "completed"
    dl_reg_id = dl_resp.json()["registration_id"]
    dl_path = dl_resp.json()["destination_path"]

    ov_dl = client.get("/api/models?probe_llama=true", headers=_auth_headers()).json()
    assert ov_dl["active_model_id"] == dl_reg_id
    assert ov_dl["active_model"]["verification"]["verification_stage"] == "file_verified"
    assert ov_dl["active_model"]["verification"]["launch_verified"] is False
    assert ov_dl["active_model"]["verification"]["translation_verified"] is False

    # Register local 0.8B and secondary local 2B -> both start at file_verified
    p08 = tmp_path / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    p08.write_bytes(bytes_08b)
    reg_08_resp = client.post(
        "/api/models/register-local",
        headers=_auth_headers(),
        json={"file_path": str(p08), "license_accepted": True, "set_active": False},
    )
    assert reg_08_resp.status_code == 200
    assert reg_08_resp.json()["verification"]["verification_stage"] == "file_verified"
    reg_08_id = reg_08_resp.json()["registration_id"]

    p2_alt = tmp_path / "alt_copy" / "plamo-3-translate-2b-Q4_K_M.gguf"
    p2_alt.parent.mkdir()
    p2_alt.write_bytes(bytes_2b)
    reg_2_alt_resp = client.post(
        "/api/models/register-local",
        headers=_auth_headers(),
        json={"file_path": str(p2_alt), "license_accepted": True, "set_active": False},
    )
    assert reg_2_alt_resp.status_code == 200
    assert reg_2_alt_resp.json()["verification"]["verification_stage"] == "file_verified"
    reg_2_alt_id = reg_2_alt_resp.json()["registration_id"]

    # 2. Immediate launch_verified on POST /api/models/{id}/select?probe_llama=true when server matches 0.8B
    server_health = {
        "status": "connected",
        "model_name": "plamo-3-translate-0.8b-q4_k_m",
        "model_path_full": str(p08),
        "model_file": p08.name,
    }
    sel_08 = client.post(f"/api/models/{reg_08_id}/select?probe_llama=true", headers=_auth_headers())
    assert sel_08.status_code == 200
    sel_08_ver = sel_08.json()["selected_model"]["verification"]
    assert sel_08_ver["verification_stage"] == "launch_verified"
    assert sel_08_ver["launch_verified"] is True
    assert sel_08_ver["translation_verified"] is False

    # 3. Immediate launch_verified in first GET /api/models?probe_llama=true after matching downloaded 2B
    sel_dl_no_probe = client.post(f"/api/models/{dl_reg_id}/select?probe_llama=false", headers=_auth_headers())
    assert sel_dl_no_probe.status_code == 200
    assert sel_dl_no_probe.json()["selected_model"]["verification"]["verification_stage"] == "file_verified"

    server_health = {
        "status": "connected",
        "model_name": "plamo-3-translate-2b-q4_k_m",
        "model_path_full": dl_path,
        "model_file": Path(dl_path).name,
    }
    ov_first_probe = client.get("/api/models?probe_llama=true", headers=_auth_headers()).json()
    dl_ver_after_probe = ov_first_probe["active_model"]["verification"]
    assert dl_ver_after_probe["verification_stage"] == "launch_verified"
    assert dl_ver_after_probe["launch_verified"] is True
    assert dl_ver_after_probe["translation_verified"] is False

    # Upload documents for job tests
    with open(generated_pdfs["single_col_math"], "rb") as f:
        doc1_id = client.post(
            "/api/documents/upload",
            files={"file": ("doc1.pdf", f, "application/pdf")},
            headers=_auth_headers(),
        ).json()["id"]
    with open(generated_pdfs["single_col_math"], "rb") as f:
        doc2_id = client.post(
            "/api/documents/upload",
            files={"file": ("doc2.pdf", f, "application/pdf")},
            headers=_auth_headers(),
        ).json()["id"]

    # 4. Mock & roundtrip-only jobs MUST NOT mark translation_verified
    mock_res = client.post(
        f"/api/documents/{doc1_id}/jobs",
        headers=_auth_headers(),
        json={"model_id": "plamo-3-translate-2b-q4_k_m", "use_mock_translator": True},
    )
    assert mock_res.status_code == 200
    assert mock_res.json()["state"] == "completed"

    rt_res = client.post(
        f"/api/documents/{doc1_id}/jobs",
        headers=_auth_headers(),
        json={
            "model_id": "plamo-3-translate-2b-q4_k_m",
            "use_mock_translator": False,
            "only_roundtrip_preservation": True,
        },
    )
    assert rt_res.status_code == 200
    assert rt_res.json()["state"] == "completed"

    ov_after_mock_rt = client.get("/api/models", headers=_auth_headers()).json()
    assert ov_after_mock_rt["active_model"]["verification"]["verification_stage"] == "launch_verified"
    assert ov_after_mock_rt["active_model"]["verification"]["translation_verified"] is False

    # 5. Real non-mock job with newly_inferenced_blocks > 0 promotes downloaded 2B -> translation_verified (and populates cache)
    real_res = client.post(
        f"/api/documents/{doc1_id}/jobs",
        headers=_auth_headers(),
        json={"model_id": "plamo-3-translate-2b-q4_k_m", "use_mock_translator": False},
    )
    assert real_res.status_code == 200
    real_job = real_res.json()
    assert real_job["state"] in ("completed", "completed_with_warnings")
    assert real_job["execution_provenance"]["newly_inferenced_blocks"] > 0

    ov_after_real = client.get("/api/models", headers=_auth_headers()).json()
    assert ov_after_real["active_model"]["verification"]["verification_stage"] == "translation_verified"
    assert ov_after_real["active_model"]["verification"]["launch_verified"] is True
    assert ov_after_real["active_model"]["verification"]["translation_verified"] is True

    # 6. Cache-hit-only real job on secondary 2B registration (newly_inferenced_blocks == 0) MUST stay at launch_verified
    server_health = {
        "status": "connected",
        "model_name": "plamo-3-translate-2b-q4_k_m",
        "model_path_full": str(p2_alt),
        "model_file": p2_alt.name,
    }
    sel_alt = client.post(f"/api/models/{reg_2_alt_id}/select?probe_llama=true", headers=_auth_headers())
    assert sel_alt.status_code == 200
    assert sel_alt.json()["selected_model"]["verification"]["verification_stage"] == "launch_verified"

    cache_hit_res = client.post(
        f"/api/documents/{doc2_id}/jobs",
        headers=_auth_headers(),
        json={"model_id": "plamo-3-translate-2b-q4_k_m", "use_mock_translator": False},
    )
    assert cache_hit_res.status_code == 200
    cache_job = cache_hit_res.json()
    assert cache_job["state"] in ("completed", "completed_with_warnings")
    assert cache_job["execution_provenance"]["cache_hit_blocks"] > 0
    assert cache_job["execution_provenance"]["newly_inferenced_blocks"] == 0
    assert cache_job["execution_provenance"]["llm_api_calls_made"] == 0

    ov_after_cache = client.get("/api/models", headers=_auth_headers()).json()
    assert ov_after_cache["active_model"]["verification"]["verification_stage"] == "launch_verified"
    assert ov_after_cache["active_model"]["verification"]["translation_verified"] is False

    # 7. Fresh uncached document with newly_inferenced_blocks > 0 transitions secondary 2B -> translation_verified
    with open(generated_pdfs["two_col_figures_tables_2p"], "rb") as f:
        doc3_id = client.post(
            "/api/documents/upload",
            files={"file": ("doc3.pdf", f, "application/pdf")},
            headers=_auth_headers(),
        ).json()["id"]

    fresh_res = client.post(
        f"/api/documents/{doc3_id}/jobs",
        headers=_auth_headers(),
        json={"model_id": "plamo-3-translate-2b-q4_k_m", "use_mock_translator": False},
    )
    assert fresh_res.status_code == 200
    fresh_job = fresh_res.json()
    assert fresh_job["state"] in ("completed", "completed_with_warnings")
    assert fresh_job["execution_provenance"]["newly_inferenced_blocks"] > 0

    ov_final = client.get("/api/models", headers=_auth_headers()).json()
    assert ov_final["active_model"]["verification"]["verification_stage"] == "translation_verified"
    assert ov_final["active_model"]["verification"]["launch_verified"] is True
    assert ov_final["active_model"]["verification"]["translation_verified"] is True
