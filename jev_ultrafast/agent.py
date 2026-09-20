"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import json
import time
from pathlib import Path

from .browser import ActionMayHaveApplied, Browser, NavigationBlocked, StalePage
from .compaction import compact_actions
from .config import load_runtime_config
from .model import action_space, choose, element_nodes, field_context, field_texts
from .policy import evaluate_policy, origin_allowed
from .questions import MAX_STEPS
from .trace import (
    TraceRecorder,
    goal_fingerprint,
    observation_summary,
    public_decision,
    public_history,
    public_text_calls,
    safe_url,
)
from .verify import verify_page

TERMINAL_STATUSES = {"done", "blocked", "confirm", "escalate"}


class AgentInterrupted(RuntimeError):
    """A caller cancellation or elapsed-time limit stopped the current stage."""

    def __init__(self, stage, category):
        super().__init__(f"Agent interrupted during {stage}: {category}")
        self.stage = stage
        self.category = category


def _verify(page, spec):
    """Backward-compatible wrapper around the declarative verification DSL."""
    return verify_page(page, spec)


def _text_key(context):
    """Cache key for a generated field value: survives action history, dies with the
    goal, the field, or the page text."""
    return json.dumps({"goal": context["goal"], "field": context["field"],
                       "page": context["page"]}, sort_keys=True)


class Agent:
    def __init__(
        self,
        url,
        goals,
        *,
        cdp_url=None,
        attach=None,
        record_dir=None,
        screenshots=False,
        trace_path=None,
        max_elapsed_ms=None,
        cancel_event=None,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not 1 <= len(task) <= 12000:
            raise ValueError("The goal must contain 1-12000 characters")
        plan = [task]
        # Freeze non-secret runtime limits for the life of this session.  A
        # process-level environment change must not silently change the action
        # space between prediction, preview, approval, and stale recovery.
        self.config = load_runtime_config()
        self.pending_text = None
        self.text_cache = {}
        self.record_dir = Path(record_dir) if record_dir else None
        if trace_path is None and self.record_dir:
            trace_path = self.record_dir / "trace.jsonl"
        self.trace = TraceRecorder(trace_path)
        self.max_elapsed_ms = max_elapsed_ms
        self.deadline_at = None
        self.cancel_event = cancel_event
        self.stage = "initial_observation"
        self.browser = Browser(url, cdp_url=cdp_url, attach=attach)
        self.browser.timeout_provider = self._remaining_timeout
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception as error:
            self.trace.record("interrupted", stage=self.stage, category=type(error).__name__)
            self.browser.close()
            self.trace.close(status="error")
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
            trace_path=str(self.trace.path) if self.trace.path else None,
        )
        self.trace.record("run_started", **goal_fingerprint(task), observation=observation_summary(page))
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        decision = self.state.get("decision") or {}
        request_state = (decision.get("request") or {}).get("state") or {}
        elements = request_state.get("elements")
        candidate_stats = decision.get("candidate_stats")
        candidate_nodes = decision.get("candidate_nodes")
        if elements is None:
            config = getattr(self, "config", None)
            limits = (config or load_runtime_config()).candidates
            candidates, candidate_stats = compact_actions(
                self.state["page"].get("actions", []),
                self.state.get("goal", ""),
                self.state.get("history", []),
                focus=self.state["page"].get("focus"),
                max_elements=limits.elements,
                max_actions=limits.actions,
                max_options_per_select=limits.options_per_select,
            )
            elements = action_space(candidates)[0]
            candidate_nodes = element_nodes(candidates)
        snapshot = {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "gate": self.state.get("gate"),
            "verify": self.state.get("verify"),
            "verified": self.state.get("verified"),
            "elements": elements,
            "element_nodes": candidate_nodes or {},
            "candidate_stats": candidate_stats,
        }
        snapshot["history"] = public_history(self.state.get("history", []))
        snapshot["decision"] = public_decision(self.state.get("decision"))
        snapshot["decisions"] = [public_decision(d) for d in self.state.get("decisions", [])]
        snapshot["text_calls"] = public_text_calls(self.state.get("text_calls", []))
        snapshot["trace_error"] = getattr(getattr(self, "trace", None), "error", None)
        return snapshot

    def _record(self, event, **payload):
        trace = getattr(self, "trace", None)
        if trace:
            trace.record(event, stage=getattr(self, "stage", None), **payload)

    def _hold_security_block(self, *, action_may_have_applied):
        state = self.state
        consume_security = getattr(state["browser"], "consume_security_block", None)
        security_block = consume_security() if callable(consume_security) else None
        if not isinstance(security_block, dict):
            return False
        reason = (
            "browser blocked navigation outside allowed origins: "
            f"{safe_url(security_block.get('url'))}"
        )
        state["status"] = "confirm"
        state["gate"] = {"verdict": "confirm", "reasons": [reason]}
        self._record(
            "security_block",
            reason=reason,
            action_may_have_applied=action_may_have_applied,
        )
        self._record("terminal", status="confirm", verified=False)
        return True

    def _hold_disallowed_origin(self, *, action_may_have_applied):
        state = self.state
        patterns = state.get("allowed_origins")
        url = state.get("page", {}).get("url", "")
        if not patterns or origin_allowed(url, patterns):
            return False
        reason = f"page is outside allowed origins: {safe_url(url)}"
        state["status"] = "confirm"
        state["gate"] = {"verdict": "confirm", "reasons": [reason]}
        state["decision"] = None
        self._record(
            "security_block",
            reason=reason,
            action_may_have_applied=action_may_have_applied,
        )
        self._record("terminal", status="confirm", verified=False)
        return True

    def _update_recovered_action(self, previous_page):
        state = self.state
        if state.get("started_at") is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        if not state.get("history") or state["history"][-1].get("page_changed") is not None:
            return
        state["history"][-1].update(
            page_changed=state["page"]["fingerprint"] != previous_page["fingerprint"],
            url=state["page"]["url"],
            elapsed_ms=state.get("elapsed_ms", 0),
        )

    def _checkpoint(self, stage):
        self.stage = stage
        cancel = getattr(self, "cancel_event", None)
        if cancel is not None and cancel.is_set():
            self._record("interrupted", category="cancelled")
            raise AgentInterrupted(stage, "cancelled")
        deadline = getattr(self, "deadline_at", None)
        if deadline is not None and time.monotonic() >= deadline:
            self._record("interrupted", category="deadline")
            raise AgentInterrupted(stage, "deadline")
        limit = getattr(self, "max_elapsed_ms", None)
        started = self.state.get("started_at") if hasattr(self, "state") else None
        if limit is not None and started is not None:
            if (time.perf_counter() - started) * 1000 >= limit:
                self._record("interrupted", category="deadline")
                raise AgentInterrupted(stage, "deadline")

    def _remaining_timeout(self):
        """Return the remaining bounded call time, or None for an unbounded run."""
        remaining = []
        deadline = getattr(self, "deadline_at", None)
        if deadline is not None:
            remaining.append(deadline - time.monotonic())
        limit = getattr(self, "max_elapsed_ms", None)
        started = self.state.get("started_at") if hasattr(self, "state") else None
        if limit is not None and started is not None:
            remaining.append(limit / 1000 - (time.perf_counter() - started))
        if not remaining:
            return None
        seconds = min(remaining)
        if seconds <= 0:
            self._record("interrupted", category="deadline")
            raise AgentInterrupted(self.stage, "deadline")
        return seconds

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            if state["status"] in TERMINAL_STATUSES:
                return self.snapshot()
            try:
                self._checkpoint("predict")
                self.command("predict", {})
                if state["status"] in TERMINAL_STATUSES:
                    return self.snapshot()
                self._checkpoint("act")
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage as error:
                action_may_have_applied = self.stage == "post_action_observation"
                self._record(
                    "stale",
                    reason=str(error),
                    action_may_have_applied=action_may_have_applied,
                )
                state["decision"] = None
                if state["status"] != "blocked":
                    state["status"] = "ready"
                previous_page = state["page"]
                self._checkpoint("reobserve")
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                self._record("observation", observation=observation_summary(state["page"]))
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                if action_may_have_applied:
                    self._update_recovered_action(previous_page)
                if self._hold_security_block(action_may_have_applied=action_may_have_applied):
                    return self.snapshot()
                if self._hold_disallowed_origin(action_may_have_applied=action_may_have_applied):
                    return self.snapshot()
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            self._checkpoint("pre_decision_observation")
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                self._record("observation", observation=observation_summary(state["page"]))
            state["decision"] = None
            if self._hold_security_block(action_may_have_applied=False):
                return self.snapshot()
            if self._hold_disallowed_origin(action_may_have_applied=False):
                return self.snapshot()
            # Caller-supplied success check runs before any paid decision call.
            if _verify(state["page"], state.get("verify")):
                state["status"] = "done"
                state["verified"] = True
                state["gate"] = {"verdict": "done", "reasons": ["verify check passed"]}
                self._record("verification", passed=True)
                self._record("terminal", status="done", verified=True)
                return self.snapshot()
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            self._checkpoint("decision")
            timeout = self._remaining_timeout()
            config = getattr(self, "config", None)
            choose_options = {
                "candidate_limits": (config or load_runtime_config()).candidates,
            }
            if timeout is not None:
                choose_options["timeout"] = timeout
            decision = choose(state["page"], state["goal"], state["history"], **choose_options)
            self._checkpoint("decision_result")
            # Do not publish an ungated model result. A cancellation, deadline, or
            # policy failure after the provider response must leave no decision
            # that approve=True could force-execute.
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
            candidate_stats = decision.get("candidate_stats") or {}
            if ((decision["operation"] in {"DONE", "BLOCKED"}
                 or decision["gate"]["verdict"] == "done")
                    and (candidate_stats.get("omitted", 0) or state["page"].get("omitted_actions", 0))):
                decision["gate"] = {
                    "verdict": "escalate",
                    "reasons": ["terminal decision made from a truncated candidate set"],
                }
            self._record(
                "decision",
                operation=decision["operation"],
                target=decision.get("target"),
                choice=decision.get("choice"),
                gate=decision["gate"],
                done=decision.get("done_p"),
                risk=decision.get("risk_p"),
                confidence=confidence,
                candidate_stats=candidate_stats,
            )
            state["decisions"].append(
                {
                    **decision,
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["decision"] = decision
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
                self._record("decision_held", status=state["status"], gate=gate)
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
                self._record("terminal", status="confirm", verified=False)
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
                    self._record("verification", passed=False)
                    self._record("terminal", status="escalate", verified=False)
                    return self.snapshot()
                state["status"] = "done" if wants_done else "blocked"
                state["verified"] = wants_done and bool(state.get("verify"))
                state["plan_index"] = int(wants_done)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._record("terminal", status=state["status"], verified=state["verified"])
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
                self._record("terminal", status="blocked", reason="action budget")
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] in ("fill", "file"):
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                self._record("text_requested", field=action["label"])
                key = _text_key(context)
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                elif key in self.text_cache:
                    text, helper = self.text_cache.pop(key), {"model": "cached", "latency_ms": 0}
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
                else:
                    # Speculative batch: values for every other empty fill field come
                    # back in the same helper call, cached by context for later steps.
                    candidate_nodes = set((decision.get("candidate_nodes") or {}).values())
                    others = [
                        a for a in page["actions"]
                        if a["kind"] in ("fill", "file")
                        and a["id"] != action["id"]
                        and not a.get("value")
                        and (not candidate_nodes or a.get("node") in candidate_nodes)
                    ][:15]
                    contexts = [context] + [
                        field_context(state["goal"], a, page, state["history"]) for a in others]
                    timeout = self._remaining_timeout()
                    text_options = {"timeout": timeout} if timeout is not None else {}
                    values, helper = field_texts(contexts, **text_options)
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
            self._checkpoint("action")
            self._record(
                "action_attempted",
                operation=decision["operation"],
                choice=selected,
                target_label=action["label"],
                text_supplied=bool(text),
                before_fingerprint=page["fingerprint"],
            )
            try:
                state["browser"].act(action, page, text=text)
            except NavigationBlocked as error:
                state["status"] = "confirm"
                state["gate"] = {"verdict": "confirm", "reasons": [str(error)]}
                self._record("security_block", reason=str(error), action_may_have_applied=False)
                self._record("terminal", status="confirm", verified=False)
                return self.snapshot()
            except ActionMayHaveApplied as error:
                state["status"] = "escalate"
                state["gate"] = {
                    "verdict": "escalate",
                    "reasons": ["browser input was dispatched but its result is unknown"],
                }
                self.pending_text = None
                self._record(
                    "action_result_unknown",
                    operation=decision["operation"],
                    choice=selected,
                    reason=str(error),
                    action_may_have_applied=True,
                )
                self._record("terminal", status="escalate", verified=False)
                raise
            except StalePage as error:
                state["stale"] = state.get("stale", 0) + 1
                self._record("stale", reason=str(error), action_may_have_applied=False)
                if state["stale"] >= 5:
                    state["status"] = "blocked"
                raise
            self._record(
                "action_executed",
                operation=decision["operation"],
                choice=selected,
                target_label=action["label"],
            )
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
            self._checkpoint("post_action_observation")
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            self._record("observation", observation=observation_summary(state["page"]))
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
            if self._hold_security_block(action_may_have_applied=True):
                return self.snapshot()
            if self._hold_disallowed_origin(action_may_have_applied=True):
                return self.snapshot()
            if _verify(state["page"], state.get("verify")):
                state["status"] = "done"
                state["verified"] = True
                state["gate"] = {"verdict": "done", "reasons": ["verify check passed"]}
                self._record("verification", passed=True)
                self._record("terminal", status="done", verified=True)
                return self.snapshot()
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
            if state["status"] == "blocked":
                self._record("terminal", status="blocked", reason="three actions made no progress")
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in TERMINAL_STATUSES:
            yield self.command("tick")

    def close(self):
        try:
            self.browser.close()
        finally:
            trace = getattr(self, "trace", None)
            if trace:
                trace.close(status=getattr(self, "state", {}).get("status"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
