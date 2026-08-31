# Local Ollama Integration Guide

This guide describes how to run the Technical Analysis Agent and OpenHarness (`oh`) completely offline using a local LLM hosted via [Ollama](https://ollama.com).

---

## 1. Prerequisites

Install and start Ollama on your machine:

```bash
# macOS via Homebrew
brew install ollama

# Start the Ollama background service
ollama serve
```

---

## 2. Pull Recommended Models

For technical analysis with MCP tool calling, models with strong native function calling are recommended:

```bash
# Recommended coding & reasoning model (7B parameters)
ollama pull qwen2.5-coder:7b

# Alternative general model (8B parameters)
ollama pull llama3.1:8b
```

---

## 3. Configure `.env`

In your `.env` file (copied from `.env.example`), set the following variables:

```ini
# Point OpenHarness to local Ollama via OpenAI-compatible endpoint
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
OPENAI_MODEL=qwen2.5-coder:7b
```

---

## 4. Verify Local Setup

Run the built-in smoke test script:

```bash
uv run python scripts/ollama_smoke.py --model qwen2.5-coder:7b
```

---

## 5. Running Technical Analysis with `oh`

Once configured, use `oh` to perform technical analysis queries:

```bash
oh -p "Analyze VNM on the daily and hourly timeframes. What does the Ichimoku cloud and foreign flow indicate?"
```
