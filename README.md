# grok-search-mcp

A zero-dependency MCP server that exposes **Grok's (xAI) built-in web search and
fetch** to MCP clients such as Claude Code — by shelling out to the locally
installed `grok` CLI and reusing its existing OAuth login.

No API key, no pip install, no network dependencies to run.

## Why

Claude Code's built-in `web_search` tool is implemented server-side by
Anthropic and cannot be redirected to a third-party search engine. Instead, this
project exposes `grok_search` / `grok_fetch` as MCP tools, reusing the login you
already have from the [xAI Grok Build](https://docs.x.ai/) CLI.

## Architecture

```
MCP client (Claude Code)
   │  MCP stdio: grok_search(query) / grok_fetch(url)
   ▼
server.py   (Python stdlib only — hand-rolled MCP stdio transport)
   │  subprocess
   ▼
grok -p "<prompt>" --no-alt-screen --permission-mode bypassPermissions
   │  uses ~/.grok/auth.json OAuth token
   ▼
xAI backend: built-in web_search + web_fetch + model
```

## Requirements

- A local [`grok` CLI](https://docs.x.ai/docs/grok-cli) installation with an
  OAuth login (`~/.grok/auth.json`).
- Python 3.10+.

## Usage

### 1. Run the server (stdio)

```bash
python3 server.py

# optional overrides
GROK_BIN=/path/to/grok GROK_SEARCH_TIMEOUT=180 python3 server.py
```

### 2. Register with Claude Code

```bash
cd /home/sym/Repo   # your working directory
claude mcp add grok-search -s project \
  -e GROK_BIN=/home/sym/.grok/bin/grok \
  -e GROK_SEARCH_TIMEOUT=180 \
  -- python3 /home/sym/Repo/grok-search-mcp/server.py
```

This writes a project-scoped `.mcp.json`. **Restart the Claude Code session**
so it connects to the server (new servers require approval on next startup).

### 3. Available tools

| Tool | Description |
|------|-------------|
| `grok_search(query, max_turns=4)` | Web search via Grok; returns a synthesized, cited Markdown answer. |
| `grok_fetch(url)` | Fetch and summarize the content of a single http(s) URL. |

## Notes / Caveats

- Each search is one independent headless `grok` run and takes ~13–30s (model
  generation time).
- A non-zero exit from `grok` (e.g. hitting `--max-turns`) still surfaces any
  partial output instead of failing, for better robustness on long pages.
- The `.mcp.json` registration is project-scoped. Use `-s user` for all projects.
