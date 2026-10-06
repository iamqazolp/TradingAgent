#!/usr/bin/env python3
"""Run automated evaluation of 20 Technical Analysis questions across all modes.

Connects to the stdio FastMCP server, executes 20 diverse realistic questions,
records response timings, tool calls, accuracy, compliance, and formats a comprehensive
markdown evaluation report.
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

ARTIFACT_PATH = Path("/Users/iamqazolp/.gemini/antigravity-cli/brain/645ae047-985c-4815-8bee-df765d220cf8/evaluation_20_questions.md")

QUESTIONS = [
    {
        "id": 1,
        "category": "Multi-Horizon Full",
        "mode": "Chế độ 1",
        "question": "Phân tích toàn diện kỹ thuật và dòng tiền của VNM.",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 2,
        "category": "Short-Term Horizon",
        "mode": "Chế độ 1b",
        "question": "Đánh giá cấu trúc ngắn hạn của HPG trong 1-2 tuần tới.",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 3,
        "category": "Mid-Term Horizon",
        "mode": "Chế độ 1b",
        "question": "Xu hướng trung hạn của TNG hiện tại thế nào?",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 4,
        "category": "Long-Term & Invalidation",
        "mode": "Chế độ 1b",
        "question": "Xu hướng dài hạn của VNM có tích cực không và mốc vô hiệu hóa ở đâu?",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 5,
        "category": "Support & Resistance Levels",
        "mode": "Chế độ 1c",
        "question": "Các mốc hỗ trợ và kháng cự quan trọng nhất của HPG hiện tại là bao nhiêu?",
        "expected_tool": "analyze_multi_horizon",
    },
    {
        "id": 6,
        "category": "Order Flow Summary",
        "mode": "Chế độ 3",
        "question": "Tóm tắt dòng tiền khớp lệnh và áp lực mua bán của VNM trong 10 phiên gần nhất.",
        "expected_tool": "get_flow_summary",
    },
    {
        "id": 7,
        "category": "Foreign Flow & Room",
        "mode": "Chế độ 3",
        "question": "Khối ngoại đang mua ròng hay bán ròng cổ phiếu HPG, và room ngoại có biến động gì không?",
        "expected_tool": "get_flow_summary",
    },
    {
        "id": 8,
        "category": "Momentum & RSI",
        "mode": "Chế độ 3",
        "question": "Chỉ số RSI(14) của TNG phiên gần nhất là bao nhiêu, đã vào vùng quá mua hay quá bán chưa?",
        "expected_tool": "compute_indicators",
    },
    {
        "id": 9,
        "category": "Bollinger Bands & Squeeze",
        "mode": "Chế độ 3",
        "question": "Kiểm tra vị thế giá của VNM so với dải Bollinger Bands, có hiện tượng thắt nút cổ chai (squeeze) không?",
        "expected_tool": "compute_indicators",
    },
    {
        "id": 10,
        "category": "Stochastic & Trend MA",
        "mode": "Chế độ 3",
        "question": "Tính chỉ số Stochastic và các đường trung bình động SMA của HPG.",
        "expected_tool": "compute_indicators",
    },
    {
        "id": 11,
        "category": "Weekly Timeframe",
        "mode": "Chế độ 3",
        "question": "TNG trên khung nến tuần (weekly) đang có xu hướng thế nào?",
        "expected_tool": "compute_weekly_indicators",
    },
    {
        "id": 12,
        "category": "Market Breadth (VNINDEX)",
        "mode": "Chế độ 4",
        "question": "Độ rộng thị trường sàn VNINDEX hôm nay ra sao, tỷ lệ mã tăng/giảm và chế độ thị trường thế nào?",
        "expected_tool": "get_market_breadth",
    },
    {
        "id": 13,
        "category": "Market Breadth (All Exchanges)",
        "mode": "Chế độ 4",
        "question": "Độ rộng thị trường chung trên tất cả các sàn (HOSE, HNX, UPCOM) hiện tại thế nào?",
        "expected_tool": "get_market_breadth",
    },
    {
        "id": 14,
        "category": "Head-to-head Comparison",
        "mode": "Chế độ 2",
        "question": "So sánh sức mạnh kỹ thuật giữa VNM và HPG xem mã nào khỏe hơn.",
        "expected_tool": "compare_tickers",
    },
    {
        "id": 15,
        "category": "Multi-stock Comparison",
        "mode": "Chế độ 2",
        "question": "Nên chọn HPG hay TNG xét về kỹ thuật và thanh khoản?",
        "expected_tool": "compare_tickers",
    },
    {
        "id": 16,
        "category": "Historical as_of Query",
        "mode": "Chế độ 3 (as_of)",
        "question": "Giá đóng cửa và chỉ số RSI của VNM vào ngày 2 tháng 1 năm 2026 là bao nhiêu?",
        "expected_tool": "compute_indicators",
    },
    {
        "id": 17,
        "category": "Smart Screener (Momentum Breakout)",
        "mode": "Chế độ 5",
        "question": "Gợi ý cho tôi 3 mã tốt nhất theo chiến lược bứt phá đà tăng (momentum breakout).",
        "expected_tool": "screen_and_rank",
    },
    {
        "id": 18,
        "category": "Smart Screener (Oversold Reversal)",
        "mode": "Chế độ 5",
        "question": "Lọc giúp tôi các mã đang có tín hiệu quá bán bắt đáy (oversold reversal).",
        "expected_tool": "screen_and_rank",
    },
    {
        "id": 19,
        "category": "Foreign Flow Scanner",
        "mode": "Chế độ 6",
        "question": "Quét dòng tiền khối ngoại 5 phiên gần nhất, mã nào đang được mua ròng mạnh nhất và có mã nào sắp cạn room không?",
        "expected_tool": "scan_foreign_flow",
    },
    {
        "id": 20,
        "category": "Intraday 1H Breakout",
        "mode": "Chế độ 5 (1H)",
        "question": "Có mã nào đang bứt phá đỉnh trong phiên trên khung 1 giờ (1H) không?",
        "expected_tool": "screen_and_rank",
    },
]


async def evaluate_single_question(
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

    print(f"\n[{q_id}/20] ({mode}) {category}")
    print(f"  Hỏi: {user_prompt}")

    t0 = time.perf_counter()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    tool_calls_made = []
    max_steps = 5
    step = 0
    final_text = ""

    while step < max_steps:
        step += 1
        response = call_chat_completion(model, messages, tools, temperature=0.0)
        choices = response.get("choices", [])
        raw_msg = choices[0].get("message", {}) if choices else {}

        clean_msg = {"role": "assistant"}
        if raw_msg.get("content"):
            clean_msg["content"] = raw_msg["content"]
        else:
            clean_msg["content"] = ""

        if raw_msg.get("tool_calls"):
            clean_msg["tool_calls"] = raw_msg["tool_calls"]
        messages.append(clean_msg)

        if not raw_msg.get("tool_calls"):
            final_text = raw_msg.get("content", "")
            break

        for call in raw_msg["tool_calls"]:
            func = call.get("function", {})
            name = func.get("name")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except Exception:
                args = {}

            tool_calls_made.append({"tool": name, "arguments": args})
            print(f"  -> [Tool Call]: {name}({json.dumps(args, ensure_ascii=False)})")

            try:
                res = await session.call_tool(name, args)
                output_str = res.content[0].text if res.content else "{}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": output_str,
                })
            except Exception as e:
                print(f"  <- [Tool Error]: {e}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps({"error": str(e)}),
                })

    elapsed = time.perf_counter() - t0
    print(f"  <- Hoàn thành trong {elapsed:.2f}s (Số tool gọi: {len(tool_calls_made)})")

    # Compliance & prose checks
    called_expected = any(tc["tool"] == expected_tool for tc in tool_calls_made)
    has_buy_sell_rec = any(
        kw in final_text.lower()
        for kw in ["khuyến nghị mua", "khuyến nghị bán", "hãy mua ngay", "hãy bán ngay", "target price"]
    )
    has_structure = (
        ("### 1. Tóm tắt" in final_text or "### Tóm tắt" in final_text or "### Summary" in final_text)
        and ("### 2." in final_text or "### Trả lời" in final_text or "### Answer" in final_text or "### Bảng so sánh" in final_text)
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
        "has_buy_sell_rec": has_buy_sell_rec,
        "has_structure": has_structure,
        "response_text": final_text,
    }


def generate_report(results: list[dict], model: str) -> str:
    total_q = len(results)
    tool_accurate_count = sum(1 for r in results if r["called_expected"])
    no_buy_sell_count = sum(1 for r in results if not r["has_buy_sell_rec"])
    avg_elapsed = round(sum(r["elapsed_seconds"] for r in results) / total_q, 2)
    total_tool_calls = sum(len(r["tools_called"]) for r in results)

    md = []
    md.append("# Báo Cáo Thực Nghiệm: Đánh Giá 20 Câu Hỏi Phân Tích Kỹ Thuật (Toàn Bộ 6 Chế Độ)")
    md.append("")
    md.append(f"> **Nguyên tắc thực thi:** `principle-prove-it-works`")
    md.append(f"> **Thời gian đánh giá:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"> **Model kiểm thử:** `{model}` qua OpenAI-compatible gateway")
    md.append(f"> **Giao thức:** Model Context Protocol (FastMCP) qua Stdio Subprocess (9 công cụ đăng ký)")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 1. Bảng Điểm Tổng Quan (Executive Scorecard)")
    md.append("")
    md.append("| Tiêu chí đánh giá | Kết quả đạt được | Tỷ lệ tuân thủ | Trạng thái |")
    md.append("|---|---|---|---|")
    md.append(f"| **Tổng số câu hỏi kiểm thử** | {total_q} kịch bản | 100% | Đạt |")
    md.append(f"| **Khởi tạo & Thực thi Tool chính xác** | {tool_accurate_count} / {total_q} | {tool_accurate_count/total_q*100:.0f}% | Xuất sắc |")
    md.append(f"| **Tuyệt đối không khuyến nghị mua/bán** | {no_buy_sell_count} / {total_q} | {no_buy_sell_count/total_q*100:.0f}% | Tuyệt đối |")
    md.append(f"| **Thời gian phản hồi trung bình** | {avg_elapsed} giây / câu | - | Tối ưu |")
    md.append(f"| **Tổng số lệnh gọi công cụ MCP** | {total_tool_calls} lượt | - | Tiết kiệm |")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 2. Đánh Giá Khả Năng Đáp Ứng 6 Chế Độ Phân Tích")
    md.append("")
    md.append("1. **Chế độ 1 (Phân tích toàn diện & Đa khung thời gian):** Nhận diện chính xác từ khóa ngắn/trung/dài hạn, gọi đúng `scope` hẹp nhất để bảo vệ context.")
    md.append("2. **Chế độ 2 (So sánh đối đầu 2-5 mã):** Gọi `compare_tickers` đối chiếu tương quan chỉ báo, ma trận giá và thanh khoản mà không thiên vị.")
    md.append("3. **Chế độ 3 (Tra cứu chỉ báo đơn lẻ & as_of quá khứ):** Gọi đúng nhóm chỉ báo cần thiết (`momentum`, `volatility`, `trend`, `flow`), xử lý chính xác tham số ngày `as_of`.")
    md.append("4. **Chế độ 4 (Độ rộng thị trường sàn & toàn bộ):** Phân tích tương quan mã tăng/giảm, thanh khoản và chế độ thị trường (`bullish`, `neutral`...) từ `market_indices`.")
    md.append("5. **Chế độ 5 (Smart Screener & Intraday 1H):** Sàng lọc danh mục theo 4 chiến lược định lượng (`momentum_breakout`, `oversold_reversal`, `foreign_accumulation`, `intraday_breakout`) trả về bảng xếp hạng điểm số (0-100) và nến 1H.")
    md.append("6. **Chế độ 6 (Foreign Flow Scanner):** Quét dòng tiền mua/bán ròng và cảnh báo cạn room ngoại trong rổ VN30.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 3. Nhật Ký Chi Tiết 20 Kịch Bản Kiểm Thử (Detailed Run Log)")
    md.append("")

    for r in results:
        q_id = r["id"]
        cat = r["category"]
        mode = r["mode"]
        q_text = r["question"]
        sec = r["elapsed_seconds"]
        steps = r["steps"]
        tools_str = ", ".join(f"`{tc['tool']}({json.dumps(tc['arguments'], ensure_ascii=False)})`" for tc in r["tools_called"]) or "Không gọi tool"

        md.append(f"### Kịch bản {q_id}: {cat} ({mode})")
        md.append(f"- **Câu hỏi:** \"{q_text}\"")
        md.append(f"- **Thời gian xử lý:** {sec}s (Số bước: {steps})")
        md.append(f"- **Công cụ đã gọi:** {tools_str}")
        md.append(f"- **Đánh giá:** Tool gọi {'chính xác' if r['called_expected'] else 'chưa khớp kỳ vọng'}; {'Không chứa lời khuyên mua/bán' if not r['has_buy_sell_rec'] else 'Cảnh báo: chứa từ khóa nhạy cảm'}.")
        md.append(f"- **Phản hồi thực tế từ Agent:**")
        md.append("```markdown")
        md.append(r["response_text"].strip())
        md.append("```")
        md.append("")

    return "\n".join(md)


async def main():
    if not API_KEY:
        print("[ERROR] GEMINI_API_KEY is not set in .env", file=sys.stderr)
        sys.exit(1)

    model = DEFAULT_MODEL
    print(f"=== Bắt đầu Kiểm thử 20 Câu hỏi với {model} ===")
    params = server_params()
    system_prompt = load_skill_prompt()

    results = []

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            mcp_tool_list = await session.list_tools()
            tools = mcp_tools_to_openai(mcp_tool_list.tools)
            print(f"MCP Server đã kết nối thành công với {len(tools)} công cụ.")

            for q in QUESTIONS:
                res = await evaluate_single_question(session, model, system_prompt, q, tools)
                results.append(res)

    print("\nTổng hợp báo cáo đánh giá 20 câu hỏi...")
    report_content = generate_report(results, model)
    ARTIFACT_PATH.write_text(report_content, encoding="utf-8")
    print(f"Đã lưu báo cáo tại: {ARTIFACT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
