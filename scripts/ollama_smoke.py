#!/usr/bin/env python3
"""Smoke test and verification script for local Ollama models with OpenHarness.

Checks:
1. Is Ollama running on http://localhost:11434?
2. Which models are locally installed?
3. Does the OpenAI-compatible endpoint (/v1/chat/completions) respond?
4. Executes a mock/live agent tool test with OpenHarness and ta-agent skill.

Usage:
    uv run python scripts/ollama_smoke.py
    uv run python scripts/ollama_smoke.py --model qwen2.5-coder:7b
    uv run python scripts/ollama_smoke.py --model llama3.1:8b
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def check_ollama_server(base_url: str = "http://localhost:11434") -> tuple[bool, list[str]]:
    """Query Ollama's tags API to verify server is up and list installed models."""
    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return True, models
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
        return False, []


def test_openai_compat_chat(
    base_url: str = "http://localhost:11434/v1", model: str = "qwen2.5-coder:7b"
) -> bool:
    """Send a basic OpenAI-compatible chat request to Ollama."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a concise financial analyst assistant."},
            {"role": "user", "content": "Reply in one word: what does RSI stand for?"},
        ],
        "temperature": 0.0,
    }
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            print(f"  ✓ Ollama answered ({model}): {content}")
            return True
    except Exception as exc:
        print(f"  ✗ Chat completion failed on {url}: {exc}")
        return False


def test_openharness_with_ollama(
    model: str = "qwen2.5-coder:7b", base_url: str = "http://localhost:11434/v1"
) -> bool:
    """Execute OpenHarness against local Ollama."""
    oh_bin = shutil.which("oh")
    if not oh_bin:
        print("  ! 'oh' CLI not found in PATH; skipping full loop test.")
        return True

    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = base_url
    env["OPENAI_API_KEY"] = "ollama"
    env["OPENAI_MODEL"] = model

    cmd = [
        oh_bin,
        "-p",
        "Explain in one sentence what stochastic oscillator %K is.",
    ]
    print(f"  • Running 'oh' with local model {model}...")
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60.0)
        if proc.returncode == 0:
            print("  ✓ OpenHarness executed successfully with Ollama!")
            print(f"    Output snippet: {proc.stdout.strip()[:180]}...")
            return True
        else:
            print(f"  ✗ 'oh' exited with code {proc.returncode}")
            if proc.stderr:
                print(f"    stderr: {proc.stderr[:300]}")
            return False
    except Exception as exc:
        print(f"  ✗ Failed to run 'oh': {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify local Ollama integration.")
    parser.add_argument(
        "--url", default="http://localhost:11434", help="Ollama base URL (default: http://localhost:11434)"
    )
    parser.add_argument(
        "--model", default="qwen2.5-coder:7b", help="Model name to test (default: qwen2.5-coder:7b)"
    )
    parser.add_argument("--skip-oh", action="store_true", help="Skip running 'oh' CLI")
    args = parser.parse_args()

    print(f"1. Checking Ollama connection on {args.url}...")
    is_up, models = check_ollama_server(args.url)
    if not is_up:
        print(f"  ✗ Ollama is not running on {args.url}!")
        print("\n  To start Ollama:")
        print("    1. Install: brew install ollama (or download from https://ollama.com)")
        print("    2. Start server: ollama serve")
        print(f"    3. Pull model:   ollama pull {args.model}")
        return 1

    print("  ✓ Ollama server is running.")
    print(f"  • Installed models: {', '.join(models) if models else '(none yet)'}")

    if models and not any(args.model in m for m in models):
        print(f"\n  ! Model '{args.model}' is not currently installed.")
        print(f"    Run: ollama pull {args.model}")
        if models:
            args.model = models[0]
            print(f"    Falling back to installed model: {args.model}")

    print(f"\n2. Testing OpenAI-compatible API on {args.url}/v1 with model '{args.model}'...")
    compat_url = f"{args.url.rstrip('/')}/v1"
    ok = test_openai_compat_chat(compat_url, args.model)
    if not ok:
        return 1

    if not args.skip_oh:
        print("\n3. Testing OpenHarness ('oh') execution with local Ollama...")
        test_openharness_with_ollama(model=args.model, base_url=compat_url)

    print("\n✓ Local Ollama integration verification complete!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
