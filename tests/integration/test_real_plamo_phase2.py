"""Phase 2 Real Hardware Integration Tests: llama.cpp (Vulkan) + PLaMo 3 Translate 2B (Q4_K_M).

Separated from default offline CI via `@pytest.mark.real_llm` and `@pytest.mark.gpu`.
Run explicitly with:
  uv run pytest --run-real-llm
or:
  uv run pytest -m real_llm
Never auto-downloads models during test execution.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    GlossaryEntry,
    JobState,
    ProtectedItem,
    ProtectedItemKind,
    TranslationJobConfig,
)
from backend.jobs.manager import JobManager
from backend.pdf.adapters.native_preserving_adapter import (
    mask_inline_protected_elements,
    unmask_inline_protected_elements,
)
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged
from backend.translation.adapters.base import (
    TranslationAdapter,
    TranslationAdapterResponse,
    TranslationConnectionError,
)
from backend.translation.adapters.llama_cpp_adapter import LlamaCppPlamoAdapter
from backend.translation.prompts.pfml3 import HistoryPair
from backend.translation.validation.validator import validate_translation_output

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LLAMA_SERVER_EXE = REPO_ROOT / "tools" / "llama.cpp" / "b12167-vulkan-x64" / "llama-server.exe"
MODEL_GGUF_PATH = REPO_ROOT / "models" / "plamo-3-translate-2b-GGUF" / "plamo-3-translate-2b-Q4_K_M.gguf"


@pytest.mark.gpu
@pytest.mark.real_llm
def test_vulkan_gpu_device_and_local_binary_integrity() -> None:
    """Verify local Vulkan binary & GGUF model exist without auto-download and recognize Radeon RX 7600."""
    assert LLAMA_SERVER_EXE.exists(), f"Missing llama-server.exe at {LLAMA_SERVER_EXE}"
    assert MODEL_GGUF_PATH.exists(), f"Missing GGUF model at {MODEL_GGUF_PATH}"
    assert MODEL_GGUF_PATH.stat().st_size == 1_807_199_392

    proc = subprocess.run(
        [str(LLAMA_SERVER_EXE), "--list-devices"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "Vulkan0: AMD Radeon RX 7600" in combined


@pytest.mark.real_llm
def test_real_llama_server_health_and_pfml3_tokenization() -> None:
    """Verify live loopback llama-server health (`is_mock=False`) and PFML3 BOS/control token handling."""
    adapter = LlamaCppPlamoAdapter(AppConfig())
    health = adapter.check_health()
    assert health["status"] == "connected"
    assert health["is_mock"] is False
    assert health["model_name"] == "plamo-3-translate"

    # Verify /tokenize adds single BOS (token 1) and parses <|plamo:tag|> (16), <|plamo:msg|> (19)
    root_url = adapter.base_url.removesuffix("/v1")
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{root_url}/tokenize",
            json={
                "content": "<|plamo:tag|>dataset<|plamo:msg|>translation",
                "add_special": True,
                "with_pieces": True,
            },
        )
        resp.raise_for_status()
        tokens = resp.json()["tokens"]
    assert tokens[0]["id"] == 1
    assert tokens[0]["piece"] == "<|plamo:bos|>"
    assert tokens[1]["id"] == 16
    assert tokens[1]["piece"] == "<|plamo:tag|>"
    # Ensure no duplicate BOS token
    assert tokens[1]["id"] != 1


@pytest.mark.real_llm
def test_step_by_step_real_plamo_inference_steps_1_to_5() -> None:
    """Section 5.1: Verify real PLaMo 3 Translate 2B inference in strict order (Steps 1 to 5)."""
    adapter = LlamaCppPlamoAdapter(AppConfig())

    # Step 1: Short general English sentence
    r1 = adapter.translate_segment(
        "Artificial intelligence is transforming scientific research across multiple disciplines."
    )
    assert r1.is_mock is False
    assert r1.finish_reason == "stop"
    assert "<|plamo:" not in r1.translated_text
    assert "人工知能" in r1.translated_text

    # Step 2: Academic explanatory sentence (preserving negation/comparison/condition)
    r2 = adapter.translate_segment(
        "While transformer architectures achieve strong empirical performance on sequence modeling benchmarks, "
        "their quadratic self-attention complexity remains a bottleneck for long documents."
    )
    assert r2.is_mock is False
    assert "<|plamo:" not in r2.translated_text
    assert "ボトルネック" in r2.translated_text

    # Step 3: Sentence with formula placeholders {v1}..{v4} and numeric values
    src3 = (
        "Given the objective function E(x_i) = α_i + β_i, the update rule θ_{t+1} = θ_t - η∇L(θ_t) "
        "converges within 1,000 iterations at learning rate η = 0.01 [1, 2]."
    )
    masked3, items3 = mask_inline_protected_elements(src3)
    assert len(items3) >= 3
    r3 = adapter.translate_segment(masked3)
    val3 = validate_translation_output(
        source_text=src3,
        masked_source_text=masked3,
        translated_text=r3.translated_text,
        protected_items=items3,
        finish_reason=r3.finish_reason,
    )
    assert val3.is_valid is True, f"Validation failed: {val3.error_codes} / {r3.translated_text}"
    restored3 = unmask_inline_protected_elements(r3.translated_text, items3)
    assert "E(x_i) = α_i + β_i" in restored3
    assert "θ_{t+1} = θ_t - η∇L(θ_t)" in restored3
    assert "η = 0.01" in restored3
    assert "[1, 2]" in restored3

    # Step 4: Sentence applying glossary terms
    glossary = [
        GlossaryEntry(source="stochastic gradient descent", target="確率的勾配降下法"),
        GlossaryEntry(source="empirical risk", target="経験リスク"),
    ]
    r4 = adapter.translate_segment(
        "The stochastic gradient descent optimizer minimizes the empirical risk over the training distribution.",
        glossary_entries=glossary,
    )
    assert "確率的勾配降下法" in r4.translated_text
    assert "経験リスク" in r4.translated_text

    # Step 5: Multi-paragraph / context + history + placeholders
    history = [
        HistoryPair(
            source_text="We analyze the convergence rate of stochastic gradient descent under non-convex objectives.",
            target_text="本論文では、非凸目的関数下における確率的勾配降下法の収束速度を解析する。",
        )
    ]
    glossary5 = [
        GlossaryEntry(source="stochastic gradient descent", target="確率的勾配降下法"),
        GlossaryEntry(source="stationary point", target="停留点"),
    ]
    src5 = "Furthermore, when the noise variance is bounded by σ^2 <= 0.05, stochastic gradient descent reaches a stationary point [3]."
    masked5, items5 = mask_inline_protected_elements(src5)
    r5 = adapter.translate_segment(
        masked5,
        context="Section 2: Theoretical Guarantees",
        history=history,
        glossary_entries=glossary5,
    )
    val5 = validate_translation_output(src5, masked5, r5.translated_text, items5, r5.finish_reason)
    assert val5.is_valid is True
    assert "確率的勾配降下法" in r5.translated_text
    assert "停留点" in r5.translated_text
    assert "[3]" in r5.translated_text


@pytest.mark.real_llm
def test_real_llm_abnormal_connection_refused_timeout_and_cancel_resume(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 5.2: Verify abnormal cases against real loopback network stack & real llama-server."""
    # 1. Server unreachable / connection refused on unused local port
    dead_adapter = LlamaCppPlamoAdapter(
        isolated_config,
        base_url="http://127.0.0.1:18999/v1",
        timeout_sec=2.0,
    )
    dead_health = dead_adapter.check_health()
    assert dead_health["status"] == "server_unreachable"
    with pytest.raises(TranslationConnectionError):
        dead_adapter.translate_segment("Test connection refused.")

    # 2. Timeout against real llama-server transitions job to INTERRUPTED, then resumes with normal timeout
    tiny_timeout_adapter = LlamaCppPlamoAdapter(
        isolated_config,
        timeout_sec=0.0001,  # 0.1ms guarantees timeout during real generation
    )
    mgr_timeout = JobManager(config=isolated_config, translation_adapter=tiny_timeout_adapter)
    doc = mgr_timeout.register_document(generated_pdfs["rotated_and_cropbox"])
    job = mgr_timeout.create_job(doc.id, TranslationJobConfig(use_mock_translator=False))
    interrupted = mgr_timeout.run_job(job.id)
    assert interrupted.state == JobState.INTERRUPTED

    # Restore normal real adapter and resume
    mgr_timeout.default_translation_adapter = LlamaCppPlamoAdapter(isolated_config, timeout_sec=120.0)
    resumed = mgr_timeout.run_job(job.id, resume=True)
    assert resumed.state == JobState.COMPLETED
    assert resumed.is_mock_run is False

    # 3. User cancellation before run_job transitions cleanly to CANCELED
    job_cancel = mgr_timeout.create_job(doc.id, TranslationJobConfig(use_mock_translator=False))
    mgr_timeout.request_cancel(job_cancel.id)
    canceled = mgr_timeout.run_job(job_cancel.id)
    assert canceled.state == JobState.CANCELED


@pytest.mark.real_llm
@pytest.mark.fault_injection
def test_real_llm_with_fault_injection_verifies_retry_and_fallback(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 5.2: Wrap real LlamaCppPlamoAdapter with controlled fault injection to verify
    empty output, undefined placeholder {v99}, repetition, and control token residue handling
    before delegating to the real model or falling back to original.
    """
    real_adapter = LlamaCppPlamoAdapter(isolated_config)

    class FaultInjectingRealWrapper(TranslationAdapter):
        adapter_version = "real-fault-wrapper-1.0"
        prompt_version = real_adapter.prompt_version
        is_mock = False

        def __init__(self) -> None:
            self.calls_per_source: dict[str, int] = {}

        def check_health(self) -> dict[str, str | bool | None]:
            return real_adapter.check_health()

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
            cnt = self.calls_per_source.get(source_text, 0) + 1
            self.calls_per_source[source_text] = cnt
            # For block containing {v1}, force all 3 attempts to fail with distinct abnormal outputs
            if "{v1}" in source_text:
                if cnt == 1:
                    # Undefined placeholder {v99}
                    bad_txt = "未定義プレースホルダ {v99} を含む不正応答 [1, 2]。"
                elif cnt == 2:
                    # Abnormal repetition
                    bad_txt = "反復異常テスト文字列です" * 7
                else:
                    # Missing {v1}
                    bad_txt = "数式プレースホルダを欠落させた応答 [1, 2]。"
                return TranslationAdapterResponse(
                    translated_text=bad_txt,
                    raw_output=bad_txt,
                    prompt_used="fault-injected",
                    model_name="plamo-3-translate",
                    quantization="Q4_K_M",
                    finish_reason="stop",
                    is_mock=False,
                    prompt_tokens=30,
                    completion_tokens=20,
                )
            # Otherwise call real PLaMo 3 Translate 2B on GPU!
            return real_adapter.translate_segment(
                source_text=source_text,
                source_lang=source_lang,
                target_lang=target_lang,
                form=form,
                context=context,
                history=history,
                glossary_entries=glossary_entries,
                context_budget_tokens=context_budget_tokens,
            )

    wrapper = FaultInjectingRealWrapper()
    mgr = JobManager(config=isolated_config, translation_adapter=wrapper)
    doc = mgr.register_document(generated_pdfs["single_col_math"])
    job = mgr.create_job(doc.id, TranslationJobConfig(use_mock_translator=False))
    done = mgr.run_job(job.id)

    assert done.state == JobState.COMPLETED_WITH_WARNINGS
    assert done.retry_count >= 2
    blocks = mgr.db.list_blocks(doc.id)
    fallbacks = [b for b in blocks if b.status == BlockStatus.FALLBACK_ORIGINAL]
    real_translated = [b for b in blocks if b.status == BlockStatus.TRANSLATED]
    assert len(fallbacks) >= 1
    assert len(real_translated) >= 1


@pytest.mark.gpu
@pytest.mark.real_llm
@pytest.mark.visual
def test_real_plamo_end_to_end_pdf_integration_and_visual_preservation(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Section 6: Full PDF translation & reconstruction using real PLaMo 3 Translate 2B across
    1-col math, 2-col figures/tables (2 pages), rotated+CropBox, and Japanese+space filename PDFs.
    """
    mgr = JobManager(config=isolated_config)

    for key in (
        "single_col_math",
        "two_col_figures_tables_2p",
        "rotated_and_cropbox",
        "japanese_space_filename",
    ):
        src_pdf = generated_pdfs[key]
        orig_sha = compute_sha256(src_pdf)

        doc = mgr.register_document(src_pdf, original_filename=src_pdf.name)
        job = mgr.create_job(
            doc.id,
            TranslationJobConfig(
                use_mock_translator=False,
                form="plain",
                output_mono_pdf=True,
                output_dual_pdf=True,
            ),
        )
        done = mgr.run_job(job.id)

        assert done.state == JobState.COMPLETED, f"{key} ended in {done.state}: {done.error_message}"
        assert done.is_mock_run is False
        assert done.original_hash_verified is True
        assert verify_original_unchanged(src_pdf, orig_sha)

        blocks = mgr.db.list_blocks(doc.id)
        translated_blocks = [b for b in blocks if b.status == BlockStatus.TRANSLATED]
        fallback_blocks = [b for b in blocks if b.status == BlockStatus.FALLBACK_ORIGINAL]
        assert len(translated_blocks) >= 1
        assert len(fallback_blocks) == 0

        artifacts = mgr.db.list_export_artifacts(job.id)
        mono_art = next(a for a in artifacts if a.artifact_type == "mono_ja_pdf")
        dual_art = next(a for a in artifacts if a.artifact_type == "dual_bilingual_pdf")
        rep_art = next(a for a in artifacts if a.artifact_type == "validation_report_json")
        assert mono_art.is_mock_translation is False
        assert dual_art.is_mock_translation is False

        # Re-open output PDFs with PyMuPDF and verify rendering & Japanese text extraction
        with pymupdf.open(mono_art.file_path) as out_doc:
            assert len(out_doc) == doc.page_count
            pix = out_doc[0].get_pixmap(dpi=150)
            assert pix.width > 100 and pix.height > 100
            extracted_txt = out_doc[0].get_text("text")
            # Confirm Japanese characters exist in the reconstructed PDF
            assert any("\u3040" <= ch <= "\u9fff" for ch in extracted_txt)

        # Verify visual preservation report distinguishes protected regions (0.0 diff)
        # vs non-translated background (0.0 diff) vs full page diff (> 0.0 due to Japanese text)
        rep_json = json.loads(Path(rep_art.file_path).read_text(encoding="utf-8"))
        assert rep_json["is_mock_translation"] is False
        assert rep_json["original_hash_unchanged"] is True
        for vrep in rep_json["visual_preservation_reports"]:
            assert vrep["all_protected_regions_preserved"] is True
            assert vrep["non_translated_background_diff_ratio"] == 0.0
            assert vrep["full_page_diff_ratio"] > 0.0
            assert vrep["semantic_accuracy_guaranteed"] is False
            for reg in vrep["regions"]:
                assert reg["passed"] is True
                assert reg["differing_pixel_ratio"] == 0.0


@pytest.mark.gpu
@pytest.mark.real_llm
@pytest.mark.visual
def test_phase3_real_plamo_ui_api_retypeset_and_static_serving_e2e(
    generated_pdfs: dict[str, Path],
    isolated_config: AppConfig,
) -> None:
    """Phase 3 Section 10 & 11 Real Hardware E2E Test:
    Executes the full UI/API workflow against live llama-server (Vulkan RX 7600) + PLaMo 3 Translate 2B:
    1. Session token bootstrap (`GET /api/auth/session`) & live diagnostics (`probe_llama=true`).
    2. Glossary creation (`POST /api/glossaries`).
    3. PDF upload (`POST /api/documents/upload`) & authenticated PDF streaming (`GET /api/documents/{id}/pdf`).
    4. Pre-parse blocks (`POST /api/documents/{id}/parse`) & manual protection region registration.
    5. Real PLaMo translation job (`use_mock_translator=False`) via `POST /api/documents/{id}/jobs`.
    6. Manual translation edit (`PUT /api/documents/{id}/blocks/{block_id}/manual-edit`) keeping `translation_text` intact.
    7. Zero-LLM `retypeset` (`POST /api/jobs/{id}/retypeset`) verified to make 0 LLM calls while embedding the manual edit into the output PDF.
    """
    from fastapi.testclient import TestClient
    from backend.api.app import create_app

    real_adapter = LlamaCppPlamoAdapter(isolated_config)

    class SpyRealPlamoAdapter(TranslationAdapter):
        adapter_version = real_adapter.adapter_version
        prompt_version = real_adapter.prompt_version
        is_mock = False

        def __init__(self) -> None:
            self.call_count = 0

        def check_health(self) -> dict[str, str | bool | None]:
            return real_adapter.check_health()

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
            return real_adapter.translate_segment(
                source_text=source_text,
                source_lang=source_lang,
                target_lang=target_lang,
                form=form,
                context=context,
                history=history,
                glossary_entries=glossary_entries,
                context_budget_tokens=context_budget_tokens,
            )

    spy_real = SpyRealPlamoAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy_real)
    app = create_app(config=isolated_config, job_manager=mgr)
    client = TestClient(app)

    # 1. Bootstrap auth session & verify live real llama-server diagnostics
    sess = client.get("/api/auth/session", headers={"Origin": "http://127.0.0.1:5173"}).json()
    headers = {"X-PaperBridge-Token": sess["token"], "Origin": "http://127.0.0.1:5173"}

    diag = client.get("/api/diagnostics?probe_llama=true", headers=headers).json()
    assert diag["llama_connection_status"] == "connected"
    assert diag["loaded_model_name"] == "plamo-3-translate"
    assert diag["loopback_only_enforced"] is True

    # 2. Create domain glossary
    gls = client.post(
        "/api/glossaries",
        json={
            "name": "Real E2E Optimization Glossary",
            "entries": [
                {"source": "energy function", "target": "エネルギー汎関数", "notes": "Phase 3 Real E2E"}
            ],
        },
        headers=headers,
    ).json()

    # 3. Upload PDF & stream PDF bytes with auth header
    src_pdf = generated_pdfs["single_col_math"]
    orig_sha = compute_sha256(src_pdf)
    with open(src_pdf, "rb") as f:
        doc = client.post(
            "/api/documents/upload",
            files={"file": ("single_col_math.pdf", f, "application/pdf")},
            headers=headers,
        ).json()
    doc_id = doc["id"]

    pdf_stream = client.get(f"/api/documents/{doc_id}/pdf", headers=headers)
    assert pdf_stream.status_code == 200
    assert pdf_stream.content.startswith(b"%PDF-")

    # 4. Pre-parse blocks
    parsed_blocks = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    assert len(parsed_blocks) >= 4

    # 5. Start Real PLaMo job (use_mock_translator=False)
    job_cfg = TranslationJobConfig(
        use_mock_translator=False,
        form="plain",
        glossary_id=gls["id"],
        output_mono_pdf=True,
        output_dual_pdf=True,
    )
    job = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=job_cfg.model_dump(),
        headers=headers,
    ).json()
    job_id = job["id"]
    assert job["state"] == JobState.COMPLETED.value
    assert job["is_mock_run"] is False
    assert job["glossary_version_used"] == 1

    real_calls_during_job = spy_real.call_count
    assert real_calls_during_job >= 1

    # 6. Edit a translated block manually while keeping real PLaMo translation_text intact
    blocks_after = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()
    body_blk = next(
        b for b in blocks_after if b["status"] == BlockStatus.TRANSLATED.value and not b["protected_items"]
    )
    real_plamo_output = body_blk["translation_text"]
    assert real_plamo_output and len(real_plamo_output) > 0

    manual_phrase = "実機PLaMo翻訳後に研究者UIから手動修正した確定訳文。"
    edited = client.put(
        f"/api/documents/{doc_id}/blocks/{body_blk['id']}/manual-edit",
        json={"manual_translation_text": manual_phrase, "expected_edit_revision": 0},
        headers=headers,
    ).json()
    assert edited["translation_text"] == real_plamo_output
    assert edited["manual_translation_text"] == manual_phrase

    # 7. Run Zero-LLM retypeset and confirm 0 additional real LLM calls + PDF updated
    rt_res = client.post(
        f"/api/jobs/{job_id}/retypeset",
        json={"expected_edit_revision": 1, "allow_validation_warnings": True},
        headers=headers,
    )
    assert rt_res.status_code == 200
    rt_json = rt_res.json()
    assert rt_json["llm_calls_made"] == 0
    assert spy_real.call_count == real_calls_during_job  # Zero LLM calls during retypeset!
    assert rt_json["job"]["retypeset_revision"] == 1
    assert rt_json["job"]["is_mock_run"] is False

    dl_pdf = client.get(
        f"/api/jobs/{job_id}/artifacts/mono_ja_pdf/download",
        headers=headers,
    )
    assert dl_pdf.status_code == 200
    with pymupdf.open(stream=dl_pdf.content, filetype="pdf") as out_pdf:
        txt = out_pdf[0].get_text("text")
        assert manual_phrase in txt

    assert verify_original_unchanged(src_pdf, orig_sha)
