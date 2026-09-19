"""The exact trajectories of the real TypeSafe runs with the decision stubbed out.

Same Agent predict/act path, same act+observe work — only the model request is
replaced by a scripted pick, so each step's cost is pure browser-side time.
No model calls; VS Code is relaunched between tasks for a clean Welcome state.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

import jev_ultrafast.agent as loop
from jev_ultrafast.agent import Agent

CDP = "http://127.0.0.1:9333"
CODE = r"C:\Program Files\Microsoft VS Code\Code.exe"
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "jev-vscode-profile")
FLAGS = ["--new-window", f"--user-data-dir={PROFILE}", "--remote-debugging-port=9333",
         "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
         "--disable-backgrounding-occluded-windows", "--force-renderer-accessibility"]

TASKS = {
    "extensions": [
        ("click", "Extensions ("),
        ("fill", "Search Extensions in Marketplace", "python"),
        "DONE",
    ],
    "newfile": [
        ("click", "New File"),
        ("click", "Text File"),
        ("fill", "Untitled-1", "hello from jev"),
        "DONE",
    ],
}


def relaunch_vscode():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"name='Code.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*jev-vscode-profile*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   check=False)
    time.sleep(1.5)
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    subprocess.Popen([CODE, *FLAGS], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=1).read())
            if any(t.get("type") == "page" and "workbench.html" in t.get("url", "") for t in targets):
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("VS Code did not come up")


def scripted_chooser(steps, pos):
    """Return a choose()-shaped stub reading steps[pos]; run() advances pos after a successful act."""
    def choose(state, goal, history):
        step = steps[pos[0]]
        if step == "DONE":
            return {"choice": "DONE", "operation": "DONE", "target": None, "confidence": 1.0,
                    "probabilities": {"DONE": 1.0}, "operation_probabilities": {"DONE": 1.0},
                    "target_probabilities": {}, "target_confidence": None, "raw_answers": {},
                    "model": "scripted", "usage": {}, "latency_ms": 0, "request": {}}
        kind, needle, *_ = step
        action = next(a for a in state["actions"]
                      if a["kind"] == kind and needle in a["label"])
        op = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}[kind]
        return {"choice": action["id"], "operation": op, "target": "1", "confidence": 1.0,
                "probabilities": {action["id"]: 1.0}, "operation_probabilities": {op: 1.0},
                "target_probabilities": {"1": 1.0}, "target_confidence": 1.0, "raw_answers": {},
                "model": "scripted", "usage": {}, "latency_ms": 0, "request": {}}

    return choose


def run(name):
    steps = TASKS[name]
    relaunch_vscode()
    texts = {s[1]: s[2] for s in steps if s[0] != "DONE" and s[0] == "fill"}
    pos = [0]
    loop.choose = scripted_chooser(steps, pos)
    loop.field_texts = lambda ctxs: ([texts.get(c["field"]["label"]) for c in ctxs],
                                     {"model": "scripted", "latency_ms": 0, "usage": {}})
    agent = Agent(None, "bench", cdp_url=CDP, attach="workbench.html")
    print(f"\n=== {name} (no Jev; same trajectory, same act+observe path)")
    totals, t0 = {"predict": [], "act": [], "reobserve": []}, time.perf_counter()
    i = 0
    while agent.state["status"] not in {"done", "blocked"} and i < 12:
        t1 = time.perf_counter()
        agent.command("predict")
        predict_ms = (time.perf_counter() - t1) * 1000
        totals["predict"].append(predict_ms)
        try:
            t1 = time.perf_counter()
            agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
            act_ms = (time.perf_counter() - t1) * 1000
        except loop.StalePage:
            act_ms = (time.perf_counter() - t1) * 1000
            agent.state["decision"] = None
            if agent.state["status"] != "blocked":
                agent.state["status"] = "ready"
            t1 = time.perf_counter()
            agent.state["page"] = agent.state["browser"].observe(screenshot=False)
            totals["reobserve"].append((time.perf_counter() - t1) * 1000)
            print(f"  step[{i}] STALE predict={predict_ms:.0f}ms act_attempt={act_ms:.0f}ms "
                  f"reobserve={totals['reobserve'][-1]:.0f}ms")
            i += 1
            continue
        totals["act"].append(act_ms)
        pos[0] += 1
        last = agent.state["history"][-1] if agent.state["history"] else {}
        print(f"  step[{i}] {agent.state['status']:8s} predict={predict_ms:.0f}ms "
              f"act+observe={act_ms:.0f}ms  exec='{str(last.get('action'))[:50]}'")
        i += 1
    elapsed = (time.perf_counter() - t0) * 1000
    agent.close()

    def med(xs):
        return sorted(xs)[len(xs) // 2] if xs else 0
    print(f"TIMING {name}: predict median={med(totals['predict']):.0f}ms "
          f"act+observe median={med(totals['act']):.0f}ms max={max(totals['act'] or [0]):.0f}ms "
          f"stale_reobserves={len(totals['reobserve'])} total={elapsed:.0f}ms")


if __name__ == "__main__":
    for name in (sys.argv[1:] or TASKS):
        run(name)
