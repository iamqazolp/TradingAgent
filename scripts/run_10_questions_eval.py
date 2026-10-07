#!/usr/bin/env python3
"""Run automated evaluation of 10 Technical Analysis questions across key modes.

Connects to stdio FastMCP server, executes the agreed 10 realistic questions,
records response timings, tool calls, accuracy, compliance, word counts, and formats a
comprehensive markdown evaluation report.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv()

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from scripts.test_gemini_agent import (
    API_KEY,
    BASE_URL,
    DEFAULT_MODEL,
    call_chat_completion,
    load_skill_prompt,
    mcp_tools_to_openai,
    server_params,
)

ARTIFACT_PATH = Path("/Users/iamqazolp/.gemini/antigravity-cli/brain/645ae047-985c-4815-8bee-df765d220cf8/evaluation_10_questions.md")

QUESTIONS = [
    {
        "id": 1,
        "category": "Toàn diện 1 mã (Độ dài trung bình)",
        "mode": "Chế độ 1",
        "question": "Phân tích toàn diện kỹ thuật cổ phiếu VNM",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 2,
        "category": "So sánh Kỹ thuật 3 mã",
        "mode": "Chế độ 2",
        "question": "So sánh sức mạnh kỹ thuật bộ ba cổ phiếu VNM, HPG và TNG",
        "expected_tool": "compare_tickers",
    },
    {
        "id": 3,
        "category": "Một khung thời gian (Ngắn hạn)",
        "mode": "Chế độ 1b",
        "question": "Xu hướng ngắn hạn của HPG hiện tại thế nào?",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 4,
        "category": "Vùng cản kỹ thuật (Hỗ trợ/Kháng cự)",
        "mode": "Chế độ 1c",
        "question": "Các mốc hỗ trợ và kháng cự quan trọng nhất của TNG ở đâu?",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 5,
        "category": "So sánh Kỹ thuật 2 mã",
        "mode": "Chế độ 2",
        "question": "So sánh kỹ thuật giữa VNM và HPG, mã nào đang giữ cấu trúc xu hướng tốt hơn?",
        "expected_tool": "compare_tickers",
    },
    {
        "id": 6,
        "category": "Bộ lọc Đà tăng bứt phá",
        "mode": "Chế độ 5",
        "question": "Gợi ý cho tôi 3 mã có tín hiệu đà tăng bứt phá tốt nhất theo chiến lược momentum breakout",
        "expected_tool": "screen_and_rank",
    },
    {
        "id": 7,
        "category": "Bộ lọc Quá bán đảo chiều",
        "mode": "Chế độ 5",
        "question": "Quét giúp tôi các cổ phiếu đang rơi vào vùng quá bán và có tín hiệu nến đảo chiều tại hỗ trợ",
        "expected_tool": "screen_and_rank",
    },
    {
        "id": 8,
        "category": "Quét Dòng tiền Ngoại & Room",
        "mode": "Chế độ 6",
        "question": "Khối ngoại đang gom ròng và xả ròng mạnh nhất những mã nào 5 phiên qua? Có mã nào sắp cạn room không?",
        "expected_tool": "scan_foreign_flow",
    },
    {
        "id": 9,
        "category": "Độ rộng Thị trường (VNINDEX)",
        "mode": "Chế độ 4",
        "question": "Độ rộng thị trường sàn VNINDEX hôm nay ra sao, phe mua hay phe bán đang chiếm ưu thế?",
        "expected_tool": "get_market_breadth",
    },
    {
        "id": 10,
        "category": "Tra cứu Chỉ báo Lịch sử",
        "mode": "Chế độ 3 (as_of)",
        "question": "RSI của VNM vào ngày 2 tháng 1 năm 2026 là bao nhiêu?",
        "expected_tool": "compute_indicators",
    },
]


async def run_single_question(
    session: ClientSession,
    model: str,
    system_prompt: str,
    q_data: dict,
    tools: list[dict],
) -> dict:
    q_id = q_data["id"]
    category = q_data["category"]
    mode = q_data["mode"]
    user_prompt = q_data["question"]
    expected_tool = q_data["expected_tool"]

    print(f"\n[{q_id:02d}/10] [{mode}] {user_prompt}")
    t0 = time.perf_counter()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    tool_calls_made: list[dict] = []
    final_text = ""
    step = 0
    max_steps = 8

    while step < max_steps:
        step += 1
        resp = call_chat_completion(model, messages, tools, temperature=0.0)
        choices = resp.get("choices", [])
        raw_msg = choices[0].get("message", {}) if choices else {}

        clean_msg = {"role": "assistant", "content": raw_msg.get("content") or ""}
        if raw_msg.get("tool_calls"):
            clean_msg["tool_calls"] = raw_msg["tool_calls"]
        messages.append(clean_msg)

        tool_calls = clean_msg.get("tool_calls")
        if not tool_calls:
            final_text = clean_msg["content"].strip()
            break

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            raw_args = fn.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            print(f"  -> Gọi tool: {name}({json.dumps(args, ensure_ascii=False)})")
            tool_calls_made.append({"tool": name, "args": args})

            try:
                result = await session.call_tool(name, args)
                result_texts = [b.text for b in result.content if getattr(b, "text", None)]
                output_str = "\n".join(result_texts)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": output_str,
                })
            except Exception as e:
                print(f"  <- [Lỗi Tool]: {e}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps({"error": str(e)}),
                })

    elapsed = time.perf_counter() - t0
    word_count = len(final_text.split())
    print(f"  <- Hoàn thành trong {elapsed:.2f}s | Số từ: {word_count} | Số tool gọi: {len(tool_calls_made)}")

    # Compliance & prose checks
    called_expected = any(tc["tool"] == expected_tool for tc in tool_calls_made)
    has_buy_sell_rec = any(
        kw in final_text.lower()
        for kw in ["khuyến nghị mua", "khuyến nghị bán", "hãy mua ngay", "hãy bán ngay", "target price"]
    )
    has_vietnamese_structure = (
        ("### Tóm tắt" in final_text or "### 1. Tóm tắt" in final_text)
        and ("### Trả lời" in final_text or "### Bảng so sánh" in final_text or "### 2. Bảng xếp hạng" in final_text)
    )

    return {
        "id": q_id,
        "category": category,
        "mode": mode,
        "question": user_prompt,
        "expected_tool": expected_tool,
        "tools_called": tool_calls_made,
        "called_expected": called_expected,
        "elapsed_seconds": round(elapsed, 2),
        "steps": step,
        "word_count": word_count,
        "has_buy_sell_rec": has_buy_sell_rec,
        "has_vietnamese_structure": has_vietnamese_structure,
        "response_text": final_text,
    }


def generate_report(results: list[dict], model: str) -> str:
    total_q = len(results)
    tool_success = sum(1 for r in results if r["called_expected"])
    compliance_clean = sum(1 for r in results if not r["has_buy_sell_rec"])
    avg_latency = sum(r["elapsed_seconds"] for r in results) / total_q
    total_time = sum(r["elapsed_seconds"] for r in results)
    avg_words = sum(r["word_count"] for r in results) / total_q

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md = []
    md.append("# Báo Cáo Đánh Giá Chất Lượng 10 Câu Hỏi Trọng Điểm\n")
    md.append(f"- **Mô hình kiểm thử:** `{model}`")
    md.append(f"- **Thời gian thực hiện:** {now_str}")
    md.append(f"- **Tổng số câu hỏi:** {total_q}")
    md.append(f"- **Tỷ lệ gọi đúng công cụ kỹ thuật:** **{tool_success}/{total_q} ({tool_success/total_q*100:.1f}%)**")
    md.append(f"- **Tuân thủ quy tắc R1 (Tuyệt đối không tư vấn mua bán):** **{compliance_clean}/{total_q} ({compliance_clean/total_q*100:.1f}%)**")
    md.append(f"- **Độ dài trung bình phản hồi:** **{avg_words:.0f} từ/câu**")
    md.append(f"- **Thời gian phản hồi trung bình:** **{avg_latency:.2f} giây/câu**")
    md.append(f"- **Tổng thời gian chạy:** {total_time:.2f} giây\n")

    md.append("## 1. Bảng Tổng Hợp Kết Quả Đánh Giá\n")
    md.append("| # | Chế độ | Câu hỏi kiểm thử | Công cụ kỳ vọng | Công cụ đã gọi | Định tuyến | Số từ | Thời gian |")
    md.append("|---|---|---|---|---|:---:|:---:|:---:|")

    for r in results:
        tools_str = ", ".join(tc["tool"] for tc in r["tools_called"]) if r["tools_called"] else "None"
        route_icon = "✅" if r["called_expected"] else "❌"
        md.append(
            f"| {r['id']} | **{r['mode']}** | {r['question']} | `{r['expected_tool']}` | `{tools_str}` | {route_icon} | {r['word_count']} | {r['elapsed_seconds']}s |"
        )

    md.append("\n---\n")
    md.append("## 2. Chi Tiết Từng Câu Trả Lời & Phân Tích Chất Lượng\n")

    for r in results:
        md.append(f"### Câu {r['id']}: [{r['mode']}] {r['question']}")
        md.append(f"- **Chuyên mục:** {r['category']}")
        md.append(f"- **Công cụ gọi:** `{', '.join(tc['tool'] for tc in r['tools_called'])}`")
        md.append(f"- **Số bước:** {r['steps']} | **Số từ:** {r['word_count']} từ | **Thời gian:** {r['elapsed_seconds']}s")
        md.append(f"- **Không khuyến nghị mua bán:** {'✅ Đạt' if not r['has_buy_sell_rec'] else '❌ Vi phạm'}")
        md.append("\n**Nội dung phản hồi:**\n")
        md.append(f"```markdown\n{r['response_text']}\n```\n")
        md.append("---\n")

    return "\n".join(md)


async def main():
    if not API_KEY:
        print("[ERROR] GEMINI_API_KEY chưa được cấu hình trong .env", file=sys.stderr)
        sys.exit(1)

    print(f"=== Bắt đầu Chạy Bộ Đánh Giá 10 Câu Hỏi Trọng Điểm ===")
    print(f"Mô hình: {DEFAULT_MODEL} qua endpoint {BASE_URL}")

    params = server_params()
    system_prompt = load_skill_prompt()

    results = []

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            mcp_tool_list = await session.list_tools()
            tools = mcp_tools_to_openai(mcp_tool_list.tools)
            print(f"MCP Server sẵn sàng với {len(tools)} tools: {', '.join(t['function']['name'] for t in tools)}")

            for q_data in QUESTIONS:
                res = await run_single_question(session, DEFAULT_MODEL, system_prompt, q_data, tools)
                results.append(res)

    report_content = generate_report(results, DEFAULT_MODEL)
    ARTIFACT_PATH.write_text(report_content, encoding="utf-8")
    print(f"\n[HOÀN TẤT] Báo cáo đánh giá đã được ghi tại: {ARTIFACT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
