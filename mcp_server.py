"""MCP stdio server wrapping jev-ultrafast.

Exposes the browser agent as tools so any MCP-capable agent (Devin, Claude
Code, Codex, Cursor, Antigravity) can drive it. The calling agent acts as the
text model: when Jev chooses TYPE_TEXT, `browser_step` returns the field
context and the agent supplies the value via `browser_supply_text` — no
external TEXT_MODEL_* credentials needed.

Run: uv run --project <repo> --with mcp python mcp_server.py
"""

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer

import jev_ultrafast.agent as agent_mod
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import COMBOS, KEYS, StalePage, press_key
from jev_ultrafast.model import CLIENT, field_context

_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _ensure_cdp(base_url, launch_cmd):
    """Probe base_url/json/version; if down, run launch_cmd and wait up to 20s."""
    base = base_url.rstrip("/")
    try:
        urllib.request.urlopen(f"{base}/json/version", timeout=1)
        return  # already up
    except Exception:
        pass
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    subprocess.Popen(launch_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base}/json/version", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"CDP endpoint did not answer on {base}")


def _ensure_browser():
    """When BU_CDP_URL is set, keep a dedicated automation browser alive on it.

    Separate user-data-dir: no M136 default-profile lockdown, no per-session
    remote-debugging prompt, and the user's own browser is never touched.
    """
    url = os.environ.get("BU_CDP_URL")
    if not url:
        return
    port = urlparse(url.rstrip("/")).port or 9223
    binary = next((b for b in _BROWSERS if os.path.isfile(b)), None)
    if not binary:
        raise RuntimeError("no Chrome/Edge binary found for BU_CDP_URL")
    profile = os.path.join(os.environ.get("LOCALAPPDATA", "."), "jev-ultrafast-profile")
    _ensure_cdp(url, [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ])


class NeedText(Exception):
    def __init__(self, context):
        super().__init__("Calling agent must supply text via browser_supply_text")
        self.context = context


def _no_external_text_model(contexts):
    # Hard guarantee: TYPE_TEXT never calls an external LLM from this server.
    raise NeedText(contexts[0] if isinstance(contexts, list) else contexts)


agent_mod.field_text = agent_mod.field_texts = _no_external_text_model

mcp = MCPServer("jev-ultrafast")
_sessions = {}


def _view(agent):
    snap = agent.snapshot()
    page = snap["page"]
    return {
        "status": snap["status"],
        "url": page["url"],
        "title": page["title"],
        "page_text": page["text"][:3000],
        "elements": snap["elements"],
        "recent_history": snap["history"][-5:],
    }


def _decision(d):
    return {
        "operation": d["operation"],
        "confidence": d["confidence"],
        "latency_ms": d["latency_ms"],
        "operation_probabilities": d["operation_probabilities"],
    }


@mcp.tool()
def browser_start(url: str, goal: str) -> dict:
    """Open a URL and start a browser task. Returns session_id plus the observed element table.

    Drive the task with browser_step; when it returns need_text, write the value
    yourself and pass it to browser_supply_text.
    """
    _ensure_browser()
    threading.Thread(target=_warm_model_conn, daemon=True).start()
    agent = Agent(url, goal)
    sid = secrets.token_hex(4)
    _sessions[sid] = {"agent": agent, "pending": None}
    return {"session_id": sid, **_view(agent)}


def _warm_model_conn():
    """First TypeSafe call in a fresh process pays ~1-2s of TLS/HTTP2 handshake; warm it
    while the browser/VS Code launch happens anyway."""
    try:
        CLIENT.get("https://api.typesafe.ai/", timeout=10)
    except Exception:
        pass


def _code_exe():
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    which = shutil.which("code")
    if which:
        path = os.path.normpath(os.path.join(os.path.dirname(which), "..", "Code.exe"))
        if os.path.isfile(path):
            return path
    return None


@mcp.tool()
def vscode_start(goal: str, workspace: str = "", port: int = 9333) -> dict:
    """Launch desktop VS Code with CDP and attach the agent to its workbench.

    Passes --disable-renderer-backgrounding, --disable-background-timer-throttling,
    and --disable-backgrounding-occluded-windows so animations don't throttle while
    the window is background or occluded, plus --force-renderer-accessibility so
    Monaco editors expose real accessible names. On a fresh launch it also seeds
    the profile's User/settings.json (never overwritten) so every run starts from
    a clean Welcome state. Returns session_id plus the observed element table;
    drive with browser_step and friends. For VS Code Web use
    browser_start with https://vscode.dev.
    """
    base = f"http://127.0.0.1:{port}"
    exe = _code_exe()
    if not exe:
        raise RuntimeError("Code.exe not found")
    profile = os.path.join(os.environ.get("LOCALAPPDATA", "."), "jev-vscode-profile")
    cmd = [exe, "--new-window", f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
           "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
           "--disable-backgrounding-occluded-windows", "--force-renderer-accessibility"]
    if workspace:
        cmd.append(workspace)
    try:
        urllib.request.urlopen(f"{base}/json/version", timeout=1)
        up = True
    except Exception:
        up = False
    settings = os.path.join(profile, "User", "settings.json")
    if not up and not os.path.exists(settings):
        # A clean Welcome state on every fresh launch of the automation profile.
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        with open(settings, "w") as f:
            json.dump({"files.hotExit": "off", "window.restoreWindows": "none",
                       "update.mode": "none", "workbench.startupEditor": "welcomePage"}, f)
    threading.Thread(target=_warm_model_conn, daemon=True).start()
    _ensure_cdp(base, cmd)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            targets = json.loads(urllib.request.urlopen(f"{base}/json/list", timeout=1).read())
            if any(t.get("type") == "page" and "workbench.html" in t.get("url", "") for t in targets):
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        raise RuntimeError(f"no workbench.html page on {base}")
    agent = Agent(None, goal, cdp_url=base, attach="workbench.html")
    sid = secrets.token_hex(4)
    _sessions[sid] = {"agent": agent, "pending": None}
    return {"session_id": sid, "target": "vscode", **_view(agent)}


@mcp.tool()
def browser_step(session_id: str) -> dict:
    """Advance one step: Jev picks the next operation and target, then executes it.

    Returns status done/blocked, need_text (call browser_supply_text next), or
    the fresh page state after a click/select/scroll/wait.
    """
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    agent, st = s["agent"], None
    st = s["agent"].state
    d = None
    try:
        agent.command("predict")
        d = st["decision"]
        selected = d["choice"]
        if selected in {"DONE", "BLOCKED"}:
            agent.command("act", {"fingerprint": st["page"]["fingerprint"]})
            return {"status": st["status"], "decision": _decision(d)}
        action = next(a for a in st["page"]["actions"] if a["id"] == selected)
        if action["kind"] in ("fill", "file"):
            ctx = field_context(st["goal"], action, st["page"], st["history"])
            s["pending"] = ctx
            s["pending_node"] = action["node"]
            return {
                "status": "need_text",
                "decision": _decision(d),
                "field": ctx["field"],
                "context": ctx,
                "hint": "Write the value for this field and call browser_supply_text(session_id, text).",
            }
        agent.command("act", {"fingerprint": st["page"]["fingerprint"]})
        return {"status": st["status"], "decision": _decision(d), **_view(agent)}
    except StalePage:
        st["decision"] = None
        if st["status"] != "blocked":
            st["status"] = "ready"
        st["page"] = st["browser"].observe(screenshot=False)
        out = {**_view(agent),
               "status": "blocked" if st["status"] == "blocked" else "stale_reobserved"}
        if d is not None:
            out["decision"] = _decision(d)
        return out
    except (ValueError, NeedText) as e:
        return {"status": "error", "error": str(e)}


@mcp.tool()
def browser_supply_text(session_id: str, text: str) -> dict:
    """Provide text for a pending TYPE_TEXT and execute the fill."""
    s = _sessions.get(session_id)
    if not s or not s["pending"]:
        return {"status": "error", "error": "no pending text request"}
    agent, st = s["agent"], s["agent"].state
    agent.pending_text = (
        s["pending"],
        text.strip(),
        {"model": "calling-agent", "latency_ms": 0, "usage": {}},
    )
    try:
        agent.command("act", {"fingerprint": st["page"]["fingerprint"]})
    except StalePage:
        st["decision"] = None
        if st["status"] != "blocked":
            st["status"] = "ready"
        st["page"] = st["browser"].observe(screenshot=False)
        ctx = s["pending"]
        # Same DOM node + byte-identical field context → the model's choice still holds;
        # retry the fill without paying another decision call.
        retry = next((a for a in st["page"]["actions"]
                      if a["kind"] in ("fill", "file") and a["node"] == s.get("pending_node")
                      and field_context(st["goal"], a, st["page"], st["history"]) == ctx), None)
        if retry is None:
            s["pending"] = s["pending_node"] = None
            agent.pending_text = None
            return {**_view(agent),
                    "status": "blocked" if st["status"] == "blocked" else "stale_reobserved"}
        st["decision"] = {"choice": retry["id"], "operation": "TYPE_TEXT", "target": "1",
                          "confidence": 1.0, "probabilities": {retry["id"]: 1.0},
                          "latency_ms": 0, "usage": {}}
        return {"status": "need_text", "field": ctx["field"], "context": ctx,
                "hint": "Same field survived the page change; call browser_supply_text again."}
    except NeedText:
        return {"status": "need_text", "error": "field context changed; call browser_step again"}
    s["pending"] = None
    return {"status": st["status"], **_view(agent)}


_KEY_ALIASES = {"left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown"}


@mcp.tool()
def browser_press_key(session_id: str, key: str) -> dict:
    """Send one key press (left/right/up/down/enter/escape/tab/backspace) to the page.

    Needed for keyboard-driven controls such as sliders and open menus, which
    the click/fill action space cannot reach.
    """
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    name = _KEY_ALIASES.get(key.lower()) or next(
        (k for k in (*KEYS, *COMBOS) if k.lower() == key.lower()), None)
    if not name:
        return {"status": "error", "error": f"unsupported key '{key}'"}
    press_key(s["agent"].state["browser"].call, name)
    time.sleep(0.15)
    return {"status": "ok", "key": name}


@mcp.tool()
def browser_click_xy(session_id: str, x: float, y: float) -> dict:
    """Click at raw page coordinates. Escape hatch for controls the element
    table cannot express (e.g. a slider track position); prefer indexed
    element actions whenever they exist.
    """
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    call = s["agent"].state["browser"].call
    for t in ("mousePressed", "mouseReleased"):
        call("Input.dispatchMouseEvent", type=t, x=x, y=y,
             button="left", clickCount=1)
    time.sleep(0.15)
    return {"status": "ok", "x": x, "y": y}


@mcp.tool()
def browser_tabs(session_id: str) -> dict:
    """List open page tabs in the session's browser, marking the observed one."""
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    b = s["agent"].state["browser"]
    return {"tabs": [{"index": i, "url": t["url"], "title": t["title"],
                      "current": t["targetId"] == b.target}
                     for i, t in enumerate(b._pages())]}


@mcp.tool()
def browser_switch_tab(session_id: str, index: int) -> dict:
    """Attach the agent to a different tab from browser_tabs and observe it."""
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    b = s["agent"].state["browser"]
    pages = b._pages()
    if not 0 <= index < len(pages):
        return {"status": "error", "error": f"no tab at index {index}"}
    if pages[index]["targetId"] != b.target:
        b.switch_to(pages[index]["targetId"])
    st = s["agent"].state
    st["decision"] = None
    st["page"] = b.observe(screenshot=False)
    return {"status": st["status"], **_view(s["agent"])}


@mcp.tool()
def browser_stop(session_id: str) -> dict:
    """Close the browser session."""
    s = _sessions.pop(session_id, None)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    s["agent"].close()
    return {"status": "closed"}


if __name__ == "__main__":
    mcp.run()
