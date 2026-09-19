"""Timed e2e against vscode.dev in Chrome through the MCP server. Real TypeSafe calls."""
import json
import subprocess
import sys
import time

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
        m = json.loads(line)
        if m.get("id") == rid:
            return m["result"]


def tool(name, args):
    t0 = time.perf_counter()
    r = call("tools/call", {"name": name, "arguments": args})
    if r.get("isError"):
        raise RuntimeError(r["content"][0]["text"])
    return json.loads(r["content"][0]["text"]), round((time.perf_counter() - t0) * 1000)


try:
    call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "e2e", "version": "1"}})
    goal = "On this vscode.dev page, open the Search view (magnifier icon). Stop when the search input is visible."
    res, ms = tool("browser_start", {"url": "https://vscode.dev", "goal": goal})
    sid = res["session_id"]
    print(f"{ms} ms browser_start {res['status']} {len(res['elements'])} elements '{res['title']}'")
    t0, calls = time.perf_counter(), 0
    for i in range(12):
        res, ms = tool("browser_step", {"session_id": sid})
        calls += 1
        d = res.get("decision") or {}
        last = (res.get("recent_history") or [{}])[-1]
        print(f"{ms:6d} ms step[{i}] {res['status']:16s} op={d.get('operation')} conf={d.get('confidence')} "
              f"exec='{str(last.get('action'))[:60]}' changed={last.get('page_changed')}")
        if res["status"] == "need_text":
            res, ms = tool("browser_supply_text", {"session_id": sid, "text": "x"})
            print(f"{ms:6d} ms   supply_text -> {res['status']}")
        if res["status"] in {"done", "blocked", "error"}:
            break
    tool("browser_stop", {"session_id": sid})
    print(f"RESULT vscode.dev: status={res['status']} model_calls={calls} "
          f"elapsed={round((time.perf_counter() - t0) * 1000)} ms")
finally:
    p.terminate()
