"""Drive the MCP server as a real stdio client, the way OpenHarness does.

This is the Phase 3 acceptance check: spawn `python -m mcp_server.server`, speak
MCP over stdio, list the tools, call all three, and print what came back.

    uv run python scripts/mcp_smoke.py [--ticker VNM]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


def server_params() -> StdioServerParameters:
    env = dict(os.environ)
    env.setdefault("TA_AGENT_DB", str(REPO_ROOT / "var" / "ta.sqlite"))
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(REPO_ROOT),
        env=env,
    )


def _newly_listed_rows() -> list[dict]:
    """Three rows, standing in for a ticker that listed this week."""
    return [
        {
            "date": day,
            "prev_close": prev,
            "close": close,
            "total_trade": 80,
            "total_value": close * 80_000,
            "total_volume": 80_000,
            "buy_count": 45,
            "sell_count": 35,
            "buy_volume": 42_000,
            "sell_volume": 38_000,
            "foreign_buy_volume": 0,
            "foreign_sell_volume": 0,
            "foreign_buy_value": 0,
            "foreign_sell_value": 0,
            "foreign_room": 3_000_000,
        }
        for day, prev, close in (
            ("2026-03-04", 15_000.0, 15_400.0),
            ("2026-03-05", 15_400.0, 15_250.0),
            ("2026-03-06", 15_250.0, 15_600.0),
        )
    ]


def text_of(result) -> str:
    for block in result.content:
        if getattr(block, "text", None):
            return block.text
    return ""


async def run(ticker: str) -> int:
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"connected: {info.server_info.name} {info.server_info.version}")

            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            prices = await session.call_tool(
                "get_price_data", {"ticker": ticker, "lookback_days": 5}
            )
            payload = json.loads(text_of(prices))
            print(
                f"get_price_data: {payload.get('row_count')} rows "
                f"{payload.get('date_range')} last_close="
                f"{payload['rows'][-1]['close'] if payload.get('rows') else None}"
            )

            flow = await session.call_tool("get_flow_summary", {"ticker": ticker, "window": 5})
            flow_payload = json.loads(text_of(flow))
            print(
                "get_flow_summary: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in flow_payload.items()
                    if k
                    in (
                        "buy_sell_volume_imbalance_avg",
                        "buy_sell_count_imbalance_avg",
                        "foreign_net_value_cum",
                        "foreign_participation_ratio_avg",
                        "foreign_room_trend",
                    )
                )
            )
            for note in flow_payload.get("notes", []):
                print(f"  note: {note}")

            indicators = await session.call_tool(
                "compute_indicators",
                {
                    "ticker": ticker,
                    "groups": ["trend", "momentum", "volatility"],
                    "series_tail": 0,
                },
            )
            groups = json.loads(text_of(indicators))["groups"]
            print(
                "compute_indicators: "
                f"sma_20={groups['trend']['sma_20']['latest']:.1f} "
                f"adx_14={groups['trend']['adx_14']['latest']['adx']:.2f} "
                f"rsi_14={groups['momentum']['rsi_14']['latest']:.2f} "
                f"stoch_k={groups['momentum']['stoch_14_3']['latest']['k']:.1f} "
                f"macd={groups['trend']['macd']['latest']['macd']:.2f} "
                f"atr_14={groups['volatility']['atr_14']['latest_atr']:.1f}"
            )

            # Multi-timeframe check (Weekly)
            weekly_ind = await session.call_tool(
                "compute_indicators",
                {
                    "ticker": ticker,
                    "timeframe": "1W",
                    "lookback_days": 50,
                    "groups": ["trend", "momentum"],
                },
            )
            w_groups = json.loads(text_of(weekly_ind))["groups"]
            print(
                f"weekly indicators: sma_20={w_groups['trend']['sma_20']['latest']:.1f} "
                f"rsi_14={w_groups['momentum']['rsi_14']['latest']:.2f}"
            )

            # A newly listed ticker: three rows passed inline, so every window
            # longer than three rows must report insufficient_data, not a number.
            short = await session.call_tool(
                "compute_indicators",
                {"rows": _newly_listed_rows(), "groups": ["trend", "momentum"]},
            )
            short_payload = json.loads(text_of(short))
            trend = short_payload["groups"]["trend"]
            momentum = short_payload["groups"]["momentum"]
            print("short history (3 rows):")
            print(f"  sma_200 -> {trend['sma_200']['reason']}")
            print(f"  macd    -> {trend['macd']['reason']}")
            print(f"  rsi_14  -> {momentum['rsi_14']['reason']}")

            unsupported = await session.call_tool(
                "compute_indicators", {"ticker": ticker, "groups": ["sentiment_group"]}
            )
            print(
                f"unsupported group 'sentiment_group' rejected: {unsupported.is_error}, "
                f"message: {text_of(unsupported)[:80]}"
            )
            bad = await session.call_tool("get_price_data", {"ticker": "not a ticker"})
            print(f"bad ticker rejected: {bad.is_error}, message: {text_of(bad)[:80]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="VNM")
    args = parser.parse_args()
    return asyncio.run(run(args.ticker))


if __name__ == "__main__":
    raise SystemExit(main())
