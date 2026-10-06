#!/usr/bin/env python3
"""Test progressive disclosure (Turn 1 medium-depth -> Turn 2 deep-dive)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from scripts.test_gemini_agent import (
    call_chat_completion,
    load_skill_prompt,
    mcp_tools_to_openai,
    server_params,
    DEFAULT_MODEL,
)


async def run_dialogue():
    params = server_params()
    system_prompt = load_skill_prompt()
    tools_list = []

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            mcp_tool_list = await session.list_tools()
            tools = mcp_tools_to_openai(mcp_tool_list.tools)

            messages = [{"role": "system", "content": system_prompt}]

            # --- TURN 1 ---
            turn1_prompt = "Phân tích toàn diện cổ phiếu VNM"
            print(f"\n==================== TURN 1: {turn1_prompt} ====================")
            messages.append({"role": "user", "content": turn1_prompt})

            step = 0
            while step < 10:
                step += 1
                resp = call_chat_completion(DEFAULT_MODEL, messages, tools, temperature=0.0)
                choices = resp.get("choices", [])
                raw_msg = choices[0].get("message", {}) if choices else {}
                clean_msg = {
                    "role": "assistant",
                    "content": raw_msg.get("content") or "",
                }
                if raw_msg.get("tool_calls"):
                    clean_msg["tool_calls"] = raw_msg["tool_calls"]
                messages.append(clean_msg)

                tool_calls = clean_msg.get("tool_calls")
                if not tool_calls:
                    print(f"\n[Turn 1 Output]:\n{clean_msg['content']}\n")
                    words = len(clean_msg["content"].split())
                    print(f"-> Turn 1 Word Count: {words} words")
                    break

                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name")
                    raw_args = fn.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    print(f"-> [Turn 1 Tool Call]: {name}({args})")
                    result = await session.call_tool(name, args)
                    result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": "\n".join(result_texts),
                    })

            # --- TURN 2 ---
            turn2_prompt = "1. Hãy đào sâu chi tiết về dòng tiền và khối lượng"
            print(f"\n==================== TURN 2: {turn2_prompt} ====================")
            messages.append({"role": "user", "content": turn2_prompt})

            step = 0
            turn2_tool_called = False
            while step < 10:
                step += 1
                resp = call_chat_completion(DEFAULT_MODEL, messages, tools, temperature=0.0)
                choices = resp.get("choices", [])
                raw_msg = choices[0].get("message", {}) if choices else {}
                clean_msg = {
                    "role": "assistant",
                    "content": raw_msg.get("content") or "",
                }
                if raw_msg.get("tool_calls"):
                    clean_msg["tool_calls"] = raw_msg["tool_calls"]
                messages.append(clean_msg)

                tool_calls = clean_msg.get("tool_calls")
                if not tool_calls:
                    print(f"\n[Turn 2 Output]:\n{clean_msg['content']}\n")
                    words = len(clean_msg["content"].split())
                    print(f"-> Turn 2 Word Count: {words} words")
                    print(f"-> Turn 2 Reused Context (Tool Called: {turn2_tool_called})")
                    break

                turn2_tool_called = True
                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name")
                    raw_args = fn.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    print(f"-> [Turn 2 Tool Call]: {name}({args})")
                    result = await session.call_tool(name, args)
                    result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": "\n".join(result_texts),
                    })


if __name__ == "__main__":
    asyncio.run(run_dialogue())
