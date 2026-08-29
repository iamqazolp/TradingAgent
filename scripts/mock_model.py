#!/usr/bin/env python3
"""A local stand-in for the Anthropic Messages API, for testing the agent loop.

It answers `POST /v1/messages` with a streamed response, exactly as the real API
does, but the content is scripted rather than generated. That makes it possible
to prove the whole loop end to end with no credentials and no tokens spent:
`oh` starts our MCP server, advertises the tools to the "model", the model asks
for one, `oh` runs it, and the result comes back into the conversation.

    uv run python scripts/mock_model.py --port 8770 --transcript /tmp/req.jsonl &
    oh -p "5-day flow on VNM" --base-url http://127.0.0.1:8770 --api-key test \
       --permission-mode full_auto --max-turns 4

The script is fixed: while the requested tool has not been called yet, reply with
a `tool_use` for it; once a `tool_result` for it appears in the conversation,
reply with text quoting a value out of that result. If the tool was never
advertised in the request, say so in plain text instead of pretending — that is
the failure this script exists to catch, so it must not pass silently.

Every request body is appended to the transcript file, so what `oh` actually
sent (system prompt, tool schemas, tool results) can be inspected afterwards.

This is a test double. It is not the model, it does not think, and it must not be
exposed beyond localhost.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_TOOL = "mcp__ta-agent__get_flow_summary"
DEFAULT_ARGS = {"ticker": "VNM", "window": 5}


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


def tool_names(body: dict) -> list[str]:
    return [t.get("name", "") for t in body.get("tools", []) or []]


def tool_results_for(body: dict, name: str) -> list[dict]:
    """Every tool_result in the conversation that answers a call to `name`.

    Matched through the assistant's `tool_use` ids, since a tool_result carries
    only the id, not the tool name.
    """
    ids = set()
    for message in body.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == name:
                ids.add(block.get("id"))
    results = []
    for message in body.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("tool_use_id") in ids:
                    results.append(block)
    return results


def result_text(result: dict) -> str:
    """The text payload of a tool_result, whatever shape it arrived in."""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def summarise(result: dict, tool: str) -> str:
    """A short answer that quotes real numbers out of the tool result.

    Quoting from the payload rather than inventing prose is the point: if the
    text below contains the numbers, the value made it all the way back through
    `oh` into the model's context.
    """
    text = result_text(result)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return f"[mock model] {tool} returned non-JSON content: {text[:200]!r}"
    if not isinstance(payload, dict):
        return f"[mock model] {tool} returned {type(payload).__name__}, not an object"
    keys = [
        "foreign_net_value_window",
        "foreign_participation_ratio_avg",
        "foreign_room_trend",
        "buy_sell_volume_imbalance_avg",
    ]
    quoted = ", ".join(f"{k}={payload[k]!r}" for k in keys if k in payload)
    if not quoted:
        # Not the flow tool: quote whatever scalars are at the top level.
        quoted = ", ".join(
            f"{k}={v!r}"
            for k, v in list(payload.items())[:6]
            if isinstance(v, (int, float, str))
        )
    return f"[mock model] {tool} answered: {quoted or '(no scalar fields)'}"


class Handler(BaseHTTPRequestHandler):
    server_version = "MockAnthropicMessages/1.0"

    # Set by make_server.
    tool = DEFAULT_TOOL
    tool_args: dict = DEFAULT_ARGS
    transcript: Path | None = None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._error(400, "request body is not JSON")
            return

        if self.transcript:
            with self.transcript.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": self.path, "body": body}) + "\n")

        if not self.path.rstrip("/").endswith("/v1/messages"):
            self._error(404, f"no handler for {self.path}")
            return

        available = tool_names(body)
        results = tool_results_for(body, self.tool)

        if results:
            self._stream_text(body, summarise(results[-1], self.tool))
        elif self.tool in available:
            self._stream_tool_use(body)
        else:
            self._stream_text(
                body,
                f"[mock model] tool {self.tool!r} was not advertised to me. "
                f"I was given: {available or 'no tools at all'}",
            )

    # ------------------------------------------------------------------ streams

    def _message_start(self, body: dict) -> bytes:
        return sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "mock-model"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        )

    def _finish(self, stop_reason: str) -> bytes:
        return sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ) + sse("message_stop", {"type": "message_stop"})

    def _stream_text(self, body: dict, text: str) -> None:
        self._begin_stream()
        self.wfile.write(self._message_start(body))
        self.wfile.write(
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        self.wfile.write(
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        self.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        self.wfile.write(self._finish("end_turn"))
        self.wfile.flush()

    def _stream_tool_use(self, body: dict) -> None:
        self._begin_stream()
        self.wfile.write(self._message_start(body))
        self.wfile.write(
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_mock_1",
                        "name": self.tool,
                        "input": {},
                    },
                },
            )
        )
        self.wfile.write(
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(self.tool_args),
                    },
                },
            )
        )
        self.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        self.wfile.write(self._finish("tool_use"))
        self.wfile.flush()

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _error(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"mock_model: {fmt % args}\n")


def make_server(
    port: int,
    *,
    tool: str = DEFAULT_TOOL,
    tool_args: dict | None = None,
    transcript: Path | None = None,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "tool": tool,
            "tool_args": dict(tool_args or DEFAULT_ARGS),
            "transcript": transcript,
        },
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--tool", default=DEFAULT_TOOL, help="tool name to request")
    parser.add_argument(
        "--tool-args", default=json.dumps(DEFAULT_ARGS), help="JSON arguments for that tool"
    )
    parser.add_argument("--transcript", help="append every request body to this JSONL file")
    args = parser.parse_args(argv)

    httpd = make_server(
        args.port,
        tool=args.tool,
        tool_args=json.loads(args.tool_args),
        transcript=Path(args.transcript) if args.transcript else None,
    )
    print(
        f"mock Anthropic endpoint on http://127.0.0.1:{args.port} "
        f"(will request {args.tool} with {args.tool_args})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
