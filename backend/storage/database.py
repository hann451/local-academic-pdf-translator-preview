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
from typing import Any

from backend.domain.models import (
    Block,
    BlockSplitAppliedStatus,
    BlockSplitSpec,
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
                    model_id TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL,
                    model_hash TEXT NOT NULL DEFAULT '',
                    quantization TEXT NOT NULL,
                    is_mock INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS block_splits (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_splits_doc_page ON block_splits(document_id, page_index);
                """
            )
            existing_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(translation_cache)").fetchall()
            }
            if "model_id" not in existing_cols:
                conn.execute(
                    "ALTER TABLE translation_cache ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"
                )
            if "model_hash" not in existing_cols:
                conn.execute(
                    "ALTER TABLE translation_cache ADD COLUMN model_hash TEXT NOT NULL DEFAULT ''"
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
    def _save_blocks_conn(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        blocks: list[Block],
        *,
        replace_all_for_pages: bool = False,
        replace_page_indices: set[int] | None = None,
        allow_clear_manual_translation: bool = False,
        clear_manual_translation_ids: set[str] | None = None,
    ) -> None:
        pages_to_replace: set[int] = set(replace_page_indices or ())
        if replace_all_for_pages and blocks:
            pages_to_replace.update(b.page_index for b in blocks)
        if pages_to_replace:
            keep_ids = {b.id for b in blocks}
            for p_idx in sorted(pages_to_replace):
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

        clear_ids = clear_manual_translation_ids or set()
        for b in blocks:
            b_to_save = b
            explicitly_cleared = allow_clear_manual_translation or (b.id in clear_ids)
            if explicitly_cleared and b.id in clear_ids and b.manual_translation_text is not None:
                b_to_save = b.model_copy(
                    update={
                        "manual_translation_text": None,
                        "manual_validation_errors": [],
                        "manual_validation_warnings": [],
                    }
                )
            elif not explicitly_cleared and b.manual_translation_text is None:
                existing_row = conn.execute(
                    "SELECT manual_translation_text, payload_json FROM blocks WHERE id = ? AND document_id = ?",
                    (b.id, document_id),
                ).fetchone()
                if existing_row and existing_row["manual_translation_text"] is not None:
                    existing_manual = existing_row["manual_translation_text"]
                    existing_payload = Block.model_validate_json(existing_row["payload_json"])
                    b_to_save = b.model_copy(
                        update={
                            "manual_translation_text": existing_manual,
                            "manual_validation_errors": list(existing_payload.manual_validation_errors),
                            "manual_validation_warnings": list(existing_payload.manual_validation_warnings),
                        }
                    )

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
                    manual_translation_text = excluded.manual_translation_text,
                    payload_json = excluded.payload_json
                """,
                (
                    b_to_save.id,
                    document_id,
                    b_to_save.page_index,
                    b_to_save.reading_order,
                    b_to_save.status.value,
                    b_to_save.translation_text,
                    b_to_save.manual_translation_text,
                    b_to_save.model_dump_json(),
                ),
            )

    def save_blocks(
        self,
        document_id: str,
        blocks: list[Block],
        replace_all_for_pages: bool = False,
        replace_page_indices: set[int] | None = None,
        allow_clear_manual_translation: bool = False,
        clear_manual_translation_ids: set[str] | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            self._save_blocks_conn(
                conn,
                document_id,
                blocks,
                replace_all_for_pages=replace_all_for_pages,
                replace_page_indices=replace_page_indices,
                allow_clear_manual_translation=allow_clear_manual_translation,
                clear_manual_translation_ids=clear_manual_translation_ids,
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
                block.needs_retranslation = False
                block.status = BlockStatus.MANUALLY_EDITED
                block.translation_provenance = "manual_edit"
                val_res = validate_translation_output(
                    source_text=block.source_text,
                    masked_source_text=block.masked_source_text or block.source_text,
                    translated_text=manual_text,
                    protected_items=block.protected_items,
                    finish_reason="stop",
                    unmasked_math_variables=block.unmasked_math_variables,
                )
                block.manual_validation_errors = list(val_res.error_codes)
                block.manual_validation_warnings = list(val_res.warning_codes)
            else:
                block.manual_validation_errors = []
                block.manual_validation_warnings = []
                if block.translation_text is not None:
                    block.status = BlockStatus.TRANSLATED
                    if block.translation_provenance == "manual_edit":
                        block.translation_provenance = "llm_inference"

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

    def increment_parse_revision(self, document_id: str) -> int:
        with self._lock, self._connect() as conn:
            doc_row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not doc_row:
                raise KeyError(f"Document not found: {document_id}")
            doc = Document.model_validate_json(doc_row["payload_json"])
            doc.parse_revision += 1
            conn.execute(
                "UPDATE documents SET payload_json = ? WHERE id = ?",
                (doc.model_dump_json(), document_id),
            )
            return doc.parse_revision

    # --- BlockSplitSpec operations ---
    def save_block_split(self, spec: BlockSplitSpec, bump_protection_revision: bool = False) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO block_splits (id, document_id, page_index, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    page_index = excluded.page_index,
                    payload_json = excluded.payload_json
                """,
                (spec.id, spec.document_id, spec.page_index, spec.model_dump_json()),
            )
            if bump_protection_revision:
                doc_row = conn.execute(
                    "SELECT payload_json FROM documents WHERE id = ?", (spec.document_id,)
                ).fetchone()
                if doc_row:
                    doc = Document.model_validate_json(doc_row["payload_json"])
                    doc.protection_revision += 1
                    conn.execute(
                        "UPDATE documents SET payload_json = ? WHERE id = ?",
                        (doc.model_dump_json(), spec.document_id),
                    )

    def get_block_split(self, document_id: str, split_id: str) -> BlockSplitSpec | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM block_splits WHERE id = ? AND document_id = ?",
                (split_id, document_id),
            ).fetchone()
            return BlockSplitSpec.model_validate_json(row["payload_json"]) if row else None

    def list_block_splits(
        self, document_id: str, page_index: int | None = None
    ) -> list[BlockSplitSpec]:
        with self._lock, self._connect() as conn:
            if page_index is None:
                rows = conn.execute(
                    "SELECT payload_json FROM block_splits WHERE document_id = ? ORDER BY page_index ASC, id ASC",
                    (document_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM block_splits WHERE document_id = ? AND page_index = ? ORDER BY id ASC",
                    (document_id, page_index),
                ).fetchall()
            specs = [BlockSplitSpec.model_validate_json(r["payload_json"]) for r in rows]
            specs.sort(key=lambda s: (s.page_index, s.created_at, s.id))
            return specs

    def update_block_split_statuses(self, document_id: str, specs: list[BlockSplitSpec]) -> None:
        if not specs:
            return
        with self._lock, self._connect() as conn:
            for sp in specs:
                conn.execute(
                    """
                    UPDATE block_splits
                    SET page_index = ?, payload_json = ?
                    WHERE id = ? AND document_id = ?
                    """,
                    (sp.page_index, sp.model_dump_json(), sp.id, document_id),
                )

    def delete_block_split(self, document_id: str, split_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM block_splits WHERE id = ? AND document_id = ?",
                (split_id, document_id),
            )
            return cur.rowcount > 0

    def list_jobs_for_document(self, document_id: str) -> list[Job]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM jobs WHERE document_id = ? ORDER BY updated_at DESC",
                (document_id,),
            ).fetchall()
            return [Job.model_validate_json(r["payload_json"]) for r in rows]

    def _prune_validation_issues_conn(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        touched_pages: set[int],
        surviving_ids_by_page: dict[int, set[str]],
        invalidated_block_ids: set[str] | None = None,
    ) -> int:
        if not touched_pages:
            return 0
        invalid_ids = invalidated_block_ids or set()
        rows = conn.execute(
            "SELECT id, page_index, payload_json FROM validation_issues WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        pruned = 0
        for r in rows:
            p_idx = int(r["page_index"])
            if p_idx not in touched_pages:
                continue
            issue = ValidationIssue.model_validate_json(r["payload_json"])
            blk_id = issue.block_id
            if blk_id is not None and (
                blk_id not in surviving_ids_by_page.get(p_idx, set())
                or blk_id in invalid_ids
            ):
                conn.execute("DELETE FROM validation_issues WHERE id = ?", (r["id"],))
                pruned += 1
        return pruned

    def commit_split_transaction(
        self,
        *,
        document_id: str,
        blocks_to_save: list[Block],
        replace_page_indices: set[int],
        clear_manual_translation_ids: set[str] | None = None,
        allow_clear_manual_translation: bool = True,
        upsert_splits: list[BlockSplitSpec] | None = None,
        delete_split_id: str | None = None,
        expected_protection_revision: int | None = None,
        expected_parse_revision: int | None = None,
        bump_protection_revision: bool = True,
        bump_parse_revision: bool = True,
        stale_reason: str | None = "split_changed",
        invalidated_block_ids: set[str] | None = None,
        fail_stage_for_test: str | None = None,
    ) -> Document:
        """Atomically persist split specs, replaced blocks, revisions, job staleness, and issue pruning."""
        with self._lock, self._connect() as conn:
            doc_row = conn.execute(
                "SELECT payload_json FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not doc_row:
                raise KeyError(f"Document not found: {document_id}")
            doc = Document.model_validate_json(doc_row["payload_json"])
            if (
                expected_protection_revision is not None
                and doc.protection_revision != expected_protection_revision
            ):
                raise ValueError(
                    f"RevisionConflict: expected protection_revision={expected_protection_revision}, actual={doc.protection_revision}"
                )
            if (
                expected_parse_revision is not None
                and doc.parse_revision != expected_parse_revision
            ):
                raise ValueError(
                    f"RevisionConflict: expected parse_revision={expected_parse_revision}, actual={doc.parse_revision}"
                )

            if delete_split_id is not None:
                cur = conn.execute(
                    "DELETE FROM block_splits WHERE id = ? AND document_id = ?",
                    (delete_split_id, document_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"BlockSplitSpec not found: {delete_split_id}")

            for sp in upsert_splits or []:
                conn.execute(
                    """
                    INSERT INTO block_splits (id, document_id, page_index, payload_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        page_index = excluded.page_index,
                        payload_json = excluded.payload_json
                    """,
                    (sp.id, sp.document_id, sp.page_index, sp.model_dump_json()),
                )

            self._save_blocks_conn(
                conn,
                document_id,
                blocks_to_save,
                replace_all_for_pages=True,
                replace_page_indices=replace_page_indices,
                allow_clear_manual_translation=allow_clear_manual_translation,
                clear_manual_translation_ids=clear_manual_translation_ids,
            )

            if fail_stage_for_test == "after_blocks":
                raise RuntimeError("Injected split transaction failure after blocks save")

            if bump_protection_revision:
                doc.protection_revision += 1
            if bump_parse_revision:
                doc.parse_revision += 1
            conn.execute(
                "UPDATE documents SET payload_json = ? WHERE id = ?",
                (doc.model_dump_json(), document_id),
            )

            if fail_stage_for_test == "after_doc":
                raise RuntimeError("Injected split transaction failure after doc update")

            if stale_reason is not None:
                job_rows = conn.execute(
                    "SELECT id, payload_json FROM jobs WHERE document_id = ?",
                    (document_id,),
                ).fetchall()
                for jr in job_rows:
                    job = Job.model_validate_json(jr["payload_json"])
                    job.outputs_stale = True
                    job.outputs_stale_reason = stale_reason
                    conn.execute(
                        "UPDATE jobs SET payload_json = ? WHERE id = ?",
                        (job.model_dump_json(), jr["id"]),
                    )

            surviving_by_page: dict[int, set[str]] = {}
            for b in blocks_to_save:
                surviving_by_page.setdefault(b.page_index, set()).add(b.id)
            self._prune_validation_issues_conn(
                conn,
                document_id,
                replace_page_indices,
                surviving_by_page,
                invalidated_block_ids=invalidated_block_ids,
            )

            if fail_stage_for_test == "before_commit":
                raise RuntimeError("Injected split transaction failure before commit")

            return doc

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
    def get_cached_translation(
        self, cache_key: str, *, expected_is_mock: bool | None = None
    ) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM translation_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if not row:
                return None
            row_is_mock = bool(row["is_mock"])
            if expected_is_mock is not None and row_is_mock != bool(expected_is_mock):
                return None
            return {
                "cache_key": row["cache_key"],
                "model_output_text": row["model_output_text"],
                "model_id": row["model_id"] if "model_id" in row.keys() else "",
                "model_name": row["model_name"],
                "model_hash": row["model_hash"] if "model_hash" in row.keys() else "",
                "quantization": row["quantization"],
                "is_mock": row_is_mock,
                "created_at": row["created_at"],
            }

    def put_cached_translation(
        self,
        cache_key: str,
        model_output_text: str,
        model_name: str,
        quantization: str,
        is_mock: bool = False,
        model_id: str = "",
        model_hash: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO translation_cache (
                    cache_key, model_output_text, model_id, model_name, model_hash, quantization, is_mock, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model_output_text = excluded.model_output_text,
                    model_id = excluded.model_id,
                    model_name = excluded.model_name,
                    model_hash = excluded.model_hash,
                    quantization = excluded.quantization,
                    is_mock = excluded.is_mock,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    model_output_text,
                    model_id,
                    model_name,
                    model_hash,
                    quantization,
                    int(is_mock),
                    utc_now_iso(),
                ),
            )
