"""
Tools the research agent can call.

Returns two things from build_tool_definitions():
  1. tool_defs — list of Anthropic tool-use JSON schema dicts
  2. tool_map  — dict mapping tool name → callable

Design principles:
- Read-only where possible. The only write tool is draft_finding.
- No network calls to external services.
- Codebase reading is scoped to the project root with path traversal blocked.
- Every tool call is logged to JSONL for later inspection.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from research.queries import ReadOnlyDB


def build_tool_definitions(
    db: ReadOnlyDB,
    codebase_root: Path,
    report_path: Path,
    log_path: Path,
) -> tuple[list[dict], dict[str, callable]]:
    """Build Anthropic tool definitions and an executor map."""

    codebase_root = codebase_root.resolve()

    def _log_call(name: str, args: dict, result_summary: str):
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "tool": name,
            "args": args,
            "result_summary": result_summary[:500],
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    # ── Tool implementations ──────────────────────────────────────

    def list_tables() -> list[str]:
        tables = db.list_tables()
        _log_call("list_tables", {}, f"{len(tables)} tables")
        return tables

    def describe_table(name: str) -> list[dict]:
        result = db.describe_table(name)
        _log_call("describe_table", {"name": name}, f"{len(result)} rows")
        return result

    def query_db(sql: str) -> list[dict]:
        try:
            rows = db.query(sql)
            _log_call("query_db", {"sql": sql[:200]}, f"{len(rows)} rows")
            if len(rows) > 500:
                return rows[:500] + [{"_truncated": f"{len(rows) - 500} more rows"}]
            return rows
        except Exception as e:
            _log_call("query_db", {"sql": sql[:200]}, f"ERROR: {e}")
            raise

    def read_codebase(relative_path: str) -> str:
        blocked = {"config.json", ".env", "price_history.db", "index.html"}
        if any(b in relative_path for b in blocked):
            raise PermissionError(f"Access to {relative_path} is blocked")
        target = (codebase_root / relative_path).resolve()
        if not str(target).startswith(str(codebase_root)):
            raise PermissionError("Path traversal blocked")
        if not target.exists():
            raise FileNotFoundError(relative_path)
        if not target.is_file():
            raise ValueError(f"Not a file: {relative_path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        _log_call("read_codebase", {"path": relative_path}, f"{len(content)} chars")
        if len(content) > 40000:
            return content[:40000] + f"\n\n[... truncated, {len(content) - 40000} chars remaining]"
        return content

    def list_codebase_dir(relative_path: str = ".") -> list[str]:
        target = (codebase_root / relative_path).resolve()
        if not str(target).startswith(str(codebase_root)):
            raise PermissionError("Path traversal blocked")
        if not target.is_dir():
            raise ValueError(f"Not a directory: {relative_path}")
        entries = sorted(
            [p.name + ("/" if p.is_dir() else "") for p in target.iterdir()
             if not p.name.startswith(".")]
        )
        _log_call("list_codebase_dir", {"path": relative_path}, f"{len(entries)} entries")
        return entries

    def draft_finding(
        title: str,
        problem_statement: str,
        evidence: dict,
        recommendation: str,
        implementation_sketch: str,
        confidence: str,
        impact: str,
        tags: list[str],
    ) -> dict:
        finding_id = db.insert_finding(
            title=title,
            problem_statement=problem_statement,
            evidence=evidence,
            recommendation=recommendation,
            implementation_sketch=implementation_sketch,
            confidence=confidence,
            impact=impact,
            tags=tags,
        )
        _log_call(
            "draft_finding",
            {"title": title, "confidence": confidence, "impact": impact},
            f"id={finding_id}",
        )
        return {"id": finding_id, "title": title}

    def write_report(markdown: str) -> dict:
        with report_path.open("a", encoding="utf-8") as f:
            f.write(markdown + "\n\n")
        _log_call("write_report", {}, f"{len(markdown)} chars")
        return {"written": True, "path": str(report_path)}

    # ── Anthropic tool definitions (JSON Schema) ──────────────────

    tool_defs = [
        {
            "name": "list_tables",
            "description": "List tables in the database.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "describe_table",
            "description": "Get the CREATE TABLE statement for a table.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Table name"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "query_db",
            "description": (
                "Run a read-only SQL query. INSERT/UPDATE/DELETE/DROP/etc are "
                "rejected. Use for exploring price_history, tcg_history, "
                "ebay_sold, discord_log, retailer_sightings, marketplace_messages. "
                "Returns list of row dicts. Limit your queries — don't SELECT * "
                "on huge tables without a LIMIT."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL SELECT query"},
                },
                "required": ["sql"],
            },
        },
        {
            "name": "read_codebase",
            "description": (
                "Read a file from the project codebase. Use this to understand "
                "how a feature is currently implemented before recommending changes. "
                "Paths are relative to project root. Cannot read config.json or "
                "anything outside the project."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Path relative to project root",
                    },
                },
                "required": ["relative_path"],
            },
        },
        {
            "name": "list_codebase_dir",
            "description": "List files in a project directory (non-recursive).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Directory path relative to project root (default '.')",
                        "default": ".",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "draft_finding",
            "description": (
                "Record a finding to the research_findings table. Use this "
                "ONLY for high-quality, evidence-backed recommendations. "
                "Every field is required. Evidence should be a dict of "
                "supporting data (counts, examples, query results)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "problem_statement": {"type": "string"},
                    "evidence": {
                        "type": "object",
                        "description": "Dict of supporting data (counts, examples, query results)",
                    },
                    "recommendation": {"type": "string"},
                    "implementation_sketch": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "impact": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "title", "problem_statement", "evidence", "recommendation",
                    "implementation_sketch", "confidence", "impact", "tags",
                ],
            },
        },
        {
            "name": "write_report",
            "description": (
                "Append a section to this run's markdown report. Use once at "
                "the end to summarize the run (not for each finding). Findings "
                "go through draft_finding, not here."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "markdown": {"type": "string", "description": "Markdown content"},
                },
                "required": ["markdown"],
            },
        },
    ]

    tool_map = {
        "list_tables": lambda **kw: list_tables(),
        "describe_table": lambda **kw: describe_table(kw["name"]),
        "query_db": lambda **kw: query_db(kw["sql"]),
        "read_codebase": lambda **kw: read_codebase(kw["relative_path"]),
        "list_codebase_dir": lambda **kw: list_codebase_dir(kw.get("relative_path", ".")),
        "draft_finding": lambda **kw: draft_finding(
            title=kw["title"],
            problem_statement=kw["problem_statement"],
            evidence=kw["evidence"],
            recommendation=kw["recommendation"],
            implementation_sketch=kw["implementation_sketch"],
            confidence=kw["confidence"],
            impact=kw["impact"],
            tags=kw["tags"],
        ),
        "write_report": lambda **kw: write_report(kw["markdown"]),
    }

    return tool_defs, tool_map


def execute_tool(tool_map: dict, name: str, input_args: dict) -> Any:
    """Execute a tool by name. Raises KeyError if tool not found."""
    if name not in tool_map:
        raise KeyError(f"Unknown tool: {name}")
    return tool_map[name](**input_args)
