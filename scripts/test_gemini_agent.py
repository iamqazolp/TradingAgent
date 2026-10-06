#!/usr/bin/env python3
"""Integration test: Run Gemini via OpenAI-compatible endpoint with the ta-agent MCP server.

Reads GEMINI_API_KEY and GEMINI_BASE_URL from .env.
Spawns the MCP server over stdio, fetches its tool definitions, equips the model with
skills/technical-analysis/SKILL.md, and tests end-to-end tool calling and reasoning.

Usage:
    uv run python scripts/test_gemini_agent.py
    uv run python scripts/test_gemini_agent.py --prompt "Phân tích ngắn hạn VNM"
    uv run python scripts/test_gemini_agent.py --prompt "Độ rộng thị trường sàn VNINDEX thế nào?"
    uv run python scripts/test_gemini_agent.py --model gemini-2.5-pro --interactive
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

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://api.shopaikey.com").rstrip("/")
API_KEY = os.environ.get("GEMINI_API_KEY", "")


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
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2].strip()
    return text


def simplify_schema(schema: Any) -> Any:
    """Normalize Pydantic/OpenAPI schema into Gemini-friendly function calling schema.
    
    Removes anyOf null unions and collapses to the primary non-null type.
    """
    if not isinstance(schema, dict):
        return schema
    new_schema = {}
    for k, v in schema.items():
        if k == "anyOf" and isinstance(v, list):
            non_null = [item for item in v if isinstance(item, dict) and item.get("type") != "null"]
            if non_null:
                simplified = simplify_schema(non_null[0])
                new_schema.update(simplified)
                continue
        if isinstance(v, dict):
            new_schema[k] = simplify_schema(v)
        elif isinstance(v, list):
            new_schema[k] = [simplify_schema(item) for item in v]
        else:
            new_schema[k] = v
    return new_schema


def mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """Convert MCP tool specifications to OpenAI-compatible function calling schema."""
    openai_tools = []
    for tool in mcp_tools:
        schema = tool.inputSchema if hasattr(tool, "inputSchema") else tool.input_schema
        cleaned_schema = simplify_schema(schema)
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": cleaned_schema,
            },
        })
    return openai_tools


def call_chat_completion(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.0,
) -> dict:
    url = f"{BASE_URL}/v1/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        print(f"\n[ERROR] HTTP {e.code} from {url}: {e.reason}\n  Detail: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n[ERROR] Connection failed to {url}: {e}", file=sys.stderr)
        sys.exit(1)


async def execute_agent_turn(
    session: ClientSession,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    temperature: float = 0.0,
) -> str:
    print(f"\n{'='*70}")
    print(f"Yêu cầu: {user_prompt}")
    print(f"{'='*70}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    max_steps = 10
    step = 0

    while step < max_steps:
        step += 1
        print(f"\n[Bước {step}] Gửi request tới {model}...")
        response = call_chat_completion(model, messages, tools, temperature=temperature)
        choices = response.get("choices", [])
        raw_msg = choices[0].get("message", {}) if choices else {}
        clean_msg: dict[str, Any] = {"role": "assistant"}
        if raw_msg.get("content"):
            clean_msg["content"] = raw_msg["content"]
        else:
            clean_msg["content"] = ""
        if raw_msg.get("tool_calls"):
            clean_msg["tool_calls"] = raw_msg["tool_calls"]
        messages.append(clean_msg)
        msg = clean_msg

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            # Model finished reasoning and produced final content
            final_text = msg.get("content", "").strip()
            print(f"\n[Phản hồi từ Agent]:\n{final_text}\n")
            return final_text

        # Execute each requested tool call against the MCP server
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            raw_args = fn.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            print(f"\n-> [Tool Call]: {name}({json.dumps(args, ensure_ascii=False)})")

            try:
                result = await session.call_tool(name, args)
                result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                output_str = "\n".join(result_texts)

                preview = output_str[:300] + ("..." if len(output_str) > 300 else "")
                print(f"<- [Tool Result]: {preview}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": output_str,
                })
            except Exception as e:
                print(f"<- [Tool Error]: {e}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps({"error": str(e)}),
                })

    print(f"[Warning] Đã đạt giới hạn số lượt gọi tool ({max_steps}).")
    return ""


async def run_suite(
    model: str,
    custom_prompt: str | None = None,
    interactive: bool = False,
) -> None:
    if not API_KEY:
        print("[ERROR] GEMINI_API_KEY is not set in .env", file=sys.stderr)
        sys.exit(1)

    print(f"Khởi động MCP Server qua stdio...")
    print(f"Sử dụng Model: {model} qua endpoint {BASE_URL}")

    params = server_params()
    system_prompt = load_skill_prompt()

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            mcp_tool_list = await session.list_tools()
            tools = mcp_tools_to_openai(mcp_tool_list.tools)
            print(f"MCP Server đã sẵn sàng với {len(tools)} tools: {', '.join(t['function']['name'] for t in tools)}")

            if custom_prompt:
                await execute_agent_turn(session, model, system_prompt, custom_prompt, tools)
            elif interactive:
                print("\nBắt đầu chế độ tương tác (Gõ 'exit' hoặc 'quit' để thoát):")
                while True:
                    try:
                        user_input = input("\nBạn > ").strip()
                        if user_input.lower() in ("exit", "quit", "q"):
                            break
                        if not user_input:
                            continue
                        await execute_agent_turn(session, model, system_prompt, user_input, tools)
                    except (KeyboardInterrupt, EOFError):
                        break
            else:
                default_prompts = [
                    "phân tích vnm trong ngắn hạn",
                    "Độ rộng thị trường sàn VNINDEX hôm nay thế nào?",
                ]
                for p in default_prompts:
                    await execute_agent_turn(session, model, system_prompt, p, tools)


def main():
    parser = argparse.ArgumentParser(description="Test Gemini agent with MCP tools via .env")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--prompt", help="Custom single prompt to test")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive terminal chat")
    args = parser.parse_args()

    asyncio.run(run_suite(args.model, custom_prompt=args.prompt, interactive=args.interactive))


if __name__ == "__main__":
    main()
