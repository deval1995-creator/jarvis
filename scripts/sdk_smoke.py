"""Week 0/1 smoke test: does the Agent SDK answer on your Pro plan, and how fast?

Usage:
    uv run python scripts/sdk_smoke.py                # one question
    uv run python scripts/sdk_smoke.py --runs 5       # repeat to see the spread
    uv run python scripts/sdk_smoke.py "your question"

Each run is logged to the usage table (kind='test') so you can see how fast
testing eats into your limits.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from core import db  # noqa: E402

JARVIS_DIR = Path(__file__).resolve().parent.parent


async def ask(client: ClaudeSDKClient, question: str) -> dict:
    start = time.perf_counter()
    first_token_ms: int | None = None
    await client.query(question)
    print("Jarvis: ", end="", flush=True)
    result: ResultMessage | None = None
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            event = msg.event
            if event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                if first_token_ms is None:
                    first_token_ms = int((time.perf_counter() - start) * 1000)
                print(event["delta"]["text"], end="", flush=True)
        elif isinstance(msg, AssistantMessage) and first_token_ms is None:
            # Fallback if partial messages are not streamed.
            first_token_ms = int((time.perf_counter() - start) * 1000)
            print("".join(b.text for b in msg.content if isinstance(b, TextBlock)), end="")
        elif isinstance(msg, ResultMessage):
            result = msg
    print()
    total_ms = int((time.perf_counter() - start) * 1000)
    return {"first_token_ms": first_token_ms, "total_ms": total_ms, "result": result}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default="In one sentence, who are you and what can you help me with?")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--slim",
        action="store_true",
        help="no built-in tools, no MCP servers, no claude.ai connectors (~1k vs ~20k+ tokens per turn)",
    )
    args = parser.parse_args()

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is set, so the SDK will bill the API, not your Pro plan.")
        print("         Unset it to test the plan sign-in.\n")

    options = ClaudeAgentOptions(
        cwd=str(JARVIS_DIR / "jarvis_home"),
        setting_sources=["project"],  # loads jarvis_home/CLAUDE.md
        allowed_tools=[],  # no tools for the smoke test
        include_partial_messages=True,
        max_turns=1,
    )
    if args.slim:
        # allowed_tools=[] only blocks tool use; the definitions still ride along in
        # every request. This removes them from the context entirely.
        options.tools = []
        options.mcp_servers = {}
        options.strict_mcp_config = True
        options.env = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    conn = db.connect()
    timings = []
    async with ClaudeSDKClient(options=options) as client:
        for i in range(args.runs):
            print(f"\n[run {i + 1}/{args.runs}] You: {args.question}")
            r = await ask(client, args.question)
            res = r["result"]
            if res is None or res.is_error:
                print(f"ERROR: {res.subtype if res else 'no result message'} {res.errors if res else ''}")
                return 1
            db.record_usage(
                conn,
                kind="test",
                claude_session_id=res.session_id,
                usage=res.usage,
                cost_usd=res.total_cost_usd,
                duration_ms=r["total_ms"],
                first_token_ms=r["first_token_ms"],
            )
            timings.append(r)
            u = res.usage or {}
            print(
                f"  first token {r['first_token_ms']} ms | total {r['total_ms']} ms | "
                f"in {u.get('input_tokens')} (+{u.get('cache_read_input_tokens')} cache read,"
                f" +{u.get('cache_creation_input_tokens')} cache write) "
                f"out {u.get('output_tokens')} | session {res.session_id}"
            )

    ft = sorted(t["first_token_ms"] for t in timings if t["first_token_ms"] is not None)
    if ft:
        print(f"\nFirst-token ms: min {ft[0]}, median {ft[len(ft) // 2]}, max {ft[-1]}")
    print(f"Logged to {db.default_db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
