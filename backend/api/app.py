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
) -> FastAPI:
    cfg = config or AppConfig()
    cfg.ensure_directories()
    mgr = job_manager or JobManager(config=cfg)
    active_token = session_token or secrets.token_urlsafe(32)

    app = FastAPI(
        title="PaperBridge Local API",
        version="0.3.0",
        description="Local-only academic PDF translation API prioritizing formula preservation.",
    )
    app.state.config = cfg
    app.state.job_manager = mgr
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
        doc = mgr.db.get_document(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if req.page_index < 0 or req.page_index >= max(1, doc.page_count):
            raise HTTPException(status_code=400, detail="page_index out of bounds")
        region = ProtectionRegion(
            id=f"reg-{uuid.uuid4().hex[:10]}",
            document_id=document_id,
            page_index=req.page_index,
            bbox=req.bbox,
            mode=req.mode,
            source="manual",
            note=req.note,
        )
        mgr.db.save_protection_region(region)
        return region

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
        deleted = mgr.db.delete_protection_region(document_id, region_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Protection region not found")
        return {"deleted": True}

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
            return mgr.db.update_block_manual_translation(
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
            job = mgr.create_job(document_id, job_config=job_config)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        if mgr.is_job_active(job_id):
            raise HTTPException(status_code=409, detail=f"Job '{job_id}' is already running.")

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
        try:
            return await anyio.to_thread.run_sync(
                lambda: mgr.run_job(job_id, resume=True, target_block_ids=set(req.block_ids))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "JobConflict" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
