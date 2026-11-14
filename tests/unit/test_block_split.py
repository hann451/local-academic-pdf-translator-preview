"""Unit & synthetic PDF tests for drag-selection formula/prose block splitting (Steps 1-3)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from backend.config import AppConfig
from backend.domain.models import (
    Block,
    BlockKind,
    BlockSplitSpec,
    BlockStatus,
    Document,
    IssueSeverity,
    ProtectionMode,
    ProtectionRegion,
    ValidationIssue,
)
from backend.pdf.adapters.native_preserving_adapter import NativePreservingAdapter
from backend.storage.database import DatabaseRepository
from backend.translation.continuation import attach_continuation_candidates


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_doc(pdf_path: Path, doc_id: str = "doc-split-unit") -> Document:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    m = pymupdf.open(pdf_path)
    n = len(m)
    m.close()
    return Document(
        id=doc_id,
        original_filename=pdf_path.name,
        safe_storage_path=str(pdf_path),
        sha256_hash=_sha256(pdf_path),
        file_size_bytes=pdf_path.stat().st_size,
        page_count=n,
    )


def _create_mixed_eq6_pdf(
    pdf_path: Path,
    *,
    with_top_prose: bool = True,
    eq_num_far_right: bool = False,
) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 240.0
    if with_top_prose:
        page.insert_text(
            (72, y),
            "To measure sudden signal drops cleanly, we define the metric:",
            fontsize=10,
            fontname="helv",
        )
        y += 12.5

    # Display formula line at x=72 (same left block column) with lowered small subscript spans
    eq_y = y
    page.insert_text((72, eq_y), "Falling width = mean(a", fontsize=10, fontname="helv")
    page.insert_text((181, eq_y + 3.0), "t0-10,t0-1", fontsize=6.8, fontname="helv")
    page.insert_text((211, eq_y), ") - mean(a", fontsize=10, fontname="helv")
    page.insert_text((261, eq_y + 3.0), "t0,t0+9", fontsize=6.8, fontname="helv")
    page.insert_text((290, eq_y), ")", fontsize=10, fontname="helv")
    if not eq_num_far_right:
        page.insert_text((328, eq_y), "(6)", fontsize=10, fontname="helv")

    y = eq_y + 12.5
    page.insert_text(
        (72, y),
        "A point is identified as the falling point if the falling width",
        fontsize=10,
        fontname="helv",
    )
    page.insert_text(
        (72, y + 12.0),
        "exceeds the difference threshold defined by Eq. (5).",
        fontsize=10,
        fontname="helv",
    )

    # Separate second block well below so we can verify block ID stability
    page.insert_text(
        (72, y + 110.0),
        "Finally, the normalized score is averaged across all test windows.",
        fontsize=10,
        fontname="helv",
    )
    if eq_num_far_right:
        # Insert after unrelated block so MuPDF emits it as a separate right-margin block
        page.insert_text((505, eq_y), "(6)", fontsize=10, fontname="helv")
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def test_split_mixed_eq6_with_subscripts_and_stable_sibling_ids(tmp_path: Path) -> None:
    cfg = AppConfig(runtime_dir=tmp_path)
    adapter = NativePreservingAdapter(cfg)
    pdf_path = _create_mixed_eq6_pdf(tmp_path / "eq6_mixed.pdf", with_top_prose=True)
    doc = _make_doc(pdf_path)

    unsplit = adapter.parse_and_extract_blocks(doc, [0])
    assert len(unsplit) == 2
    mixed_parent = unsplit[0]
    unrelated_block = unsplit[1]
    assert "To measure sudden" in mixed_parent.source_text
    assert "Falling width" in mixed_parent.source_text
    assert "A point is identified" in mixed_parent.source_text
    assert "Eq. (5)" in mixed_parent.source_text

    # Drag only over the left/middle equation area, omitting `(6)` at x=328 slightly
    drag_bbox = [70.0, 241.0, 305.0, 257.0]
    preview = adapter.preview_block_split(doc, 0, drag_bbox)
    assert preview.valid, preview.rejection_reason
    assert preview.parent_block_id == mixed_parent.id
    assert preview.equation_number_text == "(6)"
    assert preview.equation_number_auto_included is True
    roles = [p.role for p in preview.parts]
    assert roles == ["top", "formula", "bottom"]
    assert "To measure sudden signal drops" in preview.parts[0].source_text
    assert "Falling width" in preview.parts[1].source_text
    assert "t0-10" in preview.parts[1].source_text
    assert "(6)" in preview.parts[1].source_text
    assert "A point is identified" in preview.parts[2].source_text
    assert "Eq. (5)" in preview.parts[2].source_text

    # Ensure zero vertical overlap between parts so typesetting redaction won't reject or clip
    top_box = preview.parts[0].bbox
    f_box = preview.parts[1].bbox
    bot_box = preview.parts[2].bbox
    assert top_box[3] < f_box[1]
    assert f_box[3] < bot_box[1]

    # Now apply persisted spec and re-extract blocks
    spec = BlockSplitSpec(
        id="split-1",
        document_id=doc.id,
        page_index=0,
        parent_block_id=mixed_parent.id,
        parent_bbox=mixed_parent.bbox,
        parent_source_hash=mixed_parent.source_text_hash,
        requested_bbox=drag_bbox,
        snapped_bbox=preview.snapped_bbox or f_box,
        include_equation_number=True,
    )
    updated_specs: list[BlockSplitSpec] = []
    split_blocks = adapter.parse_and_extract_blocks(
        doc, [0], split_specs=[spec], updated_split_specs_out=updated_specs
    )
    assert updated_specs[0].applied_status == "applied"
    assert len(split_blocks) == 4  # top + formula + bottom + unrelated_block

    top_b, f_b, bot_b, sib_b = split_blocks
    assert sib_b.id == unrelated_block.id  # Non-split ID did not shift!
    assert top_b.split_role == "top"
    assert top_b.status == BlockStatus.PENDING
    assert f_b.split_role == "formula"
    assert f_b.status == BlockStatus.PROTECTED_SKIP
    assert f_b.kind == BlockKind.ISOLATED_FORMULA
    assert "(6)" in f_b.source_text
    assert bot_b.split_role == "bottom"
    assert bot_b.status == BlockStatus.PENDING
    assert "Eq." in bot_b.source_text
    assert "{v1}" in (bot_b.masked_source_text or "")

    # Verify continuation pairing does not chain split siblings directly across formula
    attach_continuation_candidates(split_blocks)
    assert top_b.continuation_candidates == []


def test_split_separate_eq_number_block_absorption_toggle(tmp_path: Path) -> None:
    cfg = AppConfig(runtime_dir=tmp_path)
    adapter = NativePreservingAdapter(cfg)
    pdf_path = _create_mixed_eq6_pdf(
        tmp_path / "eq6_far_right.pdf",
        with_top_prose=False,
        eq_num_far_right=True,
    )
    doc = _make_doc(pdf_path)

    unsplit = adapter.parse_and_extract_blocks(doc, [0])
    assert any(b.source_text.strip() == "(6)" for b in unsplit), [b.source_text for b in unsplit]
    mixed = next(b for b in unsplit if "Falling width" in b.source_text)

    drag_bbox = [70.0, 228.0, 305.0, 245.0]
    prev_inc = adapter.preview_block_split(doc, 0, drag_bbox, include_equation_number=True)
    assert prev_inc.valid, prev_inc.rejection_reason
    assert prev_inc.equation_number_text == "(6)"
    assert prev_inc.equation_number_auto_included is True

    spec_inc = BlockSplitSpec(
        id="split-inc",
        document_id=doc.id,
        page_index=0,
        parent_block_id=mixed.id,
        parent_bbox=mixed.bbox,
        parent_source_hash=mixed.source_text_hash,
        requested_bbox=drag_bbox,
        snapped_bbox=prev_inc.snapped_bbox or [70, 228, 520, 245],
        include_equation_number=True,
    )
    res_inc = adapter.parse_and_extract_blocks(doc, [0], split_specs=[spec_inc])
    assert not any(b.source_text.strip() == "(6)" for b in res_inc)
    f_inc = next(b for b in res_inc if b.split_role == "formula")
    assert "(6)" in f_inc.source_text

    # When include_equation_number=False, standalone "(6)" remains separate
    spec_exc = spec_inc.model_copy(update={"id": "split-exc", "include_equation_number": False})
    res_exc = adapter.parse_and_extract_blocks(doc, [0], split_specs=[spec_exc])
    assert any(b.source_text.strip() == "(6)" for b in res_exc)


def test_split_stacked_fraction_with_bar_drawing(tmp_path: Path) -> None:
    cfg = AppConfig(runtime_dir=tmp_path)
    adapter = NativePreservingAdapter(cfg)
    pdf_path = tmp_path / "fraction_mixed.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=595, height=842)
    page.insert_text((72, 240), "Let the normalized estimator be defined by:", fontsize=10, fontname="helv")
    page.insert_text((72, 252), "f = N(x + y)", fontsize=9, fontname="helv")
    page.insert_text((72, 262), "    D(z)", fontsize=9, fontname="helv")
    page.insert_text((72, 275), "Here D(z) strictly exceeds zero everywhere.", fontsize=10, fontname="helv")
    page.draw_line((85, 255), (135, 255), width=0.8)
    doc_pdf.save(pdf_path)
    doc_pdf.close()

    doc = _make_doc(pdf_path)
    # Drag covers numerator + fraction bar; fraction bar groups denominator `D(z)` automatically
    prev = adapter.preview_block_split(doc, 0, [70.0, 244.0, 150.0, 256.0])
    assert prev.valid, prev.rejection_reason
    formula_part = next(p for p in prev.parts if p.role == "formula")
    assert "N(x + y)" in formula_part.source_text
    assert "D(z)" in formula_part.source_text
    assert "normalized estimator" not in formula_part.source_text
    assert "exceeds zero" not in formula_part.source_text


def test_split_rejection_cases(tmp_path: Path) -> None:
    cfg = AppConfig(runtime_dir=tmp_path)
    adapter = NativePreservingAdapter(cfg)
    pdf_path = tmp_path / "rejections.pdf"
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page(width=595, height=842)
    page.insert_text((60, 240), "Left paragraph value x = 5 is described in detail here.", fontsize=10, fontname="helv")
    page.insert_text((60, 254), "Another line sits immediately below the first line.", fontsize=10, fontname="helv")
    page.insert_text((340, 240), "Right column text block sits across the center gap.", fontsize=10, fontname="helv")
    page.insert_text((60, 360), "Single line standalone block.", fontsize=10, fontname="helv")
    doc_pdf.save(pdf_path)
    doc_pdf.close()

    doc = _make_doc(pdf_path)

    r_empty = adapter.preview_block_split(doc, 0, [50.0, 500.0, 150.0, 530.0])
    assert not r_empty.valid
    assert r_empty.rejection_code == "EMPTY_SELECTION"

    r_col = adapter.preview_block_split(doc, 0, [55.0, 230.0, 420.0, 250.0])
    assert not r_col.valid
    assert r_col.rejection_code == "MULTI_COLUMN_SPAN"

    r_single = adapter.preview_block_split(doc, 0, [55.0, 350.0, 230.0, 366.0])
    assert not r_single.valid
    assert r_single.rejection_code == "NO_SPLIT_NEEDED"

    r_inline = adapter.preview_block_split(doc, 0, [145.0, 230.0, 185.0, 244.0])
    assert not r_inline.valid
    assert r_inline.rejection_code == "AMBIGUOUS_LINE_BOUNDARY"


def test_split_formula_priority_and_pending_states(tmp_path: Path) -> None:
    cfg = AppConfig(runtime_dir=tmp_path)
    adapter = NativePreservingAdapter(cfg)
    pdf_path = _create_mixed_eq6_pdf(tmp_path / "prio.pdf", with_top_prose=True)
    doc = _make_doc(pdf_path)
    unsplit = adapter.parse_and_extract_blocks(doc, [0])
    mixed = unsplit[0]
    drag_bbox = [70.0, 241.0, 305.0, 257.0]
    prev = adapter.preview_block_split(doc, 0, drag_bbox)
    assert prev.valid, prev.rejection_reason
    top_box = next(p.bbox for p in prev.parts if p.role == "top")
    f_box = next(p.bbox for p in prev.parts if p.role == "formula")

    spec = BlockSplitSpec(
        id="split-prio",
        document_id=doc.id,
        page_index=0,
        parent_block_id=mixed.id,
        parent_bbox=mixed.bbox,
        parent_source_hash=mixed.source_text_hash,
        requested_bbox=drag_bbox,
        snapped_bbox=f_box,
    )
    regions = [
        ProtectionRegion(
            id="reg-trans-body",
            document_id=doc.id,
            page_index=0,
            bbox=f_box,
            mode=ProtectionMode.TRANSLATE_AS_BODY,
        ),
        ProtectionRegion(
            id="reg-prot-top",
            document_id=doc.id,
            page_index=0,
            bbox=top_box,
            mode=ProtectionMode.DO_NOT_TRANSLATE,
        ),
    ]
    blocks = adapter.parse_and_extract_blocks(doc, [0], manual_regions=regions, split_specs=[spec])
    top_b = next(b for b in blocks if b.split_role == "top")
    f_b = next(b for b in blocks if b.split_role == "formula")
    bot_b = next(b for b in blocks if b.split_role == "bottom")
    assert f_b.status == BlockStatus.PROTECTED_SKIP
    assert top_b.status == BlockStatus.PROTECTED_SKIP
    assert bot_b.status == BlockStatus.PENDING

    changed_spec = spec.model_copy(update={"id": "split-changed", "parent_source_hash": "deadbeef" * 8})
    missing_spec = spec.model_copy(update={"id": "split-missing", "parent_bbox": [10.0, 700.0, 100.0, 720.0]})
    updates: list[BlockSplitSpec] = []
    adapter.parse_and_extract_blocks(doc, [0], split_specs=[changed_spec, missing_spec], updated_split_specs_out=updates)
    status_by_id = {u.id: u.applied_status for u in updates}
    assert status_by_id["split-changed"] == "pending_changed"
    assert status_by_id["split-missing"] == "pending_missing"


def test_db_manual_clear_consistency_and_atomic_rollback(tmp_path: Path) -> None:
    db = DatabaseRepository(tmp_path / "split_test.db")
    pdf_path = _create_mixed_eq6_pdf(tmp_path / "db.pdf")
    doc = _make_doc(pdf_path, doc_id="doc-db-1")
    db.save_document(doc)

    b1 = Block(
        id="b1",
        page_index=0,
        bbox=[70.0, 100.0, 300.0, 130.0],
        source_text="Original block",
        manual_translation_text="手動訳文",
        status=BlockStatus.MANUALLY_EDITED,
    )
    db.save_blocks(doc.id, [b1], replace_all_for_pages=True)
    b1_none = b1.model_copy(update={"manual_translation_text": None, "status": BlockStatus.PENDING})
    db.save_blocks(doc.id, [b1_none], allow_clear_manual_translation=False)
    loaded = db.list_blocks(doc.id)[0]
    assert loaded.manual_translation_text == "手動訳文"

    db.save_blocks(doc.id, [b1_none], clear_manual_translation_ids={"b1"})
    cleared = db.list_blocks(doc.id)[0]
    assert cleared.manual_translation_text is None
    with db._connect() as conn:
        row = conn.execute("SELECT manual_translation_text FROM blocks WHERE id = 'b1'").fetchone()
        assert row["manual_translation_text"] is None

    spec = BlockSplitSpec(
        id="sp-rollback",
        document_id=doc.id,
        page_index=0,
        parent_block_id="b1",
        parent_bbox=b1.bbox,
        parent_source_hash=b1.source_text_hash,
        requested_bbox=[70.0, 105.0, 290.0, 120.0],
        snapped_bbox=[70.0, 105.0, 290.0, 120.0],
    )
    child = b1.model_copy(update={"id": "b1:split-sp-rollback:formula", "split_spec_id": "sp-rollback"})
    db.save_validation_issues(
        [
            ValidationIssue(
                id="iss-1",
                job_id="job-1",
                document_id=doc.id,
                page_index=0,
                block_id="b1",
                code="TEST_WARN",
                severity=IssueSeverity.WARNING,
                message="warn",
            )
        ]
    )
    with pytest.raises(RuntimeError):
        db.commit_split_transaction(
            document_id=doc.id,
            blocks_to_save=[child],
            replace_page_indices={0},
            upsert_splits=[spec],
            fail_stage_for_test="before_commit",
        )
    assert db.list_block_splits(doc.id) == []
    assert [b.id for b in db.list_blocks(doc.id)] == ["b1"]
    assert db.get_document(doc.id).parse_revision == 0
    assert len(db.list_validation_issues("job-1")) == 1
