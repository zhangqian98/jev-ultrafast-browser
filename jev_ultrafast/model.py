"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import time

import httpx

from .compaction import compact_actions
from .config import load_runtime_config
from .questions import DONE_JUDGMENT, NEXT_ACTION, RISK_JUDGMENT, TARGET, TEXT_VALUE, TEXT_VALUES

CLIENT = httpx.Client(http2=True, timeout=25)

# TypeSafe pricing: $42 per billion input tokens; output is free.
PRICE_PER_INPUT_TOKEN_USD = 0.042 / 1e6


def input_cost_usd(usage):
    tokens = (usage or {}).get("input_tokens", (usage or {}).get("inputTokens", 0)) or 0
    return tokens * PRICE_PER_INPUT_TOKEN_USD


def noul_value(answer):
    """Parse a noul answer to a 0..1 probability; absent answers return None so
    test doubles can omit them, malformed ones are rejected like bad choices."""
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul", answer.get("probability"))
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return number


def post_json(url, key, body, *, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None

    def remaining():
        if deadline is None:
            return None
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Model call deadline exceeded; no action executed.")
        return value

    def retry_delay(seconds):
        left = remaining()
        if left is not None and left <= seconds:
            raise TimeoutError("Model call deadline exceeded; no action executed.")
        time.sleep(seconds)

    for attempt in range(3):
        try:
            request_timeout = remaining()
            kwargs = {"json": body, "headers": {"Authorization": f"Bearer {key}"}}
            if request_timeout is not None:
                kwargs["timeout"] = max(0.001, request_timeout)
            response = CLIENT.post(url, **kwargs)
        except httpx.HTTPError:
            if attempt < 2:
                retry_delay(0.5 * 2**attempt)
                continue
            raise RuntimeError("Model connection failed; no action executed.") from None
        remaining()
        if response.status_code in {429, 529, 503} and attempt < 2:
            retry_delay(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "file": "UPLOAD_FILE",
                  "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind == "key":
            targets.setdefault("PRESS_KEY", {})[action["key"]] = action
            continue
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {
                k: action[k]
                for k in (
                    "role", "value", "checked", "selected", "expanded", "required",
                    "href", "form_action", "modal", "focused",
                )
                if k in action
            }
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    for element in elements:
        if "CLICK" in element["operations"]:
            element["operations"] += ["RIGHT_CLICK", "DOUBLE_CLICK", "HOVER", "DRAG"]
    return elements, targets, controls


def element_nodes(actions):
    """Map public element indices to the observed DOM node IDs behind them."""
    nodes = []
    seen = set()
    for action in actions:
        if action.get("kind") not in {"click", "fill", "file", "select"}:
            continue
        node = action.get("node")
        if node in seen:
            continue
        seen.add(node)
        nodes.append(node)
    return {str(index): node for index, node in enumerate(nodes, start=1)}


def choose(state, goal, history, *, timeout=None, candidate_limits=None):
    limits = candidate_limits or load_runtime_config().candidates
    compacted, candidate_stats = compact_actions(
        state["actions"], goal, history, focus=state.get("focus"),
        max_elements=limits.elements,
        max_actions=limits.actions,
        max_options_per_select=limits.options_per_select,
    )
    elements, targets, controls = action_space(compacted)
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "RIGHT_CLICK": "Right-click an element to open its context menu.",
        "DOUBLE_CLICK": "Double-click an element to open or pin it, or to select a word.",
        "HOVER": "Hover over an element to reveal tooltips or hover-only controls.",
        "DRAG": "Drag one element onto another to reorder, dock, resize, or split a view.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "UPLOAD_FILE": "Choose a local file for a file input. A helper supplies the absolute path.",
        "SELECT": "Select an observed dropdown value.",
        "PRESS_KEY": "Press one key or offered key combination: Enter confirms a focused input or "
        "highlighted item, Escape dismisses a menu or dialog, arrows move within an open list "
        "or menu, Tab moves focus, and Ctrl+... chords trigger app shortcuts such as Quick Open.",
    }
    operations = {key: labels[key] for key in targets}
    if "CLICK" in targets:
        operations.update({key: labels[key]
                           for key in ("RIGHT_CLICK", "DOUBLE_CLICK", "HOVER", "DRAG")})
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        DONE="Every requirement is visibly satisfied.",
        BLOCKED="No supported operation can progress.",
        REVIEW="Return control for human review before a consequential action: sending, "
               "posting, submitting an order or payment, booking, deletion, permission "
               "changes, sensitive-data entry, CAPTCHA, or security warnings.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        if operation == "PRESS_KEY":
            criteria = {key: {"key": key} for key in candidates}
        else:
            criteria = {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{
                        k: a[k]
                        for k in (
                            "role", "checked", "selected", "expanded", "required",
                            "href", "form_action", "modal", "focused",
                        )
                        if k in a
                    },
                }
                for index, a in candidates.items()
            }
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": criteria,
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    if "CLICK" in targets:
        for head, role in (("drag_source", "the element to drag"),
                           ("drag_target", "the element to drop onto")):
            questions[head] = {
                "type": "choice",
                "criteria": questions["click_target"]["criteria"],
                "instructions": {"goal": goal, "operation": "DRAG", "role": role,
                                 "rules": [NEXT_ACTION, TARGET]},
            }
    # Graded judgments alongside the choices; the policy gate thresholds them in code.
    questions["done"] = {"type": "noul", "instructions": {"goal": goal, "rules": DONE_JUDGMENT}}
    questions["risk"] = {"type": "noul", "instructions": {"goal": goal, "rules": RISK_JUDGMENT}}
    page = {k: state[k] for k in ("url", "title", "text")}
    offscreen = state.get("offscreen") or {}
    if offscreen.get("above") or offscreen.get("below"):
        page["offscreen"] = offscreen
    for key in ("selected_controls", "focus", "alerts"):
        if state.get(key):
            page[key] = state[key]
    if state.get("omitted_actions"):
        page["omitted_actions"] = state["omitted_actions"]
    page["candidate_stats"] = candidate_stats
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": page,
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    post_options = {"timeout": timeout} if timeout is not None else {}
    result = post_json(
        "https://api.typesafe.ai/v1/systemone",
        os.environ["TYPESAFE_API_KEY"],
        body,
        **post_options,
    )
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    # RIGHT_CLICK, DOUBLE_CLICK, HOVER, and DRAG share CLICK's elements and target head(s);
    # only the pointer sequence differs at execution.
    drop = None
    if operation == "DRAG":
        target_answer = validate_choice(result["answers"].get("drag_source", {}), targets["CLICK"])
        drop_answer = validate_choice(result["answers"].get("drag_target", {}), targets["CLICK"])
        target = target_answer["choice"]
        if target == drop_answer["choice"]:
            raise ValueError("Invalid TypeSafe response: drag source equals its drop target")
        choice = targets["CLICK"][target]["id"]
        drop = targets["CLICK"][drop_answer["choice"]]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index]
                         for index, a in targets["CLICK"].items()}
    else:
        target_op = "CLICK" if operation in {"RIGHT_CLICK", "DOUBLE_CLICK", "HOVER"} else operation
        if target_op in targets:
            # Unused target heads cannot cause an action. Validate the head selected by the operation.
            target_answer = validate_choice(
                result["answers"].get(target_op.lower() + "_target", {}), targets[target_op])
            target = target_answer["choice"]
            choice = targets[target_op][target]["id"]
            probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[target_op].items()}
        else:
            choice = controls[operation]["id"] if operation in controls else operation
            probabilities[choice] = operation_answer["probabilities"][operation]
    usage = result.get("usage", {})
    return {
        "choice": choice,
        "drop": drop,
        "operation": operation,
        "target": target,
        "done_p": noul_value(result["answers"].get("done")),
        "risk_p": noul_value(result["answers"].get("risk")),
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": usage,
        "cost_usd": input_cost_usd(usage),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
        "candidate_stats": candidate_stats,
        "candidate_nodes": element_nodes(compacted),
    }


def field_context(goal, action, page, history):
    context = {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {
            "document_id": page.get("document_id") or [page.get("url")],
            "title": page["title"],
            "text": page["text"][:6000],
        },
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }
    for key in ("selected_controls", "focus", "alerts"):
        if page.get(key):
            context["page"][key] = page[key]
    return context


def field_texts(contexts, *, timeout=None):
    """One text-helper call for one or more field contexts (contexts[0] is the chosen field).

    Returns (values, helper); values[i] is a non-empty string or None. values[0] is
    guaranteed non-None or a ValueError is raised, matching the old field_text contract.
    """
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    if len(contexts) == 1:
        system, user = TEXT_VALUE, contexts[0]
    else:
        system = TEXT_VALUES
        user = {
            "goal": contexts[0]["goal"],
            "page": contexts[0]["page"],
            "recent_actions": contexts[0]["recent_actions"],
            "fields": [{"index": i, **c["field"]} for i, c in enumerate(contexts)],
        }
    started = time.perf_counter()
    post_options = {"timeout": timeout} if timeout is not None else {}
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(user),
                },
            ],
        },
        **post_options,
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        if len(contexts) == 1:
            if set(output) != {"text"}:
                raise ValueError()
            raw = [output["text"]]
        else:
            if set(output) != {"texts"} or set(output["texts"]) != {str(i) for i in range(len(contexts))}:
                raise ValueError()
            raw = [output["texts"][str(i)] for i in range(len(contexts))]
        values = [v if isinstance(v, str) and v.strip() and len(v) <= 2000 else None for v in raw]
        if values[0] is None:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return values, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }


def field_text(context, *, timeout=None):
    value, helper = field_texts([context], timeout=timeout)
    return value[0], helper
