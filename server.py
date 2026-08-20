#!/usr/bin/env python3
"""Zero-dependency MCP server: expose Grok's (xAI) web search to MCP clients.

Implements the Model Context Protocol (stdio transport) using only the Python
standard library, so it needs no pip install and works immediately. It shells
out to the locally-installed `grok` CLI in headless mode, reusing the existing
OAuth login in ~/.grok/auth.json.

The stdio transport auto-detects framing:
  - LSP-style `Content-Length` headers (original MCP framing)
  - newline-delimited JSON (one JSON object per line, used by Claude Code)

Run:
    python3 server.py            # serve MCP over stdio

The `grok` CLI is located automatically via $GROK_BIN or $PATH; set GROK_BIN
to override (e.g. GROK_BIN=/path/to/grok python3 server.py).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "grok-search"
SERVER_VERSION = "0.3.0"

DEFAULT_TIMEOUT = int(os.environ.get("GROK_SEARCH_TIMEOUT", "180"))


def find_grok() -> str:
    """Locate the `grok` CLI.

    Order of preference:
      1. $GROK_BIN (explicit override)
      2. The `grok` binary on $PATH
    Raises a clear error if none is found.
    """
    explicit = os.environ.get("GROK_BIN")
    if explicit:
        return explicit
    found = shutil.which("grok")
    if found:
        return found
    raise RuntimeError(
        "Could not find the `grok` CLI. Install it (e.g. `curl -fsSL "
        "https://grok.dev/install | bash` or your package manager), put it on "
        "$PATH, or set GROK_BIN=/path/to/grok."
    )


# ---------------------------------------------------------------------------
# MCP stdio transport. Supports two frame encodings which hosts use:
#   - LSP-style headers: `Content-Length: <n>\r\n\r\n` + JSON body
#   - newline-delimited JSON: one JSON object per line (used by Claude Code)
# The mode is detected on the first inbound message and mirrored on writes so
# the client can always parse our responses.
# ---------------------------------------------------------------------------
FRAMING = {"mode": "lsp"}


def read_message() -> dict | None:
    """Read one JSON-RPC message from stdin. Returns None on clean EOF."""
    # Peek the first non-empty line to decide the framing.
    first = sys.stdin.buffer.readline()
    if not first:
        return None
    first = first.strip()
    if not first:
        # Leading blank line(s) precede an LSP header block; skip them.
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            line = line.strip()
            if line:
                break
        first = line

    if first.startswith(b"{"):
        # Newline-delimited JSON: the whole line is the message.
        FRAMING["mode"] = "ndjson"
        return json.loads(first.decode("utf-8"))

    # LSP-style framing: parse `Content-Length` headers.
    content_length = None
    line = first
    while True:
        key, _, val = line.partition(b":")
        if key.strip().lower() == b"content-length":
            content_length = int(val.strip())
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            # blank line ends headers
            break
    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    if len(body) != content_length:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    if FRAMING["mode"] == "ndjson":
        # Mirror the client's framing: newline-delimited JSON.
        sys.stdout.buffer.write(body + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Grok invocation
# ---------------------------------------------------------------------------
def run_grok(prompt: str, max_turns: int = 3) -> str:
    grok_bin = find_grok()
    cmd = [
        grok_bin,
        "-p", prompt,
        "--no-alt-screen",
        "--permission-mode", "bypassPermissions",
        "--no-memory",
        "--max-turns", str(max_turns),
        "--output-format", "plain",
    ]
    # CRITICAL: the `grok` CLI discovers MCP servers from Claude Code's
    # ~/.claude.json (and project .mcp.json). Since THIS server is registered
    # there, every `grok -p` call would start ANOTHER instance of this server,
    # which in turn calls `grok -p` again — infinite recursion, and searches
    # never return. Isolate grok's HOME (carrying over the OAuth login from
    # ~/.grok) and run it from an empty temp dir so it loads no MCP config at
    # all. This matches what Claude Code's own MCP isolation expects.
    iso_home = tempfile.mkdtemp(prefix="grok-search-mcp-")
    try:
        iso_grok = os.path.join(iso_home, ".grok")
        shutil.copytree(
            os.path.join(os.path.expanduser("~"), ".grok"), iso_grok,
            ignore=shutil.ignore_patterns(
                "sessions", "memtrace", "logs", "relocations",
                "marketplace-cache", "downloads", "*.lock",
            ),
        )
        env = {
            **os.environ,
            "HOME": iso_home,
            "NO_COLOR": "1",
        }
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
                cwd=iso_home, env=env,
                # Run in a fresh process group so a timed-out grok can be killed
                # together with its worker/session processes (it spawns children).
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            # Kill the entire process group, not just the direct child.
            try:
                os.killpg(exc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise RuntimeError(f"grok search timed out after {DEFAULT_TIMEOUT}s")
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
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
                    "description": "Cap on Grok's agent search turns (default 3).",
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
        max_turns = int(arguments.get("max_turns", 3))
        # Important: phrase this as a neutral research question and only *permit*
        # web search. Hard commands like "you MUST search" or "use web search"
        # push Grok into a multi-turn agentic search loop that never converges
        # within a practical timeout, so calls end up returning nothing. Giving it
        # permission ("may use web search") lets it answer directly when it already
        # knows, and search only when the answer truly needs live information.
        prompt = (
            "Answer the research question below in concise Markdown. Cite sources "
            "as `[source: URL]` where relevant. If the question needs current or "
            "live information, you may use web search; otherwise answer from "
            "knowledge.\n\n"
            f"RESEARCH QUESTION:\n{query}"
        )
        content = run_grok(prompt, max_turns=max_turns)
    elif name == "grok_fetch":
        url = str(arguments.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        prompt = (
            "Read the web page at the URL below and summarize its key content in "
            "Markdown: facts, figures, and main points. If it cannot be read, say "
            "so plainly.\n\n"
            f"URL:\n{url}"
        )
        content = run_grok(prompt, max_turns=3)
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
