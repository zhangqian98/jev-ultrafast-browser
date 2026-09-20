<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

Element operations: `CLICK`, `RIGHT_CLICK`, `DOUBLE_CLICK`, `HOVER`, `DRAG` (source + target heads), `TYPE_TEXT`, `UPLOAD_FILE`, `SELECT`, `PRESS_KEY` (single keys and offered VS Code chords). Page controls: `SCROLL_UP/DOWN/LEFT/RIGHT` (page and nested containers), `BACK`, `FORWARD`, `RELOAD`, `WAIT`, `REVIEW`, `DONE`, `BLOCKED`. Only supported operations and targets are offered; pointer operations reuse the CLICK target space. `REVIEW` lets the model itself defer a step to human judgment — it never mutates the page, even under `approve=True`. Each snapshot also names controls just outside the viewport (`offscreen.above/below`), so scrolling is an informed choice rather than a blind probe.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

The same request also asks two graded judgments — `done` and `risk` nouls — and the choice answers carry confidence. A deterministic policy gate ([policy.py](jev_ultrafast/policy.py)) thresholds them in code: `done ≥ 0.9` finishes, a sensitive target label or `risk ≥ 0.2` holds the action for caller approval (`confirm`), low confidence escalates or stops. Held decisions are not consumed — `browser_step(approve=True)` executes them after review. A caller-supplied `verify` check is evaluated on every observation: the original `{"text": ...}` and `{"url_contains": ...}` forms remain supported, while `all`/`any`/`not`, URL origin/path/query checks, text absence, and observed-element predicates can prove multi-part outcomes. Verification can finish the task without a model call, and a `DONE` that fails it escalates instead of ending the run. A `done` status without `verify` carries `verified: false` — it is the model's claim, not proof. `browser_step(dry_run=True)` previews the decision and gate verdict without executing; `browser_step(min_confidence=...)` raises the confidence floor for a session.

`browser_start(allowed_origins=["https://*.example.com"])` pins a task to URL patterns. Observed link and form destinations are checked before input; direct-CDP sessions intercept disallowed main-frame document requests on targets that are already attached. A redirect, script navigation, or newly followed popup that still reaches another origin is held at `confirm` before its state reaches verification, the model, or another input. A popup's first document request may occur before CDP can attach to the new target, so the allowlist is an action boundary rather than a network sandbox for popups. Owned browsers deny downloads via `Browser.setDownloadBehavior`, and every session keeps a bounded diagnostic ring of console messages, page errors, failed requests, navigations, dialogs, blocked downloads, and security blocks, readable through `browser_logs(session_id, after_id)`.

MCP sessions serialize browser mutations, expose `browser_cancel(session_id)`, and accept a bounded `timeout_ms` on step/text/manual mutation calls. Errors report the stage and whether an action may already have applied. Each MCP run also writes a redacted JSONL trace and returns its `trace_path`; generated field values, prompts, screenshots, raw model requests, URL credentials, query strings, and fragments are not persisted. Traces can still contain page titles, control labels, and URL paths needed for diagnosis. Browser and VS Code child processes receive a small allowlist of operating-system and GUI launch variables instead of inheriting the server environment, so provider, cloud, database, and application credentials stay server-side.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Drive VS Code

The same agent can drive VS Code instead of a web page. For desktop VS Code, the MCP server (run: `uv run --project <repo> --with mcp python mcp_server.py`) exposes `vscode_start(goal, workspace, port)`: it launches Code.exe with a separate, port-scoped `jev-vscode-profile-<port>` user-data-dir and `--remote-debugging-port` (plus flags disabling background/occluded-window animation throttling, without which menus stall at opacity 0), seeds the profile's `User/settings.json` once for a clean Welcome state on every launch, attaches to the workbench page over CDP, and `browser_stop` detaches — VS Code keeps running. Port-scoped profiles prevent an already-running automation instance from silently swallowing a new debug-port argument. The library form is:

```python
with Agent(None, goal, cdp_url="http://127.0.0.1:9333", attach="workbench.html") as agent:
    ...
```

For VS Code Web, use `browser_start("https://vscode.dev", goal)` (or `Agent("https://vscode.dev", goal)`).

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Preserve structured state.** Selected radio/checkbox/select values remain visible to the policy after scrolling; focus and visible alert/status text are sent separately.
- **Compact by operation, not DOM position.** Dense observations reserve candidates for text, upload, select, and click operations before using generic goal relevance. Truncation statistics travel with every decision, and a truncated `DONE`/`BLOCKED` choice escalates for review.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.
- **Keyboard and nested scrolling stay in the same loop.** `PRESS_KEY` drives menus and quick pickers on the focused control, and scroll actions can target a named inner container instead of the page.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads, done/risk judgments, and text generation |
| [policy.py](jev_ultrafast/policy.py) | Thresholds and sensitive-label gate over the model's typed outputs |
| [verify.py](jev_ultrafast/verify.py) | Declarative caller-owned outcome verification |
| [compaction.py](jev_ultrafast/compaction.py) | Operation-aware candidate budgets and statistics |
| [trace.py](jev_ultrafast/trace.py) | Redacted JSONL events and public-state filtering |
| [config.py](jev_ultrafast/config.py) | Validated non-secret runtime limits |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |
| [mcp_server.py](mcp_server.py) | MCP tools: browser sessions, text handoff, `vscode_start` |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

Those timing artifacts describe the source hashes recorded with each run. Correctness, tracing, or browser-runtime changes after a recorded run need a fresh live measurement before their latency is compared with the published numbers.

A `DONE` choice still requires independent outcome verification — pass `verify` to `browser_start`/`browser_step` so code, not the model, decides completion. The DOM reader handles common HTML and ARIA controls plus same-origin iframes and open shadow roots, not the full accessible-name specification. Canvas, closed shadow roots, and cross-origin frame contents remain outside the action space; focus on an opaque frame does not create keyboard targets. Nested scrolling covers up to three visible containers per direction; PRESS_KEY offers fixed keys and VS Code chords, not arbitrary input. A raw observation retains at most 600 actions; the default model request compacts that to 180 elements and 300 actions, with up to 40 options per native select. These limits are configurable with `JEV_MAX_CANDIDATE_ELEMENTS`, `JEV_MAX_CANDIDATE_ACTIONS`, and `JEV_MAX_OPTIONS_PER_SELECT`. Dense pages can still omit a required control, so truncation is surfaced rather than treated as proof of completion. Sessions on one dedicated automation profile share the browser process, while tab listing, switching, following, and cleanup are restricted to targets owned by that session.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls; `scripts/check_containers.py` covers nested scroll containers and key actions, and `scripts/check_vscode.py` covers the VS Code attach path (needs Code.exe on CDP :9333, or set `JEV_VSCODE_CDP_URL`). Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and recording artifacts stay ignored.

`uv run python scripts/eval_snapshots.py` replays seven checked-in `fixtures/eval` cases offline and reports candidate survival, truncation, policy gates, stale observations, and verification results. Add `--live` only when explicitly running a billable one-decision-per-case Jev evaluation. `JEV_TRACE_DIR` changes the MCP JSONL directory, and `JEV_STEP_TIMEOUT_MS` configures the default bounded step duration used by integrations.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
