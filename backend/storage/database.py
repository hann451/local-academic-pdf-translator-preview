"""SQLite persistence layer for PaperBridge Local (Section 14 & 15).

Stores Documents, Pages, Blocks, ProtectionRegions, Glossaries, Jobs,
ValidationIssues, ExportArtifacts, and TranslationCache.
Separates model translation output from user manual edits so neither overwrites the other.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from backend.domain.models import (
    Block,
    BlockStatus,
    Document,
    ExportArtifact,
    Glossary,
    GlossaryEntry,
    Job,
    ProtectionRegion,
    ValidationIssue,
    utc_now_iso,
)
from backend.translation.validation.validator import validate_translation_output


class DatabaseRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS blocks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    reading_order INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    translation_text TEXT,
                    manual_translation_text TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_blocks_doc_page ON blocks(document_id, page_index);

                CREATE TABLE IF NOT EXISTS protection_regions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_prot_doc_page ON protection_regions(document_id, page_index);

                CREATE TABLE IF NOT EXISTS glossaries (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS validation_issues (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_issues_job ON validation_issues(job_id);

                CREATE TABLE IF NOT EXISTS export_artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    model_output_text TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    quantization TEXT NOT NULL,
                    is_mock INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )

    # --- Document operations ---
    def save_document(self, doc: Document) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (id, payload_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (doc.id, doc.model_dump_json(), doc.created_at),
            )

    def get_document(self, document_id: str) -> Document | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not row:
                return None
            return Document.model_validate_json(row["payload_json"])

    # --- Block operations (preserving separation of model vs manual translation) ---
    def save_blocks(
        self,
        document_id: str,
        blocks: list[Block],
        replace_all_for_pages: bool = False,
    ) -> None:
        with self._lock, self._connect() as conn:
            if replace_all_for_pages and blocks:
                pages = sorted({b.page_index for b in blocks})
                keep_ids = {b.id for b in blocks}
                for p_idx in pages:
                    rows = conn.execute(
                        "SELECT id FROM blocks WHERE document_id = ? AND page_index = ?",
                        (document_id, p_idx),
                    ).fetchall()
                    for r in rows:
                        if r["id"] not in keep_ids:
                            conn.execute(
                                "DELETE FROM blocks WHERE id = ? AND document_id = ?",
                                (r["id"], document_id),
                            )
            for b in blocks:
                conn.execute(
                    """
                    INSERT INTO blocks (
                        id, document_id, page_index, reading_order, status,
                        translation_text, manual_translation_text, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        page_index = excluded.page_index,
                        reading_order = excluded.reading_order,
                        status = excluded.status,
                        translation_text = excluded.translation_text,
                        manual_translation_text = COALESCE(excluded.manual_translation_text, blocks.manual_translation_text),
                        payload_json = excluded.payload_json
                    """,
                    (
                        b.id,
                        document_id,
                        b.page_index,
                        b.reading_order,
                        b.status.value,
                        b.translation_text,
                        b.manual_translation_text,
                        b.model_dump_json(),
                    ),
                )

    def update_block_manual_translation(
        self,
        document_id: str,
        block_id: str,
        manual_text: str | None,
        expected_edit_revision: int | None = None,
    ) -> Block:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM blocks WHERE id = ? AND document_id = ?",
                (block_id, document_id),
            ).fetchone()
            if not row:
                raise KeyError(f"Block not found: {block_id}")
            block = Block.model_validate_json(row["payload_json"])
            if (
                expected_edit_revision is not None
                and block.edit_revision != expected_edit_revision
            ):
                raise ValueError(
                    f"RevisionConflict: block '{block_id}' expected edit_revision={expected_edit_revision}, actual={block.edit_revision}"
                )

            block.manual_translation_text = manual_text
            block.edit_revision += 1
            if manual_text is not None:
                block.status = BlockStatus.MANUALLY_EDITED
                val_res = validate_translation_output(
                    source_text=block.source_text,
                    masked_source_text=block.masked_source_text or block.source_text,
                    translated_text=manual_text,
                    protected_items=block.protected_items,
                    finish_reason="stop",
                )
                block.manual_validation_errors = list(val_res.error_codes)
                block.manual_validation_warnings = list(val_res.warning_codes)
            else:
                block.manual_validation_errors = []
                block.manual_validation_warnings = []
                if block.translation_text is not None:
                    block.status = BlockStatus.TRANSLATED

            conn.execute(
                """
                UPDATE blocks
                SET status = ?, manual_translation_text = ?, payload_json = ?
                WHERE id = ? AND document_id = ?
                """,
                (
                    block.status.value,
                    block.manual_translation_text,
                    block.model_dump_json(),
                    block_id,
                    document_id,
                ),
            )
            doc_row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if doc_row:
                doc = Document.model_validate_json(doc_row["payload_json"])
                doc.edit_revision += 1
                conn.execute(
                    "UPDATE documents SET payload_json = ? WHERE id = ?",
                    (doc.model_dump_json(), document_id),
                )
            return block

    def list_blocks(self, document_id: str, page_index: int | None = None) -> list[Block]:
        with self._lock, self._connect() as conn:
            if page_index is None:
                rows = conn.execute(
                    "SELECT payload_json FROM blocks WHERE document_id = ? ORDER BY page_index ASC, reading_order ASC",
                    (document_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM blocks WHERE document_id = ? AND page_index = ? ORDER BY reading_order ASC",
                    (document_id, page_index),
                ).fetchall()
            return [Block.model_validate_json(r["payload_json"]) for r in rows]

    # --- ProtectionRegion operations ---
    def save_protection_region(self, region: ProtectionRegion) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO protection_regions (id, document_id, page_index, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    page_index = excluded.page_index,
                    payload_json = excluded.payload_json
                """,
                (region.id, region.document_id, region.page_index, region.model_dump_json()),
            )
            doc_row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (region.document_id,)
            ).fetchone()
            if doc_row:
                doc = Document.model_validate_json(doc_row["payload_json"])
                doc.protection_revision += 1
                conn.execute(
                    "UPDATE documents SET payload_json = ? WHERE id = ?",
                    (doc.model_dump_json(), region.document_id),
                )

    def delete_protection_region(self, document_id: str, region_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM protection_regions WHERE id = ? AND document_id = ?",
                (region_id, document_id),
            )
            deleted = cur.rowcount > 0
            if deleted:
                doc_row = conn.execute(
                    "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
                ).fetchone()
                if doc_row:
                    doc = Document.model_validate_json(doc_row["payload_json"])
                    doc.protection_revision += 1
                    conn.execute(
                        "UPDATE documents SET payload_json = ? WHERE id = ?",
                        (doc.model_dump_json(), document_id),
                    )
            return deleted

    def list_protection_regions(
        self, document_id: str, page_index: int | None = None
    ) -> list[ProtectionRegion]:
        with self._lock, self._connect() as conn:
            if page_index is None:
                rows = conn.execute(
                    "SELECT payload_json FROM protection_regions WHERE document_id = ? ORDER BY page_index ASC, id ASC",
                    (document_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM protection_regions WHERE document_id = ? AND page_index = ? ORDER BY id ASC",
                    (document_id, page_index),
                ).fetchall()
            return [ProtectionRegion.model_validate_json(r["payload_json"]) for r in rows]

    def clear_protection_regions_for_document(self, document_id: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM protection_regions WHERE document_id = ?",
                (document_id,),
            )
            deleted = cur.rowcount
            if deleted > 0:
                doc_row = conn.execute(
                    "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
                ).fetchone()
                if doc_row:
                    doc = Document.model_validate_json(doc_row["payload_json"])
                    doc.protection_revision += 1
                    conn.execute(
                        "UPDATE documents SET payload_json = ? WHERE id = ?",
                        (doc.model_dump_json(), document_id),
                    )
            return deleted

    def increment_protection_revision(self, document_id: str) -> int:
        with self._lock, self._connect() as conn:
            doc_row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not doc_row:
                raise KeyError(f"Document not found: {document_id}")
            doc = Document.model_validate_json(doc_row["payload_json"])
            doc.protection_revision += 1
            conn.execute(
                "UPDATE documents SET payload_json = ? WHERE id = ?",
                (doc.model_dump_json(), document_id),
            )
            return doc.protection_revision

    # --- Glossary operations ---
    def save_glossary(self, glossary: Glossary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO glossaries (id, name, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    payload_json = excluded.payload_json
                """,
                (glossary.id, glossary.name, glossary.model_dump_json()),
            )

    def get_glossary(self, glossary_id: str) -> Glossary | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM glossaries WHERE id = ?", (glossary_id,)
            ).fetchone()
            return Glossary.model_validate_json(row["payload_json"]) if row else None

    def list_glossaries(self) -> list[Glossary]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM glossaries ORDER BY name ASC, id ASC"
            ).fetchall()
            return [Glossary.model_validate_json(r["payload_json"]) for r in rows]

    def update_glossary(
        self, glossary_id: str, name: str, entries: list[GlossaryEntry]
    ) -> Glossary:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM glossaries WHERE id = ?", (glossary_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Glossary not found: {glossary_id}")
            glossary = Glossary.model_validate_json(row["payload_json"])
            glossary.name = name
            glossary.entries = entries
            glossary.version += 1
            glossary.updated_at = utc_now_iso()
            conn.execute(
                "UPDATE glossaries SET name = ?, payload_json = ? WHERE id = ?",
                (glossary.name, glossary.model_dump_json(), glossary_id),
            )
            return glossary

    def delete_glossary(self, glossary_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM glossaries WHERE id = ?", (glossary_id,))
            return cur.rowcount > 0

    # --- Job operations ---
    def save_job(self, job: Job) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, document_id, state, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (job.id, job.document_id, job.state.value, job.model_dump_json(), job.updated_at),
            )

    def get_job(self, job_id: str) -> Job | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return Job.model_validate_json(row["payload_json"]) if row else None

    # --- ValidationIssue operations ---
    def save_validation_issues(self, issues: list[ValidationIssue]) -> None:
        with self._lock, self._connect() as conn:
            for iss in issues:
                conn.execute(
                    """
                    INSERT INTO validation_issues (id, job_id, document_id, page_index, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json
                    """,
                    (iss.id, iss.job_id, iss.document_id, iss.page_index, iss.model_dump_json()),
                )

    def replace_validation_issues_for_job(self, job_id: str, issues: list[ValidationIssue]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM validation_issues WHERE job_id = ?", (job_id,))
            for iss in issues:
                conn.execute(
                    """
                    INSERT INTO validation_issues (id, job_id, document_id, page_index, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (iss.id, iss.job_id, iss.document_id, iss.page_index, iss.model_dump_json()),
                )

    def list_validation_issues(self, job_id: str) -> list[ValidationIssue]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM validation_issues WHERE job_id = ? ORDER BY page_index ASC, id ASC",
                (job_id,),
            ).fetchall()
            return [ValidationIssue.model_validate_json(r["payload_json"]) for r in rows]

    # --- ExportArtifact operations ---
    def save_export_artifact(self, artifact: ExportArtifact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO export_artifacts (id, job_id, document_id, artifact_type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (
                    artifact.id,
                    artifact.job_id,
                    artifact.document_id,
                    artifact.artifact_type,
                    artifact.model_dump_json(),
                ),
            )

    def replace_export_artifacts_for_job(
        self, job_id: str, artifacts: list[ExportArtifact]
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM export_artifacts WHERE job_id = ?", (job_id,))
            for artifact in artifacts:
                conn.execute(
                    """
                    INSERT INTO export_artifacts (id, job_id, document_id, artifact_type, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.job_id,
                        artifact.document_id,
                        artifact.artifact_type,
                        artifact.model_dump_json(),
                    ),
                )

    def list_export_artifacts(self, job_id: str) -> list[ExportArtifact]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM export_artifacts WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            return [ExportArtifact.model_validate_json(r["payload_json"]) for r in rows]

    # --- Translation Cache operations ---
    def get_cached_translation(self, cache_key: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM translation_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if not row:
                return None
            return {
                "cache_key": row["cache_key"],
                "model_output_text": row["model_output_text"],
                "model_name": row["model_name"],
                "quantization": row["quantization"],
                "is_mock": bool(row["is_mock"]),
                "created_at": row["created_at"],
            }

    def put_cached_translation(
        self,
        cache_key: str,
        model_output_text: str,
        model_name: str,
        quantization: str,
        is_mock: bool = False,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO translation_cache (
                    cache_key, model_output_text, model_name, quantization, is_mock, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model_output_text = excluded.model_output_text,
                    model_name = excluded.model_name,
                    quantization = excluded.quantization,
                    is_mock = excluded.is_mock,
                    created_at = excluded.created_at
                """,
                (cache_key, model_output_text, model_name, quantization, int(is_mock), utc_now_iso()),
            )
