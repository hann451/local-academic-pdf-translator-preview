"""Local model registration, layered validation, download, and active selection state.

Keeps local machine paths and registration records strictly inside `runtime_dir`
(`<UserDataDir>/models_registry.json`) separated from the repository's curated
`model_catalog.json`.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field, computed_field

from backend.models.catalog import CatalogArtifact, CatalogModel, ModelCatalog, load_model_catalog
from backend.models.gguf_inspector import (
    GgufInspectionResult,
    compute_file_sha256,
    inspect_gguf_file,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path_key(path: str | Path) -> str:
    resolved = os.path.realpath(str(path))
    return os.path.normcase(resolved)


def _paths_refer_to_same_file(a: Path, b: Path) -> bool:
    if _normalize_path_key(a) == _normalize_path_key(b):
        return True
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return False


VerificationStage = Literal[
    "unverified",
    "file_verified",
    "launch_verified",
    "translation_verified",
]


class ModelVerificationState(BaseModel):
    """Layered verification state distinguishing structure, hash, compatibility, and runtime."""

    file_exists: bool = False
    format_valid: bool = False
    hash_status: Literal["matched_known", "mismatch", "unverified", "missing_file"] = "unverified"
    backend_compatible: bool = False
    launch_verified: bool = False
    translation_verified: bool = False
    matched_revision: str | None = None
    matched_label: str | None = None
    validation_error: str | None = None
    validated_at: str = Field(default_factory=_utc_now_iso)

    @computed_field
    @property
    def verification_stage(self) -> VerificationStage:
        file_ok = (
            self.file_exists
            and self.format_valid
            and self.hash_status == "matched_known"
            and self.backend_compatible
        )
        if not file_ok:
            return "unverified"
        if self.translation_verified:
            return "translation_verified"
        if self.launch_verified:
            return "launch_verified"
        return "file_verified"


class RegisteredModel(BaseModel):
    """Locally registered model instance stored strictly in runtime_dir."""

    registration_id: str
    catalog_model_id: str
    display_name: str
    short_name: str
    provider: str
    parameter_size: str
    quantization: str
    source_type: Literal["downloaded", "reference", "managed_copy", "migrated_legacy"]
    file_path: str
    file_name: str
    file_size_bytes: int = 0
    file_mtime_ns: int = 0
    measured_sha256: str = ""
    gguf_architecture: str | None = None
    gguf_model_name: str | None = None
    verification: ModelVerificationState = Field(default_factory=ModelVerificationState)
    license_accepted: bool = False
    license_accepted_at: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)

    @property
    def is_usable(self) -> bool:
        v = self.verification
        return (
            v.file_exists
            and v.format_valid
            and v.hash_status == "matched_known"
            and v.backend_compatible
        )


class ModelsRegistryFile(BaseModel):
    schema_version: int = 1
    active_model_id: str | None = None
    models: list[RegisteredModel] = Field(default_factory=list)


class ModelServerMatchResult(BaseModel):
    """Comparison between UI active registered model and live llama-server process."""

    match_status: Literal[
        "matched",
        "matched_same_model_different_path",
        "mismatch",
        "server_unreachable",
        "no_active_model",
        "server_path_unverified",
        "selected_model_invalid",
    ]
    is_matched: bool = False
    active_registration_id: str | None = None
    active_catalog_model_id: str | None = None
    active_display_name: str | None = None
    active_file_path: str | None = None
    active_sha256_prefix: str | None = None
    running_loaded_alias: str | None = None
    running_model_path: str | None = None
    running_model_file: str | None = None
    running_identified_model_id: str | None = None
    running_identified_display_name: str | None = None
    message_ja: str


class ModelDownloadJob(BaseModel):
    download_id: str
    catalog_model_id: str
    display_name: str
    destination_path: str
    status: Literal["downloading", "verifying", "completed", "canceled", "failed"]
    bytes_downloaded: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    error: str | None = None
    registration_id: str | None = None
    started_at: str = Field(default_factory=_utc_now_iso)
    finished_at: str | None = None


class ModelValidationError(ValueError):
    """Explicit rejection reason when inspecting/registering an invalid or unsupported model."""


class ModelRegistryManager:
    """Manages `models_registry.json` persistence, validation, and legacy 2B migration."""

    def __init__(
        self,
        runtime_dir: Path,
        catalog_path: Path | None = None,
        extra_legacy_search_dirs: list[Path] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.catalog_path = catalog_path
        self.extra_legacy_search_dirs = [Path(p) for p in (extra_legacy_search_dirs or [])]
        self.registry_path = self.runtime_dir / "models_registry.json"
        self.managed_models_dir = self.runtime_dir / "models"
        self._lock = threading.RLock()
        self._hash_cache: dict[tuple[str, int, int], GgufInspectionResult] = {}

    def get_catalog(self) -> ModelCatalog:
        return load_model_catalog(self.catalog_path)

    def _inspect_cached(self, path: Path, *, compute_hash: bool = True) -> GgufInspectionResult:
        try:
            st = path.stat()
            cache_key = (_normalize_path_key(path), int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            return inspect_gguf_file(path, compute_hash=compute_hash)

        cached = self._hash_cache.get(cache_key)
        if cached is not None and (not compute_hash or bool(cached.sha256)):
            return cached
        res = inspect_gguf_file(path, compute_hash=compute_hash)
        if res.file_exists and (not compute_hash or bool(res.sha256)):
            self._hash_cache[cache_key] = res
        return res

    def _read_raw_registry(self) -> ModelsRegistryFile:
        if not self.registry_path.is_file():
            return ModelsRegistryFile()
        raw = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
        return ModelsRegistryFile.model_validate(raw)

    def _write_raw_registry(self, reg: ModelsRegistryFile) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_name(f".{self.registry_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        payload = reg.model_dump_json(indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.registry_path)

    def _validate_against_catalog(
        self,
        insp: GgufInspectionResult,
        catalog: ModelCatalog,
        expected_catalog_model_id: str | None = None,
    ) -> tuple[CatalogModel, CatalogArtifact, ModelVerificationState]:
        now = _utc_now_iso()
        if not insp.file_exists:
            raise ModelValidationError(insp.error or "指定されたモデルファイルが見つかりません。")
        if insp.is_split_gguf:
            reason = catalog.unsupported_policies.get(
                "multi_file_split_gguf",
                "分割 GGUF（multi-file）は初期版の対象外です。単一 GGUF ファイルを使用してください。",
            )
            raise ModelValidationError(reason)
        if not insp.format_valid:
            raise ModelValidationError(insp.error or "有効な GGUF ファイルではありません。")

        arch = (insp.architecture or "").strip().lower()
        if arch != "plamo3":
            raise ModelValidationError(
                f"非対応のモデルアーキテクチャです（general.architecture='{insp.architecture or '不明'}'）。"
                "初期版は PLaMo 3 Translate（plamo3 / PFML3形式）の単一 GGUF のみ対応しており、"
                "他モデルを 2B 等として誤認登録することはできません。"
            )

        gname = (insp.model_name or "").lower()
        if "31b" in gname or insp.file_size_bytes > 10_000_000_000:
            reason = catalog.unsupported_policies.get(
                "plamo_31b",
                "PLaMo 3 Translate 31B 等の超大容量モデルは初期版の導入対象外です。",
            )
            raise ModelValidationError(reason)

        matched = catalog.match_by_sha256(insp.sha256)
        if matched is None:
            raise ModelValidationError(
                "検証済みモデルカタログの既知 SHA-256 と一致しないため登録できません"
                f"（実測ファイル: {Path(insp.file_path).name}, サイズ: {insp.file_size_bytes:,} bytes, "
                f"SHA-256先頭: {insp.sha256[:12]}）。"
                "破損ファイルまたはカタログ未検証・未対応量子化モデルを誤って既存モデル扱いすることはできません。"
            )

        actual_cat, matched_art = matched
        if insp.file_size_bytes != matched_art.size_bytes:
            raise ModelValidationError(
                f"ファイルサイズが一致しません（期待: {matched_art.size_bytes:,} bytes / 実測: {insp.file_size_bytes:,} bytes）。"
            )

        if expected_catalog_model_id and actual_cat.model_id != expected_catalog_model_id:
            expected_cat = catalog.get_model(expected_catalog_model_id)
            expected_name = expected_cat.display_name if expected_cat else expected_catalog_model_id
            raise ModelValidationError(
                f"選択されたファイルは「{actual_cat.display_name}」ですが、"
                f"指定された導入先モデル「{expected_name}」と一致しません。"
                "正しいファイルを選択するか、自動判定または該当モデルを選んで登録してください。"
            )

        # File validation confirms existence, GGUF structure, SHA-256, and backend compatibility.
        # Runtime launch and real translation are verified separately on live server / real job completion.
        ver = ModelVerificationState(
            file_exists=True,
            format_valid=True,
            hash_status="matched_known",
            backend_compatible=("llama.cpp" in actual_cat.supported_backends),
            launch_verified=False,
            translation_verified=False,
            matched_revision=matched_art.revision,
            matched_label=matched_art.label,
            validation_error=None,
            validated_at=now,
        )
        return actual_cat, matched_art, ver

    def _revalidate_entry_in_place(
        self,
        entry: RegisteredModel,
        catalog: ModelCatalog,
    ) -> tuple[RegisteredModel, bool]:
        """Re-check existence and integrity when missing or file stats changed."""
        p = Path(entry.file_path)
        cat = catalog.get_model(entry.catalog_model_id)
        now = _utc_now_iso()
        if not p.is_file():
            if entry.verification.file_exists or entry.verification.hash_status != "missing_file":
                entry.verification.file_exists = False
                entry.verification.hash_status = "missing_file"
                entry.verification.backend_compatible = False
                entry.verification.validation_error = (
                    f"登録済みモデルファイルが見つかりません: {p.name}（移動・削除またはパス変更された可能性があります）。"
                    "ファイル参照の再指定または再ダウンロードを行ってください。"
                )
                entry.verification.validated_at = now
                entry.updated_at = now
                return entry, True
            return entry, False

        st = p.stat()
        size_changed = int(st.st_size) != entry.file_size_bytes
        mtime_changed = int(st.st_mtime_ns) != entry.file_mtime_ns
        if (
            not entry.verification.file_exists
            or size_changed
            or mtime_changed
            or not entry.measured_sha256
        ):
            old_sha = entry.measured_sha256
            old_cat_id = entry.catalog_model_id
            insp = self._inspect_cached(p, compute_hash=True)
            entry.file_size_bytes = insp.file_size_bytes
            entry.file_mtime_ns = insp.file_mtime_ns
            entry.measured_sha256 = insp.sha256
            entry.gguf_architecture = insp.architecture
            entry.gguf_model_name = insp.model_name
            entry.updated_at = now
            try:
                actual_cat, _art, ver = self._validate_against_catalog(
                    insp,
                    catalog,
                    expected_catalog_model_id=entry.catalog_model_id if cat else None,
                )
                # Preserve prior runtime flags only when the exact same verified artifact remains in place.
                same_artifact = (
                    bool(old_sha)
                    and old_sha.lower() == insp.sha256.lower()
                    and old_cat_id == actual_cat.model_id
                )
                if same_artifact:
                    ver.launch_verified = ver.launch_verified or entry.verification.launch_verified
                    ver.translation_verified = (
                        ver.translation_verified or entry.verification.translation_verified
                    )
                entry.catalog_model_id = actual_cat.model_id
                entry.verification = ver
            except ModelValidationError as exc:
                entry.verification = ModelVerificationState(
                    file_exists=insp.file_exists,
                    format_valid=insp.format_valid,
                    hash_status="mismatch" if insp.format_valid else "unverified",
                    backend_compatible=False,
                    launch_verified=False,
                    translation_verified=False,
                    validation_error=str(exc),
                    validated_at=now,
                )
            return entry, True
        return entry, False

    def _migrate_legacy_2b_if_needed(self, reg: ModelsRegistryFile, catalog: ModelCatalog) -> bool:
        """Auto-register legacy 2B GGUF so existing installations keep working without re-download."""
        default_2b = catalog.get_model("plamo-3-translate-2b-q4_k_m")
        if not default_2b:
            return False

        known_paths = {_normalize_path_key(m.file_path) for m in reg.models}
        search_roots = [self.runtime_dir, *self.extra_legacy_search_dirs]
        changed = False

        for root in search_roots:
            candidate = root / "models" / default_2b.relative_subdir / default_2b.filename
            if not candidate.is_file():
                continue
            cand_key = _normalize_path_key(candidate)
            if cand_key in known_paths:
                continue
            # Match known legacy/current 2B sizes before computing full hash to stay fast & safe
            st_size = candidate.stat().st_size
            accepted_sizes = {a.size_bytes for a in default_2b.all_accepted_artifacts()}
            if st_size not in accepted_sizes:
                continue
            insp = self._inspect_cached(candidate, compute_hash=True)
            try:
                actual_cat, _art, ver = self._validate_against_catalog(
                    insp,
                    catalog,
                    expected_catalog_model_id=default_2b.model_id,
                )
            except ModelValidationError:
                continue

            now = _utc_now_iso()
            safe_slug = actual_cat.model_id.replace(".", "_")
            item = RegisteredModel(
                registration_id=f"reg-{safe_slug}-legacy",
                catalog_model_id=actual_cat.model_id,
                display_name=actual_cat.display_name,
                short_name=actual_cat.short_name,
                provider=actual_cat.provider,
                parameter_size=actual_cat.parameter_size,
                quantization=actual_cat.quantization,
                source_type="migrated_legacy",
                file_path=str(Path(insp.file_path)),
                file_name=Path(insp.file_path).name,
                file_size_bytes=insp.file_size_bytes,
                file_mtime_ns=insp.file_mtime_ns,
                measured_sha256=insp.sha256,
                gguf_architecture=insp.architecture,
                gguf_model_name=insp.model_name,
                verification=ver,
                license_accepted=True,
                license_accepted_at=now,
                created_at=now,
                updated_at=now,
            )
            reg.models.append(item)
            known_paths.add(cand_key)
            if not reg.active_model_id:
                reg.active_model_id = item.registration_id
            changed = True
        return changed

    def get_registry_snapshot(
        self,
        *,
        auto_migrate: bool = True,
        revalidate: bool = True,
    ) -> tuple[ModelCatalog, ModelsRegistryFile]:
        with self._lock:
            catalog = self.get_catalog()
            reg = self._read_raw_registry()
            dirty = False

            if auto_migrate and self._migrate_legacy_2b_if_needed(reg, catalog):
                dirty = True

            if revalidate:
                updated_models: list[RegisteredModel] = []
                for entry in reg.models:
                    updated, entry_changed = self._revalidate_entry_in_place(entry, catalog)
                    updated_models.append(updated)
                    if entry_changed:
                        dirty = True
                reg.models = updated_models

            # Ensure active_model_id points to an existing registration id if valid
            if reg.active_model_id and not any(
                m.registration_id == reg.active_model_id for m in reg.models
            ):
                reg.active_model_id = None
                dirty = True

            if dirty:
                self._write_raw_registry(reg)
            return catalog, reg

    def get_active_model(self) -> tuple[RegisteredModel | None, CatalogModel | None]:
        catalog, reg = self.get_registry_snapshot()
        if not reg.active_model_id:
            return None, None
        for m in reg.models:
            if m.registration_id == reg.active_model_id:
                return m, catalog.get_model(m.catalog_model_id)
        return None, None

    def register_local_model(
        self,
        *,
        file_path: str | Path,
        expected_catalog_model_id: str | None = None,
        register_mode: Literal["reference", "copy"] = "reference",
        license_accepted: bool = False,
        set_active: bool = True,
        replaces_registration_id: str | None = None,
        source_type_override: Literal["downloaded", "reference", "managed_copy", "migrated_legacy"]
        | None = None,
        has_active_job: bool = False,
    ) -> RegisteredModel:
        if has_active_job and set_active:
            raise RuntimeError(
                "ModelSwitchConflict: 翻訳ジョブ実行中はアクティブモデルを切り替える登録ができません。"
            )
        with self._lock:
            catalog, reg = self.get_registry_snapshot(auto_migrate=True, revalidate=True)
            src_path = Path(str(file_path).strip().strip('"'))
            insp = inspect_gguf_file(src_path, compute_hash=True)
            if insp.file_exists and insp.sha256:
                self._hash_cache[
                    (_normalize_path_key(insp.file_path), insp.file_size_bytes, insp.file_mtime_ns)
                ] = insp
            actual_cat, _art, ver = self._validate_against_catalog(
                insp,
                catalog,
                expected_catalog_model_id=expected_catalog_model_id,
            )

            if actual_cat.license.requires_agreement and not license_accepted:
                raise ModelValidationError(
                    f"「{actual_cat.display_name}」を登録するには、モデル利用条件（{actual_cat.license.name}）への同意が必要です。"
                )

            resolved_src = Path(insp.file_path)
            final_path = resolved_src
            final_source_type: Literal["downloaded", "reference", "managed_copy", "migrated_legacy"]

            if source_type_override is not None:
                final_source_type = source_type_override
            elif register_mode == "copy":
                target_dir = self.managed_models_dir / actual_cat.relative_subdir
                target_path = (target_dir / actual_cat.filename).resolve()
                if _paths_refer_to_same_file(resolved_src, target_path):
                    # Self-copy guard: never copy a file onto itself or risk truncation!
                    final_path = resolved_src
                    final_source_type = "managed_copy"
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    tmp_copy = target_dir / f"{actual_cat.filename}.{uuid.uuid4().hex[:8]}.copying"
                    try:
                        with resolved_src.open("rb") as f_in, tmp_copy.open("wb") as f_out:
                            while True:
                                chunk = f_in.read(8 * 1024 * 1024)
                                if not chunk:
                                    break
                                f_out.write(chunk)
                        copied_sha = compute_file_sha256(tmp_copy)
                        if copied_sha != insp.sha256 or tmp_copy.stat().st_size != insp.file_size_bytes:
                            raise ModelValidationError(
                                "管理ディレクトリへのコピー中にファイル内容が一致しなくなりました（コピー中止）。"
                            )
                        os.replace(tmp_copy, target_path)
                    finally:
                        if tmp_copy.exists():
                            try:
                                tmp_copy.unlink()
                            except OSError:
                                pass
                    final_path = target_path
                    insp = self._inspect_cached(final_path, compute_hash=True)
                    final_source_type = "managed_copy"
            else:
                final_source_type = "reference"

            now = _utc_now_iso()
            final_key = _normalize_path_key(final_path)

            existing: RegisteredModel | None = None
            if replaces_registration_id:
                existing = next(
                    (m for m in reg.models if m.registration_id == replaces_registration_id),
                    None,
                )
            if existing is None:
                existing = next(
                    (m for m in reg.models if _normalize_path_key(m.file_path) == final_key),
                    None,
                )

            if existing is not None:
                # Remove any stale duplicate at final_key when replacing another registration ID
                reg.models = [
                    m
                    for m in reg.models
                    if m.registration_id == existing.registration_id
                    or _normalize_path_key(m.file_path) != final_key
                ]
                reg_id = existing.registration_id
                created_at = existing.created_at
                same_path_and_artifact = (
                    _normalize_path_key(existing.file_path) == final_key
                    and bool(existing.measured_sha256)
                    and existing.measured_sha256.lower() == insp.sha256.lower()
                    and existing.catalog_model_id == actual_cat.model_id
                )
                if same_path_and_artifact:
                    ver.launch_verified = ver.launch_verified or existing.verification.launch_verified
                    ver.translation_verified = (
                        ver.translation_verified or existing.verification.translation_verified
                    )
            else:
                safe_slug = actual_cat.model_id.replace(".", "_")
                reg_id = f"reg-{safe_slug}-{uuid.uuid4().hex[:8]}"
                created_at = now

            entry = RegisteredModel(
                registration_id=reg_id,
                catalog_model_id=actual_cat.model_id,
                display_name=actual_cat.display_name,
                short_name=actual_cat.short_name,
                provider=actual_cat.provider,
                parameter_size=actual_cat.parameter_size,
                quantization=actual_cat.quantization,
                source_type=final_source_type,
                file_path=str(final_path),
                file_name=final_path.name,
                file_size_bytes=insp.file_size_bytes,
                file_mtime_ns=insp.file_mtime_ns,
                measured_sha256=insp.sha256,
                gguf_architecture=insp.architecture,
                gguf_model_name=insp.model_name,
                verification=ver,
                license_accepted=license_accepted,
                license_accepted_at=now if license_accepted else None,
                created_at=created_at,
                updated_at=now,
            )

            replaced = False
            for idx, item in enumerate(reg.models):
                if item.registration_id == entry.registration_id:
                    reg.models[idx] = entry
                    replaced = True
                    break
            if not replaced:
                reg.models.append(entry)

            if set_active or not reg.active_model_id:
                reg.active_model_id = entry.registration_id

            self._write_raw_registry(reg)
            return entry

    def select_active_model(
        self,
        registration_id: str,
        *,
        has_active_job: bool = False,
    ) -> RegisteredModel:
        if has_active_job:
            raise RuntimeError(
                "ModelSwitchConflict: 翻訳ジョブ実行中は使用モデルを切り替えられません。"
                "進行中のジョブを完了または停止した後に切り替えてください。"
            )
        with self._lock:
            catalog, reg = self.get_registry_snapshot(auto_migrate=True, revalidate=True)
            target = next(
                (m for m in reg.models if m.registration_id == registration_id),
                None,
            )
            if target is None:
                raise KeyError(f"登録済みモデルが見つかりません: {registration_id}")
            if not target.is_usable:
                raise ModelValidationError(
                    target.verification.validation_error
                    or f"選択されたモデル「{target.display_name}」は検証を通過していないため使用できません。"
                )
            if catalog.get_model(target.catalog_model_id) is None:
                raise ModelValidationError(
                    f"カタログ外モデル '{target.catalog_model_id}' は選択できません。"
                )
            reg.active_model_id = target.registration_id
            target.updated_at = _utc_now_iso()
            self._write_raw_registry(reg)
            return target

    def remove_registration(self, registration_id: str) -> bool:
        with self._lock:
            _cat, reg = self.get_registry_snapshot(auto_migrate=False, revalidate=False)
            before = len(reg.models)
            reg.models = [m for m in reg.models if m.registration_id != registration_id]
            if len(reg.models) == before:
                return False
            if reg.active_model_id == registration_id:
                first_usable = next((m.registration_id for m in reg.models if m.is_usable), None)
                reg.active_model_id = first_usable or (
                    reg.models[0].registration_id if reg.models else None
                )
            self._write_raw_registry(reg)
            return True

    def mark_runtime_verified(
        self,
        registration_id: str,
        *,
        launch_verified: bool = True,
        translation_verified: bool = False,
    ) -> None:
        effective_launch = launch_verified or translation_verified
        with self._lock:
            _cat, reg = self.get_registry_snapshot(auto_migrate=False, revalidate=False)
            for entry in reg.models:
                if entry.registration_id == registration_id:
                    if not entry.is_usable:
                        return
                    changed = False
                    if effective_launch and not entry.verification.launch_verified:
                        entry.verification.launch_verified = True
                        changed = True
                    if translation_verified and not entry.verification.translation_verified:
                        entry.verification.translation_verified = True
                        changed = True
                    if changed:
                        entry.updated_at = _utc_now_iso()
                        self._write_raw_registry(reg)
                    return

    def compare_with_running_server(
        self,
        health: dict[str, Any] | None,
    ) -> ModelServerMatchResult:
        """Compare active selected model against the model actually loaded by llama-server."""
        catalog, reg = self.get_registry_snapshot(auto_migrate=True, revalidate=True)
        active = (
            next((m for m in reg.models if m.registration_id == reg.active_model_id), None)
            if reg.active_model_id
            else None
        )

        running_alias = (
            str(health["model_name"])
            if isinstance(health, dict) and isinstance(health.get("model_name"), str)
            else None
        )
        running_path_full = (
            str(health["model_path_full"])
            if isinstance(health, dict) and isinstance(health.get("model_path_full"), str)
            else None
        )
        running_file = (
            str(health["model_file"])
            if isinstance(health, dict) and isinstance(health.get("model_file"), str)
            else None
        )
        health_status = str(health.get("status", "server_unreachable")) if isinstance(health, dict) else "not_probed"

        # Identify which catalog model the running llama-server has loaded (when possible)
        running_cat_id: str | None = None
        running_cat_name: str | None = None
        running_sha: str | None = None

        if running_path_full and Path(running_path_full).is_file():
            # Fast match against already-registered models first
            norm_running = _normalize_path_key(running_path_full)
            reg_hit = next((m for m in reg.models if _normalize_path_key(m.file_path) == norm_running), None)
            if reg_hit and reg_hit.measured_sha256:
                running_sha = reg_hit.measured_sha256
                running_cat_id = reg_hit.catalog_model_id
                running_cat_name = reg_hit.display_name
            else:
                # Inspect local running server's GGUF without touching the server
                insp_run = self._inspect_cached(Path(running_path_full), compute_hash=True)
                matched = catalog.match_by_sha256(insp_run.sha256) if insp_run.sha256 else None
                if matched:
                    cat_m, _art = matched
                    running_sha = insp_run.sha256
                    running_cat_id = cat_m.model_id
                    running_cat_name = cat_m.display_name
        elif running_alias:
            cat_by_alias = next(
                (
                    c
                    for c in catalog.models
                    if c.launch_settings.alias == running_alias or c.model_id == running_alias
                ),
                None,
            )
            if cat_by_alias:
                running_cat_id = cat_by_alias.model_id
                running_cat_name = cat_by_alias.display_name
        if not running_cat_id and running_file:
            cat_by_file = next(
                (c for c in catalog.models if c.filename.lower() == running_file.lower()),
                None,
            )
            if cat_by_file:
                running_cat_id = cat_by_file.model_id
                running_cat_name = cat_by_file.display_name

        if active is None:
            return ModelServerMatchResult(
                match_status="no_active_model",
                is_matched=False,
                running_loaded_alias=running_alias,
                running_model_path=running_path_full,
                running_model_file=running_file,
                running_identified_model_id=running_cat_id,
                running_identified_display_name=running_cat_name,
                message_ja="使用する登録済みモデルが選択されていません（設定・モデル管理からモデルを登録・選択してください）。",
            )

        if not active.is_usable:
            return ModelServerMatchResult(
                match_status="selected_model_invalid",
                is_matched=False,
                active_registration_id=active.registration_id,
                active_catalog_model_id=active.catalog_model_id,
                active_display_name=active.display_name,
                active_file_path=active.file_path,
                active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                running_loaded_alias=running_alias,
                running_model_path=running_path_full,
                running_model_file=running_file,
                running_identified_model_id=running_cat_id,
                running_identified_display_name=running_cat_name,
                message_ja=active.verification.validation_error
                or f"選択中のモデル「{active.display_name}」は無効またはファイル欠落状態です。",
            )

        if health_status != "connected":
            reason_ja = (
                "未確認（まだ接続確認を行っていません）"
                if health_status == "not_probed"
                else (
                    f"推論エンドポイントへ接続できません: {health_status}。"
                    "サーバー未起動のほか、ポート・URL設定の差異や接続制限の可能性があります"
                )
            )
            return ModelServerMatchResult(
                match_status="server_unreachable",
                is_matched=False,
                active_registration_id=active.registration_id,
                active_catalog_model_id=active.catalog_model_id,
                active_display_name=active.display_name,
                active_file_path=active.file_path,
                active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                running_loaded_alias=running_alias,
                running_model_path=running_path_full,
                running_model_file=running_file,
                running_identified_model_id=running_cat_id,
                running_identified_display_name=running_cat_name,
                message_ja=f"選択中モデル: {active.display_name}（{reason_ja}）",
            )

        # Connected! Verify running server identity against active registered model
        if running_path_full:
            if _normalize_path_key(running_path_full) == _normalize_path_key(active.file_path):
                self.mark_runtime_verified(
                    active.registration_id,
                    launch_verified=True,
                    translation_verified=False,
                )
                return ModelServerMatchResult(
                    match_status="matched",
                    is_matched=True,
                    active_registration_id=active.registration_id,
                    active_catalog_model_id=active.catalog_model_id,
                    active_display_name=active.display_name,
                    active_file_path=active.file_path,
                    active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                    running_loaded_alias=running_alias,
                    running_model_path=running_path_full,
                    running_model_file=running_file,
                    running_identified_model_id=running_cat_id or active.catalog_model_id,
                    running_identified_display_name=running_cat_name or active.display_name,
                    message_ja=f"一致: 選択中モデル（{active.display_name}）と稼働中 llama-server のロード済みモデルが一致しています。",
                )
            if (running_sha and active.measured_sha256 and running_sha == active.measured_sha256) or (
                running_cat_id and running_cat_id == active.catalog_model_id
            ):
                return ModelServerMatchResult(
                    match_status="matched_same_model_different_path",
                    is_matched=True,
                    active_registration_id=active.registration_id,
                    active_catalog_model_id=active.catalog_model_id,
                    active_display_name=active.display_name,
                    active_file_path=active.file_path,
                    active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                    running_loaded_alias=running_alias,
                    running_model_path=running_path_full,
                    running_model_file=running_file,
                    running_identified_model_id=running_cat_id,
                    running_identified_display_name=running_cat_name,
                    message_ja=(
                        f"同一カタログモデル一致（別パス）: 選択中「{active.display_name}」と同一モデルですが、"
                        f"稼働中 llama-server は別パスのファイル（{running_file or running_path_full}）を読み込んでいます。"
                    ),
                )
            # Explicit MISMATCH!
            running_label = running_cat_name or running_file or running_alias or "不明モデル"
            return ModelServerMatchResult(
                match_status="mismatch",
                is_matched=False,
                active_registration_id=active.registration_id,
                active_catalog_model_id=active.catalog_model_id,
                active_display_name=active.display_name,
                active_file_path=active.file_path,
                active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                running_loaded_alias=running_alias,
                running_model_path=running_path_full,
                running_model_file=running_file,
                running_identified_model_id=running_cat_id,
                running_identified_display_name=running_label,
                message_ja=(
                    f"モデル不一致警告: UIでは「{active.display_name}」が選択されていますが、"
                    f"稼働中の llama-server は別のモデル「{running_label}」をロードしています。"
                    "切り替えるにはアプリ／llama-server を選択モデルで再起動してください。"
                ),
            )

        # Fallback if /props lacked model_path (e.g. mock or restricted endpoint)
        if running_cat_id and running_cat_id != active.catalog_model_id:
            return ModelServerMatchResult(
                match_status="mismatch",
                is_matched=False,
                active_registration_id=active.registration_id,
                active_catalog_model_id=active.catalog_model_id,
                active_display_name=active.display_name,
                active_file_path=active.file_path,
                active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
                running_loaded_alias=running_alias,
                running_model_path=running_path_full,
                running_model_file=running_file,
                running_identified_model_id=running_cat_id,
                running_identified_display_name=running_cat_name,
                message_ja=(
                    f"モデル不一致警告: 選択中は「{active.display_name}」ですが、"
                    f"推論サーバは「{running_cat_name}」を応答しました。"
                ),
            )

        return ModelServerMatchResult(
            match_status="server_path_unverified",
            is_matched=True,
            active_registration_id=active.registration_id,
            active_catalog_model_id=active.catalog_model_id,
            active_display_name=active.display_name,
            active_file_path=active.file_path,
            active_sha256_prefix=active.measured_sha256[:12] if active.measured_sha256 else None,
            running_loaded_alias=running_alias,
            running_model_path=running_path_full,
            running_model_file=running_file,
            running_identified_model_id=running_cat_id,
            running_identified_display_name=running_cat_name,
            message_ja=(
                f"llama-server 接続済み（{running_alias or 'alias不明'} / 実ファイルパス未取得）。"
                f"選択中モデル: {active.display_name}"
            ),
        )


class ModelDownloadManager:
    """Thread-safe model downloader with `.downloading` isolation, cancel cleanup, and hash verification."""

    def __init__(
        self,
        registry: ModelRegistryManager,
        http_client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.registry = registry
        self.http_client_factory = http_client_factory
        self._lock = threading.Lock()
        self._jobs: dict[str, ModelDownloadJob] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def _new_client(self) -> httpx.Client:
        if self.http_client_factory is not None:
            return self.http_client_factory()
        return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))

    def list_downloads(self) -> list[ModelDownloadJob]:
        with self._lock:
            return [job.model_copy(deep=True) for job in self._jobs.values()]

    def get_download(self, download_id: str) -> ModelDownloadJob | None:
        with self._lock:
            job = self._jobs.get(download_id)
            return job.model_copy(deep=True) if job else None

    def cancel_download(self, download_id: str) -> ModelDownloadJob | None:
        with self._lock:
            ev = self._cancel_events.get(download_id)
            job = self._jobs.get(download_id)
            if job is None:
                return None
            if ev is not None:
                ev.set()
            return job.model_copy(deep=True)

    def start_download(
        self,
        *,
        catalog_model_id: str,
        license_accepted: bool,
        set_active: bool = True,
        background: bool = True,
        has_active_translation_job: bool = False,
    ) -> ModelDownloadJob:
        catalog = self.registry.get_catalog()
        cat = catalog.get_model(catalog_model_id)
        if cat is None:
            raise KeyError(f"カタログに存在しないモデルです: {catalog_model_id}")
        if cat.license.requires_agreement and not license_accepted:
            raise ModelValidationError(
                f"「{cat.display_name}」をダウンロードするには、{cat.license.name} 等の利用条件への同意が必要です。"
            )
        if has_active_translation_job and set_active:
            raise RuntimeError(
                "ModelSwitchConflict: 翻訳ジョブ実行中はダウンロード後の自動モデル切替を指定できません。"
            )

        dest_dir = (self.registry.managed_models_dir / cat.relative_subdir).resolve()
        dest_path = dest_dir / cat.filename
        download_id = f"dl-{uuid.uuid4().hex[:10]}"
        cancel_ev = threading.Event()
        job = ModelDownloadJob(
            download_id=download_id,
            catalog_model_id=cat.model_id,
            display_name=cat.display_name,
            destination_path=str(dest_path),
            status="downloading",
            bytes_downloaded=0,
            total_bytes=cat.known_size_bytes,
            percent=0.0,
        )
        with self._lock:
            self._jobs[download_id] = job
            self._cancel_events[download_id] = cancel_ev

        if background:
            t = threading.Thread(
                target=self._run_download,
                args=(download_id, cat, dest_path, license_accepted, set_active, cancel_ev),
                daemon=True,
            )
            t.start()
            return job.model_copy(deep=True)

        self._run_download(download_id, cat, dest_path, license_accepted, set_active, cancel_ev)
        return self.get_download(download_id) or job

    def _run_download(
        self,
        download_id: str,
        cat: CatalogModel,
        dest_path: Path,
        license_accepted: bool,
        set_active: bool,
        cancel_ev: threading.Event,
    ) -> None:
        tmp_path = dest_path.with_name(f"{dest_path.name}.{download_id}.downloading")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._new_client() as client:
                with client.stream("GET", cat.download_url) as resp:
                    resp.raise_for_status()
                    hdr_len = resp.headers.get("content-length")
                    total_bytes = int(hdr_len) if hdr_len and hdr_len.isdigit() else cat.known_size_bytes
                    downloaded = 0
                    with tmp_path.open("wb") as f_out:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            if cancel_ev.is_set():
                                raise _DownloadCanceledError("ユーザーによりダウンロードがキャンセルされました。")
                            if not chunk:
                                continue
                            f_out.write(chunk)
                            downloaded += len(chunk)
                            pct = min(100.0, (downloaded / total_bytes) * 100.0) if total_bytes > 0 else 0.0
                            with self._lock:
                                cur = self._jobs[download_id]
                                cur.bytes_downloaded = downloaded
                                cur.total_bytes = total_bytes
                                cur.percent = round(pct, 1)

            if cancel_ev.is_set():
                raise _DownloadCanceledError("ユーザーによりダウンロードがキャンセルされました。")

            with self._lock:
                self._jobs[download_id].status = "verifying"

            # Full integrity verification before moving to final path
            insp = inspect_gguf_file(tmp_path, compute_hash=True)
            if not insp.format_valid or insp.is_split_gguf or (insp.architecture or "").lower() != "plamo3":
                raise ModelValidationError(
                    insp.error or "ダウンロードされたファイルが有効な PLaMo3 GGUF 形式ではありません。"
                )
            if insp.file_size_bytes != cat.known_size_bytes:
                raise ModelValidationError(
                    f"ダウンロードファイルサイズが不一致です（期待: {cat.known_size_bytes:,} / 実測: {insp.file_size_bytes:,} bytes）。"
                )
            if insp.sha256.lower() != cat.known_sha256.lower():
                raise ModelValidationError(
                    f"SHA-256 整合性検証に失敗しました（期待: {cat.known_sha256[:12]}... / 実測: {insp.sha256[:12]}...）。破損ファイルは削除されました。"
                )

            os.replace(tmp_path, dest_path)
            entry = self.registry.register_local_model(
                file_path=dest_path,
                expected_catalog_model_id=cat.model_id,
                register_mode="reference",
                license_accepted=license_accepted,
                set_active=set_active,
                source_type_override="downloaded",
            )
            with self._lock:
                cur = self._jobs[download_id]
                cur.status = "completed"
                cur.percent = 100.0
                cur.bytes_downloaded = insp.file_size_bytes
                cur.total_bytes = insp.file_size_bytes
                cur.registration_id = entry.registration_id
                cur.finished_at = _utc_now_iso()
        except _DownloadCanceledError as exc:
            with self._lock:
                cur = self._jobs[download_id]
                cur.status = "canceled"
                cur.error = str(exc)
                cur.finished_at = _utc_now_iso()
        except Exception as exc:
            with self._lock:
                cur = self._jobs[download_id]
                cur.status = "failed"
                cur.error = str(exc)
                cur.finished_at = _utc_now_iso()
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


class _DownloadCanceledError(RuntimeError):
    """Internal sentinel for user-canceled download cleanup."""
