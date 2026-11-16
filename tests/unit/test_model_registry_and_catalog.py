"""Unit tests for model catalog, GGUF inspection, local registry, migration, and cache key isolation."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from backend.models.catalog import CatalogModel, load_model_catalog
from backend.models.gguf_inspector import compute_file_sha256, inspect_gguf_file
from backend.models.registry import (
    ModelRegistryManager,
    ModelValidationError,
)
from backend.storage.cache import compute_translation_cache_key


def _gguf_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def build_synthetic_gguf(
    *,
    version: int = 3,
    tensor_count: int = 4,
    architecture: str | None = "plamo3",
    model_name: str | None = "plamo-3-translate-2b",
    size_label: str | None = "2B",
    file_type: int | None = 15,
    split_count: int | None = None,
    split_no: int | None = None,
    extra_padding: bytes = b"TESTGGUFDATA",
) -> bytes:
    """Create a deterministic minimal GGUF v2/v3 byte payload safe for offline tests."""
    kvs: list[tuple[str, int, bytes]] = []
    if architecture is not None:
        kvs.append(("general.architecture", 8, _gguf_str(architecture)))
    if model_name is not None:
        kvs.append(("general.name", 8, _gguf_str(model_name)))
    if size_label is not None:
        kvs.append(("general.size_label", 8, _gguf_str(size_label)))
    if file_type is not None:
        kvs.append(("general.file_type", 4, struct.pack("<I", file_type)))
    if split_count is not None:
        kvs.append(("split.count", 2, struct.pack("<H", split_count)))
    if split_no is not None:
        kvs.append(("split.no", 2, struct.pack("<H", split_no)))

    buf = bytearray()
    buf += b"GGUF"
    buf += struct.pack("<IQQ", version, tensor_count, len(kvs))
    for k, vtype, encoded_val in kvs:
        buf += _gguf_str(k)
        buf += struct.pack("<I", vtype)
        buf += encoded_val
    buf += extra_padding
    return bytes(buf)


def build_temp_catalog(
    tmp_path: Path,
    specs: list[dict[str, object]],
) -> Path:
    """Write a temporary test catalog cloned cleanly from production metadata with custom artifacts."""
    prod = json.loads(load_model_catalog().model_dump_json())
    by_id = {m["model_id"]: m for m in prod["models"]}
    selected_models: list[dict[str, object]] = []
    for spec in specs:
        base_id = str(spec["model_id"])
        m = json.loads(json.dumps(by_id[base_id]))
        primary_bytes = bytes(spec["primary_bytes"])
        primary_sha = hashlib.sha256(primary_bytes).hexdigest().lower()
        artifacts = [
            {
                "revision": "rev-primary-test",
                "size_bytes": len(primary_bytes),
                "sha256": primary_sha,
                "label": "test-primary",
            }
        ]
        legacy_bytes = spec.get("legacy_bytes")
        if isinstance(legacy_bytes, (bytes, bytearray)):
            legacy_sha = hashlib.sha256(legacy_bytes).hexdigest().lower()
            artifacts.append(
                {
                    "revision": "rev-legacy-test",
                    "size_bytes": len(legacy_bytes),
                    "sha256": legacy_sha,
                    "label": "test-legacy",
                }
            )
        m["known_size_bytes"] = len(primary_bytes)
        m["known_sha256"] = primary_sha
        m["accepted_artifacts"] = artifacts
        if "download_url" in spec:
            m["download_url"] = str(spec["download_url"])
        selected_models.append(m)

    prod["models"] = selected_models
    cat_path = tmp_path / "test_model_catalog.json"
    cat_path.write_text(json.dumps(prod, ensure_ascii=False, indent=2), encoding="utf-8")
    return cat_path


def test_production_catalog_contains_curated_plamo_models_with_accurate_metadata() -> None:
    catalog = load_model_catalog()
    ids = [m.model_id for m in catalog.models]
    assert ids == [
        "plamo-3-translate-0.8b-q4_k_m",
        "plamo-3-translate-2b-q4_k_m",
        "plamo-3-translate-8b-q4_k_m",
    ]
    default = catalog.get_default_model()
    assert default is not None
    assert default.model_id == "plamo-3-translate-2b-q4_k_m"
    assert default.catalog_verification_status == "verified_runtime"
    assert len(default.all_accepted_artifacts()) == 2  # current + legacy 2B artifact hashes

    m08 = catalog.get_model("plamo-3-translate-0.8b-q4_k_m")
    m8 = catalog.get_model("plamo-3-translate-8b-q4_k_m")
    assert m08 is not None and m08.catalog_verification_status == "catalog_verified_runtime_unverified"
    assert m8 is not None and m8.catalog_verification_status == "catalog_verified_runtime_unverified"
    assert "multi_file_split_gguf" in catalog.unsupported_policies
    assert "plamo_31b" in catalog.unsupported_policies


def test_gguf_inspector_validates_v2_v3_and_rejects_corrupt_or_split(tmp_path: Path) -> None:
    v2_path = tmp_path / "ok_v2.gguf"
    v2_bytes = build_synthetic_gguf(version=2, architecture="plamo3", model_name="plamo-3-translate-0.8b")
    v2_path.write_bytes(v2_bytes)
    insp_v2 = inspect_gguf_file(v2_path)
    assert insp_v2.file_exists and insp_v2.format_valid and not insp_v2.is_split_gguf
    assert insp_v2.gguf_version == 2
    assert insp_v2.architecture == "plamo3"
    assert insp_v2.model_name == "plamo-3-translate-0.8b"
    assert insp_v2.sha256 == hashlib.sha256(v2_bytes).hexdigest()

    v3_path = tmp_path / "ok_v3.gguf"
    v3_bytes = build_synthetic_gguf(version=3, architecture="plamo3", model_name="plamo-3-translate-8b")
    v3_path.write_bytes(v3_bytes)
    insp_v3 = inspect_gguf_file(v3_path)
    assert insp_v3.format_valid and insp_v3.gguf_version == 3 and insp_v3.architecture == "plamo3"

    # Truncated / tiny
    tiny = tmp_path / "tiny.gguf"
    tiny.write_bytes(b"GGUF\x03\x00")
    assert not inspect_gguf_file(tiny, compute_hash=False).format_valid

    # Non-GGUF magic
    bad_magic = tmp_path / "fake.gguf"
    bad_magic.write_bytes(b"NOT_A_GGUF_FILE_0123456789ABCDEF")
    assert not inspect_gguf_file(bad_magic, compute_hash=False).format_valid

    # Split detected by filename
    split_name = tmp_path / "plamo-3-translate-31b-00001-of-00004.gguf"
    split_name.write_bytes(v3_bytes)
    insp_sn = inspect_gguf_file(split_name, compute_hash=False)
    assert insp_sn.is_split_gguf

    # Split detected inside GGUF KV metadata even if renamed
    split_meta = tmp_path / "renamed_split.gguf"
    split_meta.write_bytes(build_synthetic_gguf(split_count=2, split_no=0))
    insp_sm = inspect_gguf_file(split_meta, compute_hash=False)
    assert insp_sm.is_split_gguf


def test_registry_rejects_non_plamo_31b_unknown_sha_and_wrong_target(tmp_path: Path) -> None:
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"2B")
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"08B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    mgr = ModelRegistryManager(runtime_dir=tmp_path / "runtime", catalog_path=cat_path)

    # 1. Non-plamo architecture rejected explicitly
    llama_p = tmp_path / "llama3.gguf"
    llama_p.write_bytes(build_synthetic_gguf(architecture="llama", model_name="llama-3-8b"))
    with pytest.raises(ModelValidationError, match="plamo3"):
        mgr.register_local_model(file_path=llama_p, license_accepted=True)

    # 2. PLaMo 31B rejected even with plamo3 architecture
    p31b = tmp_path / "plamo-3-translate-31b-Q4_K_M.gguf"
    p31b.write_bytes(build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-31b"))
    with pytest.raises(ModelValidationError, match="31B"):
        mgr.register_local_model(file_path=p31b, license_accepted=True)

    # 3. Unknown SHA-256 plamo3 GGUF never silently treated as 2B
    unknown_p = tmp_path / "custom-plamo-2b-quant.gguf"
    unknown_p.write_bytes(build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"UNVERIFIED"))
    with pytest.raises(ModelValidationError, match="SHA-256"):
        mgr.register_local_model(file_path=unknown_p, license_accepted=True)

    # 4. Valid 0.8B file rejected if caller explicitly expected 2B
    f08 = tmp_path / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    f08.write_bytes(bytes_08b)
    with pytest.raises(ModelValidationError, match="一致しません"):
        mgr.register_local_model(
            file_path=f08,
            expected_catalog_model_id="plamo-3-translate-2b-q4_k_m",
            license_accepted=True,
        )

    # 5. Requires license agreement
    with pytest.raises(ModelValidationError, match="同意"):
        mgr.register_local_model(file_path=f08, license_accepted=False)


def test_reference_vs_copy_japanese_spaces_self_copy_guard_and_persistence(tmp_path: Path) -> None:
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"2B_DATA")
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"08B_DATA")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    runtime_dir = tmp_path / "テスト runtime 保存先"
    mgr = ModelRegistryManager(runtime_dir=runtime_dir, catalog_path=cat_path)

    # Reference mode in Japanese + spaced folder without copying
    ext_dir = tmp_path / "手元モデル 保存先 (外部)"
    ext_dir.mkdir(parents=True)
    ext_08b = ext_dir / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    ext_08b.write_bytes(bytes_08b)

    reg08 = mgr.register_local_model(
        file_path=ext_08b,
        register_mode="reference",
        license_accepted=True,
        set_active=True,
    )
    assert reg08.source_type == "reference"
    assert Path(reg08.file_path).resolve() == ext_08b.resolve()
    assert not (mgr.managed_models_dir / "plamo-3-translate-0.8b-GGUF" / "plamo-3-translate-0.8b-Q4_K_M.gguf").exists()

    # Copy mode copies into managed dir and leaves original intact
    ext_2b = ext_dir / "plamo-3-translate-2b-Q4_K_M.gguf"
    ext_2b.write_bytes(bytes_2b)
    reg2b = mgr.register_local_model(
        file_path=ext_2b,
        register_mode="copy",
        license_accepted=True,
        set_active=False,
    )
    assert reg2b.source_type == "managed_copy"
    managed_2b = mgr.managed_models_dir / "plamo-3-translate-2b-GGUF" / "plamo-3-translate-2b-Q4_K_M.gguf"
    assert managed_2b.is_file()
    assert Path(reg2b.file_path).resolve() == managed_2b.resolve()
    assert ext_2b.is_file()  # source file untouched

    # Self-copy guard: re-registering managed file with copy mode preserves intact content
    reg2b_self = mgr.register_local_model(
        file_path=managed_2b,
        register_mode="copy",
        license_accepted=True,
        set_active=False,
    )
    assert compute_file_sha256(managed_2b) == hashlib.sha256(bytes_2b).hexdigest()
    assert reg2b_self.registration_id == reg2b.registration_id

    # Restart persistence & switching
    mgr2 = ModelRegistryManager(runtime_dir=runtime_dir, catalog_path=cat_path)
    _cat, snap = mgr2.get_registry_snapshot()
    assert len(snap.models) == 2
    assert snap.active_model_id == reg08.registration_id

    mgr2.select_active_model(reg2b.registration_id)
    active_after, _ = mgr2.get_active_model()
    assert active_after is not None and active_after.catalog_model_id == "plamo-3-translate-2b-q4_k_m"

    # Unregistering reference model preserves underlying GGUF on disk
    assert mgr2.remove_registration(reg08.registration_id)
    assert ext_08b.is_file()

    # Missing file detection + relocation via replaces_registration_id
    managed_2b.unlink()
    _cat, snap_missing = mgr2.get_registry_snapshot(revalidate=True)
    missing_entry = next(m for m in snap_missing.models if m.registration_id == reg2b.registration_id)
    assert not missing_entry.is_usable
    assert missing_entry.verification.hash_status == "missing_file"

    relocated = mgr2.register_local_model(
        file_path=ext_2b,
        register_mode="reference",
        license_accepted=True,
        replaces_registration_id=reg2b.registration_id,
        set_active=True,
    )
    assert relocated.registration_id == reg2b.registration_id
    assert relocated.is_usable
    assert Path(relocated.file_path).resolve() == ext_2b.resolve()


def test_legacy_2b_auto_migration_without_redownload(tmp_path: Path) -> None:
    legacy_bytes = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"LEGACY_2B")
    current_bytes = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"CURRENT_2B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {
                "model_id": "plamo-3-translate-2b-q4_k_m",
                "primary_bytes": current_bytes,
                "legacy_bytes": legacy_bytes,
            }
        ],
    )
    runtime_dir = tmp_path / "existing_2b_runtime"
    legacy_file = runtime_dir / "models" / "plamo-3-translate-2b-GGUF" / "plamo-3-translate-2b-Q4_K_M.gguf"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(legacy_bytes)

    mgr = ModelRegistryManager(runtime_dir=runtime_dir, catalog_path=cat_path)
    _cat, reg = mgr.get_registry_snapshot(auto_migrate=True, revalidate=True)
    assert len(reg.models) == 1
    migrated = reg.models[0]
    assert migrated.source_type == "migrated_legacy"
    assert migrated.is_usable
    assert migrated.verification.matched_label == "test-legacy"
    assert reg.active_model_id == migrated.registration_id


def test_cache_key_isolates_different_models_hashes_quantizations_and_prompts() -> None:
    k_2b = compute_translation_cache_key(
        "Hello {v1}",
        model_name="plamo-3-translate-2b-Q4_K_M",
        model_id="plamo-3-translate-2b-q4_k_m",
        model_hash="1ffd9073",
        quantization="Q4_K_M",
        prompt_version="pfml3-v1",
    )
    k_08b = compute_translation_cache_key(
        "Hello {v1}",
        model_name="plamo-3-translate-0.8b-Q4_K_M",
        model_id="plamo-3-translate-0.8b-q4_k_m",
        model_hash="70891ec7",
        quantization="Q4_K_M",
        prompt_version="pfml3-v1",
    )
    k_legacy_2b = compute_translation_cache_key(
        "Hello {v1}",
        model_name="plamo-3-translate-2b-Q4_K_M",
        model_id="plamo-3-translate-2b-q4_k_m",
        model_hash="f36d0813",
        quantization="Q4_K_M",
        prompt_version="pfml3-v1",
    )
    k_quant = compute_translation_cache_key(
        "Hello {v1}",
        model_name="plamo-3-translate-2b-Q4_K_M",
        model_id="plamo-3-translate-2b-q4_k_m",
        model_hash="1ffd9073",
        quantization="Q8_0",
        prompt_version="pfml3-v1",
    )
    k_prompt = compute_translation_cache_key(
        "Hello {v1}",
        model_name="plamo-3-translate-2b-Q4_K_M",
        model_id="plamo-3-translate-2b-q4_k_m",
        model_hash="1ffd9073",
        quantization="Q4_K_M",
        prompt_version="pfml3-v2",
    )
    assert len({k_2b, k_08b, k_legacy_2b, k_quant, k_prompt}) == 5


def test_server_match_matrix_detects_exact_copy_mismatch_and_unverified(tmp_path: Path) -> None:
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"2B")
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"08B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b},
        ],
    )
    mgr = ModelRegistryManager(runtime_dir=tmp_path / "rt", catalog_path=cat_path)

    # Before registration: always no_active_model even if server is unreachable
    assert mgr.compare_with_running_server({"status": "server_unreachable"}).match_status == "no_active_model"
    assert mgr.compare_with_running_server({"status": "connected"}).match_status == "no_active_model"

    p2b = tmp_path / "plamo-3-translate-2b-Q4_K_M.gguf"
    p2b.write_bytes(bytes_2b)
    p2b_dup = tmp_path / "dup" / "plamo-3-translate-2b-Q4_K_M.gguf"
    p2b_dup.parent.mkdir()
    p2b_dup.write_bytes(bytes_2b)
    p08b = tmp_path / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    p08b.write_bytes(bytes_08b)

    mgr.register_local_model(file_path=p2b, license_accepted=True, set_active=True)

    unreachable_res = mgr.compare_with_running_server({"status": "server_unreachable"})
    assert unreachable_res.match_status == "server_unreachable"
    assert "接続できません" in unreachable_res.message_ja
    assert "停止中" not in unreachable_res.message_ja

    # Exact path match
    m_exact = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-2b-q4_k_m",
            "model_path_full": str(p2b),
            "model_file": p2b.name,
        }
    )
    assert m_exact.match_status == "matched" and m_exact.is_matched

    # Same model at different path
    m_diff_path = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-2b-q4_k_m",
            "model_path_full": str(p2b_dup),
            "model_file": p2b_dup.name,
        }
    )
    assert m_diff_path.match_status == "matched_same_model_different_path" and m_diff_path.is_matched

    # Mismatch: running 0.8B while 2B is active
    m_mismatch = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-0.8b-q4_k_m",
            "model_path_full": str(p08b),
            "model_file": p08b.name,
        }
    )
    assert m_mismatch.match_status == "mismatch" and not m_mismatch.is_matched

    # Connected without model_path -> server_path_unverified (unless alias mismatches)
    m_unver = mgr.compare_with_running_server({"status": "connected", "model_name": "plamo-3-translate-2b-q4_k_m"})
    assert m_unver.match_status == "server_path_unverified" and m_unver.is_matched

    m_alias_mismatch = mgr.compare_with_running_server({"status": "connected", "model_name": "plamo-3-translate-0.8b-q4_k_m"})
    assert m_alias_mismatch.match_status == "mismatch" and not m_alias_mismatch.is_matched

    # Active model removed from disk -> selected_model_invalid
    p2b.unlink()
    m_invalid = mgr.compare_with_running_server({"status": "connected"})
    assert m_invalid.match_status == "selected_model_invalid" and not m_invalid.is_matched


def test_four_stage_verification_lifecycle_and_fail_closed_transitions(tmp_path: Path) -> None:
    """Verify honest 4-stage progression (unverified / file_verified / launch_verified / translation_verified)."""
    bytes_2b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"4STAGE_2B")
    bytes_2b_alt = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-2b", extra_padding=b"4STAGE_2B_ALT")
    bytes_08b = build_synthetic_gguf(architecture="plamo3", model_name="plamo-3-translate-0.8b", extra_padding=b"4STAGE_08B")
    cat_path = build_temp_catalog(
        tmp_path,
        [
            {"model_id": "plamo-3-translate-2b-q4_k_m", "primary_bytes": bytes_2b, "legacy_bytes": bytes_2b_alt},
            {"model_id": "plamo-3-translate-0.8b-q4_k_m", "primary_bytes": bytes_08b},
        ],
    )
    mgr = ModelRegistryManager(runtime_dir=tmp_path / "rt_stages", catalog_path=cat_path)

    # Sanity check: production 2B catalog metadata still carries verified_runtime without leaking into fresh instance flags.
    cat_2b = mgr.get_catalog().get_model("plamo-3-translate-2b-q4_k_m")
    assert cat_2b is not None and cat_2b.catalog_verification_status == "verified_runtime"

    p2b = tmp_path / "plamo-3-translate-2b-Q4_K_M.gguf"
    p2b.write_bytes(bytes_2b)
    p2b_dup = tmp_path / "dup" / "plamo-3-translate-2b-Q4_K_M.gguf"
    p2b_dup.parent.mkdir()
    p2b_dup.write_bytes(bytes_2b)
    p08b = tmp_path / "plamo-3-translate-0.8b-Q4_K_M.gguf"
    p08b.write_bytes(bytes_08b)

    # 1. Fresh registration of 2B starts at file_verified (NOT launch_verified / translation_verified)
    reg2 = mgr.register_local_model(file_path=p2b, license_accepted=True, set_active=True)
    assert reg2.verification.hash_status == "matched_known"
    assert reg2.verification.launch_verified is False
    assert reg2.verification.translation_verified is False
    assert reg2.verification.verification_stage == "file_verified"

    # 2. Ambiguous / mismatched server probes MUST NOT promote launch_verified
    m_diff = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-2b-q4_k_m",
            "model_path_full": str(p2b_dup),
            "model_file": p2b_dup.name,
        }
    )
    assert m_diff.match_status == "matched_same_model_different_path"
    m_unver = mgr.compare_with_running_server({"status": "connected", "model_name": "plamo-3-translate-2b-q4_k_m"})
    assert m_unver.match_status == "server_path_unverified"
    m_mismatch = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-0.8b-q4_k_m",
            "model_path_full": str(p08b),
            "model_file": p08b.name,
        }
    )
    assert m_mismatch.match_status == "mismatch"

    after_ambiguous, _ = mgr.get_active_model()
    assert after_ambiguous is not None
    assert after_ambiguous.verification.launch_verified is False
    assert after_ambiguous.verification.translation_verified is False
    assert after_ambiguous.verification.verification_stage == "file_verified"

    # 3. Exact server match promotes launch_verified only (still not translation_verified)
    m_exact = mgr.compare_with_running_server(
        {
            "status": "connected",
            "model_name": "plamo-3-translate-2b-q4_k_m",
            "model_path_full": str(p2b),
            "model_file": p2b.name,
        }
    )
    assert m_exact.match_status == "matched"
    after_launch, _ = mgr.get_active_model()
    assert after_launch is not None
    assert after_launch.verification.launch_verified is True
    assert after_launch.verification.translation_verified is False
    assert after_launch.verification.verification_stage == "launch_verified"

    # 4. Translation verification sets translation_verified (and propagates launch_verified)
    mgr.mark_runtime_verified(reg2.registration_id, launch_verified=False, translation_verified=True)
    after_trans, _ = mgr.get_active_model()
    assert after_trans is not None
    assert after_trans.verification.launch_verified is True
    assert after_trans.verification.translation_verified is True
    assert after_trans.verification.verification_stage == "translation_verified"

    # 5. Swapping file contents to another accepted hash on the same path resets runtime flags -> file_verified
    p2b.write_bytes(bytes_2b_alt)
    after_swap_reval, _ = mgr.get_active_model()
    assert after_swap_reval is not None
    assert after_swap_reval.verification.launch_verified is False
    assert after_swap_reval.verification.translation_verified is False
    assert after_swap_reval.verification.verification_stage == "file_verified"

    # Re-promote to translation_verified, then verify register_local_model with swapped hash on same path also resets
    mgr.mark_runtime_verified(reg2.registration_id, launch_verified=True, translation_verified=True)
    p2b.write_bytes(bytes_2b)
    re_reg = mgr.register_local_model(file_path=p2b, license_accepted=True, set_active=True)
    assert re_reg.verification.launch_verified is False
    assert re_reg.verification.translation_verified is False
    assert re_reg.verification.verification_stage == "file_verified"

    # Re-registering identical file+hash on same path preserves runtime verification
    mgr.mark_runtime_verified(re_reg.registration_id, launch_verified=True, translation_verified=True)
    re_reg_same = mgr.register_local_model(file_path=p2b, license_accepted=True, set_active=True)
    assert re_reg_same.verification.launch_verified is True
    assert re_reg_same.verification.translation_verified is True
    assert re_reg_same.verification.verification_stage == "translation_verified"

    # 6. Deleting file drops verification_stage -> unverified immediately and blocks mark_runtime_verified
    p2b.unlink()
    missing_active, _ = mgr.get_active_model()
    assert missing_active is not None
    assert missing_active.is_usable is False
    assert missing_active.verification.hash_status == "missing_file"
    assert missing_active.verification.verification_stage == "unverified"

    # Tampered/corrupt file on same path clears runtime flags -> unverified
    p2b.write_bytes(b"CORRUPT_NON_GGUF_BYTES")
    corrupt_active, _ = mgr.get_active_model()
    assert corrupt_active is not None
    assert corrupt_active.is_usable is False
    assert corrupt_active.verification.launch_verified is False
    assert corrupt_active.verification.translation_verified is False
    assert corrupt_active.verification.verification_stage == "unverified"

    # mark_runtime_verified MUST refuse to promote any unusable (corrupt or missing) entry
    mgr.mark_runtime_verified(re_reg_same.registration_id, launch_verified=True, translation_verified=True)
    p2b.unlink()
    missing_fresh, _ = mgr.get_active_model()
    assert missing_fresh is not None
    assert missing_fresh.verification.verification_stage == "unverified"
    assert missing_fresh.verification.launch_verified is False
    assert missing_fresh.verification.translation_verified is False
    mgr.mark_runtime_verified(re_reg_same.registration_id, launch_verified=True, translation_verified=True)
    _, snap_no_reval = mgr.get_registry_snapshot(revalidate=False)
    still_missing = next(
        (m for m in snap_no_reval.models if m.registration_id == re_reg_same.registration_id),
        None,
    )
    assert still_missing is not None
    assert still_missing.verification.verification_stage == "unverified"
    assert still_missing.verification.launch_verified is False
    assert still_missing.verification.translation_verified is False
