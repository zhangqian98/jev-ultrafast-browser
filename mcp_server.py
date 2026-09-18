"""MCP stdio server wrapping jev-ultrafast.

Exposes the browser agent as tools so any MCP-capable agent (Devin, Claude
Code, Codex, Cursor, Antigravity) can drive it. The calling agent acts as the
text model: when Jev chooses TYPE_TEXT, `browser_step` returns the field
context and the agent supplies the value via `browser_supply_text` — no
external TEXT_MODEL_* credentials needed.

Run: uv run --project <repo> --with mcp python mcp_server.py
"""

import os
import secrets
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

import jev_ultrafast.agent as agent_mod
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import StalePage
from jev_ultrafast.model import field_context

_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _ensure_browser():
    """When BU_CDP_URL is set, keep a dedicated automation browser alive on it.

    Separate user-data-dir: no M136 default-profile lockdown, no per-session
    remote-debugging prompt, and the user's own browser is never touched.
    """
    url = os.environ.get("BU_CDP_URL")
    if not url:
        return
    base = url.rstrip("/")
    try:
        urllib.request.urlopen(f"{base}/json/version", timeout=1)
        return  # already up
    except Exception:
        pass
    port = urlparse(base).port or 9223
    binary = next((b for b in _BROWSERS if os.path.isfile(b)), None)
    if not binary:
        raise RuntimeError("no Chrome/Edge binary found for BU_CDP_URL")
    profile = os.path.join(os.environ.get("LOCALAPPDATA", "."), "jev-ultrafast-profile")
    subprocess.Popen(
        [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base}/json/version", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"automation browser did not open CDP on {base}")


class NeedText(Exception):
    def __init__(self, context):
        super().__init__("Calling agent must supply text via browser_supply_text")
        self.context = context


def _no_external_text_model(context):
    # Hard guarantee: TYPE_TEXT never calls an external LLM from this server.
    raise NeedText(context)


agent_mod.field_text = _no_external_text_model

mcp = FastMCP("jev-ultrafast")
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
        "operation_probabilities": d["operation_probabilities"],
    }


@mcp.tool()
def browser_start(url: str, goal: str) -> dict:
    """Open a URL and start a browser task. Returns session_id plus the observed element table.

    Drive the task with browser_step; when it returns need_text, write the value
    yourself and pass it to browser_supply_text.
    """
    _ensure_browser()
    agent = Agent(url, goal)
    sid = secrets.token_hex(4)
    _sessions[sid] = {"agent": agent, "pending": None}
    return {"session_id": sid, **_view(agent)}


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
    try:
        agent.command("predict")
        d = st["decision"]
        selected = d["choice"]
        if selected in {"DONE", "BLOCKED"}:
            agent.command("act", {"fingerprint": st["page"]["fingerprint"]})
            return {"status": st["status"], "decision": _decision(d)}
        action = next(a for a in st["page"]["actions"] if a["id"] == selected)
        if action["kind"] == "fill":
            ctx = field_context(st["goal"], action, st["page"], st["history"])
            s["pending"] = ctx
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
        st["status"] = "ready"
        st["page"] = st["browser"].observe(screenshot=False)
        return {**_view(agent), "status": "stale_reobserved"}
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
        s["pending"] = None
        agent.pending_text = None
        st["decision"] = None
        st["status"] = "ready"
        st["page"] = st["browser"].observe(screenshot=False)
        return {**_view(agent), "status": "stale_reobserved"}
    except NeedText:
        return {"status": "need_text", "error": "field context changed; call browser_step again"}
    s["pending"] = None
    return {"status": st["status"], **_view(agent)}


_KEYS = {
    "left": ("ArrowLeft", 37), "right": ("ArrowRight", 39),
    "up": ("ArrowUp", 38), "down": ("ArrowDown", 40),
    "enter": ("Enter", 13), "escape": ("Escape", 27),
    "tab": ("Tab", 9), "backspace": ("Backspace", 8),
}


@mcp.tool()
def browser_press_key(session_id: str, key: str) -> dict:
    """Send one key press (left/right/up/down/enter/escape/tab/backspace) to the page.

    Needed for keyboard-driven controls such as sliders and open menus, which
    the click/fill action space cannot reach.
    """
    s = _sessions.get(session_id)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    k = _KEYS.get(key.lower())
    if not k:
        return {"status": "error", "error": f"unsupported key '{key}'"}
    name, vk = k
    call = s["agent"].state["browser"].call
    for t in ("rawKeyDown", "keyUp"):
        call("Input.dispatchKeyEvent", type=t, key=name, code=name,
             windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
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
def browser_stop(session_id: str) -> dict:
    """Close the browser session."""
    s = _sessions.pop(session_id, None)
    if not s:
        return {"status": "error", "error": "unknown session_id"}
    s["agent"].close()
    return {"status": "closed"}


if __name__ == "__main__":
    mcp.run()
