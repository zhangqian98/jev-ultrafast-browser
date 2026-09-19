"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import json
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_texts
from .policy import evaluate_policy
from .questions import MAX_STEPS

TERMINAL_STATUSES = {"done", "blocked", "confirm", "escalate"}


def _verify(page, spec):
    """Caller-supplied success check, e.g. {"text": "Saved", "url_contains": "/done"}.
    Every given condition must hold; returns None when no spec is configured."""
    if not spec:
        return None
    if spec.get("text") and spec["text"] not in page.get("text", ""):
        return False
    if spec.get("url_contains") and spec["url_contains"] not in page.get("url", ""):
        return False
    return True


def _text_key(context):
    """Cache key for a generated field value: survives action history, dies with the
    goal, the field, or the page text."""
    return json.dumps({"goal": context["goal"], "field": context["field"],
                       "page": context["page"]}, sort_keys=True)


class Agent:
    def __init__(self, url, goals, *, cdp_url=None, attach=None, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not 1 <= len(task) <= 12000:
            raise ValueError("The goal must contain 1-12000 characters")
        plan = [task]
        self.pending_text = None
        self.text_cache = {}
        self.browser = Browser(url, cdp_url=cdp_url, attach=attach)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            stale=0,
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "gate": self.state.get("gate"),
            "verify": self.state.get("verify"),
            "verified": self.state.get("verified"),
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] == "done":
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                if state["status"] != "blocked":
                    state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            # Caller-supplied success check runs before any paid decision call.
            if _verify(state["page"], state.get("verify")):
                state["status"] = "done"
                state["verified"] = True
                state["gate"] = {"verdict": "done", "reasons": ["verify check passed"]}
                return self.snapshot()
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            state["decision"] = choose(state["page"], state["goal"], state["history"])
            decision = state["decision"]
            label = decision["choice"]
            if decision["choice"] not in {"DONE", "BLOCKED"}:
                hit = next((a for a in state["page"]["actions"]
                            if a["id"] == decision["choice"]), None)
                if hit:
                    label = hit["label"]
                if decision["operation"] == "DRAG":
                    drop = next((a for a in state["page"]["actions"]
                                 if a["id"] == decision["drop"]), None)
                    if drop:
                        label = f"{label} onto {drop['label']}"
            confidence = decision.get("target_confidence")
            if confidence is None:
                confidence = decision["confidence"]
            decision["gate"] = evaluate_policy(
                operation=decision["operation"], label=label,
                done=decision.get("done_p"), risk=decision.get("risk_p"),
                confidence=confidence, thresholds=state.get("thresholds"))
            state["decisions"].append(
                {
                    **decision,
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            gate = decision.get("gate") or {"verdict": "proceed", "reasons": []}
            verdict = gate["verdict"]
            state["gate"] = gate
            if (not body.get("force") and not decision.get("approved")
                    and verdict in {"confirm", "escalate", "stop"}):
                # Held, not consumed: browser_step(approve=true) can still execute it.
                state["status"] = "blocked" if verdict == "stop" else verdict
                return self.snapshot()
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected == "REVIEW" and verdict != "done":
                # REVIEW carries no target; there is nothing to execute even under force.
                state["status"] = "confirm"
                state["gate"] = {"verdict": "confirm",
                                 "reasons": ["Jev returned REVIEW: inspect the page and "
                                            "authorize the next action yourself"]}
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            if selected in {"DONE", "BLOCKED"} or verdict == "done":
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                wants_done = verdict == "done" or selected == "DONE"
                if wants_done and _verify(page, state.get("verify")) is False:
                    state["status"] = "escalate"
                    state["gate"] = {"verdict": "escalate",
                                     "reasons": ["model reported done but the verify check failed"]}
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    return self.snapshot()
                state["status"] = "done" if wants_done else "blocked"
                state["verified"] = wants_done and state.get("verify") is not None
                state["plan_index"] = int(wants_done)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if decision["operation"] == "RIGHT_CLICK":
                action = {**action, "button": "right"}
            elif decision["operation"] == "DOUBLE_CLICK":
                action = {**action, "clicks": 2}
            elif decision["operation"] == "HOVER":
                action = {**action, "hover": True}
            elif decision["operation"] == "DRAG":
                drop = next(a for a in page["actions"] if a["id"] == decision["drop"])
                action = {**action, "kind": "drag", "target_node": drop["node"],
                          "label": f"Drag {action['label']} onto {drop['label']}"}
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] in ("fill", "file"):
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                key = _text_key(context)
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                elif key in self.text_cache:
                    text, helper = self.text_cache.pop(key), {"model": "cached", "latency_ms": 0}
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
                else:
                    # Speculative batch: values for every other empty fill field come
                    # back in the same helper call, cached by context for later steps.
                    others = [a for a in page["actions"]
                              if a["kind"] in ("fill", "file") and a["id"] != action["id"]
                              and not a.get("value")]
                    contexts = [context] + [
                        field_context(state["goal"], a, page, state["history"]) for a in others]
                    values, helper = field_texts(contexts)
                    text = values[0]
                    for c, v in zip(contexts[1:], values[1:]):
                        if v is not None:
                            self.text_cache[_text_key(c)] = v
                    while len(self.text_cache) > 16:
                        self.text_cache.pop(next(iter(self.text_cache)))
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            if action["kind"] == "file" and not (text and Path(text.strip()).is_file()):
                raise ValueError(f"Upload path does not exist: {text!r}")
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                state["browser"].act(action, page, text=text)
            except StalePage:
                state["stale"] = state.get("stale", 0) + 1
                if state["stale"] >= 5:
                    state["status"] = "blocked"
                raise
            state["stale"] = 0
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            if _verify(state["page"], state.get("verify")):
                state["status"] = "done"
                state["verified"] = True
                state["gate"] = {"verdict": "done", "reasons": ["verify check passed"]}
                return self.snapshot()
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in TERMINAL_STATUSES:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
