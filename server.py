#!/usr/bin/env python3
"""Zero-dependency MCP server: expose Grok's (xAI) web search to MCP clients.

Implements the Model Context Protocol (stdio transport, LSP-style
Content-Length framing) using only the Python standard library, so it needs
no pip install and works immediately. It shells out to the locally-installed
`grok` CLI in headless mode, reusing the existing OAuth login in ~/.grok/auth.json.

Run:
    python3 server.py            # serve MCP over stdio
    GROK_BIN=... python3 server.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "grok-search"
SERVER_VERSION = "0.1.0"

GROK_BIN = Path(os.environ.get("GROK_BIN", "/home/sym/.grok/bin/grok"))
DEFAULT_TIMEOUT = int(os.environ.get("GROK_SEARCH_TIMEOUT", "180"))


# ---------------------------------------------------------------------------
# MCP stdio transport: fixed header `Content-Length: <n>\r\n\r\n` + JSON body.
# ---------------------------------------------------------------------------
def read_message() -> dict | None:
    """Read one JSON-RPC message from stdin. Returns None on clean EOF."""
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            # blank line ends headers
            break
        key, _, val = line.partition(b":")
        if key.strip().lower() == b"content-length":
            content_length = int(val.strip())
    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    if len(body) != content_length:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Grok invocation
# ---------------------------------------------------------------------------
def run_grok(prompt: str, max_turns: int = 4) -> str:
    if not GROK_BIN.exists():
        raise RuntimeError(f"grok CLI not found at {GROK_BIN}")
    cmd = [
        str(GROK_BIN),
        "-p", prompt,
        "--no-alt-screen",
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(max_turns),
        "--output-format", "plain",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"grok search timed out after {DEFAULT_TIMEOUT}s")
    out = proc.stdout.strip()
    # grok exits non-zero when it hits --max-turns or otherwise stops before a
    # clean single-turn answer; whatever it already produced is still useful.
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"grok exited {proc.returncode} with no output")
    if not out:
        raise RuntimeError("grok returned no output")
    return out


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "grok_search",
        "description": (
            "Search the web via Grok and return a synthesized, cited answer. "
            "Use for live or current information. Frame the query as a "
            "natural-language research question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Research question or keywords to search.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Cap on Grok's agent search turns (default 4).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "grok_fetch",
        "description": (
            "Fetch and summarize the content of a single http(s) URL using Grok."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full http(s) URL to read.",
                },
            },
            "required": ["url"],
        },
    },
]


def call_tool(name: str, arguments: dict) -> list[dict]:
    if name == "grok_search":
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        max_turns = int(arguments.get("max_turns", 4))
        prompt = (
            "Use your web search tool to research the user's request. Present the "
            "answer in Markdown, cite sources as `[source: URL]`, and clearly "
            "separate established facts from uncertainties. For anything needing a "
            "current/live answer, you MUST search rather than rely on memory.\n\n"
            f"USER REQUEST:\n{query}"
        )
        content = run_grok(prompt, max_turns=max_turns)
    elif name == "grok_fetch":
        url = str(arguments.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        prompt = (
            "Use your web fetch tool to read this URL and summarize its key content "
            "in Markdown: facts, figures, and main points. If it cannot be read, say "
            "so plainly.\n\n"
            f"URL:\n{url}"
        )
        content = run_grok(prompt, max_turns=4)
    else:
        raise ValueError(f"unknown tool: {name}")
    return [{"type": "text", "text": content}]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def handle(message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        capabilities = {
            "tools": {"listChanged": False},
        }
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": capabilities,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Search the web using Grok. Prefer grok_search for live/current "
                "information and grok_fetch to read a specific URL."
            ),
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        tool_result = call_tool(params.get("name", ""), params.get("arguments") or {})
        result = {"content": tool_result, "isError": False}
    elif method == "shutdown":
        sys.exit(0)
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    while True:
        msg = read_message()
        if msg is None:
            break
        # Notifications have no id; respond to requests only.
        if "id" not in msg:
            if msg.get("method") == "notifications/initialized":
                continue
            # notifications/initialized already handled via handle() above being
            # skipped; keep loop simple: just skip any id-less message.
            continue
        try:
            resp = handle(msg)
        except Exception as exc:  # noqa: BLE001 - surface as JSON-RPC error
            resp = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32000, "message": str(exc)},
            }
        if resp is not None:
            write_message(resp)


if __name__ == "__main__":
    main()
