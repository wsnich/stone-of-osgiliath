"""
Research agent for Stone of Osgiliath.

Analyzes historical data from price_history.db and generates feature
recommendations. Runs weekly (or on-demand via API).

Safety:
- Read-only DB access (except scoped writes to research_findings)
- No imports from monitors/ (no live scraping)
- No access to config.json (no Discord token exposure)
- Outputs only to research_findings table and research/output/

Uses the Anthropic Python SDK (messages API with tool use).
"""
from __future__ import annotations

import asyncio
import json
import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from research.tools import build_tool_definitions, execute_tool
from research.queries import ReadOnlyDB

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ResearchRunConfig:
    db_path: str
    lookback_days: int = 7
    max_findings: int = 7
    model: str = "claude-sonnet-4-5"
    api_key: str | None = None
    codebase_root: Path = Path(".")
    max_iterations: int = 80


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def run_research(cfg: ResearchRunConfig) -> dict[str, Any]:
    """Run one research cycle (synchronous). Returns summary dict."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    log_path = OUTPUT_DIR / f"agent_log_{timestamp}.jsonl"
    report_path = OUTPUT_DIR / f"report_{timestamp}.md"

    db = ReadOnlyDB(cfg.db_path)
    tool_defs, tool_map = build_tool_definitions(
        db=db,
        codebase_root=cfg.codebase_root,
        report_path=report_path,
        log_path=log_path,
    )

    system_prompt = load_prompt("system")
    task_prompt = load_prompt("weekly_research").format(
        lookback_days=cfg.lookback_days,
        max_findings=cfg.max_findings,
        today=datetime.utcnow().strftime("%Y-%m-%d"),
    )

    client = Anthropic(api_key=cfg.api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # Conversation messages
    messages = [{"role": "user", "content": task_prompt}]

    iterations = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for _ in range(cfg.max_iterations):
        iterations += 1

        response = client.messages.create(
            model=cfg.model,
            max_tokens=4096,
            system=system_prompt,
            tools=tool_defs,
            messages=messages,
        )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Log the response
        _log_entry(log_path, {
            "iteration": iterations,
            "stop_reason": response.stop_reason,
            "content_blocks": len(response.content),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })

        # If the model stopped without tool use, we're done
        if response.stop_reason == "end_turn":
            # Append assistant response to messages for completeness
            messages.append({"role": "assistant", "content": response.content})
            break

        # Process tool calls
        if response.stop_reason == "tool_use":
            # Append assistant response (contains tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id

                    try:
                        result = execute_tool(tool_map, tool_name, tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(result, default=str)
                            if not isinstance(result, str) else result,
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": f"ERROR: {type(e).__name__}: {e}",
                            "is_error": True,
                        })
                        _log_entry(log_path, {
                            "tool_error": tool_name,
                            "error": str(e),
                            "traceback": traceback.format_exc(),
                        })

            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason — break
            messages.append({"role": "assistant", "content": response.content})
            break

    # Tally what was written
    findings_count = db.count_findings_since(
        timestamp.replace("_", "T").replace("T", " ", 1) if "_" in timestamp
        else timestamp
    )

    summary = {
        "timestamp": timestamp,
        "run_id": timestamp,
        "findings_written": findings_count,
        "report_path": str(report_path),
        "log_path": str(log_path),
        "iterations": iterations,
        "tokens_used": {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "total": total_input_tokens + total_output_tokens,
        },
    }

    # Save run metadata
    (OUTPUT_DIR / f"run_{timestamp}.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    return summary


def _log_entry(log_path: Path, entry: dict):
    entry["ts"] = datetime.utcnow().isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


async def run_research_async(cfg: ResearchRunConfig) -> dict[str, Any]:
    """Async wrapper — runs the synchronous agent in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_research, cfg)


async def main():
    """CLI entry point for manual runs."""
    import argparse
    parser = argparse.ArgumentParser(description="Run Stone of Osgiliath research agent")
    parser.add_argument("--db", default="price_history.db")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-findings", type=int, default=7)
    parser.add_argument("--model", default="claude-sonnet-4-5")
    args = parser.parse_args()

    cfg = ResearchRunConfig(
        db_path=args.db,
        lookback_days=args.days,
        max_findings=args.max_findings,
        model=args.model,
    )
    summary = run_research(cfg)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
