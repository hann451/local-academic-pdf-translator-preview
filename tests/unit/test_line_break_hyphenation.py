"""Unit and pipeline tests for geometry- and morphology-aware line-break dehyphenation."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    BlockKind,
    Document,
    ProtectedItem,
    ProtectedItemKind,
    TranslationJobConfig,
)
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256
from backend.pdf.adapters.native_preserving_adapter import (
    NativePreservingAdapter,
    mask_inline_protected_elements_with_mapping,
    unmask_inline_protected_elements,
)
from backend.pdf.hyphenation import (
    PREPROCESSING_VERSION_DEHYPHENATED,
    PREPROCESSING_VERSION_RAW,
    LineBreakHyphenEdit,
    build_document_token_inventory,
    detect_line_break_hyphen_edits,
    project_line_break_edits_to_masked,
)
from backend.storage.cache import compute_translation_cache_key
from backend.storage.database import DatabaseRepository
from backend.translation.adapters.base import TranslationAdapter, TranslationAdapterResponse

pytestmark = pytest.mark.unit


def _make_line(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    font: str = "CMR10",
    size: float = 10.0,
    flags: int = 0,
    spans: list[dict] | None = None,
) -> dict:
    if spans is None:
        spans = [
            {
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "font": font,
                "size": size,
                "flags": flags,
            }
        ]
    return {"bbox": [x0, y0, x1, y1], "spans": spans}


def test_detect_line_break_hyphen_joins_wrapped_english_words() -> None:
    lines = [
        _make_line("derived from the moving vari-", 50.0, 100.0, 248.0, 110.0),
        _make_line("ances of CSI amplitude peri-", 50.0, 112.0, 249.0, 122.0),
        _make_line("ods for detecting for-", 50.0, 124.0, 248.0, 134.0),
        _make_line("eign object intrusions.", 50.0, 136.0, 190.0, 146.0),
    ]
    text_lines = [ln["spans"][0]["text"] for ln in lines]
    full_text = " ".join(text_lines)
    inventory = build_document_token_inventory(
        ["In foreign regions, variances over stable periods are small."]
    )

    edits = detect_line_break_hyphen_edits(
        lines,
        text_lines,
        [50.0, 100.0, 250.0, 146.0],
        full_text,
        inventory=inventory,
    )
    assert [(e.left_token, e.right_token, e.joined_word) for e in edits] == [
        ("vari", "ances", "variances"),
        ("peri", "ods", "periods"),
        ("for", "eign", "foreign"),
    ]


def test_detect_line_break_hyphen_preserves_compounds_units_math_and_urls() -> None:
    lines = [
        _make_line("The system uses a two-", 50.0, 100.0, 248.0, 110.0),
        _make_line("dimensional array and time-", 50.0, 112.0, 249.0, 122.0),
        _make_line("series values on a cost-", 50.0, 124.0, 248.0, 134.0),
        _make_line("effective 0.17-", 50.0, 136.0, 248.0, 146.0),
        _make_line("m-high Re-Tx →L-", 50.0, 148.0, 248.0, 158.0),
        _make_line("Rx path at https://example.org/foo-", 50.0, 160.0, 249.0, 170.0),
        _make_line("bar and range [t −", 50.0, 172.0, 248.0, 182.0),
        _make_line("tw/2] for email r-tokumine-", 50.0, 184.0, 249.0, 194.0),
        _make_line("9n5@example.com test.", 50.0, 196.0, 190.0, 206.0),
    ]
    text_lines = [ln["spans"][0]["text"] for ln in lines]
    full_text = " ".join(text_lines)

    # Empty inventory: compounds and non-word tokens must still NOT be joined.
    edits_no_inv = detect_line_break_hyphen_edits(
        lines,
        text_lines,
        [50.0, 100.0, 250.0, 206.0],
        full_text,
        inventory=None,
    )
    assert edits_no_inv == []

    # Document inventory containing explicit inline hyphenated forms must also preserve them.
    inv_with_compound = build_document_token_inventory(
        ["A two-dimensional grid and time-series data are compared."]
    )
    edits_with_inv = detect_line_break_hyphen_edits(
        lines,
        text_lines,
        [50.0, 100.0, 250.0, 206.0],
        full_text,
        inventory=inv_with_compound,
    )
    assert edits_with_inv == []


def test_detect_skips_short_non_wrapped_lines_and_math_font_lines() -> None:
    # Right gap is huge and next word is narrow -> NOT a right-margin line wrap.
    short_lines = [
        _make_line("Case A: semi-", 50.0, 100.0, 115.0, 110.0),
        _make_line("major axis", 50.0, 112.0, 120.0, 122.0, spans=[
            {"text": "major", "bbox": [50.0, 112.0, 75.0, 122.0], "font": "CMR10", "size": 10.0, "flags": 0},
            {"text": " axis", "bbox": [75.0, 112.0, 105.0, 122.0], "font": "CMR10", "size": 10.0, "flags": 0},
        ]),
    ]
    assert (
        detect_line_break_hyphen_edits(
            short_lines,
            ["Case A: semi-", "major axis"],
            [50.0, 100.0, 260.0, 122.0],
            "Case A: semi- major axis",
        )
        == []
    )

    # Math font token before hyphen must never be joined.
    math_lines = [
        _make_line("value is alpha-", 50.0, 100.0, 248.0, 110.0, spans=[
            {"text": "value is ", "bbox": [50.0, 100.0, 190.0, 110.0], "font": "CMR10", "size": 10.0, "flags": 0},
            {"text": "alpha-", "bbox": [190.0, 100.0, 248.0, 110.0], "font": "CMMI10", "size": 10.0, "flags": 4},
        ]),
        _make_line("ization", 50.0, 112.0, 120.0, 122.0),
    ]
    assert (
        detect_line_break_hyphen_edits(
            math_lines,
            ["value is alpha-", "ization"],
            [50.0, 100.0, 250.0, 122.0],
            "value is alpha- ization",
        )
        == []
    )


def test_projection_preserves_placeholders_and_aborts_on_mapping_loss() -> None:
    src = (
        "The component ˆH(m, n, k, t) shows moving vari- ances over Eq. (5) "
        "and peri- ods for two- dimensional analysis [1]."
    )
    masked, items, orig_to_masked = mask_inline_protected_elements_with_mapping(src)
    assert "{v1}" in masked
    assert "{v2}" in masked
    assert orig_to_masked is not None

    edits = [
        LineBreakHyphenEdit(
            line_index=0,
            hyph_idx=src.index("vari-") + 4,
            space_idx=src.index("vari-") + 5,
            token_start=src.index("vari-"),
            token_end=src.index("vari-") + len("vari- ances"),
            left_token="vari",
            right_token="ances",
            joined_word="variances",
            reason="known_solid_word",
        ),
        LineBreakHyphenEdit(
            line_index=1,
            hyph_idx=src.index("peri-") + 4,
            space_idx=src.index("peri-") + 5,
            token_start=src.index("peri-"),
            token_end=src.index("peri-") + len("peri- ods"),
            left_token="peri",
            right_token="ods",
            joined_word="periods",
            reason="bound_right_fragment",
        ),
    ]

    projected = project_line_break_edits_to_masked(src, masked, orig_to_masked, edits)
    assert projected is not None
    assert "variances" in projected
    assert "periods" in projected
    assert "two- dimensional" in projected
    assert "{v1}" in projected
    assert "{v2}" in projected
    assert "[1]" in projected
    # Unmasking on projected output preserves every protected formula/ref cleanly.
    restored = unmask_inline_protected_elements(projected, items)
    assert "ˆH(m, n, k, t)" in restored
    assert "Eq. (5)" in restored

    # When mapping is unavailable (`None`) or overlaps a placeholder, projection safely skips.
    assert project_line_break_edits_to_masked(src, masked, None, edits) is None
    overlapping_edit = [
        LineBreakHyphenEdit(
            line_index=0,
            hyph_idx=src.index("ˆH"),
            space_idx=src.index("ˆH") + 1,
            token_start=src.index("ˆH"),
            token_end=src.index("ˆH") + 6,
            left_token="ˆH",
            right_token="m",
            joined_word="ˆHm",
            reason="overlap_test",
        )
    ]
    assert (
        project_line_break_edits_to_masked(src, masked, orig_to_masked, overlapping_edit)
        is None
    )


def test_cache_key_separates_preprocessing_versions() -> None:
    kwargs = dict(
        masked_source_text="Moving {v1} periods in analysis.",
        model_name="plamo-3-translate-2b-Q4_K_M",
        model_id="plamo-3-translate-2b-q4_k_m",
        model_hash="1ffd9073",
        quantization="Q4_K_M",
        prompt_version="pfml3-v1",
        protected_items=[
            ProtectedItem(
                placeholder="{v1}",
                kind=ProtectedItemKind.INLINE_FORMULA,
                original_text="ht",
            )
        ],
    )
    k_raw = compute_translation_cache_key(
        **kwargs, preprocessing_version=PREPROCESSING_VERSION_RAW
    )
    k_norm = compute_translation_cache_key(
        **kwargs, preprocessing_version=PREPROCESSING_VERSION_DEHYPHENATED
    )
    assert k_raw != k_norm


def _build_wrapped_test_pdf(pdf_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    wrapped = [
        (70.0, "Title: Sample CSI Study"),
        (95.0, "Abstract"),
        (115.0, "The moving variances and periods for foreign two-dimensional objects are stable."),
        (145.0, "CSI variation values are computed from vari-"),
        (157.0, "ances across stable measurement time peri-"),
        (169.0, "ods to detect unexpected approaching for-"),
        (181.0, "eign intrusions using a fixed classic two-"),
        (193.0, "dimensional planar array near Eq. (1)."),
        (225.0, "Subsequent evaluation checks stable vari-"),
        (237.0, "ances across short measurement time peri-"),
        (249.0, "ods under standard signal conditions."),
    ]
    for y, text in wrapped:
        # Place lines with x0=300 and tight right edge alignment for wrapped lines
        page.insert_text((300.0, y), text, fontname="helv", fontsize=10)
    doc.save(str(pdf_path))
    doc.close()


class _RecordingTranslatorAdapter(TranslationAdapter):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def is_mock(self) -> bool:
        return True

    def check_health(self) -> dict:
        return {"ok": True, "mode": "recording_mock"}

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
        self.calls.append(
            {
                "source_text": source_text,
                "context": context,
                "history": list(history or []),
            }
        )
        placeholders = " ".join(re.findall(r"\{v\d+\}", source_text))
        tag = "正規化入力" if "variances" in source_text else "生入力"
        out = (
            f"本手法は分散と期間および異物検知の実験条件を整理し、配列構成と信号特性を詳しく解析する（{tag}）。"
            + (f" {placeholders}" if placeholders else "")
        )
        return TranslationAdapterResponse(
            translated_text=out,
            raw_output=out,
            prompt_used=source_text,
            finish_reason="stop",
            model_name="recording-mock",
            quantization="Q4_K_M",
        )


def test_adapter_and_job_manager_toggle_preserves_invariants_and_cache_isolation(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "wrapped_sample.pdf"
    _build_wrapped_test_pdf(pdf_path)

    pdf_hash = compute_sha256(pdf_path)
    doc = Document(
        id="doc-hyph-test",
        original_filename=pdf_path.name,
        safe_storage_path=str(pdf_path),
        sha256_hash=pdf_hash,
        file_size_bytes=pdf_path.stat().st_size,
        page_count=1,
    )
    adapter = NativePreservingAdapter()
    blocks_norm = adapter.parse_and_extract_blocks(
        doc,
        [0],
        job_config=TranslationJobConfig(normalize_line_break_hyphens=True),
    )
    blocks_raw = adapter.parse_and_extract_blocks(
        doc,
        [0],
        job_config=TranslationJobConfig(normalize_line_break_hyphens=False),
    )

    assert len(blocks_norm) == len(blocks_raw)
    for b_norm, b_raw in zip(blocks_norm, blocks_raw):
        assert b_norm.source_text == b_raw.source_text
        assert b_norm.bbox == b_raw.bbox
        assert b_norm.masked_source_text == b_raw.masked_source_text
        assert b_norm.protected_items == b_raw.protected_items
        assert b_norm.inline_math_spans == b_raw.inline_math_spans
        assert b_raw.llm_input_text is None

    target_norm = next(b for b in blocks_norm if "vari-" in b.source_text)
    assert "vari- ances" in target_norm.source_text
    assert "vari- ances" in (target_norm.masked_source_text or "")
    assert target_norm.llm_input_text is not None
    assert "variances" in target_norm.llm_input_text
    assert "periods" in target_norm.llm_input_text
    assert "foreign" in target_norm.llm_input_text
    assert "two- dimensional" in target_norm.llm_input_text
    assert "{v1}" in target_norm.llm_input_text

    # Run end-to-end JobManager with normalize_line_break_hyphens=False then True on separate docs
    cfg = AppConfig(runtime_dir=tmp_path / "runtime")
    cfg.ensure_directories()
    db = DatabaseRepository(cfg.db_path)
    recorder = _RecordingTranslatorAdapter()
    manager = JobManager(config=cfg, db=db, translation_adapter=recorder)

    def _register_doc(did: str) -> None:
        d = Document(
            id=did,
            original_filename=pdf_path.name,
            safe_storage_path=str(pdf_path),
            sha256_hash=pdf_hash,
            file_size_bytes=pdf_path.stat().st_size,
            page_count=1,
        )
        db.save_document(d)

    _register_doc("doc-ab-raw")
    raw_cfg = TranslationJobConfig(
        use_mock_translator=True,
        normalize_line_break_hyphens=False,
    )
    job_raw = manager.run_job(
        manager.create_job(document_id="doc-ab-raw", job_config=raw_cfg).id
    )
    assert job_raw.execution_provenance["preprocessing_version"] == PREPROCESSING_VERSION_RAW
    assert job_raw.execution_provenance["line_break_hyphen_normalized_blocks"] == 0
    raw_prompts = [c["source_text"] for c in recorder.calls]
    assert any("vari- ances" in p for p in raw_prompts)

    # Same preprocessing mode reuses cache cleanly
    recorder.calls.clear()
    _register_doc("doc-ab-raw-repeat")
    job_raw_repeat = manager.run_job(
        manager.create_job(document_id="doc-ab-raw-repeat", job_config=raw_cfg).id
    )
    assert job_raw_repeat.execution_provenance["cache_hit_blocks"] >= 2
    assert len(recorder.calls) == 0

    # Switching to line-hyphen-v1 MUST NOT hit raw-v0 cache entries
    recorder.calls.clear()
    _register_doc("doc-ab-norm")
    norm_cfg = TranslationJobConfig(
        use_mock_translator=True,
        normalize_line_break_hyphens=True,
    )
    job_norm = manager.run_job(
        manager.create_job(document_id="doc-ab-norm", job_config=norm_cfg).id
    )
    assert (
        job_norm.execution_provenance["preprocessing_version"]
        == PREPROCESSING_VERSION_DEHYPHENATED
    )
    assert job_norm.execution_provenance["line_break_hyphen_normalized_blocks"] >= 2
    assert job_norm.execution_provenance["cache_hit_blocks"] == 0
    assert job_norm.execution_provenance["newly_inferenced_blocks"] >= 2
    norm_prompts = [c["source_text"] for c in recorder.calls]
    assert any("variances" in p and "vari- ances" not in p for p in norm_prompts)
    # History passed to the second paragraph must also use normalized text
    assert any(
        any("variances" in h.source_text and "vari- ances" not in h.source_text for h in c["history"])
        for c in recorder.calls
    )
