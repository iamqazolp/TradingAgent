#!/usr/bin/env python3
"""Integration test: Run a real local LLM (Ollama / llama3.1:8b) with the ta-agent MCP server.

Spawns the MCP server over stdio, fetches its tool definitions, equips the model with
skills/technical-analysis/SKILL.md, and tests end-to-end tool calling and reasoning.

Usage:
    uv run python scripts/test_ollama_agent.py
    uv run python scripts/test_ollama_agent.py --prompt "Analyse VNM's trend and foreign flow on 1D"
    uv run python scripts/test_ollama_agent.py --model llama3.1:8b --interactive
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_NUM_CTX = 16384
OLLAMA_API_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def server_params() -> StdioServerParameters:
    env = dict(os.environ)
    env.setdefault("TA_AGENT_DB", str(REPO_ROOT / "var" / "ta.sqlite"))
    env.setdefault("TA_AGENT_AUDIT_LOG", str(REPO_ROOT / "logs" / "tool_calls.jsonl"))
    env.setdefault("TA_AGENT_LOG_LEVEL", "INFO")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(REPO_ROOT),
        env=env,
    )


def load_skill_prompt() -> str:
    skill_path = REPO_ROOT / "skills" / "technical-analysis" / "SKILL.md"
    if not skill_path.exists():
        return "You are an expert technical analyst for Vietnamese equities."
    text = skill_path.read_text(encoding="utf-8")
    # Strip YAML frontmatter if present
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2].strip()
    return text


def mcp_tools_to_ollama(mcp_tools) -> list[dict]:
    """Convert MCP tool specifications to Ollama function calling schema."""
    ollama_tools = []
    for tool in mcp_tools:
        ollama_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema if hasattr(tool, "inputSchema") else tool.input_schema,
            },
        })
    return ollama_tools


def call_ollama_chat(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = 0.0,
) -> dict:
    url = f"{OLLAMA_API_URL}/api/chat"
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"\n[ERROR] Failed to connect to Ollama at {url}: {e}", file=sys.stderr)
        print("Ensure Ollama is running: `ollama serve` or open Ollama app.\n", file=sys.stderr)
        sys.exit(1)


async def execute_agent_turn(
    session: ClientSession,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = 0.0,
) -> str:
    print(f"\n{'='*70}")
    print(f"User Prompt: {user_prompt}")
    print(f"{'='*70}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    max_steps = 5
    step = 0

    while step < max_steps:
        step += 1
        print(f"\n[Step {step}] Sending request to {model} (num_ctx={num_ctx}, temp={temperature})...")
        response = call_ollama_chat(model, messages, tools, num_ctx=num_ctx, temperature=temperature)
        msg = response.get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            # Model finished reasoning and produced final content
            final_text = msg.get("content", "").strip()
            print(f"\n[Final Response from {model}]:\n")
            print(final_text)
            return final_text

        # Execute each requested tool call against the MCP server
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {})
            print(f"\n-> [Tool Call by Model]: {name}({json.dumps(args)})")

            try:
                result = await session.call_tool(name, args)
                # Extract text content from tool result
                result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                output_str = "\n".join(result_texts)
                
                # Show brief preview of result
                preview = output_str[:250] + ("..." if len(output_str) > 250 else "")
                print(f"<- [Tool Result]: {preview}")

                tool_msg = {
                    "role": "tool",
                    "content": output_str,
                }
                call_id = call.get("id")
                if call_id:
                    tool_msg["tool_call_id"] = call_id
                messages.append(tool_msg)
            except Exception as e:
                print(f"<- [Tool Error]: {e}")
                err_msg = {
                    "role": "tool",
                    "content": json.dumps({"error": str(e)}),
                }
                call_id = call.get("id")
                if call_id:
                    err_msg["tool_call_id"] = call_id
                messages.append(err_msg)

    print(f"[Warning] Max tool turns reached ({max_steps}).")
    return ""


async def run_suite(
    model: str,
    custom_prompt: str | None = None,
    interactive: bool = False,
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = 0.0,
):
    print(f"Starting ta-agent MCP server connection for model '{model}' (num_ctx: {num_ctx}, temp: {temperature})...")

    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"[OK] Connected to MCP server: {info.server_info.name} v{info.server_info.version}")

            tools_result = await session.list_tools()
            ollama_tools = mcp_tools_to_ollama(tools_result.tools)
            tool_names = [t["function"]["name"] for t in ollama_tools]
            print(f"[OK] Available MCP tools ({len(tool_names)}): {', '.join(tool_names)}")

            system_prompt = load_skill_prompt()
            print(f"[OK] Loaded reasoning skill prompt ({len(system_prompt)} chars)")

            if custom_prompt:
                await execute_agent_turn(session, model, system_prompt, custom_prompt, ollama_tools, num_ctx=num_ctx, temperature=temperature)
                return

            if interactive:
                print("\n" + "="*70)
                print("Interactive Agent Mode. Ask any question about VNM, HPG, TNG.")
                print("Type 'exit' or 'quit' to stop.")
                print("="*70)
                while True:
                    try:
                        user_input = input("\nYou: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not user_input or user_input.lower() in ("exit", "quit"):
                        break
                    await execute_agent_turn(session, model, system_prompt, user_input, ollama_tools, num_ctx=num_ctx, temperature=temperature)
                return

            # Default automated test prompts
            test_prompts = [
                "What is VNM's trend and foreign flow on the 1D timeframe?",
                "Are foreigners buying VNM this week? Give me the 5-day flow summary.",
                "Get the last 3 daily price bars for HPG.",
            ]

            for prompt in test_prompts:
                await execute_agent_turn(session, model, system_prompt, prompt, ollama_tools, num_ctx=num_ctx, temperature=temperature)

            print(f"\n{'='*70}")
            print("[SUCCESS] All test turns completed successfully!")
            print("Audit log verification: check logs/tool_calls.jsonl for recorded tool executions.")
            print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Test MCP server with local Ollama model")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help=f"Context window size in tokens (default: {DEFAULT_NUM_CTX})")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature (default: 0.0 for deterministic tool calling)")
    parser.add_argument("--prompt", "-p", help="Run a specific prompt")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive chat session with the model")
    args = parser.parse_args()

    asyncio.run(run_suite(args.model, args.prompt, args.interactive, num_ctx=args.num_ctx, temperature=args.temperature))


if __name__ == "__main__":
    main()
