# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.
- Launching Code.exe from this environment: remove ELECTRON_RUN_AS_NODE from the env or every CLI option is rejected as "bad option".
- VS Code desktop uses DirectCDP (raw websocket to --remote-debugging-port); Chrome still goes through browser-harness. scripts/check_vscode.py needs Code.exe on CDP :9333.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build.
Live smokes (no model calls): scripts/check_guards.py, scripts/check_containers.py, scripts/check_vscode.py.
