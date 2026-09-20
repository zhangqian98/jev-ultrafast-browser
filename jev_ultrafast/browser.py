"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

import websockets.sync.client
from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .trace import safe_url

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
KEY_MARKER = (
    f"(() => {{ const state={READ_STATE}; "
    "return state ? [state.marker,state.focus_guard] : null; })()"
)
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

KEYS = {"Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8,
        "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39}

# Code-owned key combinations (modifiers bit: Alt=1, Ctrl=2, Shift=8). The model can only
# pick from this set; it cannot invent chords. Offered mainly on desktop-app surfaces.
def _combo(base, code, vk, shift=False):
    return (10 if shift else 2, base.upper() if shift else base, code, vk)


COMBOS = {
    "Ctrl+P": _combo("p", "KeyP", 80), "Ctrl+Shift+P": _combo("p", "KeyP", 80, True),
    "Ctrl+`": _combo("`", "Backquote", 192), "Ctrl+N": _combo("n", "KeyN", 78),
    "Ctrl+S": _combo("s", "KeyS", 83), "Ctrl+W": _combo("w", "KeyW", 87),
    "Ctrl+F": _combo("f", "KeyF", 70), "Ctrl+B": _combo("b", "KeyB", 66),
    "Ctrl+Z": _combo("z", "KeyZ", 90), "Ctrl+Y": _combo("y", "KeyY", 89),
    "Ctrl+Shift+E": _combo("e", "KeyE", 69, True),
    "Ctrl+Shift+F": _combo("f", "KeyF", 70, True),
    "Ctrl+Shift+X": _combo("x", "KeyX", 88, True),
}


class DirectCDP:
    """Raw CDP over one browser-level websocket; same call shape as helpers.cdp."""

    def __init__(self, base_url, timeout=30):
        info = json.loads(
            _LOCAL_OPENER.open(base_url.rstrip("/") + "/json/version", timeout=5).read())
        self.ws = websockets.sync.client.connect(
            info["webSocketDebuggerUrl"], max_size=None, open_timeout=timeout)
        self.timeout, self.next_id = timeout, 0
        self.events = []
        self.dialog_accept = {"accept": False}
        self.logs, self._log_id = [], 0
        self.security_blocks = {}
        self.allowed_origins, self.main_frames = {}, {}

    @classmethod
    def from_ws_url(cls, url, timeout=30):
        cdp_ = cls.__new__(cls)
        cdp_.ws = websockets.sync.client.connect(url, max_size=None, open_timeout=timeout)
        cdp_.timeout, cdp_.next_id = timeout, 0
        cdp_.events = []
        cdp_.dialog_accept = {"accept": False}
        cdp_.logs, cdp_._log_id = [], 0
        cdp_.security_blocks = {}
        cdp_.allowed_origins, cdp_.main_frames = {}, {}
        return cdp_

    def _append_log(self, kind, level, text, url=None, session_id=None):
        self._log_id += 1
        entry = {"id": self._log_id, "ts": round(time.time(), 3),
                 "type": kind, "level": level,
                 "text": str(text).strip()[:500], "url": url}
        self.logs.append(entry)
        if kind == "security":
            # Security decisions must not disappear when noisy pages overflow
            # the general diagnostic ring. One pending latch per CDP session is
            # sufficient because any block pauses the agent for review.
            blocks = getattr(self, "security_blocks", None)
            if blocks is None:
                blocks = self.security_blocks = {}
            blocks[session_id] = entry
        if len(self.logs) > 1000:
            del self.logs[: len(self.logs) - 1000]

    def _capture_log(self, method, params):
        """Translate CDP events into a bounded diagnostic log ring."""
        kind = level = text = url = None
        if method == "Runtime.consoleAPICalled":
            kind, level = "console", params.get("type", "log")
            text = " ".join(str(a.get("value", a.get("description", "")))
                            for a in params.get("args", []))
        elif method == "Runtime.exceptionThrown":
            kind, level = "pageerror", "error"
            details = params.get("exceptionDetails", {})
            text = details.get("text", "") + " " + str(
                (details.get("exception") or {}).get("description", ""))
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            kind, level, url = "log", entry.get("level", "info"), entry.get("url")
            text = entry.get("text", "")
        elif method == "Network.loadingFailed":
            kind, level = "requestfailed", "error"
            text = params.get("errorText", "request failed")
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if frame.get("parentId"):
                return
            kind, level, text = "navigation", "info", frame.get("url", "")
        elif method == "Page.javascriptDialogOpening":
            kind, level, text = "dialog", "info", params.get("message", "")
        elif method.startswith("Browser.download"):
            kind, level = "download", "blocked"
            text = params.get("suggestedFilename") or params.get("url", "download attempt")
        if kind is None:
            return
        self._append_log(kind, level, text, url)

    def _send_aux(self, method, params, session_id=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))

    def __call__(self, method, session_id=None, _response_timeout=None, **params):
        self.next_id += 1
        request_id = self.next_id
        msg = {"id": request_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + (_response_timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} timed out")
            reply = json.loads(self.ws.recv(timeout=remaining))
            if reply.get("method") == "Page.javascriptDialogOpening":
                # A modal dialog blocks all evaluation; dismiss it in-band so calls unblock.
                self.events.append(reply)
                self._capture_log(reply["method"], reply.get("params") or {})
                self._send_aux("Page.handleJavaScriptDialog", dict(self.dialog_accept),
                               reply.get("sessionId"))
                continue
            if reply.get("method") == "Fetch.requestPaused":
                event = reply.get("params") or {}
                event_session = reply.get("sessionId")
                request = event.get("request") or {}
                url = request.get("url", "")
                patterns = self.allowed_origins.get(event_session)
                main_frame = self.main_frames.get(event_session)
                blocked = False
                if patterns and main_frame and event.get("frameId") == main_frame:
                    from .policy import origin_allowed

                    blocked = not origin_allowed(url, patterns)
                if blocked:
                    self._append_log("security", "blocked",
                                     "Blocked navigation outside allowed origins", url,
                                     event_session)
                    self._send_aux("Fetch.failRequest",
                                   {"requestId": event["requestId"],
                                    "errorReason": "BlockedByClient"}, event_session)
                else:
                    self._send_aux("Fetch.continueRequest",
                                   {"requestId": event["requestId"]}, event_session)
                continue
            if "method" in reply:
                if reply["method"] == "Page.frameNavigated":
                    frame = (reply.get("params") or {}).get("frame") or {}
                    if not frame.get("parentId") and frame.get("id"):
                        self.main_frames[reply.get("sessionId")] = frame["id"]
                self.events.append(reply)
                self._capture_log(reply["method"], reply.get("params") or {})
                continue
            if reply.get("id") != request_id:
                if "method" in reply:
                    self.events.append(reply)
                    self._capture_log(reply["method"], reply.get("params") or {})
                continue  # events and stale replies
            if "error" in reply:
                raise RuntimeError(reply["error"].get("message", reply["error"]))
            return reply.get("result", {})

    def close(self):
        self.ws.close()


def press_key(call, key):
    if key in COMBOS:
        modifiers, value, code, vk = COMBOS[key]
        for event in ("rawKeyDown", "keyUp"):
            call("Input.dispatchKeyEvent", type=event, key=value, code=code,
                 windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=modifiers)
        return
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


class ActionMayHaveApplied(RuntimeError):
    """Input was dispatched, but its result could not be observed safely."""


class NavigationBlocked(ValueError):
    """A known action destination falls outside the caller's origin allowlist."""

    def __init__(self, url):
        self.url = str(url or "")
        super().__init__(f"navigation outside allowed origins blocked: {safe_url(self.url)}")


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
        self._owned_targets = set()
        self._known_targets = set()
        self.dialogs = []
        self.allowed_origins = None
        self.timeout_provider = None
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
            self._owned_targets = {self.target}
            self.session = self.cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=url, _response_timeout=30)
        # Page.enable is required for dialog events; the rest feed the diagnostic log ring.
        for domain in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            try:
                self.call(domain)
            except RuntimeError:
                pass
        if self.owned and isinstance(self.cdp, DirectCDP):
            try:
                self.cdp("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=True)
            except RuntimeError:
                pass
        self.follow = self.owned  # only dedicated automation browsers get new tabs
        self._known_targets = {t["targetId"] for t in self._all_pages()}
        self._settle()

    def _send(self, method, session_id=None, **params):
        if "_response_timeout" not in params:
            provider = getattr(self, "timeout_provider", None)
            remaining = provider() if callable(provider) else None
            if remaining is not None:
                params["_response_timeout"] = max(0.001, remaining)
        return self.cdp(method, session_id=session_id, **params)

    def _all_pages(self):
        provider = getattr(self, "timeout_provider", None)
        if callable(provider):
            provider()  # propagate cancellation/deadline before the fallback path below
        try:
            return [t for t in self._send("Target.getTargets").get("targetInfos", [])
                    if t.get("type") == "page"]
        except TimeoutError:
            raise
        except Exception:
            return []

    def _pages(self):
        pages = self._all_pages()
        allowed = self._owned_targets if getattr(self, "owned", False) else {self.target}
        return [page for page in pages if page.get("targetId") in allowed]

    def switch_to(self, target_id):
        allowed = self._owned_targets if getattr(self, "owned", False) else {self.target}
        if target_id not in allowed:
            raise ValueError("Target is not owned by this browser session")
        old_session = self.session
        session = self._send("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        try:
            self._send("Target.detachFromTarget", session_id=None, sessionId=self.session)
        except RuntimeError:
            pass
        if isinstance(self.cdp, DirectCDP):
            self.cdp.allowed_origins.pop(old_session, None)
            self.cdp.main_frames.pop(old_session, None)
        self.target, self.session = target_id, session
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        for domain in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            try:
                self.call(domain)
            except RuntimeError:
                pass
        if getattr(self, "allowed_origins", None):
            self.set_allowed_origins(self.allowed_origins)

    def _maybe_retarget(self):
        """Follow tabs the page opened (window.open, target=_blank, ctrl+click)."""
        if not self.follow or not self.target:
            return
        pages = self._all_pages()
        fresh = [t for t in pages if t["targetId"] not in self._known_targets]
        self._known_targets = {t["targetId"] for t in pages}
        opened = next((t for t in fresh if t.get("openerId") == self.target), None)
        if opened:
            self._owned_targets.add(opened["targetId"])
            self.switch_to(opened["targetId"])

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
        return self._send(method, session_id=self.session, **params)

    def set_allowed_origins(self, patterns):
        self.allowed_origins = tuple(patterns or ())
        if not isinstance(self.cdp, DirectCDP):
            return
        if self.allowed_origins:
            tree = self.call("Page.getFrameTree").get("frameTree", {})
            frame_id = (tree.get("frame") or {}).get("id")
            if frame_id:
                self.cdp.main_frames[self.session] = frame_id
            self.cdp.allowed_origins[self.session] = self.allowed_origins
            self.call("Fetch.enable", patterns=[{
                "urlPattern": "*",
                "resourceType": "Document",
                "requestStage": "Request",
            }])
        else:
            self.cdp.allowed_origins.pop(self.session, None)
            self.cdp.main_frames.pop(self.session, None)
            try:
                self.call("Fetch.disable")
            except RuntimeError:
                pass

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            details = response["exceptionDetails"]
            description = str((details.get("exception") or {}).get("description", "")).splitlines()[0]
            reason = description or details.get("text") or "evaluation failed"
            raise StalePage(f"Document changed during evaluation: {reason[:300]}")
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
        self._maybe_retarget()
        for attempt in range(10):
            try:
                state = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot,
                     "cdp": self._send}
                )
                if isinstance(self.cdp, DirectCDP):
                    fresh = [e["params"] for e in self.cdp.events
                             if e.get("method") == "Page.javascriptDialogOpening"]
                    if fresh:
                        self.dialogs.extend(fresh)
                        self.cdp.events = [
                            e for e in self.cdp.events
                            if e.get("method") != "Page.javascriptDialogOpening"]
                    if self.dialogs:
                        state["dialogs"] = self.dialogs
                return state
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
        if action is not None and action["kind"] == "key":
            return self.evaluate(KEY_MARKER) == [page["marker"], page.get("focus_guard")]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        # A wait targets nothing; a changed page is exactly what it waited for.
        if action["kind"] != "wait" and not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        destination = action.get("href") or action.get("form_action")
        if destination and self.allowed_origins:
            from .policy import origin_allowed

            absolute = urljoin(page.get("url", ""), destination)
            if not origin_allowed(absolute, self.allowed_origins):
                raise NavigationBlocked(absolute)
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action,
                                    "text": text, "cdp": self._send})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def consume_security_block(self):
        if not isinstance(self.cdp, DirectCDP):
            return None
        blocks = getattr(self.cdp, "security_blocks", {})
        entry = blocks.pop(getattr(self, "session", None), None) or blocks.pop(None, None)
        if entry is not None:
            return entry
        if not blocks:
            return None
        # A popup retarget detaches the opener session before the next policy
        # checkpoint. Preserve a block emitted by that just-detached session;
        # each Browser owns its DirectCDP connection, so these entries cannot
        # belong to another agent session.
        session_id, entry = min(
            blocks.items(), key=lambda item: item[1].get("id", float("inf"))
        )
        blocks.pop(session_id, None)
        return entry

    def logs(self, after_id=0, limit=200):
        entries = getattr(self.cdp, "logs", [])
        entries = [e for e in entries if e["id"] > after_id][:limit]
        return {"logs": entries,
                "last_id": entries[-1]["id"] if entries else after_id}

    def close(self):
        if not self.target:
            return
        self.timeout_provider = None
        target, session = self.target, self.session
        self.target = self.session = None
        if self.owned:
            for owned in self._owned_targets | {target}:
                try:
                    self._send("Target.closeTarget", targetId=owned)
                except RuntimeError:
                    pass
        else:
            try:
                self._send("Target.detachFromTarget", sessionId=session)
            except RuntimeError:
                pass
        if isinstance(self.cdp, DirectCDP):
            self.cdp.allowed_origins.pop(session, None)
            self.cdp.main_frames.pop(session, None)
            self.cdp.close()


def fingerprint(state):
    # Hash every structured field the policy can observe.  This fingerprint is
    # used for caller hand-off and progress detection, so changes to focus,
    # selected controls, alerts, or offscreen controls must count even when the
    # visible text and action rows happen to stay byte-identical.
    keys = (
        "url", "title", "text", "actions", "scroll", "offscreen",
        "selected_controls", "selected_controls_truncated", "focus", "alerts", "omitted_actions",
        "text_complete", "elements_complete", "cross_origin_frames", "document_id",
    )
    content = {key: state.get(key) for key in keys}
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
                x, y, pos_expr = 550, 650, "scrollY+','+scrollX"
            else:
                if type(node) is not int:
                    raise ValueError("Invalid observed node")
                center = evaluate("""(node => {
                  const e=window.__jevFast?.nodes.get(node);
                  if (!e?.isConnected ||
                      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
                  const r=window.__jevFast.abs(e), x=r.x+r.w/2, y=r.y+r.h/2;
                  return r.w && r.h && x>=0 && y>=0 && x<innerWidth && y<innerHeight
                    ? {x,y} : null;
                })(""" + json.dumps(node) + ")")
                if center is None:
                    raise StalePage("Scroll container changed. Observe again.")
                x, y = center["x"], center["y"]
                pos_expr = ("(() => { const e=window.__jevFast?.nodes.get("
                            + json.dumps(node) + "); return e ? e.scrollTop+','+e.scrollLeft : null; })()")
            # The wheel must hover first; the renderer also needs a beat between the move
            # and the wheel or it drops the event. If it still drops one, a scroll that
            # demonstrably did not happen is safe to dispatch again.
            before = evaluate(pos_expr)
            for _attempt in range(2):
                call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                time.sleep(0.05)
                try:
                    call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y,
                         deltaX=action.get("dx", 0), deltaY=action["delta"])
                except (RuntimeError, TimeoutError) as error:
                    raise ActionMayHaveApplied(
                        "Scroll input was dispatched; inspect before continuing."
                    ) from error
                time.sleep(0.08)
                try:
                    changed = evaluate(pos_expr) != before
                except (StalePage, RuntimeError, TimeoutError) as error:
                    raise ActionMayHaveApplied(
                        "Scroll input was dispatched before the page changed; inspect before continuing."
                    ) from error
                if changed:
                    break
        elif kind == "file":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            obj = call("Runtime.evaluate", expression="""(node => {
              const e=window.__jevFast?.nodes.get(node);
              return e?.isConnected && e.type==='file' ? e : null;
            })(""" + json.dumps(action["node"]) + ")")
            oid = obj.get("result", {}).get("objectId")
            if not oid:
                raise StalePage("File input changed. Observe again.")
            call("DOM.setFileInputFiles", files=[request["text"]], objectId=oid)
        elif kind == "key":
            press_key(call, action["key"])
        elif kind == "drag":
            if type(action["node"]) is not int or type(action["target_node"]) is not int:
                raise ValueError("Invalid observed node")
            if "geom" in action and type(action["geom"]) is not int:
                raise ValueError("Invalid observed node")
            points = evaluate("""(action => {
              const n=window.__jevFast?.nodes;
              const s=n.get(action.node), sg=n.get(action.geom ?? action.node), d=n.get(action.target_node);
              if (!s?.isConnected || !sg?.isConnected || !d?.isConnected ||
                  !s.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                  !sg.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                  !d.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              const c=e=>{const r=window.__jevFast.abs(e);
                return {x:r.x+r.w/2,y:r.y+r.h/2,w:r.w,h:r.h}};
              const a=c(sg), b=c(d);
              if (!a.w||!a.h||!b.w||!b.h||a.x<0||a.y<0||a.x>=innerWidth||a.y>=innerHeight||
                  b.x<0||b.y<0||b.x>=innerWidth||b.y>=innerHeight) return null;
              return {sx:a.x, sy:a.y, dx:b.x, dy:b.y};
            })(""" + json.dumps(action) + ")")
            if points is None:
                raise StalePage("Drag source or target changed. Observe again.")
            call("Input.dispatchMouseEvent", type="mouseMoved", x=points["sx"], y=points["sy"])
            call("Input.dispatchMouseEvent", type="mousePressed", x=points["sx"], y=points["sy"],
                 button="left", clickCount=1)
            for step in (1, 2, 3):
                call("Input.dispatchMouseEvent", type="mouseMoved",
                     x=points["sx"] + (points["dx"] - points["sx"]) * step / 3,
                     y=points["sy"] + (points["dy"] - points["sy"]) * step / 3)
            call("Input.dispatchMouseEvent", type="mouseReleased", x=points["dx"], y=points["dy"],
                 button="left", clickCount=1)
        elif kind in ("back", "forward", "reload"):
            evaluate({"back": "history.back()", "forward": "history.forward()",
                      "reload": "location.reload()"}[kind])
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
              const r=g.getBoundingClientRect();
              if (!r.width || !r.height) return null;
              const root=g.getRootNode(), win=root.nodeType===11?root.ownerDocument.defaultView:root.defaultView;
              const lx=r.x+r.width/2, ly=r.y+r.height/2;
              if (lx<0 || ly<0 || lx>=win.innerWidth || ly>=win.innerHeight) return null;
              if (!g.contains(root.elementFromPoint(lx,ly))) return null;
              const ar=window.__jevFast.abs(g), x=ar.x+ar.w/2, y=ar.y+ar.h/2;
              if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
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
                if action.get("hover"):
                    call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                else:
                    for event in ("mousePressed", "mouseReleased"):
                        call("Input.dispatchMouseEvent", type=event, x=x, y=y,
                             button="right" if action.get("button") == "right" else "left",
                             clickCount=action.get("clicks", 1))
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
