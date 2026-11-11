"""Check whether there are unfinished jobs in paperbridge.sqlite3.

Exit Codes:
  0: Success. No unfinished jobs (safe to shut down without warning).
  1: Unfinished job(s) detected.
  2: Database file does not exist (e.g. clean initial run before DB creation).
  3: Error checking jobs (e.g. missing env, DB lock, read/query failure).
     Callers must NOT treat exit code 3 as 'no jobs'.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import urllib.request

# Non-terminal states from backend/domain/models.py (VALID_JOB_TRANSITIONS)
UNFINISHED_STATES = (
    "created",
    "preflight",
    "parsing",
    "awaiting_review",
    "translating",
    "validating",
    "typesetting",
)


def main() -> int:
    runtime_dir = os.environ.get("PAPERBRIDGE_RUNTIME_DIR", "").strip()
    if runtime_dir:
        db_path = os.path.join(runtime_dir, "db", "paperbridge.sqlite3")
    else:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            print(
                "ERROR: Neither PAPERBRIDGE_RUNTIME_DIR nor LOCALAPPDATA environment variable is set.",
                file=sys.stderr,
            )
            return 3
        db_path = os.path.join(local_app_data, "PaperBridgeLocal", "db", "paperbridge.sqlite3")

    if not os.path.isfile(db_path):
        print(f"INFO: Database file does not exist at '{db_path}'.", file=sys.stderr)
        return 2

    # Open strictly in read-only mode using SQLite URI to forbid write lock or auto-creation
    uri = f"file:{urllib.request.pathname2url(db_path)}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in UNFINISHED_STATES)
        query = f"SELECT id, state, document_id FROM jobs WHERE state IN ({placeholders})"
        cur.execute(query, UNFINISHED_STATES)
        rows = cur.fetchall()

        if rows:
            print(f"UNFINISHED_JOBS_FOUND: count={len(rows)}", file=sys.stdout)
            for r in rows:
                print(f"  job_id={r[0]}, state={r[1]}, document_id={r[2]}", file=sys.stdout)
            return 1

        print("INFO: No unfinished jobs found.", file=sys.stdout)
        return 0
    except Exception as exc:
        print(f"ERROR: Failed to query database '{db_path}': {exc}", file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())

