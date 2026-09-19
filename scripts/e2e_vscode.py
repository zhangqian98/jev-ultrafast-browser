"""Timed e2e against desktop VS Code through the MCP server. Makes real TypeSafe calls.

The driver plays the text model: when Jev picks TYPE_TEXT it supplies the one
string the goal asks for. The final outcome is verified independently over CDP.
"""

import json
import subprocess
import sys
import time

from jev_ultrafast.browser import Browser

CDP = "http://127.0.0.1:9333"
TASKS = {
    "extensions": {
        "goal": "In this VS Code window, open the Extensions view and search the marketplace for 'python'. "
                "Stop when extension results for python are listed.",
        "text": "python",
        "verify": "(() => { const q=document.querySelector('.extensions-viewlet .monaco-editor .view-lines');"
                  " const rows=document.querySelectorAll('.extensions-list .monaco-list-row');"
                  " return {query:q?.innerText ?? null, rows:rows.length}; })()",
        "ok": lambda r: (r.get("query") or "").replace("\u00a0", " ").strip().lower() == "python" and r["rows"] > 0,
    },
    "newfile": {
        "goal": "Create a new untitled text file in this VS Code window and type exactly 'hello from jev' into it. "
                "Stop when that text is visible in the editor.",
        "text": "hello from jev",
        "verify": "(() => ({title:document.title, lines:[...document.querySelectorAll('.view-lines')]"
                  ".map(e=>e.innerText).join('\\n')}))()",
        "ok": lambda r: "hello from jev" in r["lines"].replace("\u00a0", " "),
    },
}

p = subprocess.Popen([sys.executable, "mcp_server.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                     encoding="utf-8")
rid = 0


def call(method, params=None):
    global rid
    rid += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    while True:
        line = p.stdout.readline()
        if not line:
            raise RuntimeError("server closed")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == rid:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg["result"]


def tool(name, args):
    t0 = time.perf_counter()
    r = call("tools/call", {"name": name, "arguments": args})
    text = r["content"][0]["text"]
    if r.get("isError"):
        raise RuntimeError(text)
    return json.loads(text), round((time.perf_counter() - t0) * 1000)


def run(name):
    task = TASKS[name]
    print(f"\n=== {name}: {task['goal']}")
    # Every task starts from a fresh VS Code: close the automation profile's windows, vscode_start relaunches.
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"name='Code.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*jev-vscode-profile*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], check=False)
    time.sleep(1.5)
    res, ms = tool("vscode_start", {"goal": task["goal"]})
    sid = res["session_id"]
    print(f"{ms:6d} ms  vscode_start -> {res['status']} {len(res['elements'])} elements '{res['title']}'")
    model_calls, t0 = 0, time.perf_counter()
    seen_steps, timings = 0, {"model": [], "exec": [], "stale": [], "supply": []}
    for i in range(20):
        res, ms = tool("browser_step", {"session_id": sid})
        model_calls += 1
        d = res.get("decision") or {}
        model_ms = d.get("latency_ms")
        if model_ms is not None:
            timings["model"].append(model_ms)
        last = (res.get("recent_history") or [{}])[-1]
        executed = last.get("step", 0) > seen_steps
        if executed:
            seen_steps = last["step"]
            timings["exec"].append(ms - (model_ms or 0))
        elif res["status"] == "stale_reobserved":
            timings["stale"].append(ms - (model_ms or 0))
        print(f"{ms:6d} ms  step[{i}] {res['status']:16s} op={d.get('operation')} conf={d.get('confidence')}"
              f" model={model_ms}ms exec+obs={ms - (model_ms or 0)}ms"
              f"  exec={last.get('kind')} '{str(last.get('action'))[:50]}' changed={last.get('page_changed')}")
        if res["status"] == "need_text":
            print(f"           field: {res['field']}")
            res, ms = tool("browser_supply_text", {"session_id": sid, "text": task["text"]})
            timings["supply"].append(ms)
            last = (res.get("recent_history") or [{}])[-1]
            if last.get("step", 0) > seen_steps:
                seen_steps = last["step"]
            print(f"{ms:6d} ms    supply_text -> {res['status']} exec='{str(last.get('action'))[:50]}' "
                  f"changed={last.get('page_changed')}")
        if res["status"] in {"done", "blocked", "error"}:
            break
    elapsed = round((time.perf_counter() - t0) * 1000)

    def med(xs):
        return sorted(xs)[len(xs) // 2] if xs else 0
    print(f"TIMING {name}: model_call median={med(timings['model'])}ms "
          f"(n={len(timings['model'])}, max={max(timings['model'] or [0])}) "
          f"supply_text median={med(timings['supply'])}ms stale_steps={len(timings['stale'])}")
    tool("browser_stop", {"session_id": sid})
    # Independent verification through a fresh attach; no model involved.
    browser = Browser(cdp_url=CDP, attach="workbench.html")
    try:
        for _ in range(30):  # marketplace/results render asynchronously; poll the outcome
            actual = browser.evaluate(task["verify"])
            if task["ok"](actual):
                break
            time.sleep(1)
    finally:
        browser.close()
    ok = task["ok"](actual)
    print(f"RESULT {name}: status={res['status']} model_calls={model_calls} elapsed={elapsed} ms "
          f"verified={'PASS' if ok else 'FAIL'} actual={json.dumps(actual, ensure_ascii=False)[:300]}")
    return ok


try:
    call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "e2e", "version": "1"}})
    results = {name: run(name) for name in (sys.argv[1:] or TASKS)}
    print("\n=== summary ===", results)
finally:
    p.terminate()
