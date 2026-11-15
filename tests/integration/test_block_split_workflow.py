"""Integration tests for drag-selection block splitting, APIs, locking, rollback, and mono/dual PDF export.

All tests here use synthetic PyMuPDF documents and mock/spy translation adapters
(neither real user PDF nor real PLaMo LLM server is used).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.config import AppConfig
from backend.domain.models import (
    BlockKind,
    BlockStatus,
    GlossaryEntry,
    IssueSeverity,
    JobState,
    ProtectionMode,
    TranslationJobConfig,
    ValidationIssue,
)
from backend.jobs.manager import JobManager
from backend.pdf.validation.preflight import compute_sha256, verify_original_unchanged
from backend.storage.database import DatabaseRepository
from backend.translation.adapters.base import (
    TranslationAdapter,
    TranslationAdapterResponse,
)
from backend.translation.adapters.mock_adapter import MockPlamoAdapter
from backend.translation.prompts.pfml3 import HistoryPair

pytestmark = pytest.mark.integration


class RecordingSpyAdapter(TranslationAdapter):
    """Records every prompt sent to translate_segment and wraps MockPlamoAdapter."""

    adapter_version = "recording-spy-mock-1.0"
    prompt_version = "pfml3-v1"
    is_mock = True

    def __init__(self) -> None:
        self.inner = MockPlamoAdapter()
        self.call_count = 0
        self.recorded_prompts: list[str] = []

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
        self.recorded_prompts.append(source_text)
        ja_override: str | None = None
        if "A point is identified" in source_text:
            ja_override = (
                "降下幅が {v1} の差の閾値を超える場合、その点は降下点と判定される。"
                if "{v1}" in source_text
                else "降下幅が差の閾値を超える場合、その点は降下点と判定される。"
            )
        elif "To characterize" in source_text or "normalization window" in source_text:
            ja_override = "急峻な遷移を特徴づけるため、正規化誤差指標を次式で与える。"
        elif "where k denotes" in source_text:
            ja_override = "ここで k は標本番号を示し、正規化係数はいずれも厳密に正である。"
        elif "After thresholding" in source_text:
            ja_override = "閾値処理の後、二次フィルタにより短い過渡スパイクを除去する。"
        if ja_override is not None:
            return TranslationAdapterResponse(
                translated_text=ja_override,
                raw_output=ja_override,
                prompt_used=source_text,
                model_name="mock-spy-ja",
                quantization="MOCK",
                is_mock=True,
                prompt_tokens=16,
                completion_tokens=16,
            )
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


def _create_synthetic_eq6_pdf(
    pdf_path: Path,
    *,
    with_top_prose: bool = False,
) -> tuple[Path, list[float]]:
    """Build synthetic PDF reproducing Eq.(6) display formula coupled into one prose block.

    Returns `(pdf_path, drag_bbox)` where `drag_bbox` intentionally includes small padding around
    the Eq.(6) display formula and `(6)`.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 180.0

    if with_top_prose:
        page.insert_text(
            (72, y),
            "To characterize abrupt signal transitions cleanly, consider the metric:",
            fontsize=10,
            fontname="helv",
        )
        y += 13.0

    eq_y = y
    page.insert_text((72, eq_y), "Falling width = mean(a", fontsize=10, fontname="helv")
    page.insert_text((181, eq_y + 3.0), "t0-10,t0-1", fontsize=6.8, fontname="helv")
    page.insert_text((211, eq_y), ") - mean(a", fontsize=10, fontname="helv")
    page.insert_text((261, eq_y + 3.0), "t0,t0+9", fontsize=6.8, fontname="helv")
    page.insert_text((290, eq_y), ")", fontsize=10, fontname="helv")
    page.insert_text((328, eq_y), "(6)", fontsize=10, fontname="helv")

    body_y = eq_y + 12.5
    page.insert_text(
        (72, body_y),
        "A point is identified as the falling point if the falling width",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text(
        (72, body_y + 12.0),
        "exceeds the difference threshold defined by Eq. (5).",
        fontsize=10,
        fontname="helv",
    )

    # Second unrelated paragraph well below to check non-split IDs stay stable
    page.insert_text(
        (72, body_y + 68.0),
        "After thresholding, secondary filtering rejects short transient spikes cleanly.",
        fontsize=10,
        fontname="helv",
    )
    # Distant unrelated notice box (testing manual ProtectionRegion preservation)
    page.insert_text(
        (72, 620.0),
        "Unrelated appendix figure legend preserved independently.",
        fontsize=10,
        fontname="helv",
    )

    doc.save(pdf_path)
    doc.close()

    # Baseline text origin at eq_y -> glyph box roughly [eq_y - 10, eq_y + 5]; give generous y margin
    drag_bbox = [68.0, eq_y - 11.0, 355.0, eq_y + 5.0]
    return pdf_path, drag_bbox


def _create_three_way_vector_fraction_pdf(pdf_path: Path) -> tuple[Path, list[float], list[float]]:
    """Build synthetic 3-part block (top prose + vector fraction & radical + bottom prose) plus adjacent box."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    page.insert_text(
        (72, 240),
        "Given the normalization window, the scaled error metric satisfies:",
        fontsize=10,
        fontname="helv",
    )
    # Tightly spaced stacked numerator + denominator/radical inside the same raw MuPDF block (all x=72)
    page.insert_text((72, 252), "f = N(x + y)     + z_0", fontsize=9.0, fontname="helv")
    page.insert_text((72, 262), "    D(k + 1)", fontsize=9.0, fontname="helv")
    page.insert_text((280, 262), "(7)", fontsize=9.0, fontname="helv")
    page.draw_line((85, 255), (132, 255), color=(0, 0, 0), width=1.0)
    page.draw_line((148, 245), (172, 245), color=(0, 0, 0), width=1.0)
    page.insert_text(
        (72, 275),
        "where k denotes the sample index and D remains strictly positive.",
        fontsize=10,
        fontname="helv",
    )

    # Separate adjacent figure/note rect outside the block (must not be absorbed)
    unrelated_bbox = [395.0, 235.0, 520.0, 300.0]
    page.draw_rect(pymupdf.Rect(*unrelated_bbox), color=(0, 0, 1), width=1.0)
    page.insert_text((405, 265), "Figure Box", fontsize=9.5, fontname="helv")

    doc.save(pdf_path)
    doc.close()

    drag_bbox = [70.0, 244.0, 185.0, 265.0]
    return pdf_path, drag_bbox, unrelated_bbox


def _rects_intersect_with_margin(a: list[float], b: list[float], tol: float = 0.25) -> bool:
    return not (
        a[2] <= b[0] + tol
        or a[0] >= b[2] - tol
        or a[3] <= b[1] + tol
        or a[1] >= b[3] - tol
    )


def test_block_split_end_to_end_translation_mono_and_dual_pdf(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify full workflow on synthetic Eq.(6) mixed PDF:
    preview (valid & invalid), split confirmation, prompt verification, mono+dual export,
    PyMuPDF vector/glyph non-overlap check, DB restart persistence, repeated zero-LLM retypeset,
    original PDF hash invariance, and split deletion preserving unrelated ProtectionRegions.
    """
    pdf_path, drag_bbox = _create_synthetic_eq6_pdf(tmp_path / "eq6_mixed.pdf", with_top_prose=False)
    orig_sha = compute_sha256(pdf_path)

    spy = RecordingSpyAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="split-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "split-tok"}

    # 1. Upload & parse unsplit baseline -> reproduces coupled single block
    with open(pdf_path, "rb") as fh:
        up = client.post(
            "/api/documents/upload",
            files={"file": ("eq6_mixed.pdf", fh, "application/pdf")},
            headers=headers,
        )
    assert up.status_code == 200
    doc = up.json()
    doc_id = doc["id"]

    raw_blocks = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    coupled = next(
        b for b in raw_blocks if "Falling width" in b["source_text"] and "A point is identified" in b["source_text"]
    )
    coupled_id = coupled["id"]
    after_block_ids = [b["id"] for b in raw_blocks if b["id"] != coupled_id]

    # Add an unrelated manual ProtectionRegion before splitting (must survive split & delete)
    reg_resp = client.post(
        f"/api/documents/{doc_id}/protection-regions",
        json={
            "page_index": 0,
            "bbox": [68.0, 608.0, 340.0, 630.0],
            "mode": ProtectionMode.DO_NOT_TRANSLATE.value,
            "note": "Keep appendix untouched",
        },
        headers=headers,
    )
    assert reg_resp.status_code == 200
    reg_id = reg_resp.json()["id"]

    # 2. Reject invalid empty selection without saving anything
    empty_prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": [450.0, 700.0, 520.0, 730.0], "include_equation_number": True},
        headers=headers,
    )
    assert empty_prev.status_code == 200
    empty_data = empty_prev.json()
    assert empty_data["valid"] is False
    assert empty_data["rejection_code"] in {"EMPTY_SELECTION", "NO_MATCHING_BLOCK"}
    assert empty_data["retry_guidance"]
    assert client.get(f"/api/documents/{doc_id}/block-splits", headers=headers).json() == []

    # Reject multi-block spanning drag without saving
    multi_prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": [65.0, 170.0, 360.0, 290.0], "include_equation_number": True},
        headers=headers,
    )
    assert multi_prev.status_code == 200
    assert multi_prev.json()["valid"] is False

    # 3. Valid preview over Eq.(6) + confirm split
    prev_resp = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    )
    assert prev_resp.status_code == 200
    prev = prev_resp.json()
    assert prev["valid"] is True
    assert prev["parent_block_id"] == coupled_id
    assert prev["equation_number_text"] == "(6)"
    roles = [p["role"] for p in prev["parts"]]
    assert roles == ["formula", "bottom"]
    formula_part = next(p for p in prev["parts"] if p["role"] == "formula")
    bottom_part = next(p for p in prev["parts"] if p["role"] == "bottom")
    assert formula_part["will_protect"] is True
    assert "Falling width" in formula_part["source_text"]
    assert "(6)" in formula_part["source_text"]
    assert bottom_part["will_protect"] is False
    assert "A point is identified" in bottom_part["source_text"]
    assert "Eq. (5)" in bottom_part["source_text"]

    confirm = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "note": "Eq.(6) separation",
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    )
    assert confirm.status_code == 200
    mut = confirm.json()
    split_id = mut["split"]["id"]
    assert mut["split"]["applied_status"] == "applied"
    split_blocks = mut["blocks"]

    # Repeated re-parse must not duplicate or re-split children, and unrelated block IDs remain identical
    reparse_2 = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    reparse_3 = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    assert [b["id"] for b in reparse_2] == [b["id"] for b in split_blocks]
    assert [b["id"] for b in reparse_3] == [b["id"] for b in split_blocks]
    for keep_id in after_block_ids:
        assert any(b["id"] == keep_id for b in split_blocks)

    f_blk = next(b for b in reparse_3 if b["split_role"] == "formula")
    b_blk = next(b for b in reparse_3 if b["split_role"] == "bottom")
    assert f_blk["status"] == BlockStatus.PROTECTED_SKIP.value
    assert b_blk["status"] == BlockStatus.PENDING.value or b_blk["protection_source"] == "none"
    assert not any(b["id"] == coupled_id for b in reparse_3)

    # Attempt to force-translate split formula child must be rejected cleanly
    bad_override = client.put(
        f"/api/documents/{doc_id}/blocks/{f_blk['id']}/protection",
        json={"user_protection_override": "force_translate"},
        headers=headers,
    )
    assert bad_override.status_code in (400, 422)

    # 4. Run full job -> inspect SpyAdapter prompts + mono/dual PDF outputs
    cfg = TranslationJobConfig(
        use_mock_translator=True,
        output_mono_pdf=True,
        output_dual_pdf=True,
    ).model_dump()
    job_resp = client.post(f"/api/documents/{doc_id}/jobs", json=cfg, headers=headers)
    assert job_resp.status_code == 200
    job = job_resp.json()
    job_id = job["id"]
    assert job["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)

    # Formula text must NEVER enter any translation prompt; bottom body text MUST enter with masked inline Eq.(5)
    assert spy.call_count >= 1
    assert all("Falling width" not in p for p in spy.recorded_prompts)
    assert all("(6)" not in p for p in spy.recorded_prompts)
    matching_bottom_prompts = [p for p in spy.recorded_prompts if "A point is identified" in p]
    assert len(matching_bottom_prompts) == 1
    assert "{v1}" in matching_bottom_prompts[0]

    # Inspect artifacts via download & PyMuPDF inspection
    artifacts = client.get(f"/api/jobs/{job_id}/artifacts", headers=headers).json()
    mono_art = next(a for a in artifacts if a["artifact_type"] == "mono_ja_pdf")
    dual_art = next(a for a in artifacts if a["artifact_type"] == "dual_bilingual_pdf")
    assert mono_art["is_finalized"] is True
    assert dual_art["is_finalized"] is True

    mono_pdf = pymupdf.open(mono_art["file_path"])
    assert len(mono_pdf) == 1
    mono_page = mono_pdf[0]
    mono_text = mono_page.get_text("text").replace("\xa0", " ")
    mono_compact = mono_text.replace("\n", "")
    assert "Falling width" in mono_text
    assert "(6)" in mono_text
    assert "t0-10,t0-1" in mono_compact
    assert "t0,t0+9" in mono_compact
    assert "A point is identified" not in mono_text
    assert "Eq. (5)" in mono_text
    assert "降下点と判定" in mono_compact

    # Verify translated spans on mono page do not geometrically collide with protected formula bbox
    f_bbox = f_blk["bbox"]
    mono_dict = mono_page.get_text("dict")
    for blk_d in mono_dict.get("blocks", []):
        for ln in blk_d.get("lines", []):
            for sp in ln.get("spans", []):
                sp_txt = sp.get("text", "").strip()
                if any(tok in sp_txt for tok in ("降下", "閾値", "二次フィルタ")):
                    assert not _rects_intersect_with_margin(list(sp["bbox"]), f_bbox, tol=0.2), (
                        f"Translated span {sp_txt!r} intersects protected formula bbox {f_bbox}"
                    )
    mono_pdf.close()

    dual_pdf = pymupdf.open(dual_art["file_path"])
    assert len(dual_pdf) == 1
    dual_page = dual_pdf[0]
    dual_w = float(dual_page.rect.width)
    dual_h = float(dual_page.rect.height)
    left_text = dual_page.get_text("text", clip=pymupdf.Rect(0, 0, dual_w / 2.0, dual_h)).replace("\xa0", " ")
    right_text = dual_page.get_text("text", clip=pymupdf.Rect(dual_w / 2.0, 0, dual_w, dual_h)).replace("\xa0", " ")
    assert "Falling width" in left_text
    assert "A point is identified" in left_text
    assert "Falling width" in right_text
    assert "(6)" in right_text
    assert "A point is identified" not in right_text
    assert "降下点と判定" in right_text.replace("\n", "")
    dual_pdf.close()

    assert verify_original_unchanged(pdf_path, orig_sha)

    # 5. Repeated Zero-LLM Retypeset after manual edit on bottom block must not duplicate unrelated issues
    issues_before = client.get(f"/api/jobs/{job_id}/issues", headers=headers).json()
    b_latest = next(
        b for b in client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json() if b["id"] == b_blk["id"]
    )
    manual_edit_text = "差の閾値 {v1} を超えた場合、その点は降下点として判定される。"
    edit_resp = client.put(
        f"/api/documents/{doc_id}/blocks/{b_blk['id']}/manual-edit",
        json={
            "manual_translation_text": manual_edit_text,
            "expected_edit_revision": b_latest["edit_revision"],
        },
        headers=headers,
    )
    assert edit_resp.status_code == 200
    calls_before_rt = spy.call_count

    for _ in range(2):
        rt_resp = client.post(
            f"/api/jobs/{job_id}/retypeset",
            json={"allow_validation_warnings": True},
            headers=headers,
        )
        assert rt_resp.status_code == 200
        assert rt_resp.json()["llm_calls_made"] == 0
    assert spy.call_count == calls_before_rt

    issues_after = client.get(f"/api/jobs/{job_id}/issues", headers=headers).json()
    unrelated_before = [i for i in issues_before if i.get("block_id") != b_blk["id"]]
    unrelated_after = [i for i in issues_after if i.get("block_id") != b_blk["id"]]
    assert len(unrelated_after) == len(unrelated_before)

    mono_rt = pymupdf.open(
        next(
            a for a in client.get(f"/api/jobs/{job_id}/artifacts", headers=headers).json() if a["artifact_type"] == "mono_ja_pdf"
        )["file_path"]
    )
    mono_rt_text = mono_rt[0].get_text("text").replace("\xa0", " ")
    assert "Falling width" in mono_rt_text
    assert "(6)" in mono_rt_text
    assert "降下点として判定" in mono_rt_text.replace("\n", "")
    assert "Eq. (5)" in mono_rt_text
    mono_rt.close()

    # 6. Simulate fresh app restart -> split spec, child blocks & manual region persist identically
    mgr_restarted = JobManager(config=isolated_config, translation_adapter=spy)
    app_restarted = create_app(config=isolated_config, job_manager=mgr_restarted, session_token="restart-tok")
    client2 = TestClient(app_restarted)
    h2 = {"X-PaperBridge-Token": "restart-tok"}

    splits_reloaded = client2.get(f"/api/documents/{doc_id}/block-splits", headers=h2).json()
    assert len(splits_reloaded) == 1
    assert splits_reloaded[0]["id"] == split_id
    assert splits_reloaded[0]["applied_status"] == "applied"
    reparse_after_restart = client2.post(f"/api/documents/{doc_id}/parse", headers=h2).json()
    assert any(b["id"] == f_blk["id"] and b["split_role"] == "formula" for b in reparse_after_restart)

    # 7. Delete split -> restores parent block & leaves unrelated manual ProtectionRegion intact
    del_resp = client2.delete(f"/api/documents/{doc_id}/block-splits/{split_id}", headers=h2)
    assert del_resp.status_code == 200
    del_data = del_resp.json()
    assert del_data["deleted"] is True
    assert del_data["splits"] == []
    assert any(b["id"] == coupled_id and b["split_role"] is None for b in del_data["blocks"])
    regs_after_del = client2.get(f"/api/documents/{doc_id}/protection-regions", headers=h2).json()
    assert [r["id"] for r in regs_after_del] == [reg_id]


def test_three_way_split_fraction_and_radical_vector_preservation(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify 3-way split (top / formula / bottom) preserves fraction/radical drawings without absorbing neighbors."""
    pdf_path, drag_bbox, unrelated_bbox = _create_three_way_vector_fraction_pdf(tmp_path / "vec_frac.pdf")
    spy = RecordingSpyAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="vec-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "vec-tok"}

    with open(pdf_path, "rb") as fh:
        doc_id = client.post(
            "/api/documents/upload",
            files={"file": ("vec_frac.pdf", fh, "application/pdf")},
            headers=headers,
        ).json()["id"]

    prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()
    assert prev["valid"] is True
    assert [p["role"] for p in prev["parts"]] == ["top", "formula", "bottom"]
    assert prev["equation_number_text"] == "(7)"

    formula_bbox = next(p["bbox"] for p in prev["parts"] if p["role"] == "formula")
    assert not _rects_intersect_with_margin(formula_bbox, unrelated_bbox, tol=0.1)

    conf = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    )
    assert conf.status_code == 200
    blocks = conf.json()["blocks"]
    top_blk = next(b for b in blocks if b["split_role"] == "top")
    f_blk = next(b for b in blocks if b["split_role"] == "formula")
    bot_blk = next(b for b in blocks if b["split_role"] == "bottom")
    assert top_blk["reading_order"] < f_blk["reading_order"] < bot_blk["reading_order"]

    job_resp = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True, output_mono_pdf=True, output_dual_pdf=False).model_dump(),
        headers=headers,
    )
    assert job_resp.status_code == 200
    arts = client.get(f"/api/jobs/{job_resp.json()['id']}/artifacts", headers=headers).json()
    mono_art = next(a for a in arts if a["artifact_type"] == "mono_ja_pdf")

    # Verify vector drawings inside the formula box (fraction bar & radical line) survive into mono_ja_pdf
    m_doc = pymupdf.open(mono_art["file_path"])
    m_page = m_doc[0]
    drawings = m_page.get_drawings()
    formula_drawings = [
        d for d in drawings if _rects_intersect_with_margin(list(d["rect"]), f_blk["bbox"], tol=0.5)
    ]
    assert len(formula_drawings) >= 2
    txt = m_page.get_text("text")
    assert "(7)" in txt
    assert "N(x + y)" in txt
    assert "D(k + 1)" in txt
    assert "Figure Box" in txt
    assert "誤差指標" in txt
    assert "標本番号" in txt
    m_doc.close()


def test_split_stale_preview_409_and_invalidation_of_prior_translations(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify:
    1) Stale preview returns 409 Conflict after protection_revision changes.
    2) Valid split after prior translation/manual edit clears stale parent translations in BOTH SQLite and JSON,
       flags needs_retranslation=True on translatable children, marks existing job outputs_stale=True, and
       rejects retypeset reuse of stale children without re-translation.
    """
    pdf_path, drag_bbox = _create_synthetic_eq6_pdf(tmp_path / "stale_cache.pdf", with_top_prose=True)
    spy = RecordingSpyAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="stale-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "stale-tok"}

    with open(pdf_path, "rb") as fh:
        doc_id = client.post(
            "/api/documents/upload",
            files={"file": ("stale_cache.pdf", fh, "application/pdf")},
            headers=headers,
        ).json()["id"]

    # Translate BEFORE splitting so parent block has translation_text AND manual_translation_text
    job1 = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    ).json()
    job1_id = job1["id"]

    blocks_pre = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()
    parent_blk = next(b for b in blocks_pre if "Falling width" in b["source_text"])
    client.put(
        f"/api/documents/{doc_id}/blocks/{parent_blk['id']}/manual-edit",
        json={
            "manual_translation_text": "古い親手動訳文: {v1}" if parent_blk["protected_items"] else "古い親手動訳文",
            "expected_edit_revision": parent_blk["edit_revision"],
        },
        headers=headers,
    )

    # Capture preview (notices will_invalidate_manual_edits=True)
    prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()
    assert prev["valid"] is True
    assert prev["will_invalidate_manual_edits"] is True
    assert parent_blk["id"] in prev["affected_manual_block_ids"]

    # Mutate protection_revision between preview and confirm -> must reject with 409
    client.post(
        f"/api/documents/{doc_id}/protection-regions",
        json={
            "page_index": 0,
            "bbox": [68.0, 608.0, 340.0, 630.0],
            "mode": ProtectionMode.DO_NOT_TRANSLATE.value,
        },
        headers=headers,
    )
    stale_confirm = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    )
    assert stale_confirm.status_code == 409

    # Fresh preview & confirm -> children have NULL translations/manual_translation_text in BOTH SQL & JSON
    prev2 = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()
    ok_confirm = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev2["protection_revision"],
            "expected_parse_revision": prev2["parse_revision"],
            "expected_parent_signature": prev2["parent_signature"],
        },
        headers=headers,
    )
    assert ok_confirm.status_code == 200
    child_blocks = [b for b in ok_confirm.json()["blocks"] if b["split_role"] is not None]
    assert {b["split_role"] for b in child_blocks} == {"top", "formula", "bottom"}
    for cb in child_blocks:
        assert cb["translation_text"] is None
        assert cb["manual_translation_text"] is None
        if cb["split_role"] in ("top", "bottom"):
            assert cb["needs_retranslation"] is True

    # Verify SQLite column & payload_json consistency directly
    with sqlite3.connect(isolated_config.db_path) as conn:
        rows = conn.execute(
            "SELECT id, translation_text, manual_translation_text, payload_json FROM blocks WHERE document_id = ?",
            (doc_id,),
        ).fetchall()
    for bid, sql_tr, sql_man, raw_json in rows:
        data = json.loads(raw_json)
        if data.get("split_role") is not None:
            assert sql_tr is None and data["translation_text"] is None, f"Stale translation_text leaked in {bid}"
            assert sql_man is None and data["manual_translation_text"] is None, f"Stale manual_translation_text leaked in {bid}"

    # Prior completed job must be flagged outputs_stale=True
    job1_after = client.get(f"/api/jobs/{job1_id}", headers=headers).json()
    assert job1_after["outputs_stale"] is True
    assert job1_after["outputs_stale_reason"]

    # Zero-LLM retypeset must NOT inject stale old parent text onto un-retranslated split children
    calls_before = spy.call_count
    rt_after_split = client.post(
        f"/api/jobs/{job1_id}/retypeset",
        json={"allow_validation_warnings": True},
        headers=headers,
    )
    assert rt_after_split.status_code == 200
    assert spy.call_count == calls_before
    # Running a new job actually translates top & bottom independently without reusing old parent text
    calls_before_job2 = spy.call_count
    job2 = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    ).json()
    assert job2["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)
    assert spy.call_count >= calls_before_job2 + 2
    blocks_post_job2 = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()
    for cb in [b for b in blocks_post_job2 if b["split_role"] in ("top", "bottom")]:
        assert cb["needs_retranslation"] is False
        assert "古い親手動訳文" not in (cb["translation_text"] or "")


def test_document_operation_locking_409_and_split_rollback_safety(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify atomic check-and-acquire per-document lock (409 on conflict, release on exception)
    and atomic SQLite rollback when commit_split_transaction fails mid-flight (both create & delete).
    """
    pdf_path, drag_bbox = _create_synthetic_eq6_pdf(tmp_path / "lock_rb.pdf", with_top_prose=False)
    spy = RecordingSpyAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="lock-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "lock-tok"}

    with open(pdf_path, "rb") as fh:
        doc_id = client.post(
            "/api/documents/upload",
            files={"file": ("lock_rb.pdf", fh, "application/pdf")},
            headers=headers,
        ).json()["id"]
    prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()

    # 1. Simulate active document operation lock -> all mutating doc/job endpoints must return 409 immediately
    entered = threading.Event()
    release = threading.Event()

    def hold_lock_then_fail() -> None:
        try:
            with mgr.acquire_document_operation(doc_id, "test_active_op"):
                entered.set()
                release.wait(timeout=5.0)
                raise RuntimeError("simulated worker crash")
        except RuntimeError:
            pass

    t = threading.Thread(target=hold_lock_then_fail, daemon=True)
    t.start()
    assert entered.wait(timeout=3.0)
    try:
        r_split = client.post(
            f"/api/documents/{doc_id}/block-splits",
            json={
                "page_index": 0,
                "requested_bbox": drag_bbox,
                "include_equation_number": True,
                "expected_protection_revision": prev["protection_revision"],
                "expected_parse_revision": prev["parse_revision"],
                "expected_parent_signature": prev["parent_signature"],
            },
            headers=headers,
        )
        assert r_split.status_code == 409

        r_parse = client.post(f"/api/documents/{doc_id}/parse", headers=headers)
        assert r_parse.status_code == 409

        r_reg = client.post(
            f"/api/documents/{doc_id}/protection-regions",
            json={"page_index": 0, "bbox": [68.0, 608.0, 340.0, 630.0], "mode": ProtectionMode.DO_NOT_TRANSLATE.value},
            headers=headers,
        )
        assert r_reg.status_code == 409

        r_job = client.post(
            f"/api/documents/{doc_id}/jobs",
            json=TranslationJobConfig(use_mock_translator=True).model_dump(),
            headers=headers,
        )
        assert r_job.status_code == 409
    finally:
        release.set()
        t.join(timeout=3.0)

    # Lock must be released even after an exception!
    assert mgr.has_active_document_operation(doc_id) is False

    # 2. Inject SQLite transaction failure during CREATE split -> zero half-written state
    blocks_before = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()
    doc_before = client.get(f"/api/documents/{doc_id}", headers=headers).json()

    orig_commit = mgr.db.commit_split_transaction

    def failing_commit_create(*args: Any, **kwargs: Any) -> None:
        kwargs["fail_stage_for_test"] = "after_blocks"
        return orig_commit(*args, **kwargs)

    mgr.db.commit_split_transaction = failing_commit_create  # type: ignore[method-assign]
    with pytest.raises(Exception):
        mgr.create_block_split(
            doc_id,
            page_index=0,
            requested_bbox=drag_bbox,
            include_equation_number=True,
            expected_protection_revision=prev["protection_revision"],
            expected_parse_revision=prev["parse_revision"],
            expected_parent_signature=prev["parent_signature"],
        )
    mgr.db.commit_split_transaction = orig_commit  # type: ignore[method-assign]

    assert mgr.has_active_document_operation(doc_id) is False
    assert client.get(f"/api/documents/{doc_id}/block-splits", headers=headers).json() == []
    assert [b["id"] for b in client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()] == [
        b["id"] for b in blocks_before
    ]
    assert client.get(f"/api/documents/{doc_id}", headers=headers).json()["parse_revision"] == doc_before["parse_revision"]

    # Succeed cleanly now
    ok_split = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    ).json()
    split_id = ok_split["split"]["id"]

    # 3. Inject SQLite transaction failure during DELETE split -> split spec AND child blocks must remain intact
    blocks_mid = client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()

    def failing_commit_delete(*args: Any, **kwargs: Any) -> None:
        kwargs["fail_stage_for_test"] = "before_commit"
        return orig_commit(*args, **kwargs)

    mgr.db.commit_split_transaction = failing_commit_delete  # type: ignore[method-assign]
    with pytest.raises(Exception):
        mgr.delete_block_split(doc_id, split_id)
    mgr.db.commit_split_transaction = orig_commit  # type: ignore[method-assign]

    assert len(client.get(f"/api/documents/{doc_id}/block-splits", headers=headers).json()) == 1
    assert [b["id"] for b in client.get(f"/api/documents/{doc_id}/blocks", headers=headers).json()] == [
        b["id"] for b in blocks_mid
    ]


def test_backward_compatibility_and_pending_ambiguous_split(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify legacy documents without block_splits work untouched and ambiguous/changed splits stay pending."""
    pdf_path, drag_bbox = _create_synthetic_eq6_pdf(tmp_path / "compat.pdf", with_top_prose=False)
    app = create_app(config=isolated_config, session_token="compat-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "compat-tok"}

    with open(pdf_path, "rb") as fh:
        doc_id = client.post(
            "/api/documents/upload",
            files={"file": ("compat.pdf", fh, "application/pdf")},
            headers=headers,
        ).json()["id"]

    # No block splits -> normal completion
    assert client.get(f"/api/documents/{doc_id}/block-splits", headers=headers).json() == []
    job = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    ).json()
    assert job["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)

    # Confirm valid split, then corrupt stored parent_source_hash in DB to simulate source change
    prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()
    conf = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    ).json()
    split_id = conf["split"]["id"]

    repo = DatabaseRepository(isolated_config.db_path)
    sp = repo.list_block_splits(doc_id)[0]
    sp.parent_source_hash = "0" * 64
    repo.save_block_split(sp)

    # Reparse must NOT force-apply to wrong block; must mark split pending_* cleanly and explain why
    blocks_after = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    splits_after = client.get(f"/api/documents/{doc_id}/block-splits", headers=headers).json()
    assert len(splits_after) == 1
    assert splits_after[0]["id"] == split_id
    assert splits_after[0]["applied_status"].startswith("pending_")
    assert splits_after[0]["status_reason"]
    assert all(b["split_spec_id"] != split_id for b in blocks_after)

    # Starting a job with unresolved pending_* splits MUST reject with 409 by default
    blocked_job = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    )
    assert blocked_job.status_code == 409
    assert "PendingSplitsConflict" in blocked_job.json()["detail"]

    # Explicit opt-in allow_pending_splits=True proceeds and records warning issue
    allowed_job = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(
            use_mock_translator=True,
            allow_pending_splits=True,
        ).model_dump(),
        headers=headers,
    )
    assert allowed_job.status_code == 200
    allowed_data = allowed_job.json()
    assert allowed_data["state"] == JobState.COMPLETED_WITH_WARNINGS.value
    job_issues = client.get(f"/api/jobs/{allowed_data['id']}/issues", headers=headers).json()
    assert any(iss["code"] == "PENDING_BLOCK_SPLIT_UNAPPLIED" for iss in job_issues)


def test_preexisting_protection_region_and_partial_retypeset_stale_behavior(
    tmp_path: Path,
    isolated_config: AppConfig,
) -> None:
    """Verify:
    1) Pre-existing manual ProtectionRegion over formula (even overlapping bottom edge) does
       NOT re-protect split bottom prose; deleting split restores original priority cleanly.
    2) Retypesetting while untranslated/needs_retranslation blocks remain keeps outputs_stale=True
       and job state COMPLETED_WITH_WARNINGS across repeated retypesets.
    """
    pdf_path, drag_bbox = _create_synthetic_eq6_pdf(tmp_path / "prio_stale.pdf", with_top_prose=False)
    spy = RecordingSpyAdapter()
    mgr = JobManager(config=isolated_config, translation_adapter=spy)
    app = create_app(config=isolated_config, job_manager=mgr, session_token="prio-tok")
    client = TestClient(app)
    headers = {"X-PaperBridge-Token": "prio-tok"}

    with open(pdf_path, "rb") as fh:
        doc_id = client.post(
            "/api/documents/upload",
            files={"file": ("prio_stale.pdf", fh, "application/pdf")},
            headers=headers,
        ).json()["id"]

    # Add a slightly generous PROTECT_AS_FORMULA ProtectionRegion covering Eq.(6) + part of bottom prose
    eq_reg = client.post(
        f"/api/documents/{doc_id}/protection-regions",
        json={
            "page_index": 0,
            "bbox": [drag_bbox[0] - 2.0, drag_bbox[1] - 2.0, drag_bbox[2] + 2.0, drag_bbox[3] + 18.0],
            "mode": "protect_as_formula",
            "note": "先につけた手動数式保護",
        },
        headers=headers,
    )
    assert eq_reg.status_code == 200
    pre_split_blocks = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    coupled = next(b for b in pre_split_blocks if "Falling width" in b["source_text"])
    assert coupled["status"] == BlockStatus.PROTECTED_SKIP.value

    # Now drag-split Eq.(6): bottom prose MUST remain translatable (pending, NOT protected_skip)
    prev = client.post(
        f"/api/documents/{doc_id}/block-splits/preview",
        json={"page_index": 0, "requested_bbox": drag_bbox, "include_equation_number": True},
        headers=headers,
    ).json()
    assert prev["valid"] is True
    assert any(p["role"] == "bottom" and p["will_protect"] is False for p in prev["parts"])

    conf = client.post(
        f"/api/documents/{doc_id}/block-splits",
        json={
            "page_index": 0,
            "requested_bbox": drag_bbox,
            "include_equation_number": True,
            "expected_protection_revision": prev["protection_revision"],
            "expected_parse_revision": prev["parse_revision"],
            "expected_parent_signature": prev["parent_signature"],
        },
        headers=headers,
    ).json()
    split_id = conf["split"]["id"]
    split_blocks = [b for b in conf["blocks"] if b["split_spec_id"] == split_id]
    f_blk = next(b for b in split_blocks if b["split_role"] == "formula")
    b_blk = next(b for b in split_blocks if b["split_role"] == "bottom")
    assert f_blk["status"] == BlockStatus.PROTECTED_SKIP.value
    assert b_blk["status"] == BlockStatus.PENDING.value

    # Run job 1: bottom prose gets translated while formula stays skipped
    job1 = client.post(
        f"/api/documents/{doc_id}/jobs",
        json=TranslationJobConfig(use_mock_translator=True).model_dump(),
        headers=headers,
    ).json()
    assert job1["state"] in (JobState.COMPLETED.value, JobState.COMPLETED_WITH_WARNINGS.value)

    # Delete split -> restored parent must immediately recover pre-existing ProtectionRegion priority
    del_res = client.delete(f"/api/documents/{doc_id}/block-splits/{split_id}", headers=headers)
    assert del_res.status_code == 200
    restored = next(b for b in del_res.json()["blocks"] if "Falling width" in b["source_text"])
    assert restored["status"] == BlockStatus.PROTECTED_SKIP.value
    assert "手動：数式として保護" in (restored["protection_reason"] or "")

    # Remove manual region & mark restored parent translatable (still untranslated) -> retypeset twice without LLM
    client.delete(f"/api/documents/{doc_id}/protection-regions/{eq_reg.json()['id']}", headers=headers)
    reparsed = client.post(f"/api/documents/{doc_id}/parse", headers=headers).json()
    unprot = next(b for b in reparsed if "Falling width" in b["source_text"])
    assert unprot["status"] == BlockStatus.PENDING.value

    for _ in range(2):
        rt = client.post(
            f"/api/jobs/{job1['id']}/retypeset",
            json={"allow_validation_warnings": True},
            headers=headers,
        ).json()
        assert rt["llm_calls_made"] == 0
        assert rt["job"]["outputs_stale"] is True
        assert rt["job"]["outputs_stale_reason"]
        assert rt["job"]["state"] == JobState.COMPLETED_WITH_WARNINGS.value
