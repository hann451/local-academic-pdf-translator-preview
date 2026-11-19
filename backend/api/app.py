"""FastAPI Local Backend Server for PaperBridge Local (Section 4, 13, 16).

Security enforcement:
- Bound to `127.0.0.1` by default.
- Forbids wildcard CORS (`*`).
- Validates `Origin` header when present and enforces session token header (`X-PaperBridge-Token`) on all `/api/*` endpoints (except session bootstrap `GET /api/auth/session`).
- Rejects authentication tokens passed in URL query strings.
- Prevents arbitrary path traversal on `document_id`, `job_id`, `artifact_type`, and verifies all served files reside inside `runtime_dir`.
- Supports zero-LLM `retypeset` (`POST /api/jobs/{job_id}/retypeset`), authenticated PDF streaming with HTTP Range support, and optional static serving of `frontend/dist`.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import AppConfig
from backend.diagnostics import collect_system_diagnostics
from backend.domain.models import (
    Block,
    BlockSplitPreviewResult,
    BlockSplitSpec,
    Document,
    ExportArtifact,
    Glossary,
    GlossaryEntry,
    Job,
    ProtectionMode,
    ProtectionRegion,
    SystemDiagnostics,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.jobs.manager import JobManager
from backend.models.catalog import CatalogModel
from backend.models.registry import (
    ModelDownloadJob,
    ModelDownloadManager,
    ModelServerMatchResult,
    ModelValidationError,
    RegisteredModel,
)
from backend.models.server_process import (
    LlamaServerConflictError,
    LlamaServerLaunchError,
    LlamaServerProcessManager,
    LlamaServerRestartResult,
    LlamaServerSafetyError,
)
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ALLOWED_ARTIFACT_TYPES = {
    "mono_ja_pdf",
    "dual_bilingual_pdf",
    "roundtrip_pdf",
    "validation_report_json",
}


class CreateProtectionRegionRequest(BaseModel):
    page_index: int
    bbox: list[float]
    mode: ProtectionMode
    note: str | None = None


class BlockSplitPreviewRequest(BaseModel):
    page_index: int
    requested_bbox: list[float]
    include_equation_number: bool = True


class CreateBlockSplitRequest(BaseModel):
    page_index: int
    requested_bbox: list[float]
    include_equation_number: bool = True
    note: str | None = None
    expected_protection_revision: int | None = None
    expected_parse_revision: int | None = None
    expected_parent_signature: str | None = None


class BlockSplitMutationResponse(BaseModel):
    split: BlockSplitSpec | None = None
    deleted: bool = False
    document: Document
    blocks: list[Block]
    splits: list[BlockSplitSpec]


class ManualEditBlockRequest(BaseModel):
    manual_translation_text: str | None
    expected_edit_revision: int | None = None


class UpdateBlockProtectionRequest(BaseModel):
    user_protection_override: Literal["force_translate", "force_protect"] | None = None


class ReapplyAutoProtectionRequest(BaseModel):
    reset_user_overrides: bool = True
    clear_manual_regions: bool = False


class CreateGlossaryRequest(BaseModel):
    name: str = Field(..., min_length=1)
    entries: list[GlossaryEntry]


class UpdateGlossaryRequest(BaseModel):
    name: str = Field(..., min_length=1)
    entries: list[GlossaryEntry]


class PartialRetranslateRequest(BaseModel):
    block_ids: list[str]


class RetypesetJobRequest(BaseModel):
    expected_edit_revision: int | None = None
    allow_validation_warnings: bool = True
    fail_stage_for_test: bool = False


class RetypesetJobResponse(BaseModel):
    job: Job
    artifacts: list[ExportArtifact]
    issues: list[ValidationIssue]
    requires_reparse_or_retranslate: bool = False
    llm_calls_made: int = 0


class RegisterLocalModelRequest(BaseModel):
    file_path: str = Field(..., min_length=1)
    expected_catalog_model_id: str | None = None
    register_mode: Literal["reference", "copy"] = "reference"
    license_accepted: bool = False
    set_active: bool = True
    replaces_registration_id: str | None = None


class StartModelDownloadRequest(BaseModel):
    catalog_model_id: str = Field(..., min_length=1)
    license_accepted: bool = False
    set_active: bool = True
    background: bool = True


class ModelSelectResponse(BaseModel):
    active_model_id: str
    selected_model: RegisteredModel
    server_match: ModelServerMatchResult | None = None


class ModelServerRestartRequest(BaseModel):
    registration_id: str | None = None
    use_cpu: bool | None = None


class ModelServerRestartResponse(LlamaServerRestartResult):
    pass


class ModelsOverviewResponse(BaseModel):
    catalog: list[CatalogModel]
    unsupported_policies: dict[str, str]
    registered: list[RegisteredModel]
    active_model_id: str | None = None
    active_model: RegisteredModel | None = None
    has_active_translation_job: bool = False
    managed_models_dir: str
    downloads: list[ModelDownloadJob] = Field(default_factory=list)
    server_match: ModelServerMatchResult | None = None


def _open_windows_gguf_file_dialog(initial_dir: Path | None = None) -> str | None:
    """Open a native Windows file dialog on localhost to select a GGUF file without copying it."""
    if initial_dir and initial_dir.exists():
        init_str = str(initial_dir).replace("'", "''")
        init_line = f"$dlg.InitialDirectory = '{init_str}'; "
    else:
        init_line = ""
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dlg = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dlg.Title = '登録する GGUF モデルファイルを選択してください'; "
        "$dlg.Filter = 'GGUF モデル (*.gguf)|*.gguf|すべてのファイル (*.*)|*.*'; "
        "$dlg.CheckFileExists = $true; "
        "$dlg.Multiselect = $false; "
        + init_line
        + "if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.FileName }"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    chosen = (proc.stdout or "").strip()
    return chosen or None


def _validate_safe_id(value: str, label: str = "id") -> str:
    if not _SAFE_ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label} format.",
        )
    return value


def _ensure_within_runtime_dir(file_path: Path, runtime_dir: Path) -> Path:
    resolved_file = file_path.resolve()
    resolved_root = runtime_dir.resolve()
    if not resolved_file.is_relative_to(resolved_root):
        raise HTTPException(
            status_code=403,
            detail="Path traversal blocked: file is outside runtime_dir.",
        )
    if not resolved_file.exists() or not resolved_file.is_file():
        raise HTTPException(status_code=404, detail="Requested file does not exist.")
    return resolved_file


def _serve_file_with_range(
    request: Request,
    file_path: Path,
    media_type: str,
    download_filename: str,
    inline: bool = False,
) -> Response:
    file_size = file_path.stat().st_size
    encoded_name = urllib.parse.quote(download_filename)
    disp_type = "inline" if inline else "attachment"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Disposition": f"{disp_type}; filename*=UTF-8''{encoded_name}",
    }

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            range_spec = range_header.split("=", 1)[1].strip()
            start_str, end_str = range_spec.split("-", 1)
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            if start < 0 or end >= file_size or start > end:
                raise ValueError("Range out of bounds")
        except ValueError:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}", "Cache-Control": "no-store"},
            )

        chunk_len = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(chunk_len)

        def iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = chunk_len
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    headers["Content-Length"] = str(file_size)
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers=headers,
    )


def create_app(
    config: AppConfig | None = None,
    job_manager: JobManager | None = None,
    session_token: str | None = None,
    frontend_dist_dir: Path | None = None,
    model_download_manager: ModelDownloadManager | None = None,
    llama_server_manager: LlamaServerProcessManager | None = None,
) -> FastAPI:
    cfg = config or AppConfig()
    cfg.ensure_directories()
    mgr = job_manager or JobManager(config=cfg)
    dl_mgr = model_download_manager or ModelDownloadManager(registry=mgr.model_registry)
    srv_mgr = llama_server_manager or LlamaServerProcessManager(
        config=cfg,
        registry=mgr.model_registry,
    )
    active_token = session_token or secrets.token_urlsafe(32)

    app = FastAPI(
        title="PaperBridge Local API",
        version="0.3.0",
        description="Local-only academic PDF translation API prioritizing formula preservation.",
    )
    app.state.config = cfg
    app.state.job_manager = mgr
    app.state.model_download_manager = dl_mgr
    app.state.llama_server_manager = srv_mgr
    app.state.session_token = active_token

    # Section 4 & 16: Never allow wildcard '*' CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-PaperBridge-Token", "Range"],
        expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges", "Content-Length"],
    )

    def verify_origin_only(request: Request) -> None:
        # Forbid passing tokens in URL query string
        for k in request.query_params.keys():
            if k.lower() in ("token", "x-paperbridge-token", "auth_token", "session_token"):
                raise HTTPException(
                    status_code=400,
                    detail="Security policy violation: never pass auth token in URL query string.",
                )
        origin = request.headers.get("origin")
        if origin and origin not in cfg.allowed_origins:
            raise HTTPException(
                status_code=403,
                detail=f"Origin '{origin}' is not allowed by local loopback policy.",
            )

    def verify_security_headers(request: Request) -> None:
        verify_origin_only(request)
        supplied_token = request.headers.get("x-paperbridge-token")
        current_token = app.state.session_token
        if not current_token or not supplied_token or not secrets.compare_digest(supplied_token, current_token):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid X-PaperBridge-Token",
            )

    # --- Auth Session Lifecycle Endpoints (Section 4) ---
    @app.get("/api/auth/session")
    def get_auth_session(
        reissue: bool = False,
        _origin: None = Depends(verify_origin_only),
    ) -> dict[str, Any]:
        if app.state.session_token is None:
            if reissue:
                app.state.session_token = secrets.token_urlsafe(32)
            else:
                raise HTTPException(status_code=401, detail="Session token has been revoked.")
        return {
            "token": app.state.session_token,
            "header_name": "X-PaperBridge-Token",
            "loopback_only": True,
        }

    @app.post("/api/auth/rotate")
    def rotate_auth_session(
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, Any]:
        app.state.session_token = secrets.token_urlsafe(32)
        return {
            "token": app.state.session_token,
            "header_name": "X-PaperBridge-Token",
            "rotated": True,
        }

    @app.post("/api/auth/revoke")
    def revoke_auth_session(
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, bool]:
        app.state.session_token = None
        return {"revoked": True}

    # --- Diagnostics Endpoint ---
    @app.get("/api/diagnostics", response_model=SystemDiagnostics)
    def get_diagnostics(
        probe_llama: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> SystemDiagnostics:
        return collect_system_diagnostics(config=cfg, probe_llama=probe_llama)

    # --- Model Catalog & Local Registry Endpoints ---
    def _probe_llama_health_quick() -> dict[str, Any] | None:
        try:
            active_reg, active_cat = mgr.model_registry.get_active_model()
            alias = (
                active_cat.launch_settings.alias
                if active_cat is not None
                else cfg.llama_model_alias
            )
            adapter = LlamaCppPlamoAdapter(config=cfg, model_alias=alias, timeout_sec=2.0)
            res = adapter.check_health()
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    @app.get("/api/models", response_model=ModelsOverviewResponse)
    def list_models_overview(
        probe_llama: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelsOverviewResponse:
        # Probe first when requested so exact server match promotes launch_verified in the returned snapshot.
        server_match = (
            mgr.model_registry.compare_with_running_server(_probe_llama_health_quick())
            if probe_llama
            else None
        )
        catalog, reg = mgr.model_registry.get_registry_snapshot(
            auto_migrate=not probe_llama,
            revalidate=not probe_llama,
        )
        active = (
            next((m for m in reg.models if m.registration_id == reg.active_model_id), None)
            if reg.active_model_id
            else None
        )
        return ModelsOverviewResponse(
            catalog=catalog.models,
            unsupported_policies=catalog.unsupported_policies,
            registered=reg.models,
            active_model_id=reg.active_model_id,
            active_model=active,
            has_active_translation_job=mgr.has_any_active_job(),
            managed_models_dir=str(mgr.model_registry.managed_models_dir),
            downloads=dl_mgr.list_downloads(),
            server_match=server_match,
        )

    @app.post("/api/models/browse-local-file")
    async def browse_local_model_file(
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, Any]:
        try:
            chosen = await anyio.to_thread.run_sync(
                lambda: _open_windows_gguf_file_dialog(mgr.model_registry.managed_models_dir)
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"ローカルファイル選択ダイアログを開けませんでした: {exc}",
            ) from exc
        if not chosen:
            return {"canceled": True, "file_path": None}
        return {"canceled": False, "file_path": chosen}

    @app.post("/api/models/register-local", response_model=RegisteredModel)
    async def register_local_model_endpoint(
        req: RegisterLocalModelRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> RegisteredModel:
        if req.replaces_registration_id:
            _validate_safe_id(req.replaces_registration_id, "replaces_registration_id")
        try:
            return await anyio.to_thread.run_sync(
                lambda: mgr.model_registry.register_local_model(
                    file_path=req.file_path,
                    expected_catalog_model_id=req.expected_catalog_model_id,
                    register_mode=req.register_mode,
                    license_accepted=req.license_accepted,
                    set_active=req.set_active,
                    replaces_registration_id=req.replaces_registration_id,
                    has_active_job=mgr.has_any_active_job(),
                )
            )
        except ModelValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "ModelSwitchConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.post("/api/models/{registration_id}/select", response_model=ModelSelectResponse)
    def select_active_model_endpoint(
        registration_id: str,
        probe_llama: bool = True,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelSelectResponse:
        _validate_safe_id(registration_id, "registration_id")
        try:
            selected = mgr.model_registry.select_active_model(
                registration_id,
                has_active_job=mgr.has_any_active_job(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "ModelSwitchConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise
        server_match = (
            mgr.model_registry.compare_with_running_server(_probe_llama_health_quick())
            if probe_llama
            else None
        )
        if server_match is not None and server_match.match_status == "matched":
            _cat, reg_after = mgr.model_registry.get_registry_snapshot(
                auto_migrate=False,
                revalidate=False,
            )
            refreshed = next(
                (m for m in reg_after.models if m.registration_id == selected.registration_id),
                None,
            )
            if refreshed is not None:
                selected = refreshed
        return ModelSelectResponse(
            active_model_id=selected.registration_id,
            selected_model=selected,
            server_match=server_match,
        )

    async def _restart_llama_server_impl(
        req: ModelServerRestartRequest | None,
    ) -> ModelServerRestartResponse:
        payload = req or ModelServerRestartRequest()
        if payload.registration_id:
            _validate_safe_id(payload.registration_id, "registration_id")
        if mgr.has_any_active_job():
            raise HTTPException(
                status_code=409,
                detail="ModelSwitchConflict: 翻訳ジョブ実行中は推論サーバーの再起動・モデル切替を行えません。",
            )
        try:
            res = await anyio.to_thread.run_sync(
                lambda: srv_mgr.restart_for_active_model(
                    registration_id=payload.registration_id,
                    use_cpu=payload.use_cpu,
                    has_active_job=mgr.has_any_active_job,
                )
            )
            return ModelServerRestartResponse(**res.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ModelValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (LlamaServerConflictError, LlamaServerSafetyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LlamaServerLaunchError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/models/server/restart", response_model=ModelServerRestartResponse)
    async def restart_llama_server_endpoint(
        req: ModelServerRestartRequest | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelServerRestartResponse:
        return await _restart_llama_server_impl(req)

    @app.post("/api/models/server/switch", response_model=ModelServerRestartResponse)
    async def switch_llama_server_endpoint(
        req: ModelServerRestartRequest | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelServerRestartResponse:
        return await _restart_llama_server_impl(req)

    @app.delete("/api/models/{registration_id}")
    def delete_registered_model_endpoint(
        registration_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, Any]:
        _validate_safe_id(registration_id, "registration_id")
        active_reg, _ = mgr.model_registry.get_active_model()
        if (
            active_reg is not None
            and active_reg.registration_id == registration_id
            and mgr.has_any_active_job()
        ):
            raise HTTPException(
                status_code=409,
                detail="ModelSwitchConflict: 翻訳ジョブ実行中に使用中モデルの登録を解除できません。",
            )
        deleted = mgr.model_registry.remove_registration(registration_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Registered model not found")
        _cat, reg_after = mgr.model_registry.get_registry_snapshot(auto_migrate=False, revalidate=False)
        return {
            "deleted": True,
            "registration_id": registration_id,
            "active_model_id": reg_after.active_model_id,
        }

    @app.post("/api/models/downloads", response_model=ModelDownloadJob)
    async def start_model_download_endpoint(
        req: StartModelDownloadRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelDownloadJob:
        try:
            if not req.background:
                return await anyio.to_thread.run_sync(
                    lambda: dl_mgr.start_download(
                        catalog_model_id=req.catalog_model_id,
                        license_accepted=req.license_accepted,
                        set_active=req.set_active,
                        background=False,
                        has_active_translation_job=mgr.has_any_active_job(),
                    )
                )
            return dl_mgr.start_download(
                catalog_model_id=req.catalog_model_id,
                license_accepted=req.license_accepted,
                set_active=req.set_active,
                background=True,
                has_active_translation_job=mgr.has_any_active_job(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "ModelSwitchConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.get("/api/models/downloads", response_model=list[ModelDownloadJob])
    def list_model_downloads_endpoint(
        _sec: None = Depends(verify_security_headers),
    ) -> list[ModelDownloadJob]:
        return dl_mgr.list_downloads()

    @app.get("/api/models/downloads/{download_id}", response_model=ModelDownloadJob)
    def get_model_download_endpoint(
        download_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelDownloadJob:
        _validate_safe_id(download_id, "download_id")
        job = dl_mgr.get_download(download_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Download task not found")
        return job

    @app.post("/api/models/downloads/{download_id}/cancel", response_model=ModelDownloadJob)
    def cancel_model_download_endpoint(
        download_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> ModelDownloadJob:
        _validate_safe_id(download_id, "download_id")
        job = dl_mgr.cancel_download(download_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Download task not found")
        return job

    # --- Document Endpoints ---
    @app.post("/api/documents/upload", response_model=Document)
    async def upload_document(
        file: UploadFile = File(...),
        _sec: None = Depends(verify_security_headers),
    ) -> Document:
        # Save to isolated temporary file first; never use file.filename as path!
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            doc = await anyio.to_thread.run_sync(
                lambda: mgr.register_document(
                    source_pdf_path=tmp_path,
                    original_filename=file.filename or "uploaded.pdf",
                )
            )
            return doc
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.get("/api/documents/{document_id}", response_model=Document)
    def get_document(
        document_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> Document:
        _validate_safe_id(document_id, "document_id")
        doc = mgr.db.get_document(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    @app.get("/api/documents/{document_id}/pdf")
    def stream_document_pdf(
        document_id: str,
        request: Request,
        _sec: None = Depends(verify_security_headers),
    ) -> Response:
        _validate_safe_id(document_id, "document_id")
        doc = mgr.db.get_document(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        safe_path = _ensure_within_runtime_dir(Path(doc.safe_storage_path), cfg.runtime_dir)
        return _serve_file_with_range(
            request=request,
            file_path=safe_path,
            media_type="application/pdf",
            download_filename=doc.original_filename or f"{doc.id}.pdf",
            inline=True,
        )

    @app.post("/api/documents/{document_id}/parse", response_model=list[Block])
    async def parse_document_endpoint(
        document_id: str,
        job_config: TranslationJobConfig | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> list[Block]:
        _validate_safe_id(document_id, "document_id")
        try:
            return await anyio.to_thread.run_sync(
                lambda: mgr.parse_document_blocks(document_id, job_config=job_config)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.get("/api/documents/{document_id}/blocks", response_model=list[Block])
    async def list_document_blocks(
        document_id: str,
        page_index: int | None = None,
        auto_parse: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> list[Block]:
        _validate_safe_id(document_id, "document_id")
        blocks = mgr.db.list_blocks(document_id, page_index=page_index)
        if not blocks and auto_parse:
            all_blocks = mgr.db.list_blocks(document_id)
            if not all_blocks:
                try:
                    await anyio.to_thread.run_sync(
                        lambda: mgr.parse_document_blocks(document_id)
                    )
                except KeyError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                except RuntimeError as exc:
                    if "JobConflict" in str(exc):
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    raise
                blocks = mgr.db.list_blocks(document_id, page_index=page_index)
        return blocks

    @app.post(
        "/api/documents/{document_id}/protection-regions",
        response_model=ProtectionRegion,
    )
    def add_protection_region(
        document_id: str,
        req: CreateProtectionRegionRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> ProtectionRegion:
        _validate_safe_id(document_id, "document_id")
        try:
            return mgr.add_protection_region(
                document_id,
                page_index=req.page_index,
                bbox=req.bbox,
                mode=req.mode,
                note=req.note,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.get(
        "/api/documents/{document_id}/protection-regions",
        response_model=list[ProtectionRegion],
    )
    def list_protection_regions(
        document_id: str,
        page_index: int | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> list[ProtectionRegion]:
        _validate_safe_id(document_id, "document_id")
        return mgr.db.list_protection_regions(document_id, page_index=page_index)

    @app.delete("/api/documents/{document_id}/protection-regions/{region_id}")
    def remove_protection_region(
        document_id: str,
        region_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, bool]:
        _validate_safe_id(document_id, "document_id")
        _validate_safe_id(region_id, "region_id")
        try:
            deleted = mgr.remove_protection_region(document_id, region_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise
        if not deleted:
            raise HTTPException(status_code=404, detail="Protection region not found")
        return {"deleted": True}

    @app.get(
        "/api/documents/{document_id}/block-splits",
        response_model=list[BlockSplitSpec],
    )
    def list_block_splits_endpoint(
        document_id: str,
        page_index: int | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> list[BlockSplitSpec]:
        _validate_safe_id(document_id, "document_id")
        if not mgr.db.get_document(document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        return mgr.db.list_block_splits(document_id, page_index=page_index)

    @app.post(
        "/api/documents/{document_id}/block-splits/preview",
        response_model=BlockSplitPreviewResult,
    )
    async def preview_block_split_endpoint(
        document_id: str,
        req: BlockSplitPreviewRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> BlockSplitPreviewResult:
        _validate_safe_id(document_id, "document_id")
        try:
            return await anyio.to_thread.run_sync(
                lambda: mgr.preview_block_split(
                    document_id,
                    page_index=req.page_index,
                    requested_bbox=req.requested_bbox,
                    include_equation_number=req.include_equation_number,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/documents/{document_id}/block-splits",
        response_model=BlockSplitMutationResponse,
    )
    async def create_block_split_endpoint(
        document_id: str,
        req: CreateBlockSplitRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> BlockSplitMutationResponse:
        _validate_safe_id(document_id, "document_id")
        try:
            res = await anyio.to_thread.run_sync(
                lambda: mgr.create_block_split(
                    document_id,
                    page_index=req.page_index,
                    requested_bbox=req.requested_bbox,
                    include_equation_number=req.include_equation_number,
                    note=req.note,
                    expected_protection_revision=req.expected_protection_revision,
                    expected_parse_revision=req.expected_parse_revision,
                    expected_parent_signature=req.expected_parent_signature,
                )
            )
            return BlockSplitMutationResponse(
                split=res["split"],
                deleted=False,
                document=res["document"],
                blocks=res["blocks"],
                splits=res["splits"],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            if "RevisionConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            if "InvalidSplitSelection" in msg:
                raise HTTPException(status_code=422, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        except RuntimeError as exc:
            msg = str(exc)
            if "JobConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise HTTPException(status_code=500, detail=msg) from exc

    @app.delete(
        "/api/documents/{document_id}/block-splits/{split_id}",
        response_model=BlockSplitMutationResponse,
    )
    async def delete_block_split_endpoint(
        document_id: str,
        split_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> BlockSplitMutationResponse:
        _validate_safe_id(document_id, "document_id")
        _validate_safe_id(split_id, "split_id")
        try:
            res = await anyio.to_thread.run_sync(
                lambda: mgr.delete_block_split(document_id, split_id)
            )
            return BlockSplitMutationResponse(
                split=None,
                deleted=True,
                document=res["document"],
                blocks=res["blocks"],
                splits=res["splits"],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            if "RevisionConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        except RuntimeError as exc:
            msg = str(exc)
            if "JobConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise HTTPException(status_code=500, detail=msg) from exc

    @app.put(
        "/api/documents/{document_id}/blocks/{block_id}/manual-edit",
        response_model=Block,
    )
    def manual_edit_block(
        document_id: str,
        block_id: str,
        req: ManualEditBlockRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> Block:
        _validate_safe_id(document_id, "document_id")
        try:
            return mgr.manual_edit_block(
                document_id,
                block_id,
                req.manual_translation_text,
                expected_edit_revision=req.expected_edit_revision,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            if "RevisionConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    # --- Auto Protection Endpoints ---
    @app.put(
        "/api/documents/{document_id}/blocks/{block_id}/protection",
        response_model=Block,
    )
    def update_block_protection_endpoint(
        document_id: str,
        block_id: str,
        req: UpdateBlockProtectionRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> Block:
        _validate_safe_id(document_id, "document_id")
        try:
            return mgr.update_block_protection(document_id, block_id, req.user_protection_override)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.post("/api/documents/{document_id}/reapply-auto-protection")
    def reapply_auto_protection_endpoint(
        document_id: str,
        req: ReapplyAutoProtectionRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, Any]:
        _validate_safe_id(document_id, "document_id")
        try:
            return mgr.reapply_auto_protection(
                document_id,
                reset_user_overrides=req.reset_user_overrides,
                clear_manual_regions=req.clear_manual_regions,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    # --- Glossary Endpoints ---
    @app.get("/api/glossaries", response_model=list[Glossary])
    def list_glossaries_endpoint(
        _sec: None = Depends(verify_security_headers),
    ) -> list[Glossary]:
        return mgr.db.list_glossaries()

    @app.post("/api/glossaries", response_model=Glossary)
    def create_glossary(
        req: CreateGlossaryRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> Glossary:
        glossary = Glossary(
            id=f"gls-{uuid.uuid4().hex[:10]}",
            name=req.name,
            entries=req.entries,
        )
        mgr.db.save_glossary(glossary)
        return glossary

    @app.get("/api/glossaries/{glossary_id}", response_model=Glossary)
    def get_glossary_endpoint(
        glossary_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> Glossary:
        _validate_safe_id(glossary_id, "glossary_id")
        gl = mgr.db.get_glossary(glossary_id)
        if not gl:
            raise HTTPException(status_code=404, detail="Glossary not found")
        return gl

    @app.put("/api/glossaries/{glossary_id}", response_model=Glossary)
    def update_glossary_endpoint(
        glossary_id: str,
        req: UpdateGlossaryRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> Glossary:
        _validate_safe_id(glossary_id, "glossary_id")
        try:
            return mgr.db.update_glossary(glossary_id, name=req.name, entries=req.entries)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/glossaries/{glossary_id}")
    def delete_glossary_endpoint(
        glossary_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, Any]:
        _validate_safe_id(glossary_id, "glossary_id")
        deleted = mgr.db.delete_glossary(glossary_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Glossary not found")
        return {"deleted": True, "id": glossary_id}

    # --- Job Endpoints ---
    @app.post("/api/documents/{document_id}/jobs", response_model=Job)
    async def start_job(
        document_id: str,
        job_config: TranslationJobConfig,
        background: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> Job:
        _validate_safe_id(document_id, "document_id")
        try:
            # Validate page range upfront if document is valid and page_range_spec is provided
            doc = mgr.db.get_document(document_id)
            if not doc:
                raise KeyError(f"Document not found: {document_id}")
            if job_config.page_range_spec and doc.page_count > 0:
                from backend.pdf.coordinates import parse_page_range_spec

                parse_page_range_spec(job_config.page_range_spec, doc.page_count)
            if mgr.has_active_document_operation(document_id):
                raise RuntimeError(
                    f"JobConflict: Document '{document_id}' already has an active operation."
                )
            resolved_cfg = mgr._resolve_job_model_config(job_config)
            mgr.validate_real_model_preflight(resolved_cfg)
            job = mgr.create_job(document_id, job_config=resolved_cfg)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            if "PendingSplitsConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            msg = str(exc)
            if "JobConflict" in msg or "ModelMismatchConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise

        if background:
            threading.Thread(target=mgr.run_job, args=(job.id,), daemon=True).start()
            return mgr.db.get_job(job.id) or job

        try:
            return await anyio.to_thread.run_sync(mgr.run_job, job.id)
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.get("/api/jobs/{job_id}", response_model=Job)
    def get_job_status(
        job_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> Job:
        _validate_safe_id(job_id, "job_id")
        job = mgr.db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.post("/api/jobs/{job_id}/cancel", response_model=Job)
    def cancel_job_endpoint(
        job_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> Job:
        _validate_safe_id(job_id, "job_id")
        job = mgr.request_cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/api/jobs/{job_id}/issues", response_model=list[ValidationIssue])
    def get_job_issues(
        job_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> list[ValidationIssue]:
        _validate_safe_id(job_id, "job_id")
        return mgr.db.list_validation_issues(job_id)

    @app.get("/api/jobs/{job_id}/artifacts", response_model=list[ExportArtifact])
    def get_job_artifacts(
        job_id: str,
        _sec: None = Depends(verify_security_headers),
    ) -> list[ExportArtifact]:
        _validate_safe_id(job_id, "job_id")
        return mgr.db.list_export_artifacts(job_id)

    @app.get("/api/jobs/{job_id}/artifacts/{artifact_type}/download")
    def download_job_artifact(
        job_id: str,
        artifact_type: str,
        request: Request,
        inline: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> Response:
        _validate_safe_id(job_id, "job_id")
        if artifact_type not in _ALLOWED_ARTIFACT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported artifact_type: {artifact_type}",
            )
        job = mgr.db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        artifacts = mgr.db.list_export_artifacts(job_id)
        art = next((a for a in artifacts if a.artifact_type == artifact_type and a.is_finalized), None)
        if not art:
            raise HTTPException(
                status_code=404,
                detail=f"Finalized artifact '{artifact_type}' not found for job '{job_id}'.",
            )
        safe_path = _ensure_within_runtime_dir(Path(art.file_path), cfg.runtime_dir)
        doc = mgr.db.get_document(job.document_id)
        base_stem = Path(doc.original_filename).stem if doc and doc.original_filename else job.document_id
        suffix_map = {
            "mono_ja_pdf": f"{base_stem}.ja.pdf",
            "dual_bilingual_pdf": f"{base_stem}.dual.pdf",
            "roundtrip_pdf": f"{base_stem}.roundtrip.pdf",
            "validation_report_json": f"{base_stem}.report.json",
        }
        media_type = (
            "application/json; charset=utf-8"
            if artifact_type == "validation_report_json"
            else "application/pdf"
        )
        return _serve_file_with_range(
            request=request,
            file_path=safe_path,
            media_type=media_type,
            download_filename=suffix_map.get(artifact_type, safe_path.name),
            inline=inline,
        )

    @app.post("/api/jobs/{job_id}/resume", response_model=Job)
    async def resume_job_endpoint(
        job_id: str,
        background: bool = False,
        _sec: None = Depends(verify_security_headers),
    ) -> Job:
        _validate_safe_id(job_id, "job_id")
        job = mgr.db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if mgr.is_job_active(job_id) or mgr.has_active_document_operation(job.document_id):
            raise HTTPException(
                status_code=409,
                detail=f"Document '{job.document_id}' / Job '{job_id}' is already running.",
            )
        try:
            mgr.validate_real_model_preflight(job.config)
        except ModelValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "ModelMismatchConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

        if background:
            threading.Thread(
                target=lambda: mgr.run_job(job_id, resume=True),
                daemon=True,
            ).start()
            return mgr.db.get_job(job_id) or job

        try:
            return await anyio.to_thread.run_sync(lambda: mgr.run_job(job_id, resume=True))
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    @app.post("/api/jobs/{job_id}/retranslate-blocks", response_model=Job)
    async def retranslate_specific_blocks(
        job_id: str,
        req: PartialRetranslateRequest,
        _sec: None = Depends(verify_security_headers),
    ) -> Job:
        _validate_safe_id(job_id, "job_id")
        job_existing = mgr.db.get_job(job_id)
        if not job_existing:
            raise HTTPException(status_code=404, detail="Job not found")
        try:
            mgr.validate_real_model_preflight(job_existing.config)
            return await anyio.to_thread.run_sync(
                lambda: mgr.run_job(job_id, resume=True, target_block_ids=set(req.block_ids))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            msg = str(exc)
            if "JobConflict" in msg or "ModelMismatchConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise

    @app.post("/api/jobs/{job_id}/retypeset", response_model=RetypesetJobResponse)
    async def retypeset_job_endpoint(
        job_id: str,
        req: RetypesetJobRequest | None = None,
        _sec: None = Depends(verify_security_headers),
    ) -> RetypesetJobResponse:
        _validate_safe_id(job_id, "job_id")
        payload = req or RetypesetJobRequest()
        try:
            result = await anyio.to_thread.run_sync(
                lambda: mgr.retypeset_job(
                    job_id=job_id,
                    expected_edit_revision=payload.expected_edit_revision,
                    allow_validation_warnings=payload.allow_validation_warnings,
                    fail_stage_for_test=payload.fail_stage_for_test,
                )
            )
            return RetypesetJobResponse(**result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            if "RevisionConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            if "RetypesetValidationFailed" in msg:
                raise HTTPException(status_code=422, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        except RuntimeError as exc:
            msg = str(exc)
            if "JobConflict" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise HTTPException(status_code=500, detail=msg) from exc

    @app.delete("/api/runtime/temp-data")
    def purge_temp_data(
        _sec: None = Depends(verify_security_headers),
    ) -> dict[str, str]:
        """Purge temporary working directory (Section 16)."""
        work_dir = cfg.runtime_dir / "working"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "purged", "directory": str(work_dir)}

    # --- Optional Static Frontend Serving (`frontend/dist`) ---
    dist_dir = frontend_dist_dir or (
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    )
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_spa_or_static(full_path: str) -> Response:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API endpoint not found")
            candidate = (dist_dir / full_path).resolve()
            if (
                full_path
                and candidate.is_relative_to(dist_dir.resolve())
                and candidate.exists()
                and candidate.is_file()
            ):
                return FileResponse(str(candidate))
            return FileResponse(str(dist_dir / "index.html"))

    return app


app = create_app()
