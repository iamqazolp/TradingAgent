"""Phase 5 validation: engine output vs the independent ground truth.

Reads the stored rows through the same path the MCP server uses, computes every
Tier 0 indicator, and compares each value against
`tests/fixtures/ground_truth.json`.

Pass criteria (from the plan):
  * relative discrepancy under 0.5 percent for Tier 0 indicators
  * zero tolerance for a wrong sign or a wrong order of magnitude

    uv run python scripts/validate.py [--db var/ta.sqlite] [--json]

Writes a full report to logs/validation_report.json and exits non-zero if any
check fails.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so the script runs from any working directory

from data import store  # noqa: E402
from indicators import engine  # noqa: E402

GROUND_TRUTH = REPO_ROOT / "tests" / "fixtures" / "ground_truth.json"
REPORT = REPO_ROOT / "logs" / "validation_report.json"

RELATIVE_TOLERANCE = 0.005  # 0.5 percent

#: ground-truth key -> dotted path inside the engine's compute() payload.
CHECKS: dict[str, str] = {
    "latest_close": "latest_close",
    "sma_20": "groups.trend.sma_20.latest",
    "sma_50": "groups.trend.sma_50.latest",
    "sma_200": "groups.trend.sma_200.latest",
    "ema_12": "groups.trend.ema_12.latest",
    "ema_26": "groups.trend.ema_26.latest",
    "macd.macd": "groups.trend.macd.latest.macd",
    "macd.signal": "groups.trend.macd.latest.signal",
    "macd.histogram": "groups.trend.macd.latest.histogram",
    "rsi_14": "groups.momentum.rsi_14.latest",
    "bollinger_20_2.middle": "groups.volatility.bollinger_20_2.latest.middle",
    "bollinger_20_2.upper": "groups.volatility.bollinger_20_2.latest.upper",
    "bollinger_20_2.lower": "groups.volatility.bollinger_20_2.latest.lower",
    "bollinger_20_2.percent_b": "groups.volatility.bollinger_20_2.latest.percent_b",
    "bollinger_20_2.width": "groups.volatility.bollinger_20_2.latest.width",
    "close_to_close_volatility_20": (
        "groups.volatility.close_to_close_volatility_20.latest"
    ),
    "buy_sell_volume_imbalance": "groups.volume_flow.buy_sell_volume_imbalance_5.latest",
    "buy_sell_volume_imbalance_avg_5": (
        "groups.volume_flow.buy_sell_volume_imbalance_5.latest_rolling_avg"
    ),
    "obv": "groups.volume_flow.obv.latest",
    "buy_sell_count_imbalance": "groups.trade_flow.buy_sell_count_imbalance_5.latest",
    "buy_sell_count_imbalance_avg_5": (
        "groups.trade_flow.buy_sell_count_imbalance_5.latest_rolling_avg"
    ),
    "avg_buy_trade_size": "groups.trade_flow.avg_trade_size_by_side_20.latest.avg_buy_trade_size",
    "avg_sell_trade_size": (
        "groups.trade_flow.avg_trade_size_by_side_20.latest.avg_sell_trade_size"
    ),
    "avg_buy_trade_size_ratio_20": (
        "groups.trade_flow.avg_trade_size_by_side_20.latest.buy_ratio_to_baseline"
    ),
    "avg_sell_trade_size_ratio_20": (
        "groups.trade_flow.avg_trade_size_by_side_20.latest.sell_ratio_to_baseline"
    ),
    "avg_trade_value": "groups.value_flow.avg_trade_value_20.latest",
    "avg_trade_value_ratio_20": "groups.value_flow.avg_trade_value_20.ratio_to_baseline",
    "value_spike_ratio_20": "groups.value_flow.value_spike_20.ratio_to_baseline",
    "foreign_net_volume": "groups.foreign_flow.foreign_net_volume.latest",
    "foreign_net_volume_cum": "groups.foreign_flow.foreign_net_volume.cumulative",
    "foreign_net_value": "groups.foreign_flow.foreign_net_value.latest",
    "foreign_net_value_cum": "groups.foreign_flow.foreign_net_value.cumulative",
    "foreign_participation_ratio": (
        "groups.foreign_flow.foreign_participation_ratio_5.latest"
    ),
    "foreign_participation_ratio_avg_5": (
        "groups.foreign_flow.foreign_participation_ratio_5.latest_rolling_avg"
    ),
    "foreign_room_trend_5": "groups.foreign_flow.foreign_room_trend_5.latest",
}


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path, returning None if any step is missing."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def dig_truth(truth: dict, key: str) -> Any:
    """Ground-truth keys use the same dotted convention for nested groups."""
    return dig(truth, key) if "." in key else truth.get(key)


def compare(expected: Any, actual: Any) -> dict:
    """One check. Returns status plus the numbers behind it."""
    if expected is None and actual is None:
        return {"status": "skip", "reason": "not defined in either implementation"}
    if expected is None or actual is None:
        return {
            "status": "fail",
            "reason": "one implementation has a value and the other does not",
            "expected": expected,
            "actual": actual,
        }
    expected = float(expected)
    actual = float(actual)
    if expected == 0.0 and actual == 0.0:
        return {"status": "pass", "expected": expected, "actual": actual, "relative": 0.0}
    # Absolute tolerance: tiny floating-point artefacts near zero should not fail.
    if abs(expected - actual) < 1e-9:
        return {"status": "pass", "expected": expected, "actual": actual, "relative": 0.0}
    scale = max(abs(expected), abs(actual))
    relative = abs(expected - actual) / scale
    result = {
        "status": "pass",
        "expected": expected,
        "actual": actual,
        "relative": relative,
    }
    if (expected > 0) != (actual > 0) and expected != 0 and actual != 0:
        result["status"] = "fail"
        result["reason"] = "sign mismatch"
        return result
    if actual != 0 and expected != 0:
        magnitude = abs(math.log10(abs(actual / expected)))
        if magnitude >= 1.0:
            result["status"] = "fail"
            result["reason"] = "order-of-magnitude mismatch"
            return result
    if relative > RELATIVE_TOLERANCE:
        result["status"] = "fail"
        result["reason"] = f"relative discrepancy {relative:.4%} exceeds 0.5%"
    return result


def validate(db_path: str | None = None) -> dict:
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    lookback = int(truth.get("lookback_rows", 300))
    conn = store.connect(db_path)
    try:
        report: dict[str, Any] = {
            "lookback_rows": lookback,
            "relative_tolerance": RELATIVE_TOLERANCE,
            "tickers": {},
            "failures": [],
        }
        for ticker, expected in truth["tickers"].items():
            rows = store.get_recent(conn, ticker, lookback)
            if not rows:
                report["failures"].append(f"{ticker}: no rows in the store")
                report["tickers"][ticker] = {"error": "no rows in the store"}
                continue
            computed = engine.compute(rows, series_tail=0)
            checks: dict[str, dict] = {}
            for key, path in CHECKS.items():
                outcome = compare(dig_truth(expected, key), dig(computed, path))
                checks[key] = outcome
                if outcome["status"] == "fail":
                    report["failures"].append(
                        f"{ticker}.{key}: {outcome.get('reason')} "
                        f"(expected {outcome.get('expected')}, got {outcome.get('actual')})"
                    )
            report["tickers"][ticker] = {
                "rows_used": computed["rows_used"],
                "date_range": computed["date_range"],
                "row_count_matches_truth": computed["rows_used"] == expected["rows"],
                "checks": checks,
                "passed": sum(1 for c in checks.values() if c["status"] == "pass"),
                "failed": sum(1 for c in checks.values() if c["status"] == "fail"),
                "skipped": sum(1 for c in checks.values() if c["status"] == "skip"),
            }
            if computed["rows_used"] != expected["rows"]:
                report["failures"].append(
                    f"{ticker}: engine used {computed['rows_used']} rows, ground truth "
                    f"used {expected['rows']}"
                )
        report["ok"] = not report["failures"]
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the engine against ground truth.")
    parser.add_argument("--db", help="SQLite path (default: TA_AGENT_DB or var/ta.sqlite)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    report = validate(args.db)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        for ticker, result in report["tickers"].items():
            if "error" in result:
                print(f"{ticker}: {result['error']}")
                continue
            print(
                f"{ticker}: {result['passed']} passed, {result['failed']} failed, "
                f"{result['skipped']} skipped "
                f"({result['date_range']['start']}..{result['date_range']['end']}, "
                f"{result['rows_used']} rows)"
            )
            worst = sorted(
                (
                    (c.get("relative", 0.0), k)
                    for k, c in result["checks"].items()
                    if c["status"] == "pass" and "relative" in c
                ),
                reverse=True,
            )[:3]
            for relative, key in worst:
                print(f"    largest agreed gap: {key} {relative:.3e}")
        for failure in report["failures"]:
            print(f"FAIL {failure}")
        print(f"\nreport: {REPORT}")
        print("RESULT:", "ok" if report["ok"] else "FAILURES PRESENT")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
