"""Tests for Proposal A (high-confidence isolated math variable protection) and Proposal B (MATH_VARIABLE_MISSING)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    BlockStatus,
    Document,
    InlineMathSpan,
    IssueSeverity,
    JobState,
    ProtectedItem,
    ProtectedItemKind,
    TranslationJobConfig,
    UnmaskedMathVariableCandidate,
)
from backend.jobs.manager import JobManager
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    extract_inline_math_spans_with_candidates,
    mask_inline_protected_elements_with_mapping,
    unmask_inline_protected_elements,
)
from backend.pdf.inline_math_vars import (
    INLINE_MASK_VERSION_LEGACY,
    INLINE_MASK_VERSION_STANDALONE_VAR,
    collect_unmasked_math_candidates,
    is_block_eligible_for_standalone_math_vars,
)
from backend.pdf.validation.preflight import compute_sha256
from backend.storage.cache import compute_translation_cache_key
from backend.storage.database import DatabaseRepository
from backend.translation.adapters.base import TranslationAdapter, TranslationAdapterResponse
from backend.translation.validation.validator import validate_translation_output

pytestmark = pytest.mark.unit


def _sp(
    text: str,
    x0: float,
    x1: float,
    *,
    y: float = 100.0,
    font: str = "CMR10",
    flags: int = 4,
    size: float = 9.96,
) -> dict:
    return {
        "text": text,
        "font": font,
        "flags": flags,
        "size": size,
        "bbox": [x0, y - 8.0, x1, y + 2.0],
        "origin": [x0, y],
    }


def _reproduce_block16_raw_lines() -> list[dict]:
    """Exact line/span structure matching `Wi-FiCSI based.pdf` Page 3 block-16 (`RAW BLOCK 14`)."""
    return [
        {
            "bbox": [50.7, 540.7, 283.2, 558.0],
            "lines_tag": "L0",
            "spans": [
                _sp("where,", 50.7, 74.0, y=550.0),
                _sp(" ", 74.0, 77.0, y=550.0, font="CMSY10", flags=6),
                _sp("∥", 77.0, 81.0, y=550.0, font="CMSY10", flags=6),
                _sp("h", 81.0, 86.0, y=550.0, font="CMMI10", flags=6),
                _sp("t", 86.0, 89.0, y=551.5, font="CMMI7", flags=6, size=6.97),
                _sp("∥", 89.0, 93.0, y=550.0, font="CMSY10", flags=6),
                _sp(" represents the vector norm of the CSI", 94.0, 283.2, y=550.0),
            ],
        },
        {
            "bbox": [50.7, 552.9, 283.2, 564.4],
            "spans": [
                _sp("amplitude components", 50.7, 130.0, y=562.0),
                _sp(" ", 130.0, 133.0, y=562.0, font="CMMI10", flags=6),
                _sp("h", 133.0, 138.0, y=562.0, font="CMMI10", flags=6),
                _sp("t", 138.0, 141.0, y=563.5, font="CMMI7", flags=6, size=6.97),
                _sp(" in all transmission paths at", 141.5, 283.2, y=562.0),
            ],
        },
        {
            "bbox": [50.7, 564.9, 57.1, 574.9],
            "spans": [
                _sp("t", 50.7, 54.3, y=572.0, font="CMMI10", flags=6),
                _sp(".", 54.3, 57.1, y=572.0),
            ],
        },
        {
            "bbox": [65.1, 564.9, 283.2, 574.9],
            "spans": [
                _sp("Let the window size be", 65.1, 160.0, y=572.0),
                _sp(" ", 160.0, 163.0, y=572.0, font="CMMI10", flags=6),
                _sp("tw", 163.0, 174.0, y=572.0, font="CMMI10", flags=6),
                _sp(", then the normalized", 174.0, 283.2, y=572.0),
            ],
        },
        {
            "bbox": [50.7, 574.3, 283.2, 588.3],
            "spans": [
                _sp("CSI amplitude component ", 50.7, 160.0, y=584.0),
                _sp("ˆ", 160.0, 164.0, y=581.5, font="CMR10", flags=5),
                _sp("H", 160.0, 167.0, y=584.0, font="CMMI10", flags=6),
                _sp("m,n,k,t", 167.0, 193.0, y=585.5, font="CMMI7", flags=6, size=6.97),
                _sp(" for each window", 193.5, 283.2, y=584.0),
            ],
        },
        {
            "bbox": [50.7, 588.8, 115.9, 598.8],
            "spans": [_sp("is expressed by", 50.7, 115.9, y=596.0)],
        },
    ]


def test_reproduces_block16_at_t_protection_and_restoration() -> None:
    lines = _reproduce_block16_raw_lines()
    adapter = NativePreservingAdapter()
    text_lines = adapter._extract_lines_and_style(lines)[0]
    src = " ".join(t.strip() for t in text_lines if t.strip())
    assert "in all transmission paths at t. Let the window size be tw" in src

    spans, candidates = extract_inline_math_spans_with_candidates(lines, [])
    standalone = [s for s in spans if s.role == "standalone_var"]
    assert len(standalone) == 1
    assert standalone[0].context_text == "t"
    assert standalone[0].mask_occurrence_indices == [0]

    masked, items, orig_to_masked = mask_inline_protected_elements_with_mapping(
        src, inline_math_spans=spans, include_standalone_vars=True
    )
    assert "paths at {v3}." in masked
    assert "size be tw," in masked
    t_item = next(it for it in items if it.placeholder == "{v3}")
    assert t_item.original_text == "t"
    assert collect_unmasked_math_candidates(src, orig_to_masked, candidates) == []

    translated_with_ph = (
        "ここで、{v1} は時刻 {v3} におけるすべての伝送路におけるCSI振幅成分 {v2} のベクトルノルムを表す。"
        "ウィンドウサイズを tw とすると、各ウィンドウにおける正規化CSI振幅成分 {v4} は次のように表される。"
    )
    val_res = validate_translation_output(
        source_text=src,
        masked_source_text=masked,
        translated_text=translated_with_ph,
        protected_items=items,
        finish_reason="stop",
        unmasked_math_variables=[],
    )
    assert val_res.is_valid is True
    assert val_res.findings == []
    restored = unmask_inline_protected_elements(translated_with_ph, items)
    assert "時刻 t における" in restored
    assert "∥ht∥" in restored


def test_positive_t_k_m_n_positions_line_edges_and_multiple_occurrences() -> None:
    lines = [
        {
            "bbox": [50.0, 100.0, 280.0, 112.0],
            "spans": [
                _sp("Let ", 50.0, 68.0, y=108.0),
                _sp("n", 68.0, 74.0, y=108.0, font="CMMI10", flags=6),
                _sp(" denote rows, ", 74.0, 130.0, y=108.0),
                _sp("m", 130.0, 138.0, y=108.0, font="CMMI10", flags=6),
                _sp(" cols, ", 138.0, 165.0, y=108.0),
                _sp("k", 165.0, 171.0, y=108.0, font="CMMI10", flags=6),
                _sp(" subcarriers, and keep t fixed.", 171.0, 280.0, y=108.0),
            ],
        },
        {
            "bbox": [50.0, 114.0, 280.0, 126.0],
            "spans": [
                _sp("t", 50.0, 54.0, y=122.0, font="CMMI10", flags=6),
                _sp(", representing index,", 54.0, 140.0, y=122.0),
                _sp(" matches at ", 140.0, 195.0, y=122.0),
                _sp("t", 195.0, 199.0, y=122.0, font="CMMI10", flags=6),
                _sp(".", 199.0, 202.0, y=122.0),
            ],
        },
    ]
    src = (
        "Let n denote rows, m cols, k subcarriers, and keep t fixed. "
        "t, representing index, matches at t."
    )
    spans, _ = extract_inline_math_spans_with_candidates(lines, [])
    by_ctx = {s.context_text: s.mask_occurrence_indices for s in spans if s.role == "standalone_var"}
    assert by_ctx["n"] == [0]
    assert by_ctx["m"] == [0]
    assert by_ctx["k"] == [0]
    # Occ 0 (`keep t fixed` in CMR10) MUST be skipped; occ 1 (line start `t,`) and occ 2 (line end `at t.`) masked
    assert by_ctx["t"] == [1, 2]

    masked, items, orig_to_masked = mask_inline_protected_elements_with_mapping(
        src, inline_math_spans=spans
    )
    assert masked == (
        "Let {v1} denote rows, {v2} cols, {v3} subcarriers, and keep t fixed. "
        "{v4}, representing index, matches at {v5}."
    )
    assert orig_to_masked is not None
    assert unmask_inline_protected_elements(masked, items) == src


def test_negative_exclusions_non_tex_italic_bullets_abbrevs_articles_urls_and_scripts() -> None:
    lines = [
        {
            "bbox": [50.0, 100.0, 300.0, 112.0],
            "spans": [
                # Non-TeX italic prose + CMMI without italic flag -> MUST NOT mask
                _sp("In ", 50.0, 62.0, y=108.0),
                _sp("x", 62.0, 68.0, y=108.0, font="Times-Italic", flags=2),
                _sp(" or ", 68.0, 82.0, y=108.0),
                _sp("y", 82.0, 88.0, y=108.0, font="CMTI10", flags=6),
                _sp(" or ", 88.0, 102.0, y=108.0),
                _sp("z", 102.0, 108.0, y=108.0, font="CMMI10", flags=4),
                # Conservative prose 'a' and 'I' even in CMMI10 -> MUST NOT standalone-mask
                _sp(" and ", 108.0, 125.0, y=108.0),
                _sp("a", 125.0, 130.0, y=108.0, font="CMMI10", flags=6),
                _sp(" path I see ", 130.0, 185.0, y=108.0),
                _sp("I", 185.0, 190.0, y=108.0, font="CMMI10", flags=6),
                # Subscripted `a_t` -> existing sub protection MUST remain intact!
                _sp(" with ", 190.0, 210.0, y=108.0),
                _sp("a", 210.0, 216.0, y=108.0, font="CMMI10", flags=6),
                _sp("t", 216.0, 220.0, y=109.5, font="CMMI7", flags=6, size=6.97),
                _sp(".", 220.0, 223.0, y=108.0),
            ],
        },
        {
            "bbox": [50.0, 114.0, 320.0, 126.0],
            "spans": [
                # Bullets, section labels, units, abbreviations, URLs, compound formulas
                _sp("(", 50.0, 53.0, y=122.0),
                _sp("b", 53.0, 58.0, y=122.0, font="CMMI10", flags=6),
                _sp(") ", 58.0, 64.0, y=122.0),
                _sp("k", 64.0, 69.0, y=122.0, font="CMMI10", flags=6),
                _sp(") item in Section ", 69.0, 145.0, y=122.0),
                _sp("B", 145.0, 152.0, y=122.0, font="CMMI10", flags=6),
                _sp(" for 10 ", 152.0, 180.0, y=122.0),
                _sp("s", 180.0, 185.0, y=122.0, font="CMMI10", flags=6),
                _sp(" e.", 185.0, 195.0, y=122.0),
                _sp("g", 195.0, 200.0, y=122.0, font="CMMI10", flags=6),
                _sp(". at https://x.org/t and [", 200.0, 290.0, y=122.0),
                _sp("u", 290.0, 296.0, y=122.0, font="CMMI10", flags=6),
                _sp(" − 1] {v1}.", 296.0, 330.0, y=122.0),
            ],
        },
    ]
    spans, _ = extract_inline_math_spans_with_candidates(lines, [])
    assert [s for s in spans if s.role == "standalone_var"] == []
    # Existing sub protection for `a_t` remains intact!
    assert any(s.role == "sub" and s.context_text == "at" for s in spans)
    # Heading block must never enable standalone single-letter variable masking
    assert is_block_eligible_for_standalone_math_vars(
        block_kind := next(iter([__import__("backend.domain.models").domain.models.BlockKind.HEADING])),
        "section_heading",
    ) is False


def test_combined_dehyphenation_and_standalone_var_protection() -> None:
    lines = [
        {
            "bbox": [50.0, 100.0, 248.0, 110.0],
            "spans": [_sp("derived from the moving vari-", 50.0, 248.0, y=108.0)],
        },
        {
            "bbox": [50.0, 112.0, 220.0, 122.0],
            "spans": [
                _sp("ances in all paths at ", 50.0, 160.0, y=120.0),
                _sp("t", 160.0, 164.0, y=120.0, font="CMMI10", flags=6),
                _sp(". Next step.", 164.0, 220.0, y=120.0),
            ],
        },
    ]
    adapter = NativePreservingAdapter()
    blk, _ = adapter._build_text_block_from_lines(
        block_id="doc-1:page-1:block-1",
        page_index=0,
        bbox=[50.0, 100.0, 250.0, 122.0],
        lines=lines,
        drawings=[],
        page_w=595.0,
        page_h=842.0,
        body_font_size=10.0,
        frontmatter_end_y=0.0,
        drawing_boxes=[],
        in_references_section=False,
        page_manual=[],
        cfg=TranslationJobConfig(
            normalize_line_break_hyphens=True,
            protect_isolated_math_vars=True,
        ),
    )
    assert blk is not None
    assert blk.source_text == "derived from the moving vari- ances in all paths at t. Next step."
    assert blk.masked_source_text == "derived from the moving vari- ances in all paths at {v1}. Next step."
    assert blk.llm_input_text == "derived from the moving variances in all paths at {v1}. Next step."
    assert blk.unmasked_math_variables == []
    assert unmask_inline_protected_elements("時刻 {v1} における移動分散。", blk.protected_items) == "時刻 t における移動分散。"


def test_proposal_b_missing_detection_no_false_preserved_and_unverifiable() -> None:
    cand_t = UnmaskedMathVariableCandidate(
        symbol="t",
        source_start=103,
        source_end=104,
        context_excerpt="paths at t. Let",
        evidence_basis="math_italic_standalone_prose_var",
    )
    src_masked = (
        "where, {v1} represents the vector norm of {v2} in all transmission paths at t. "
        "Let tw be window at https://example.org/t within 10 s."
    )
    # 1. Missing `t` even when `tw`, URL `/t`, `10 s`, `{v2}` (`ht`) contain letter `t` -> MUST warn!
    bad_out = (
        "ここで、{v1} は時刻におけるすべての伝送路における {v2} のベクトルノルムを表す。"
        "ツール tw と https://example.org/t を 10 s 以内で使う。"
    )
    res_missing = validate_translation_output(
        source_text=src_masked,
        masked_source_text=src_masked,
        translated_text=bad_out,
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="∥ht∥"),
            ProtectedItem(placeholder="{v2}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="ht"),
        ],
        finish_reason="stop",
        unmasked_math_variables=[cand_t],
    )
    assert res_missing.is_valid is True  # Non-blocking!
    assert res_missing.should_retry is False
    assert res_missing.error_codes == []
    assert "MATH_VARIABLE_MISSING" in res_missing.warning_codes
    finding = next(f for f in res_missing.findings if f.code == "MATH_VARIABLE_MISSING")
    assert finding.severity == IssueSeverity.WARNING
    assert finding.details["symbol"] == "t"
    assert finding.details["source_positions"][0]["start"] == 103
    assert finding.details["output_isolated_count"] == 0

    # 2. If `t` WAS masked into `{v4}` -> NO duplicate MATH_VARIABLE_MISSING (only PLACEHOLDER_MISSING if dropped)
    masked_with_ph = (
        "where, {v1} represents {v2} in all transmission paths at {v4}. "
        "Let tw be window."
    )
    res_no_dup = validate_translation_output(
        source_text=src_masked,
        masked_source_text=masked_with_ph,
        translated_text="ここで、{v1} は時刻における {v2} を表す。ウィンドウは tw とする。",
        protected_items=[
            ProtectedItem(placeholder="{v1}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="∥ht∥"),
            ProtectedItem(placeholder="{v2}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="ht"),
            ProtectedItem(placeholder="{v4}", kind=ProtectedItemKind.INLINE_FORMULA, original_text="t"),
        ],
        finish_reason="stop",
        unmasked_math_variables=[cand_t],
    )
    assert "PLACEHOLDER_MISSING" in res_no_dup.error_codes
    assert "MATH_VARIABLE_MISSING" not in res_no_dup.warning_codes

    # 3. Multiple unmasked occurrences where count is ambiguous -> explicitly marked "照合不能" (`unverifiable`)
    src_multi = "Keep t and range t as median."
    res_unverifiable = validate_translation_output(
        source_text=src_multi,
        masked_source_text=src_multi,
        translated_text="中央値として t を保持する。",
        protected_items=[],
        finish_reason="stop",
        unmasked_math_variables=[
            UnmaskedMathVariableCandidate(
                symbol="t",
                source_start=19,
                source_end=20,
                context_excerpt="range t as",
                evidence_basis="math_italic_standalone_prose_var",
            )
        ],
    )
    assert res_unverifiable.warning_codes == []
    assert res_unverifiable.math_variable_checks[0]["status"] == "unverifiable"
    assert res_unverifiable.math_variable_checks[0]["status_ja"] == "照合不能"


class _DropUnmaskedTTranslator(TranslationAdapter):
    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def is_mock(self) -> bool:
        return True

    def check_health(self) -> dict:
        return {"ok": True}

    def translate_segment(
        self,
        source_text: str,
        *,
        source_lang: str = "en",
        target_lang: str = "ja",
        form: str = "plain",
        context: str | None = None,
        history: list | None = None,
        glossary_entries: list | None = None,
        context_budget_tokens: int = 8192,
    ) -> TranslationAdapterResponse:
        self.calls.append(source_text)
        # Replicate 0.8B failure mode: keeps `{v1}`, drops unmasked `t`, preserves `{v1}` when masked
        if "{v1}" in source_text:
            out = "すべての伝送路の時刻 {v1} におけるベクトル特性を示す。"
        else:
            out = "すべての伝送路の時刻におけるベクトル特性を示す。"
        return TranslationAdapterResponse(
            translated_text=out,
            raw_output=out,
            prompt_used=source_text,
            finish_reason="stop",
            model_name="stub-mock",
            quantization="Q4_K_M",
        )


def test_cache_invalidation_and_job_manager_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k_legacy = compute_translation_cache_key(
        "At t.",
        model_name="m",
        masking_version=INLINE_MASK_VERSION_LEGACY,
    )
    k_v2 = compute_translation_cache_key(
        "At t.",
        model_name="m",
        masking_version=INLINE_MASK_VERSION_STANDALONE_VAR,
    )
    assert k_legacy != k_v2

    body_line = (
        "The vector norm represents the CSI amplitude components across all transmission paths at t."
    )
    pdf_path = tmp_path / "sample.pdf"
    pdf = pymupdf.open()
    pg = pdf.new_page(width=595, height=842)
    pg.insert_text((60.0, 400.0), body_line, fontname="helv", fontsize=10)
    pdf.save(str(pdf_path))
    pdf.close()

    t_pos = body_line.rindex("t.")
    cand_t = UnmaskedMathVariableCandidate(
        symbol="t",
        source_start=t_pos,
        source_end=t_pos + 1,
        context_excerpt="paths at t.",
        evidence_basis="math_italic_standalone_prose_var",
    )
    span_t = InlineMathSpan(
        context_text="t",
        target_text="t",
        role="standalone_var",
        scale=1.0,
        dy_em=0.0,
        ambiguous=False,
        mask_occurrence_indices=[0],
    )
    monkeypatch.setattr(
        "backend.pdf.adapters.native_preserving_adapter.extract_inline_math_spans_with_candidates",
        lambda raw_lines, page_drawings=None, **kw: ([span_t], [cand_t]),
    )

    cfg = AppConfig(runtime_dir=tmp_path / "rt")
    cfg.ensure_directories()
    db = DatabaseRepository(cfg.db_path)
    stub = _DropUnmaskedTTranslator()
    mgr = JobManager(config=cfg, db=db, translation_adapter=stub)

    def _mk_doc(did: str) -> None:
        db.save_document(
            Document(
                id=did,
                original_filename=pdf_path.name,
                safe_storage_path=str(pdf_path),
                sha256_hash=compute_sha256(pdf_path),
                file_size_bytes=pdf_path.stat().st_size,
                page_count=1,
            )
        )

    _mk_doc("doc-off")
    # 1. With protection OFF, stub drops `t` -> non-blocking Proposal B emits `MATH_VARIABLE_MISSING`
    j_off = mgr.run_job(
        mgr.create_job(
            document_id="doc-off",
            job_config=TranslationJobConfig(
                use_mock_translator=True,
                protect_isolated_math_vars=False,
            ),
        ).id
    )
    assert j_off.state.value == "completed_with_warnings"
    assert j_off.execution_provenance["masking_version"] == INLINE_MASK_VERSION_LEGACY
    assert j_off.execution_provenance["protect_isolated_math_vars"] is False
    assert j_off.execution_provenance["fallback_original_blocks"] == 0
    off_issues = db.list_validation_issues(j_off.id)
    assert [i.code for i in off_issues] == ["MATH_VARIABLE_MISSING"]
    assert off_issues[0].severity == IssueSeverity.WARNING
    assert off_issues[0].fallback_applied is False
    assert stub.calls == [body_line]

    # Repeat OFF -> hits cache AND still emits MATH_VARIABLE_MISSING warning cleanly
    _mk_doc("doc-off-cache")
    j_off_cache = mgr.run_job(
        mgr.create_job(
            document_id="doc-off-cache",
            job_config=TranslationJobConfig(
                use_mock_translator=True,
                protect_isolated_math_vars=False,
            ),
        ).id
    )
    assert j_off_cache.execution_provenance["cache_hit_blocks"] == 1
    assert [i.code for i in db.list_validation_issues(j_off_cache.id)] == ["MATH_VARIABLE_MISSING"]

    # 2. Switch protection ON on SAME document (`doc-off`): must NOT reuse old translation or old cache!
    stub.calls.clear()
    j_on = mgr.run_job(
        mgr.create_job(
            document_id="doc-off",
            job_config=TranslationJobConfig(
                use_mock_translator=True,
                protect_isolated_math_vars=True,
            ),
        ).id
    )
    assert j_on.state.value == "completed"
    assert j_on.warning_count == 0
    assert j_on.execution_provenance["masking_version"] == INLINE_MASK_VERSION_STANDALONE_VAR
    assert j_on.execution_provenance["protection_policy_version"] == "v2.1-inline-standalone-var"
    assert j_on.execution_provenance["standalone_math_var_protected_blocks"] == 1
    assert j_on.execution_provenance["cache_hit_blocks"] == 0
    assert j_on.execution_provenance["reused_existing_blocks"] == 0
    assert j_on.execution_provenance["newly_inferenced_blocks"] == 1
    assert stub.calls == [body_line.replace("at t.", "at {v1}.")]

    # 3. Retypeset with zero LLM calls preserves `{v1}` -> `'t'` cleanly
    re_res = mgr.retypeset_job(j_on.id, allow_validation_warnings=True)
    re_job = re_res["job"]
    assert re_job.state.value == "completed"
    assert re_job.warning_count == 0
    assert re_res["llm_calls_made"] == 0
    saved_blk = db.list_blocks("doc-off")[0]
    assert saved_blk.unmasked_math_variables == []
    restored = unmask_inline_protected_elements(
        saved_blk.effective_translation or "", saved_blk.protected_items
    )
    assert "時刻 t における" in restored

    # 4. Target block retranslation (`target_block_ids`) + `resume=True` keep new masking intact
    stub.calls.clear()
    retrans = mgr.run_job(
        mgr.create_job(
            document_id="doc-off",
            job_config=TranslationJobConfig(
                use_mock_translator=True,
                protect_isolated_math_vars=True,
            ),
        ).id,
        target_block_ids={saved_blk.id},
    )
    assert retrans.state.value == "completed"
    assert retrans.warning_count == 0
    # Interrupted -> resume=True reuses masked block state without corrupting placeholders
    j_resumed_seed = mgr.create_job(
        document_id="doc-off",
        job_config=TranslationJobConfig(
            use_mock_translator=True,
            protect_isolated_math_vars=True,
        ),
    )
    # Simulate interrupted job resuming cleanly without losing standalone_var masking
    mgr.request_simulate_interrupt(j_resumed_seed.id)
    j_intr = mgr.run_job(j_resumed_seed.id)
    assert j_intr.state == JobState.INTERRUPTED
    mgr._interrupt_flags[j_resumed_seed.id].clear()
    resumed = mgr.run_job(j_resumed_seed.id, resume=True)
    assert resumed.state.value == "completed"
    assert db.list_blocks("doc-off")[0].masked_source_text == body_line.replace("at t.", "at {v1}.")
