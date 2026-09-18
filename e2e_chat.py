"""Timed e2e: drive jev-ultrafast MCP server against the user's real Edge.

Task: in ChatGPT, set response speed to the fastest/lowest thinking mode,
send a test message, report wall-clock time of every step.
"""
import json
import os
import subprocess
import time

env = dict(os.environ)
env.pop("BU_CDP_URL", None)  # local mode: attach to the user's real Edge via daemon
env.pop("BU_CDP_WS", None)

p = subprocess.Popen(
    ["uv", "run", "--with", "mcp<2", "python", "mcp_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
)

def call(rid, method, params=None):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}}) + "\n")
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
            return msg

def tool(rid, name, args):
    t0 = time.perf_counter()
    r = call(rid, "tools/call", {"name": name, "arguments": args})
    ms = (time.perf_counter() - t0) * 1000
    res = json.loads(r["result"]["content"][0]["text"])
    return res, ms

timings = []

def mark(label, ms, extra=""):
    timings.append((label, ms))
    print(f"{ms:9.0f} ms  {label} {extra}", flush=True)

GOAL = ("This is a fresh empty ChatGPT chat. Type exactly 'Hello, this is "
        "an automated test.' into the message composer, click the send "
        "button, and stop once the reply starts streaming.")

t0 = time.perf_counter()
call(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "e2e", "version": "1"}})
res, ms = tool(2, "browser_start", {"url": "https://chatgpt.com/", "goal": GOAL})
sid = res["session_id"]
mark("browser_start chatgpt.com", ms, f"| {res['status']} {res['url']}")
print("   elements:", len(res["elements"]), "| title:", res["title"])
print("   page_text:", res.get("page_text", "")[:400])
for e in res["elements"]:
    print("   ", e.get("index"), e.get("role"), "|", e.get("label", "")[:50], "|", e.get("operations"))

# Set thinking level to lowest via the escape hatch: open the level menu
# (button right of the composer), click the slider track's far-left end,
# then close the popover. Coordinates verified against the live page.
for j, (lbl, x, y) in enumerate([
    ("open level menu", 952, 353), ("slider -> lowest", 812, 440), ("close menu", 560, 300)]):
    r2, ms2 = tool(40 + j, "browser_click_xy", {"session_id": sid, "x": x, "y": y})
    mark(f"  {lbl}", ms2, "| " + r2.get("status", ""))

for i in range(25):
    res, ms = tool(10 + i, "browser_step", {"session_id": sid})
    d = res.get("decision") or {}
    extra = f"| {res['status']} op={d.get('operation')} conf={d.get('confidence')}"
    mark(f"browser_step[{i}]", ms, extra)
    last = (res.get("recent_history") or [{}])[-1]
    if last.get("action"):
        print(f"      exec: {last.get('kind')} '{last.get('action')}' changed={last.get('page_changed')}")
    if res["status"] == "need_text":
        res, ms = tool(20 + i, "browser_supply_text",
                       {"session_id": sid, "text": "Hello, this is an automated test."})
        mark(f"  supply_text[{i}]", ms, f"| {res['status']} {res.get('url','')}")
    if res["status"] in {"done", "blocked", "error"}:
        break

res, ms = tool(99, "browser_stop", {"session_id": sid})
mark("browser_stop", ms)

total = time.perf_counter() - t0
print("\n=== timings ===")
for label, ms in timings:
    print(f"{ms:9.0f} ms  {label}")
print(f"{total*1000:9.0f} ms  TOTAL")
p.terminate()
