#!/usr/bin/env python3
"""End-to-end check of the agent loop with a scripted model instead of a real one.

Answers one question: if a model provider were plugged in, would the loop
actually work? It starts `scripts/mock_model.py`, runs `oh` against it, and then
checks the three things that have to be true:

1. `oh` advertised our MCP tools to the model (`mcp__ta-agent__*`),
2. `oh` executed the tool the model asked for and fed the result back,
3. the value the model quoted also appears in the audit log.

    uv run python scripts/loop_smoke.py

Nothing here needs credentials, and nothing contacts a real API. A failure at
step 1 is almost always the MCP client version (see the hint it prints); a
failure at step 3 means a number reached the model that the audit trail cannot
account for, which is the one outcome this project must not allow.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TOOL = "mcp__ta-agent__get_flow_summary"
TOOL_ARGS = {"ticker": "VNM", "window": 5}
PROMPT = "Are foreigners buying VNM this week?"


def load_mock_model():
    spec = importlib.util.spec_from_file_location(
        "mock_model", REPO_ROOT / "scripts" / "mock_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_dir() -> Path:
    return Path(os.environ.get("OPENHARNESS_CONFIG_DIR") or Path.home() / ".openharness")


def audit_log_path() -> Path:
    """Where the *configured* MCP server writes its audit log.

    Read from settings.json rather than assumed, because that is the file `oh`
    will actually launch, and its env block decides the path.
    """
    settings = config_dir() / "settings.json"
    if settings.exists():
        entry = (
            json.loads(settings.read_text(encoding="utf-8"))
            .get("mcp_servers", {})
            .get("ta-agent", {})
        )
        configured = (entry.get("env") or {}).get("TA_AGENT_AUDIT_LOG")
        if configured:
            return Path(configured)
    return REPO_ROOT / "logs" / "tool_calls.jsonl"


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{f': {detail}' if detail else ''}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oh", default=shutil.which("oh"), help="path to the oh executable")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--keep-transcript", action="store_true")
    args = parser.parse_args(argv)

    if not args.oh:
        print("oh not found on PATH; install it with: uv tool install openharness-ai")
        return 2

    mock_model = load_mock_model()
    transcript = REPO_ROOT / "logs" / "loop_smoke_requests.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("", encoding="utf-8")

    httpd = mock_model.make_server(0, tool=TOOL, tool_args=TOOL_ARGS, transcript=transcript)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    base_url = f"http://{host}:{port}"

    audit = audit_log_path()
    before = line_count(audit)
    print(f"mock model on {base_url}; audit log {audit}")

    started = time.time()
    try:
        completed = subprocess.run(
            [
                args.oh,
                "-p",
                PROMPT,
                "--base-url",
                base_url,
                "--api-key",
                "mock-not-a-real-key",
                "--api-format",
                "anthropic",
                "--permission-mode",
                "full_auto",
                "--max-turns",
                "4",
            ],
            capture_output=True,
            text=True,
            timeout=args.timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        print(f"FAIL oh did not finish within {args.timeout}s")
        return 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    answer = completed.stdout.strip()
    requests = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    print(f"oh exited {completed.returncode} in {time.time() - started:.1f}s, "
          f"{len(requests)} model request(s)")
    print(f"answer: {answer.splitlines()[-1] if answer else '(none)'}")
    print("checks:")

    results = []
    results.append(check(bool(requests), "oh called the model endpoint"))
    advertised = (
        [t.get("name", "") for t in requests[0]["body"].get("tools") or []] if requests else []
    )
    ours = [name for name in advertised if name.startswith("mcp__ta-agent__")]
    ok_tools = check(
        len(ours) == 3, "all three MCP tools advertised to the model", ", ".join(ours) or "none"
    )
    results.append(ok_tools)
    if not ok_tools:
        print(
            "\n  hint: oh 0.1.9 reads `tool.inputSchema`, which the MCP SDK renamed in 2.x, so\n"
            "  every MCP server fails to connect. Pin its client:\n"
            "    uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force\n"
        )

    system_prompt = json.dumps(requests[0]["body"].get("system")) if requests else ""
    results.append(check("technical-analysis" in system_prompt, "the skill is in the system prompt"))

    tool_results = mock_model.tool_results_for(requests[-1]["body"], TOOL) if requests else []
    results.append(check(bool(tool_results), "the tool ran and its result went back to the model"))

    payload = {}
    if tool_results:
        try:
            payload = json.loads(mock_model.result_text(tool_results[-1]))
        except json.JSONDecodeError:
            pass
    quoted = payload.get("foreign_room_trend")
    results.append(
        check(
            quoted is not None and str(quoted) in answer,
            "the answer quotes a value from the tool result",
            f"foreign_room_trend={quoted}",
        )
    )

    entries = []
    if audit.exists():
        entries = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()[before:]
        ]
    logged = [e for e in entries if e.get("tool") == TOOL.rsplit("__", 1)[-1]]
    results.append(
        check(
            bool(logged) and logged[-1].get("result", {}).get("foreign_room_trend") == quoted,
            "the same value is in the audit log",
            f"{len(entries)} new entr(y/ies)",
        )
    )

    if not args.keep_transcript:
        transcript.unlink(missing_ok=True)

    if all(results):
        print("\nRESULT ok - the loop works; plug in a real provider with `oh setup`")
        return 0
    print("\nRESULT failed")
    if completed.stderr.strip():
        print("--- oh stderr ---")
        print(completed.stderr.strip()[-2000:])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
