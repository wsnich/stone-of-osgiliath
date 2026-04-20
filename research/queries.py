"""
Read-only database access for the research agent.

Uses SQLite URI mode with mode=ro to enforce read-only at the driver level.
Any INSERT/UPDATE/DELETE attempts will raise. The one exception is the
insert_finding() helper, which uses a separate writable connection scoped
to the research_findings table only.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class ReadOnlyDB:
    """Read-only access to the main DB + scoped writes to research_findings."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._verify_path()

    def _verify_path(self):
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"DB not found: {self.db_path}")

    @contextmanager
    def _ro_conn(self):
        # mode=ro enforces read-only at the SQLite level
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _rw_conn(self):
        """Writable connection - ONLY used for research_findings writes."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a read-only SQL query. Raises on any write attempt."""
        sql_lower = sql.strip().lower()
        # Belt-and-suspenders: block obvious write keywords even though
        # mode=ro would reject them anyway.
        forbidden = ("insert", "update", "delete", "drop", "alter",
                     "create", "replace", "attach", "detach", "pragma")
        first_word = sql_lower.split()[0] if sql_lower.split() else ""
        if first_word in forbidden:
            raise PermissionError(f"Write/schema operations not allowed: {first_word}")

        with self._ro_conn() as conn:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def list_tables(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [r["name"] for r in rows]

    def describe_table(self, name: str) -> list[dict]:
        # PRAGMA is blocked by our filter above, so use sqlite_master
        rows = self.query(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return rows

    # --- Scoped writes to research_findings only ---

    def ensure_findings_table(self):
        """Create research_findings table if it doesn't exist (idempotent)."""
        with self._rw_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_findings (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    title                 TEXT    NOT NULL,
                    problem_statement     TEXT    NOT NULL,
                    evidence_json         TEXT    NOT NULL,
                    recommendation        TEXT    NOT NULL,
                    implementation_sketch TEXT    NOT NULL,
                    confidence            TEXT    NOT NULL,
                    impact                TEXT    NOT NULL,
                    tags_json             TEXT    NOT NULL DEFAULT '[]',
                    status                TEXT    NOT NULL DEFAULT 'new',
                    run_id                TEXT,
                    created_at            TEXT    NOT NULL
                )
            """)

    def insert_finding(
        self,
        title: str,
        problem_statement: str,
        evidence: dict,
        recommendation: str,
        implementation_sketch: str,
        confidence: str,
        impact: str,
        tags: list[str],
        run_id: str | None = None,
    ) -> int:
        if confidence not in ("low", "medium", "high"):
            raise ValueError(f"invalid confidence: {confidence}")
        if impact not in ("low", "medium", "high"):
            raise ValueError(f"invalid impact: {impact}")

        with self._rw_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_findings
                (title, problem_statement, evidence_json, recommendation,
                 implementation_sketch, confidence, impact, tags_json,
                 status, run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    title,
                    problem_statement,
                    json.dumps(evidence),
                    recommendation,
                    implementation_sketch,
                    confidence,
                    impact,
                    json.dumps(tags),
                    run_id,
                    datetime.utcnow().isoformat(),
                ),
            )
            return cur.lastrowid

    def count_findings_since(self, iso_timestamp: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) as c FROM research_findings WHERE created_at >= ?",
            (iso_timestamp,),
        )
        return rows[0]["c"] if rows else 0
