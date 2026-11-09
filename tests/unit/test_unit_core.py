"""Unit tests covering Section 8, 9, 10, 11, 14, 15, 16, 18 requirements."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.models import (
    Block,
    GlossaryEntry,
    IssueSeverity,
    Job,
    JobState,
    ProtectedItem,
    ProtectedItemKind,
)
from backend.pdf.adapters.native_preserving_adapter import (
    fit_japanese_text_in_bbox,
    mask_inline_protected_elements,
    unmask_inline_protected_elements,
)
from backend.pdf.coordinates import (
    backend_bbox_to_pdf_bottom_left,
    backend_to_viewport_bbox,
    display_to_internal_page,
    internal_to_display_page,
    parse_page_range_spec,
    pdf_bottom_left_to_backend_bbox,
    viewport_to_backend_bbox,
)
from backend.pdf.validation.preflight import resolve_safe_storage_path
from backend.storage.cache import compute_translation_cache_key
from backend.translation.prompts.pfml3 import (
    HistoryPair,
    build_pfml3_prompt,
    plan_prompt_within_budget,
    split_paragraph_preserving_placeholders,
    strip_plamo_control_tokens,
)
from backend.translation.validation.validator import validate_translation_output

pytestmark = pytest.mark.unit


def test_legacy_block_payload_defaults_to_no_continuation_candidates() -> None:
    legacy_payload = (
        '{"id":"doc:page-1:block-1","page_index":0,'
        '"bbox":[10,20,30,40],"source_text":"A legacy block."}'
    )

    block = Block.model_validate_json(legacy_payload)

    assert block.continuation_candidates == []


# ------------------------------------------------------------------
# 1. Page Numbering & Page Range Spec Tests (Section 8)
# ------------------------------------------------------------------
def test_page_number_separation_and_range_parsing() -> None:
    assert display_to_internal_page(1, total_pages=10) == 0
    assert internal_to_display_page(0, total_pages=10) == 1

    with pytest.raises(ValueError):
        display_to_internal_page(0, total_pages=10)
    with pytest.raises(ValueError):
        display_to_internal_page(11, total_pages=10)

    assert parse_page_range_spec(None, 5) == [0, 1, 2, 3, 4]
    assert parse_page_range_spec("1, 3-5", 6) == [0, 2, 3, 4]
    assert parse_page_range_spec("-2, 5-", 6) == [0, 1, 4, 5]

    with pytest.raises(ValueError):
        parse_page_range_spec("4-2", 6)
    with pytest.raises(ValueError):
        parse_page_range_spec("1, 8", 6)


# ------------------------------------------------------------------
# 2. Coordinate Conversion Round-Trip Tests across Zoom & Rotation (Section 8)
# ------------------------------------------------------------------
@pytest.mark.parametrize("scale", [0.5, 1.0, 1.25, 2.5])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_viewport_and_backend_coordinate_roundtrip(scale: float, rotation: int) -> None:
    page_w, page_h = 612.0, 792.0
    original_bbox = [72.5, 120.0, 280.25, 215.75]

    vp_bbox = backend_to_viewport_bbox(
        original_bbox, page_w, page_h, scale=scale, view_rotation_deg=rotation
    )
    recovered_bbox = viewport_to_backend_bbox(
        vp_bbox, page_w, page_h, scale=scale, view_rotation_deg=rotation
    )
    for orig_v, rec_v in zip(original_bbox, recovered_bbox):
        assert abs(orig_v - rec_v) < 1e-6


def test_pdf_bottom_left_to_top_left_with_cropbox() -> None:
    page_h = 720.0
    crop_x, crop_y = 36.0, 36.0
    raw_bl = [108.0, 536.0, 308.0, 636.0]
    tl_bbox = pdf_bottom_left_to_backend_bbox(raw_bl, page_h, crop_x, crop_y)
    recovered_bl = backend_bbox_to_pdf_bottom_left(tl_bbox, page_h, crop_x, crop_y)
    for a, b in zip(raw_bl, recovered_bl):
        assert abs(a - b) < 1e-6


# ------------------------------------------------------------------
# 3. PFML3 Prompt Format, Special Tokens, and Context Budget Tests (Section 9 & 10)
# ------------------------------------------------------------------
def test_pfml3_prompt_structure_and_no_duplicate_bos() -> None:
    glossary = [
        GlossaryEntry(source="gradient descent", target="勾配降下法"),
        GlossaryEntry(source="unused term", target="未使用語"),
    ]
    history = [
        HistoryPair(source_text="Open the file.", target_text="ファイルを開く。"),
    ]
    prompt = build_pfml3_prompt(
        source_text="We apply gradient descent to {v1}.",
        source_lang="en",
        target_lang="ja",
        form="plain",
        context="1. Introduction",
        history=history,
        glossary_entries=glossary,
        include_bos=False,
    )
    # Must NOT prepend <|plamo:bos|> when include_bos=False (llama.cpp adds BOS)
    assert not prompt.startswith("<|plamo:bos|>")
    assert prompt.startswith("<|plamo:tag|>dataset<|plamo:msg|>translation")
    # Order check: dataset -> context -> history -> glossary -> current input -> output
    idx_dataset = prompt.index("<|plamo:tag|>dataset")
    idx_context = prompt.index("<|plamo:tag|>context")
    idx_hist_in = prompt.index("Open the file.")
    idx_glossary = prompt.index("<|plamo:tag|>glossary")
    idx_cur_in = prompt.index("We apply gradient descent to {v1}.")
    idx_output = prompt.rfind("<|plamo:tag|>output")
    assert idx_dataset < idx_context < idx_hist_in < idx_glossary < idx_cur_in < idx_output

    # Only matched glossary term is injected
    assert "<|plamo:index|>gradient descent<|plamo:item|>勾配降下法" in prompt
    assert "unused term" not in prompt
    assert prompt.endswith(
        "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:key|>form<|plamo:val|>plain<|plamo:msg|>"
    )


def test_strip_plamo_control_tokens() -> None:
    raw = "<|plamo:bos|>これは翻訳結果である。<|plamo:tag|>input<|plamo:msg|>extra"
    assert strip_plamo_control_tokens(raw) == "これは翻訳結果である。"


def test_token_budget_trims_history_and_context_before_failing() -> None:
    long_history = [
        HistoryPair(
            source_text="Prior sentence number " + str(i) + " " * 120,
            target_text="過去の訳文番号 " + str(i) + "あ" * 120,
        )
        for i in range(10)
    ]
    plan = plan_prompt_within_budget(
        source_text="Short paragraph with {v1}.",
        context_budget_tokens=600,
        context="Very long context header " * 30,
        history=long_history,
    )
    assert plan.was_context_trimmed is True
    assert plan.included_history_count < len(long_history)
    assert plan.estimated_input_tokens + plan.reserved_output_tokens <= 600


def test_sentence_splitting_never_breaks_placeholders() -> None:
    para = (
        "In Fig. 1 and Eq. (2), Smith et al. show that {v1} holds for all inputs. "
        "Furthermore, the integral {v2} converges rapidly when {v3} is bounded by 100. "
        "Finally, we conclude that {v4} is optimal."
    )
    chunks = split_paragraph_preserving_placeholders(para, max_segment_chars=85)
    assert len(chunks) >= 3
    joined = " ".join(chunks)
    for ph in ("{v1}", "{v2}", "{v3}", "{v4}"):
        assert ph in joined
        assert any(ph in c for c in chunks)


# ------------------------------------------------------------------
# 4. Inline Formula Masking & Translation Validation Tests (Section 7 & 11)
# ------------------------------------------------------------------
def test_inline_formula_masking_and_unmasking() -> None:
    src = "The energy E(x_i) = α_i + β_i converges at https://example.com/test [1]."
    masked, items = mask_inline_protected_elements(src)
    assert "{v1}" in masked
    assert "[1]" in masked
    # Ensure trailing sentence period was NOT swallowed into the URL placeholder
    assert masked.endswith(".")
    restored = unmask_inline_protected_elements(masked, items)
    assert restored == src

    # Regression test (Phase 2): complex subscript/gradient expression inside parentheses
    src_grad = (
        "Given the objective function E(x_i) = α_i + β_i, we prove that the update rule "
        "(defined by θ_{t+1} = θ_t - η∇L(θ_t)) converges within 1,000 iterations [1, 2]."
    )
    masked_grad, items_grad = mask_inline_protected_elements(src_grad)
    # The outer closing parenthesis `)` of `(defined by ...)` must remain in masked text
    assert "(defined by {v2})" in masked_grad
    assert unmask_inline_protected_elements(masked_grad, items_grad) == src_grad

    equation_ref = "The result follows from Eq. (2) under the stated assumptions."
    masked_ref, ref_items = mask_inline_protected_elements(equation_ref)
    equation_number = next(
        item for item in ref_items if item.kind == ProtectedItemKind.EQUATION_NUMBER
    )
    assert equation_number.original_text == "Eq. (2)"
    assert unmask_inline_protected_elements(masked_ref, ref_items) == equation_ref

    ordinary_terms = "RSSI and CSI are standard radio measurements in this setting."
    masked_terms, term_items = mask_inline_protected_elements(ordinary_terms)
    assert term_items == []
    assert masked_terms == ordinary_terms



def test_translation_validator_detects_all_failure_modes() -> None:
    items = [
        ProtectedItem(
            placeholder="{v1}",
            kind=ProtectedItemKind.INLINE_FORMULA,
            original_text="E(x_i) = α_i",
        )
    ]
    src = "In this paper, we show that E(x_i) = α_i holds for 1,000 samples [1]."
    masked = "In this paper, we show that {v1} holds for 1,000 samples [1]."

    # Valid case (note `1000` without comma matches `1,000` value without false positive!)
    ok_res = validate_translation_output(
        src, masked, "本論文では、1000個のサンプルに対して {v1} が成り立つことを示す [1]。", items
    )
    assert ok_res.is_valid is True
    assert ok_res.has_warnings is False

    # Missing placeholder {v1} -> ERROR
    miss_res = validate_translation_output(
        src, masked, "本論文では、1000個のサンプルに対して成り立つことを示す [1]。", items
    )
    assert miss_res.is_valid is False
    assert "PLACEHOLDER_MISSING" in miss_res.error_codes

    # Duplicated placeholder {v1} -> ERROR
    dup_res = validate_translation_output(
        src, masked, "本論文では {v1} と {v1} が 1000個で成り立つ [1]。", items
    )
    assert dup_res.is_valid is False
    assert "PLACEHOLDER_DUPLICATED" in dup_res.error_codes

    # Altered fullwidth placeholder ｛v1｝ -> ERROR
    alt_res = validate_translation_output(
        src, masked, "本論文では ｛v1｝ が 1000個で成り立つ [1]。", items
    )
    assert alt_res.is_valid is False
    assert "PLACEHOLDER_ALTERED" in alt_res.error_codes

    # Missing citation [1] -> ERROR
    cite_res = validate_translation_output(
        src, masked, "本論文では 1000個のサンプルに対して {v1} が成り立つ。", items
    )
    assert cite_res.is_valid is False
    assert "REFERENCE_MARKER_MISSING" in cite_res.error_codes

    # Residual special token -> ERROR
    tok_res = validate_translation_output(
        src, masked, "本論文では {v1} が 1000個で成り立つ [1]。<|plamo:tag|>", items
    )
    assert tok_res.is_valid is False
    assert "SPECIAL_TOKEN_RESIDUE" in tok_res.error_codes

    # Truncated by length -> ERROR + recommend_resplit
    trunc_res = validate_translation_output(
        src, masked, "本論文では {v1} が 1000個で成り立つ [1]。", items, finish_reason="length"
    )
    assert trunc_res.is_valid is False
    assert trunc_res.recommend_resplit is True
    assert "TRUNCATED_BY_LENGTH" in trunc_res.error_codes

    # Abnormal repetition -> ERROR
    rep_res = validate_translation_output(
        src, masked, "本論文では {v1} [1] 1000 " + "同じフレーズの反復" * 6, items
    )
    assert rep_res.is_valid is False
    assert "ABNORMAL_REPETITION" in rep_res.error_codes

    # Numeric value altered (1,000 -> 500) -> WARNING
    num_res = validate_translation_output(
        src, masked, "本論文では、500個のサンプルに対して {v1} が成り立つことを示す [1]。", items
    )
    assert num_res.is_valid is True
    assert num_res.has_warnings is True
    assert "NUMERIC_VALUE_MISMATCH" in num_res.warning_codes


# ------------------------------------------------------------------
# 5. Cache Key Invalidation, State Machine, Safe Path, Kinsoku Tests
# ------------------------------------------------------------------
def test_cache_key_invalidates_on_any_parameter_change() -> None:
    base_key = compute_translation_cache_key(
        masked_source_text="Hello {v1}",
        model_name="plamo-3-translate-2b",
        quantization="Q4_K_M",
        form="plain",
        glossary_entries=[GlossaryEntry(source="Hello", target="こんにちは")],
    )
    diff_form = compute_translation_cache_key(
        masked_source_text="Hello {v1}",
        model_name="plamo-3-translate-2b",
        quantization="Q4_K_M",
        form="polite",
        glossary_entries=[GlossaryEntry(source="Hello", target="こんにちは")],
    )
    diff_glossary = compute_translation_cache_key(
        masked_source_text="Hello {v1}",
        model_name="plamo-3-translate-2b",
        quantization="Q4_K_M",
        form="plain",
        glossary_entries=[GlossaryEntry(source="Hello", target="ハロー")],
    )
    diff_model = compute_translation_cache_key(
        masked_source_text="Hello {v1}",
        model_name="plamo-3-translate-8b",
        quantization="Q4_K_M",
        form="plain",
        glossary_entries=[GlossaryEntry(source="Hello", target="こんにちは")],
    )
    diff_context = compute_translation_cache_key(
        masked_source_text="Hello {v1}",
        model_name="plamo-3-translate-2b",
        quantization="Q4_K_M",
        form="plain",
        context="Following fragment: {protected item}",
        glossary_entries=[GlossaryEntry(source="Hello", target="こんにちは")],
    )
    assert len({base_key, diff_form, diff_glossary, diff_model, diff_context}) == 5


def test_job_state_machine_and_safe_storage_path(tmp_path: Path) -> None:
    job = Job(id="job-1", document_id="doc-1")
    job.transition_to(JobState.PREFLIGHT)
    job.transition_to(JobState.PARSING)
    job.transition_to(JobState.TRANSLATING)
    job.transition_to(JobState.VALIDATING)
    job.transition_to(JobState.TYPESETTING)
    job.transition_to(JobState.COMPLETED_WITH_WARNINGS)
    assert job.completed_at is not None

    with pytest.raises(ValueError):
        job.transition_to(JobState.CREATED)

    safe_p = resolve_safe_storage_path(tmp_path, "doc-abc_123")
    assert safe_p.parent == tmp_path.resolve()

    with pytest.raises(ValueError):
        resolve_safe_storage_path(tmp_path, "../evil_traversal")


def test_japanese_fitting_respects_min_font_size() -> None:
    # Fits comfortably
    sz, lines, fits = fit_japanese_text_in_bbox(
        "本研究ではエネルギー関数を提案する。",
        bbox=[72.0, 100.0, 300.0, 140.0],
        initial_font_size=10.5,
        min_font_size=8.0,
    )
    assert fits is True
    assert sz >= 8.0

    # Extremely tiny box -> must report fits=False and never shrink below min_font_size=8.0
    sz_over, _, fits_over = fit_japanese_text_in_bbox(
        "非常に長い日本語の段落テキストであり、小さな矩形領域には8ptでも絶対に収まらない文章です。" * 5,
        bbox=[72.0, 100.0, 120.0, 112.0],
        initial_font_size=10.5,
        min_font_size=8.0,
    )
    assert fits_over is False
    assert sz_over == 8.0
