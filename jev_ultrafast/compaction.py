"""Generic, operation-aware compaction of observed browser actions.

Dense pages can expose hundreds of links or dropdown options.  This module keeps
the model request bounded without letting one prolific operation crowd every
other operation out of the candidate set.  It uses only generic UI state and
the user's goal; there are no site-specific labels or prepared field values.
"""

from __future__ import annotations

import re
from collections import Counter

_KIND_OPERATION = {
    "click": "CLICK",
    "fill": "TYPE_TEXT",
    "file": "UPLOAD_FILE",
    "select": "SELECT",
}
_OPERATION_ORDER = ("TYPE_TEXT", "UPLOAD_FILE", "SELECT", "CLICK")
_ROLE_BONUS = {
    "button": 4,
    "menuitem": 4,
    "menuitemradio": 4,
    "combobox": 4,
    "searchbox": 4,
    "textbox": 3,
    "spinbutton": 3,
    "checkbox": 3,
    "radio": 3,
    "switch": 3,
    "tab": 2,
    "link": 1,
}


def _tokens(value):
    text = str(value or "").lower()
    tokens = {token for token in re.findall(r"[a-z0-9]+", text) if len(token) >= 2}
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(sequence) >= 2:
            tokens.add(sequence)
            tokens.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return tokens


def _identity(action, index):
    node = action.get("node")
    return ("node", node) if node is not None else ("action", action.get("id"), index)


def _operation(action):
    kind = action.get("kind")
    if kind in _KIND_OPERATION:
        return _KIND_OPERATION[kind]
    if kind == "key":
        return "PRESS_KEY"
    return str(action.get("id") or kind or "CONTROL").upper()


def _score_group(group, goal_tokens, recent_tokens, focus):
    actions = group["actions"]
    representative = actions[0]
    text = " ".join(
        str(action.get(key, ""))
        for action in actions
        for key in ("label", "role", "current_value")
    ).lower()
    tokens = _tokens(text)
    score = 0
    score += 10 * len(tokens & goal_tokens)
    score += 5 * len(tokens & recent_tokens)
    if any(action.get("modal") for action in actions):
        score += 100
    if any(action.get("focused") for action in actions):
        score += 90
    if focus:
        if representative.get("node") == focus.get("node"):
            score += 90
        elif focus.get("label") and focus["label"].lower() in text:
            score += 30
    if any(action.get("required") for action in actions):
        score += 35
    score += max((_ROLE_BONUS.get(str(action.get("role", "")).lower(), 0) for action in actions), default=0)
    if "TYPE_TEXT" in group["operations"] and any(not action.get("value") for action in actions):
        score += 3
    if not any(str(action.get("label", "")).strip() for action in actions):
        score -= 10
    return score


def _option_score(action, goal_tokens, recent_tokens):
    tokens = _tokens(action.get("label"))
    return 10 * len(tokens & goal_tokens) + 4 * len(tokens & recent_tokens)


def _quota(max_elements, operation):
    shares = {"TYPE_TEXT": 0.08, "UPLOAD_FILE": 0.03, "SELECT": 0.12, "CLICK": 0.30}
    floors = {"TYPE_TEXT": 6, "UPLOAD_FILE": 2, "SELECT": 8, "CLICK": 24}
    return max(floors[operation], int(max_elements * shares[operation]))


def _control_score(action):
    kind = action.get("kind")
    if kind == "wait":
        return 100
    if kind == "key":
        if action.get("key") in {"Enter", "Escape"}:
            return 95
        if action.get("key") == "Tab":
            return 85
        return 75
    if kind == "scroll":
        return 90
    if kind in {"back", "forward", "reload"}:
        return 50
    return 60


def compact_actions(
    actions,
    goal,
    history=None,
    *,
    focus=None,
    max_elements=180,
    max_actions=300,
    max_options_per_select=40,
):
    """Return ``(actions, stats)`` with stable IDs and bounded candidates.

    Page controls such as WAIT, scrolling, navigation, and key presses are kept.
    Element groups are selected with per-operation quotas first, then by a generic
    relevance score.  The final list retains original observation order.
    """
    if type(max_elements) is not int or max_elements < 1:
        raise ValueError("max_elements must be a positive integer")
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    if type(max_options_per_select) is not int or max_options_per_select < 1:
        raise ValueError("max_options_per_select must be a positive integer")
    rows = list(actions or [])
    controls = []
    groups_by_id = {}
    for index, action in enumerate(rows):
        if action.get("kind") not in _KIND_OPERATION:
            controls.append((index, action))
            continue
        identity = _identity(action, index)
        group = groups_by_id.setdefault(identity, {
            "identity": identity,
            "first": index,
            "actions": [],
            "operations": set(),
        })
        group["actions"].append(action)
        group["operations"].add(_operation(action))

    history = history or []
    goal_tokens = _tokens(goal)
    recent_tokens = set()
    for item in history[-10:]:
        recent_tokens |= _tokens(item.get("action"))
    groups = list(groups_by_id.values())
    for group in groups:
        group["score"] = _score_group(group, goal_tokens, recent_tokens, focus)
    priority = sorted(groups, key=lambda group: (-group["score"], group["first"]))

    selected = {}
    # First protect the active modal/focus scope. Required fields get a bounded
    # reserve so a large form cannot consume every operation quota by itself.
    for group in priority:
        if len(selected) >= max_elements:
            break
        focused = bool(focus and group["actions"][0].get("node") == focus.get("node"))
        if focused or any(action.get("modal") or action.get("focused") for action in group["actions"]):
            selected[group["identity"]] = group
    required_budget = max(1, max_elements // 4)
    required_added = 0
    for group in priority:
        if len(selected) >= max_elements or required_added >= required_budget:
            break
        if group["identity"] in selected:
            continue
        if any(action.get("required") for action in group["actions"]):
            selected[group["identity"]] = group
            required_added += 1

    # Then reserve room for every supported operation represented on the page.
    # Small budgets use round-robin allocation so the first operation in this
    # list cannot consume all remaining elements before the others get one.
    candidates_by_operation = {
        operation: [group for group in priority if operation in group["operations"]]
        for operation in _OPERATION_ORDER
    }
    wanted_by_operation = {
        operation: min(len(candidates), _quota(max_elements, operation))
        for operation, candidates in candidates_by_operation.items()
    }

    def operation_count(operation):
        return sum(operation in group["operations"] for group in selected.values())

    def add_next(operation):
        for candidate in candidates_by_operation[operation]:
            if candidate["identity"] not in selected:
                selected[candidate["identity"]] = candidate
                return True
        return False

    for operation in _OPERATION_ORDER:
        if len(selected) >= max_elements:
            break
        if wanted_by_operation[operation] and operation_count(operation) == 0:
            add_next(operation)

    while len(selected) < max_elements:
        progressed = False
        for operation in _OPERATION_ORDER:
            if len(selected) >= max_elements:
                break
            if operation_count(operation) >= wanted_by_operation[operation]:
                continue
            progressed = add_next(operation) or progressed
        if not progressed:
            break

    for group in priority:
        if len(selected) >= max_elements:
            break
        selected.setdefault(group["identity"], group)

    # Controls are cheap and semantically distinct, but a low action budget must
    # still leave room for observed elements. Under pressure keep WAIT, the most
    # useful focused keys, and scrolling before generic navigation controls.
    desired_control_limit = max(4, max_actions // 4)
    # Direct callers may intentionally use budgets below the validated runtime
    # defaults. Keep at least one slot available for an observed element group.
    available_control_slots = max_actions if not groups else max(0, max_actions - 1)
    control_limit = min(len(controls), desired_control_limit, available_control_slots)
    selected_controls = sorted(
        controls,
        key=lambda item: (-_control_score(item[1]), item[0]),
    )[:control_limit]
    control_rows = sorted(selected_controls, key=lambda item: item[0])
    remaining = max(0, max_actions - len(control_rows))
    allocated = []
    admitted_groups = []
    extras = []
    for group in sorted(selected.values(), key=lambda item: (-item["score"], item["first"])):
        non_select = [action for action in group["actions"] if action.get("kind") != "select"]
        select = [action for action in group["actions"] if action.get("kind") == "select"]
        base = list(non_select)
        ranked_options = sorted(
            enumerate(select),
            key=lambda item: (-_option_score(item[1], goal_tokens, recent_tokens), item[0]),
        )[:max_options_per_select]
        if select:
            base.append(ranked_options[0][1])
        if len(base) > remaining and remaining > 0:
            # An editable element commonly contributes both TYPE_TEXT and a
            # secondary CLICK row. Under a tiny budget retain the operation
            # that can actually satisfy the field instead of dropping the
            # entire element group.
            preferred = sorted(
                base,
                key=lambda action: (
                    {"fill": 0, "file": 1, "select": 2, "click": 3}.get(
                        action.get("kind"), 4
                    ),
                    group["actions"].index(action),
                ),
            )
            base = preferred[:remaining]
        if not base or len(base) > remaining:
            continue
        admitted_groups.append(group)
        allocated.extend(base)
        remaining -= len(base)
        if select:
            extras.extend(
                (group["score"], _option_score(action, goal_tokens, recent_tokens), group["first"], offset, action)
                for offset, action in ranked_options[1:]
            )

    for _group_score, _option_relevance, _first, _offset, action in sorted(
        extras, key=lambda item: (-item[1], -item[0], item[2], item[3])
    ):
        if remaining <= 0:
            break
        allocated.append(action)
        remaining -= 1

    chosen_ids = {id(action) for action in allocated}
    chosen_ids.update(id(action) for _index, action in control_rows)
    compacted = [action for action in rows if id(action) in chosen_ids]

    observed_by_operation = Counter(_operation(action) for action in rows)
    sent_by_operation = Counter(_operation(action) for action in compacted)
    stats = {
        "observed": len(rows),
        "sent": len(compacted),
        "omitted": len(rows) - len(compacted),
        "observed_elements": len(groups),
        "sent_elements": len(admitted_groups),
        "omitted_elements": len(groups) - len(admitted_groups),
        "by_operation": {
            operation: {
                "observed": observed_by_operation[operation],
                "sent": sent_by_operation[operation],
            }
            for operation in sorted(observed_by_operation)
        },
        "truncated": len(compacted) < len(rows),
    }
    return compacted, stats
