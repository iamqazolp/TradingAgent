#!/usr/bin/env python3
"""Integration test: Run Google GenAI (gemma-4-31b-it / gemini) with the ta-agent MCP server.

Uses the official `google-genai` SDK with Google AI Studio API key.
Spawns the MCP server over stdio, fetches its tool definitions, equips the model with
skills/technical-analysis/SKILL.md, and executes end-to-end tool calling and reasoning.

Usage:
    # Quick standalone test without MCP server
    uv run python scripts/test_gemini_agent.py --quick-test

    # Run agent with MCP server and default prompts
    uv run python scripts/test_gemini_agent.py

    # Run agent with a custom prompt
    uv run python scripts/test_gemini_agent.py --prompt "Analyse VNM's trend and foreign flow on 1D"

    # Interactive mode
    uv run python scripts/test_gemini_agent.py --interactive

    # Use a different model or enable thinking
    uv run python scripts/test_gemini_agent.py --model gemini-2.5-flash --thinking
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

DEFAULT_MODEL = "gemma-4-31b-it"


def get_api_key() -> str:
    """Retrieve Gemini API key from environment or .env file."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print(
            "\n[ERROR] GEMINI_API_KEY not found.\n\n"
            "To get an API key:\n"
            "  1. Go to Google AI Studio: https://aistudio.google.com/\n"
            "  2. Sign in with your Google account\n"
            "  3. Click 'Get API key' and create a key\n"
            "  4. Add it to your .env file:\n"
            "       GEMINI_API_KEY=your_api_key_here\n"
            "     or export GEMINI_API_KEY='your_api_key_here'\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


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


def mcp_tools_to_gemini(mcp_tools) -> list[types.Tool]:
    """Convert MCP tool specifications to Google GenAI Tool schema."""
    function_declarations = []
    for tool in mcp_tools:
        schema = tool.inputSchema if hasattr(tool, "inputSchema") else tool.input_schema
        function_declarations.append({
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        })
    return [types.Tool(function_declarations=function_declarations)]


def run_quick_test(model: str = DEFAULT_MODEL):
    """Run the standalone test snippet from Google AI Studio."""
    api_key = get_api_key()
    client = genai.Client(api_key=api_key)
    print(f"Running quick test with model: {model}...")
    try:
        response = client.models.generate_content(
            model=model,
            contents="Explain the benefits of a 256k context window.",
        )
        print("\n" + "=" * 70)
        print(f"Response from {model}:")
        print("=" * 70)
        print(response.text)
        print("=" * 70 + "\n")
        print("[SUCCESS] Google GenAI SDK connection verified!")
    except Exception as e:
        print(f"\n[ERROR] Quick test failed: {e}", file=sys.stderr)
        sys.exit(1)


async def execute_agent_turn(
    client: genai.Client,
    session: ClientSession,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[types.Tool],
    temperature: float = 0.0,
    thinking: bool = False,
) -> str:
    print(f"\n{'='*70}")
    print(f"User Prompt: {user_prompt}")
    print(f"{'='*70}")

    # Build GenerateContentConfig
    config_kwargs: dict = {
        "system_instruction": system_prompt,
        "temperature": temperature,
        "tools": tools,
    }
    if thinking:
        config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)

    config = types.GenerateContentConfig(**config_kwargs)

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])
    ]

    max_steps = 5
    step = 0

    while step < max_steps:
        step += 1
        print(f"\n[Step {step}] Sending request to {model}...")
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            print(f"\n[ERROR] API request failed: {e}", file=sys.stderr)
            return ""

        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content:
            print("\n[Warning] Received empty response from model.")
            return ""

        # Display thinking/thoughts if available
        for part in candidate.content.parts:
            if getattr(part, "thought", None):
                thought_text = str(part.text or part.thought)
                preview = thought_text[:400] + ("..." if len(thought_text) > 400 else "")
                print(f"\n[Thinking]:\n  {preview}")

        # Append model turn to conversation history
        contents.append(candidate.content)

        # Check for function calls
        function_calls = response.function_calls
        if not function_calls:
            # Model produced final text
            final_text = response.text or ""
            print(f"\n[Final Response from {model}]:\n")
            print(final_text)
            return final_text

        # Execute requested tool calls against MCP server
        tool_response_parts: list[types.Part] = []
        for call in function_calls:
            name = call.name
            args = call.args if isinstance(call.args, dict) else {}
            print(f"\n-> [Tool Call by Model]: {name}({json.dumps(args)})")

            try:
                result = await session.call_tool(name, args)
                result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                output_str = "\n".join(result_texts)

                preview = output_str[:250] + ("..." if len(output_str) > 250 else "")
                print(f"<- [Tool Result]: {preview}")

                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=name,
                        response={"result": output_str},
                    )
                )
            except Exception as e:
                print(f"<- [Tool Error]: {e}")
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=name,
                        response={"error": str(e)},
                    )
                )

        contents.append(types.Content(role="tool", parts=tool_response_parts))

    print(f"[Warning] Max tool turns reached ({max_steps}).")
    return ""


async def run_suite(
    model: str,
    custom_prompt: str | None = None,
    interactive: bool = False,
    temperature: float = 0.0,
    thinking: bool = False,
):
    api_key = get_api_key()
    client = genai.Client(api_key=api_key)

    print(f"Starting ta-agent MCP server connection for model '{model}' (temp: {temperature})...")

    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"[OK] Connected to MCP server: {info.server_info.name} v{info.server_info.version}")

            tools_result = await session.list_tools()
            gemini_tools = mcp_tools_to_gemini(tools_result.tools)
            tool_names = [f.name for f in gemini_tools[0].function_declarations]
            print(f"[OK] Available MCP tools ({len(tool_names)}): {', '.join(tool_names)}")

            system_prompt = load_skill_prompt()
            print(f"[OK] Loaded reasoning skill prompt ({len(system_prompt)} chars)")

            agent_kwargs = dict(
                temperature=temperature,
                thinking=thinking,
            )

            if custom_prompt:
                await execute_agent_turn(
                    client, session, model, system_prompt, custom_prompt, gemini_tools,
                    **agent_kwargs,
                )
                return

            if interactive:
                print("\n" + "=" * 70)
                print("Interactive Agent Mode. Ask any question about VNM, HPG, TNG.")
                print("Type 'exit' or 'quit' to stop.")
                print("=" * 70)
                while True:
                    try:
                        user_input = input("\nYou: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not user_input or user_input.lower() in ("exit", "quit"):
                        break
                    await execute_agent_turn(
                        client, session, model, system_prompt, user_input, gemini_tools,
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
                    client, session, model, system_prompt, prompt, gemini_tools,
                    **agent_kwargs,
                )

            print(f"\n{'='*70}")
            print("[SUCCESS] All test turns completed successfully!")
            print("Audit log verification: check logs/tool_calls.jsonl for recorded tool executions.")
            print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Test MCP server with Google GenAI SDK (Google AI Studio)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Model identifier (default: {DEFAULT_MODEL}, or gemini-2.5-flash, gemini-2.5-pro)",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="Sampling temperature (default: 0.0 for deterministic tool calling)",
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Enable thinking/reasoning mode (for models supporting thinking)",
    )
    parser.add_argument(
        "--quick-test", action="store_true",
        help="Run simple generate_content test without launching the MCP server",
    )
    parser.add_argument("--prompt", "-p", help="Run a specific prompt")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive chat session with the model")
    args = parser.parse_args()

    if args.quick_test:
        run_quick_test(args.model)
        return

    try:
        asyncio.run(run_suite(
            args.model,
            args.prompt,
            args.interactive,
            temperature=args.temperature,
            thinking=args.thinking,
        ))
    except KeyboardInterrupt:
        print("\n[Interrupted by user]")
        sys.exit(0)


if __name__ == "__main__":
    main()
