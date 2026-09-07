#!/usr/bin/env python3
"""Integration test: Run an online LLM (OpenRouter / google/gemma-4-31b-it:free)
with the ta-agent MCP server.

Spawns the MCP server over stdio, fetches its tool definitions, equips the model with
skills/technical-analysis/SKILL.md, and tests end-to-end tool calling and reasoning.

Usage:
    uv run python scripts/test_openrouter_agent.py
    uv run python scripts/test_openrouter_agent.py --prompt "Analyse VNM's trend and foreign flow on 1D"
    uv run python scripts/test_openrouter_agent.py --model google/gemma-4-31b-it:free --interactive
    uv run python scripts/test_openrouter_agent.py --reasoning        # enable reasoning tokens
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

DEFAULT_MODEL = "google/gemma-4-31b-it:free"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


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


def mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """Convert MCP tool specifications to OpenAI-compatible function calling schema
    (used by OpenRouter)."""
    openai_tools = []
    for tool in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema if hasattr(tool, "inputSchema") else tool.input_schema,
            },
        })
    return openai_tools


def call_openrouter_chat(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    reasoning: bool = False,
) -> dict:
    """Send a chat completion request to OpenRouter API."""
    if not OPENROUTER_API_KEY:
        print(
            "\n[ERROR] OPENROUTER_API_KEY not set.\n"
            "Set it in your .env file or environment:\n"
            "  export OPENROUTER_API_KEY='sk-or-...'\n"
            "Get your key at: https://openrouter.ai/keys\n",
            file=sys.stderr,
        )
        sys.exit(1)

    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if reasoning:
        payload["reasoning"] = {"enabled": True}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/TAOpenHarness",
            "X-Title": "TA Open Harness",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"\n[ERROR] OpenRouter API returned HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n[ERROR] Failed to connect to OpenRouter: {e}", file=sys.stderr)
        sys.exit(1)


async def execute_agent_turn(
    session: ClientSession,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    reasoning: bool = False,
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
        print(f"\n[Step {step}] Sending request to {model} (temp={temperature}, reasoning={reasoning})...")
        response = call_openrouter_chat(
            model, messages, tools,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning=reasoning,
        )

        # Handle API errors returned in response body
        if "error" in response:
            err = response["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            print(f"\n[API Error]: {err_msg}", file=sys.stderr)
            return ""

        choice = response.get("choices", [{}])[0]
        msg = choice.get("message", {})

        # Show reasoning if present
        reasoning_details = msg.get("reasoning_details")
        if reasoning_details:
            print(f"\n[Reasoning]:")
            for detail in reasoning_details:
                content = detail.get("content", "") if isinstance(detail, dict) else str(detail)
                if content:
                    # Show a preview of reasoning
                    preview = content[:500] + ("..." if len(content) > 500 else "")
                    print(f"  {preview}")

        # Build the assistant message for conversation history
        assistant_msg: dict = {"role": "assistant", "content": msg.get("content", "")}
        # Preserve reasoning_details for multi-turn reasoning continuity
        if reasoning_details:
            assistant_msg["reasoning_details"] = reasoning_details

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        messages.append(assistant_msg)

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
            raw_args = fn.get("arguments", "{}")

            # OpenRouter returns arguments as a JSON string; parse it
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = raw_args

            print(f"\n-> [Tool Call by Model]: {name}({json.dumps(args)})")

            try:
                result = await session.call_tool(name, args)
                # Extract text content from tool result
                result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                output_str = "\n".join(result_texts)

                # Show brief preview of result
                preview = output_str[:250] + ("..." if len(output_str) > 250 else "")
                print(f"<- [Tool Result]: {preview}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": output_str,
                })
            except Exception as e:
                print(f"<- [Tool Error]: {e}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps({"error": str(e)}),
                })

    print(f"[Warning] Max tool turns reached ({max_steps}).")
    return ""


async def run_suite(
    model: str,
    custom_prompt: str | None = None,
    interactive: bool = False,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    reasoning: bool = False,
):
    print(f"Starting ta-agent MCP server connection for model '{model}' (temp: {temperature}, reasoning: {reasoning})...")

    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"[OK] Connected to MCP server: {info.server_info.name} v{info.server_info.version}")

            tools_result = await session.list_tools()
            openai_tools = mcp_tools_to_openai(tools_result.tools)
            tool_names = [t["function"]["name"] for t in openai_tools]
            print(f"[OK] Available MCP tools ({len(tool_names)}): {', '.join(tool_names)}")

            system_prompt = load_skill_prompt()
            print(f"[OK] Loaded reasoning skill prompt ({len(system_prompt)} chars)")

            agent_kwargs = dict(
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning=reasoning,
            )

            if custom_prompt:
                await execute_agent_turn(
                    session, model, system_prompt, custom_prompt, openai_tools,
                    **agent_kwargs,
                )
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
                    await execute_agent_turn(
                        session, model, system_prompt, user_input, openai_tools,
                        **agent_kwargs,
                    )
                return

            # Default automated test prompts
            test_prompts = [
                "What is VNM's trend and foreign flow on the 1D timeframe?",
                "Are foreigners buying VNM this week? Give me the 5-day flow summary.",
                "Get the last 3 daily price bars for HPG.",
            ]

            for prompt in test_prompts:
                await execute_agent_turn(
                    session, model, system_prompt, prompt, openai_tools,
                    **agent_kwargs,
                )

            print(f"\n{'='*70}")
            print("[SUCCESS] All test turns completed successfully!")
            print("Audit log verification: check logs/tool_calls.jsonl for recorded tool executions.")
            print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Test MCP server with OpenRouter online model")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"OpenRouter model identifier (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="Sampling temperature (default: 0.0 for deterministic tool calling)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=None,
        help="Max tokens for response (default: model default)",
    )
    parser.add_argument(
        "--reasoning", action="store_true",
        help="Enable reasoning tokens (model shows step-by-step thinking)",
    )
    parser.add_argument("--prompt", "-p", help="Run a specific prompt")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive chat session with the model")
    args = parser.parse_args()

    asyncio.run(run_suite(
        args.model,
        args.prompt,
        args.interactive,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning=args.reasoning,
    ))


if __name__ == "__main__":
    main()
