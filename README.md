<img src="docs/banner.svg" alt="Jev Ultrafast" width="100%" />

# Jev Ultrafast

Jev Ultrafast is an MCP browser agent for Codex, Claude Code, Cursor, and other MCP clients. Give it one complete natural-language goal. The agent observes the current page, chooses from the available actions, and returns the updated page state.

It can drive web pages and desktop VS Code through Browser Harness or DirectCDP. The calling agent supplies field values when a `TYPE_TEXT` step needs input.

## Install

```bash
git clone https://github.com/zhangqian98/jev-ultrafast-browser.git
cd jev-ultrafast
uv sync
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

Set `TYPESAFE_API_KEY` in `.env` or in the server environment. The standalone Python library and local inspector also need `TEXT_MODEL_API_KEY` for generated field values. MCP mode receives those values from the calling agent and does not need a second text-model credential.

Keep credentials in environment variables. Do not put keys in goals, page content, traces, or source files.

## Configure Codex

The MCP server uses stdio transport. Add it to Codex with a project-local Python environment:

```toml
[mcp_servers.jev-ultrafast]
command = "<repo>/.venv/Scripts/python.exe" # Windows; use .venv/bin/python on Unix
args = ["mcp_server.py"]
cwd = "<repo>"
env_vars = ["TYPESAFE_API_KEY"]
```

Or start it directly:

```bash
uv run python mcp_server.py
```

After changing the MCP configuration or source, restart the MCP server or start a new Codex task. Check the connection with:

```bash
codex mcp get jev-ultrafast
```

In Codex, `/mcp` shows the tools available to the current task.

## MCP workflow

1. Start a session with `browser_start`:

   ```json
   {
     "url": "https://example.com",
     "goal": "Open the documentation link and stop when the page title is visible.",
     "verify": {"text_contains": "Example Domain"}
   }
   ```

2. Advance the task with `browser_step`.
3. If the result is `need_text`, provide the requested value with `browser_supply_text`.
4. If the result is `confirm` or `escalate`, review the decision and call `browser_step` with `approve=true` only when it is appropriate.
5. Continue until the server returns `done`, `blocked`, or another terminal status.
6. Call `browser_stop` when the session is no longer needed.

Useful supporting tools are `browser_tabs`, `browser_switch_tab`, `browser_logs`, `browser_cancel`, `browser_dialog`, and `browser_click_xy`.

`browser_start` and `browser_step` accept a caller-owned `verify` specification. Verification is checked on every observation, so a verified result can finish without another model decision. A model-reported `DONE` without verification is only a claim.

Supported verification forms include:

```json
{
  "all": [
    {"url": {"origin": "https://example.com", "path": "/done"}},
    {"text_contains": "Saved"},
    {"element": {"role": "checkbox", "label": "Enabled", "checked": true}}
  ]
}
```

The server also supports `any`, `not`, `text_absent`, URL query checks, observed-element values and counts, selected options, focus, alerts, and modal state.

## Browser and VS Code

For web pages, call `browser_start` with the URL, goal, and optional `allowed_origins` list. The server returns the observed page and available actions. Browser sessions are isolated and can be closed with `browser_stop`.

For desktop VS Code, call `vscode_start` with a goal, workspace, and optional debugging port. It launches or attaches through DirectCDP with a separate profile. `browser_stop` detaches the agent without closing an already-running VS Code instance.

For VS Code Web, use `browser_start` with `https://vscode.dev`.

## Python library

```python
from jev_ultrafast import Agent

with Agent(
    "https://example.com",
    "Open the documentation link and stop when the page title is visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run the generic example with:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal "Find and open the Wikipedia article about Gödel's incompleteness theorems."
```

The local inspector is optional:

```bash
uv run jev
```

It is available at `http://127.0.0.1:8766` and shows observations, decisions, policy results, and executed actions.

## Tests and evaluation

The standard checks are offline:

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Replay the checked-in snapshot cases without API calls:

```bash
uv run python scripts/eval_snapshots.py
```

Run the live Jev benchmark only when API usage is intended:

```bash
uv run python scripts/eval_snapshots.py --live
```

Optional browser checks that do not call the model are available in `scripts/check_guards.py`, `scripts/check_containers.py`, and `scripts/check_vscode.py`.

## Limitations

The DOM reader covers common HTML and ARIA controls, same-origin iframes, and open shadow roots. Canvas, closed shadow roots, and cross-origin frame contents are outside the action space. Candidate lists and scrolling are bounded. Define a concrete `verify` condition whenever the final outcome matters.

## License

MIT
