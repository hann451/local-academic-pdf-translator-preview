"""PDF preflight inspection, text extractability check, and immutability verification.

Covers Section 6 (Stages 1-3) and Section 16 (File security):
- Never uses uploaded filename directly as storage path.
- Prevents path traversal.
- Detects encryption without attempting unauthorized password/DRM bypass.
- Detects scanned/image-only PDFs and reports clear reason without silent OCR fallback.
- Computes and verifies SHA-256 hash so original PDF is guaranteed untouched.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

import pymupdf

from backend.domain.models import Document, Page


def compute_sha256(file_path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file without modifying it."""
    p = Path(file_path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_original_unchanged(file_path: str | Path, expected_sha256: str) -> bool:
    """Verify that the original PDF file's SHA-256 matches the initial hash."""
    actual = compute_sha256(file_path)
    return actual.lower() == expected_sha256.lower()


def resolve_safe_storage_path(
    base_dir: str | Path,
    document_id: str,
    suffix: str = ".pdf",
) -> Path:
    """Create a path-traversal-safe storage path using only a validated document_id.

    Never embeds raw user-uploaded filenames into filesystem paths (Section 16).
    """
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    # Allow only alphanumeric, hyphen, underscore in document_id
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", document_id)
    if not safe_id or safe_id != document_id:
        raise ValueError(f"Unsafe document_id rejected: {document_id!r}")

    if not suffix.startswith("."):
        suffix = "." + suffix
    candidate = (base / f"{safe_id}{suffix}").resolve()
    if base not in candidate.parents:
        raise ValueError("Path traversal attempt detected")
    return candidate


def inspect_pdf_preflight(
    pdf_path: str | Path,
    original_filename: str | None = None,
    document_id: str | None = None,
    max_file_mb: int = 100,
    max_pages: int = 300,
) -> Document:
    """Execute Stages 1-3: File validation, page metadata extraction, and text extractability check."""
    p = Path(pdf_path).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"PDF file not found: {p}")

    doc_id = document_id or f"doc-{uuid.uuid4().hex[:12]}"
    orig_name = original_filename or p.name
    file_size = p.stat().st_size
    sha256 = compute_sha256(p)

    if file_size == 0:
        return Document(
            id=doc_id,
            original_filename=orig_name,
            safe_storage_path=str(p),
            sha256_hash=sha256,
            file_size_bytes=0,
            page_count=0,
            is_corrupted=True,
            extractable_Sync_status="error",
            unsupported_reason="ファイルが空（0バイト）です。",
        )

    if file_size > max_file_mb * 1024 * 1024:
        return Document(
            id=doc_id,
            original_filename=orig_name,
            safe_storage_path=str(p),
            sha256_hash=sha256,
            file_size_bytes=file_size,
            page_count=0,
            extractable_Sync_status="unsupported",
            unsupported_reason=f"ファイル容量が安全上限（{max_file_mb} MB）を超えています。",
        )

    # Check PDF magic header within first 1024 bytes
    with p.open("rb") as f:
        header = f.read(1024)
    if b"%PDF-" not in header:
        return Document(
            id=doc_id,
            original_filename=orig_name,
            safe_storage_path=str(p),
            sha256_hash=sha256,
            file_size_bytes=file_size,
            page_count=0,
            is_corrupted=True,
            extractable_Sync_status="error",
            unsupported_reason="有効なPDFヘッダー（%PDF-）が見つかりません。破損ファイルの可能性があります。",
        )

    try:
        mupdf_doc = pymupdf.open(str(p))
    except Exception as exc:
        return Document(
            id=doc_id,
            original_filename=orig_name,
            safe_storage_path=str(p),
            sha256_hash=sha256,
            file_size_bytes=file_size,
            page_count=0,
            is_corrupted=True,
            extractable_Sync_status="error",
            unsupported_reason=f"PDF構文の破損により開けません: {type(exc).__name__}",
        )

    try:
        # Check encryption without attempting to crack or bypass it (Section 13.B)
        if mupdf_doc.needs_pass or mupdf_doc.is_encrypted:
            return Document(
                id=doc_id,
                original_filename=orig_name,
                safe_storage_path=str(p),
                sha256_hash=sha256,
                file_size_bytes=file_size,
                page_count=0,
                is_encrypted=True,
                extractable_Sync_status="unsupported",
                unsupported_reason="暗号化（パスワード保護）されたPDFです。無断で保護解除は行いません。",
            )

        page_count = len(mupdf_doc)
        if page_count == 0:
            return Document(
                id=doc_id,
                original_filename=orig_name,
                safe_storage_path=str(p),
                sha256_hash=sha256,
                file_size_bytes=file_size,
                page_count=0,
                is_corrupted=True,
                extractable_Sync_status="error",
                unsupported_reason="PDFにページが含まれていません。",
            )

        if page_count > max_pages:
            return Document(
                id=doc_id,
                original_filename=orig_name,
                safe_storage_path=str(p),
                sha256_hash=sha256,
                file_size_bytes=file_size,
                page_count=page_count,
                extractable_Sync_status="unsupported",
                unsupported_reason=f"ページ数（{page_count}頁）が安全上限（{max_pages}頁）を超えています。ページ範囲指定をご利用ください。",
            )

        pages_meta: list[Page] = []
        scanned_page_count = 0

        for idx in range(page_count):
            mpage = mupdf_doc[idx]
            rect = mpage.rect
            mb = mpage.mediabox
            cb = mpage.cropbox
            rot = int(mpage.rotation) % 360

            raw_text = mpage.get_text("text").strip()
            images = mpage.get_images(full=True)
            drawings = mpage.get_drawings()

            # Check CID / unmapped character ratio
            total_chars = len(raw_text)
            cid_matches = len(re.findall(r"\(cid:\d+\)|\ufffd", raw_text))
            cid_ratio = (cid_matches / total_chars) if total_chars > 0 else 0.0

            has_extractable = total_chars >= 10 and cid_ratio < 0.5
            # Scanned-like if almost no extractable text (<10 chars) and has embedded raster image or drawing
            is_scanned_like = (total_chars < 10) and (len(images) > 0 or len(drawings) == 0)
            if is_scanned_like:
                scanned_page_count += 1

            # Estimate column count (1 vs 2 columns) from text blocks (in unrotated CropBox space)
            blocks = mpage.get_text("blocks")
            col_count = _estimate_column_count(blocks, float(cb.width))

            pages_meta.append(
                Page(
                    document_id=doc_id,
                    page_index=idx,
                    width_pt=float(cb.width),
                    height_pt=float(cb.height),
                    rotation_deg=rot,
                    media_box=[float(mb.x0), float(mb.y0), float(mb.x1), float(mb.y1)],
                    crop_box=[float(cb.x0), float(cb.y0), float(cb.x1), float(cb.y1)],
                    has_extractable_text=has_extractable,
                    is_scanned_like=is_scanned_like,
                    cid_ratio=min(1.0, cid_ratio),
                    column_count=col_count,
                )
            )

        is_mostly_scanned = scanned_page_count == page_count or (
            page_count > 1 and scanned_page_count / page_count >= 0.8
        )
        high_cid_pages = [p.display_page_number for p in pages_meta if p.cid_ratio >= 0.5]

        unsupported_reason: str | None = None
        status = "ok"
        if is_mostly_scanned:
            status = "unsupported_scanned"
            unsupported_reason = (
                "文字選択できるテキストがほとんど含まれないスキャン相当PDFです。"
                "初期対象外のため、無断でOCRやページ画像化へは切り替えません。"
            )
        elif high_cid_pages:
            status = "unsupported_cid_encoding"
            unsupported_reason = (
                f"フォントのCMap欠落によりテキスト抽出時に文字化け（CID未定義）が多発しています（該当ページ: {high_cid_pages}）。"
            )

        return Document(
            id=doc_id,
            original_filename=orig_name,
            safe_storage_path=str(p),
            sha256_hash=sha256,
            file_size_bytes=file_size,
            page_count=page_count,
            is_encrypted=False,
            is_corrupted=False,
            is_scanned_pdf=is_mostly_scanned,
            extractable_Sync_status=status,
            unsupported_reason=unsupported_reason,
            pages=pages_meta,
        )
    finally:
        mupdf_doc.close()


def _estimate_column_count(text_blocks: list, page_width_pt: float) -> int:
    """Detect whether a page uses 1-column or 2-column layout based on text block x-spans."""
    if page_width_pt <= 0:
        return 1
    mid_x = page_width_pt / 2.0
    left_blocks = 0
    right_blocks = 0

    for b in text_blocks:
        if len(b) < 7 or b[6] != 0:  # b[6] == 0 is text block
            continue
        x0, y0, x1, y1, text = float(b[0]), float(b[1]), float(b[2]), float(b[3]), str(b[4]).strip()
        if len(text) < 15:
            continue
        width = x1 - x0
        if width < page_width_pt * 0.48:
            if x1 <= mid_x + 15.0:
                left_blocks += 1
            elif x0 >= mid_x - 15.0:
                right_blocks += 1

    if left_blocks >= 1 and right_blocks >= 1:
        return 2
    return 1
