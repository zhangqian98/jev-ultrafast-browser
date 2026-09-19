"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets.sync.client
from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

KEYS = {"Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8,
        "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39}


class DirectCDP:
    """Raw CDP over one browser-level websocket; same call shape as helpers.cdp."""

    def __init__(self, base_url, timeout=30):
        info = json.loads(
            urllib.request.urlopen(base_url.rstrip("/") + "/json/version", timeout=5).read())
        self.ws = websockets.sync.client.connect(
            info["webSocketDebuggerUrl"], max_size=None, open_timeout=timeout)
        self.timeout, self.next_id = timeout, 0

    @classmethod
    def from_ws_url(cls, url, timeout=30):
        cdp_ = cls.__new__(cls)
        cdp_.ws = websockets.sync.client.connect(url, max_size=None, open_timeout=timeout)
        cdp_.timeout, cdp_.next_id = timeout, 0
        return cdp_

    def __call__(self, method, session_id=None, _response_timeout=None, **params):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + (_response_timeout or self.timeout)
        while True:
            reply = json.loads(self.ws.recv(timeout=max(0.01, deadline - time.monotonic())))
            if reply.get("id") != self.next_id:
                continue  # events and stale replies
            if "error" in reply:
                raise RuntimeError(reply["error"].get("message", reply["error"]))
            return reply.get("result", {})

    def close(self):
        self.ws.close()


def press_key(call, key):
    if key not in KEYS:
        raise ValueError(f"Unsupported key {key!r}")
    vk = KEYS[key]
    down = {"type": "keyDown" if key == "Enter" else "rawKeyDown", "key": key, "code": key,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    if key == "Enter":
        down["text"] = "\r"
    call("Input.dispatchKeyEvent", **down)
    call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key,
         windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url=None, *, cdp_url=None, attach=None):
        if (url is None) == (attach is None):
            raise ValueError("Supply exactly one of url or attach")
        if cdp_url:
            self.cdp = DirectCDP(cdp_url)
        else:
            ensure_daemon()
            self.cdp = cdp
        self.target = None
        self.session = None
        if attach is not None:
            self.owned = False
            targets = self.cdp("Target.getTargets").get("targetInfos", [])
            pages = [t for t in targets if t.get("type") == "page"]
            match = next((t for t in pages
                          if attach in t.get("url", "") or attach in t.get("title", "")), None)
            if match is None:
                raise RuntimeError(
                    f"No page target containing {attach!r}; pages: {[t.get('url') for t in pages]}")
            self.target = match["targetId"]
            self.session = self.cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        else:
            self.owned = True
            self.target = self.cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
            self.session = self.cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=url, _response_timeout=30)
        self._settle()

    def _settle(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        # Let the app hydrate: stop once a nonzero actionable-element count is stable.
        count_expr = ("document.querySelectorAll('a[href],button,input,textarea,select,"
                      "summary,[contenteditable=\"true\"]').length")
        prev = -1
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            n = self.evaluate(count_expr)
            if n == prev and n > 0:
                break
            prev = n
            time.sleep(0.25)

    def call(self, method, **params):
        return self.cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.geom ?? action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      const animating=()=>document.getAnimations().some(a=>{
                        const t=a.effect?.getTiming?.();
                        if(!t || t.iterations===Infinity) return false;
                        if(!['running','pending'].includes(a.playState)) return false;
                        const ct=a.effect.getComputedTiming();
                        return ct.endTime!=null && (ct.endTime-(a.currentTime??0))<1500;
                      });
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,350);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && !animating() && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot,
                     "cdp": self.cdp}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        # A wait targets nothing; a changed page is exactly what it waited for.
        if action["kind"] != "wait" and not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action,
                                    "text": text, "cdp": self.cdp})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if not self.target:
            return
        target, session = self.target, self.session
        self.target = self.session = None
        if self.owned:
            self.cdp("Target.closeTarget", targetId=target)
        else:
            try:
                self.cdp("Target.detachFromTarget", sessionId=session)
            except RuntimeError:
                pass
        if isinstance(self.cdp, DirectCDP):
            self.cdp.close()


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]
    send = request.get("cdp", cdp)

    def call(method, **params):
        return send(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            node = action.get("node")
            if node is None:
                call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650,
                     deltaX=0, deltaY=action["delta"])
            else:
                if type(node) is not int:
                    raise ValueError("Invalid observed node")
                center = evaluate("""(node => {
                  const e=window.__jevFast?.nodes.get(node);
                  if (!e?.isConnected ||
                      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
                  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
                  return r.width && r.height && x>=0 && y>=0 && x<innerWidth && y<innerHeight
                    ? {x,y} : null;
                })(""" + json.dumps(node) + ")")
                if center is None:
                    raise StalePage("Scroll container changed. Observe again.")
                call("Input.dispatchMouseEvent", type="mouseWheel", x=center["x"], y=center["y"],
                     deltaX=0, deltaY=action["delta"])
        elif kind == "key":
            press_key(call, action["key"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            if "geom" in action and type(action["geom"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              const g=window.__jevFast?.nodes.get(action.geom ?? action.node);
              if (!e?.isConnected || !g?.isConnected || e.matches(':disabled') ||
                  e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                  !g.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=g.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!g.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
