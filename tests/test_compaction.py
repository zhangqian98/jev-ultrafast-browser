from jev_ultrafast.compaction import compact_actions


def _click(index, label, **extra):
    return {"id": f"c{index}", "node": index, "kind": "click", "role": "link", "label": label, **extra}


def test_operation_quotas_prevent_clicks_from_crowding_out_other_kinds():
    actions = [_click(i, f"Search result {i}") for i in range(1, 80)]
    actions += [
        {"id": "fill", "node": 100, "kind": "fill", "role": "textbox", "label": "Destination", "value": ""},
        {"id": "open-fill", "node": 100, "kind": "click", "role": "textbox", "label": "Open Destination"},
        {"id": "select", "node": 101, "kind": "select", "role": "combobox",
         "label": "Sort → Price", "current_value": "Relevant", "value": "price"},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]

    compacted, stats = compact_actions(
        actions, "Search for a destination and sort by price", max_elements=5, max_actions=10,
    )
    ids = {action["id"] for action in compacted}
    assert {"fill", "open-fill", "select", "wait"} <= ids
    assert stats["truncated"] is True
    assert stats["by_operation"]["TYPE_TEXT"]["sent"] == 1
    assert stats["by_operation"]["SELECT"]["sent"] == 1


def test_structural_state_is_kept_without_goal_text_overlap():
    actions = [_click(i, f"Unrelated {i}") for i in range(1, 30)]
    actions += [
        _click(100, "Dismiss", modal=True),
        _click(101, "Current field", focused=True),
        {"id": "required", "node": 102, "kind": "fill", "role": "textbox",
         "label": "Reference", "required": True, "value": ""},
    ]
    compacted, _stats = compact_actions(actions, "Open a report", max_elements=3, max_actions=6)
    nodes = {action.get("node") for action in compacted}
    assert {100, 101, 102} <= nodes


def test_relevant_select_options_are_kept_under_action_budget():
    actions = [
        {"id": f"o{i}", "node": 1, "kind": "select", "role": "combobox",
         "label": f"City → City {i}", "current_value": "", "value": str(i)}
        for i in range(30)
    ]
    actions[23]["label"] = "City → Zurich"
    actions.append({"id": "wait", "kind": "wait", "label": "Wait"})
    compacted, stats = compact_actions(
        actions, "Choose Zurich", max_elements=2, max_actions=5, max_options_per_select=30,
    )
    ids = [action["id"] for action in compacted]
    assert "o23" in ids
    assert "wait" in ids
    assert len(ids) == 5
    assert stats["by_operation"]["SELECT"]["sent"] == 4


def test_compaction_preserves_original_order_and_reports_counts():
    actions = [
        _click(1, "Alpha"),
        {"id": "scroll", "kind": "scroll", "label": "Scroll down"},
        _click(2, "Beta", focused=True),
        _click(3, "Gamma"),
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]
    compacted, stats = compact_actions(actions, "Beta", max_elements=1, max_actions=4)
    assert [action["id"] for action in compacted] == ["scroll", "c2", "wait"]
    assert stats["observed"] == 5
    assert stats["sent"] == 3
    assert stats["omitted_elements"] == 2


def test_many_required_fields_do_not_consume_every_operation_quota():
    actions = [
        {"id": f"r{i}", "node": i, "kind": "fill", "role": "textbox",
         "label": f"Required {i}", "required": True, "value": ""}
        for i in range(1, 12)
    ]
    actions += [
        {"id": "choice", "node": 20, "kind": "select", "role": "combobox",
         "label": "Mode → Fast", "current_value": "", "value": "fast"},
        _click(21, "Continue"),
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]
    compacted, _stats = compact_actions(actions, "Choose Fast and continue",
                                         max_elements=4, max_actions=8)
    ids = {action["id"] for action in compacted}
    assert {"choice", "c21", "wait"} <= ids


def test_chinese_goal_tokens_rank_matching_controls():
    actions = [_click(index, label) for index, label in enumerate(
        ["帮助中心", "账户信息", "删除账户", "返回首页"], start=1
    )]
    actions.append({"id": "wait", "kind": "wait", "label": "Wait"})
    compacted, _stats = compact_actions(actions, "删除这个账户", max_elements=1, max_actions=2)
    assert [action["label"] for action in compacted if action.get("kind") == "click"] == ["删除账户"]


def test_many_page_controls_leave_room_for_element_actions():
    actions = [_click(index, f"Result {index}") for index in range(1, 20)]
    actions += [
        {"id": f"key_{index}", "kind": "key", "key": key, "label": f"Press {key}"}
        for index, key in enumerate([
            "Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
            "Ctrl+P", "Ctrl+F", "Ctrl+S", "Ctrl+B", "Ctrl+W",
        ])
    ]
    actions += [
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "back", "kind": "back", "label": "Go back"},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]
    compacted, stats = compact_actions(actions, "Open Result 19", max_elements=10, max_actions=20)
    assert any(action.get("kind") == "click" for action in compacted)
    assert any(action.get("kind") == "wait" for action in compacted)
    assert len(compacted) <= 20
    assert stats["by_operation"]["CLICK"]["sent"] > 0


def test_tiny_action_budget_still_keeps_an_element_and_never_overflows():
    actions = [
        _click(1, "Target"),
        {"id": "key_enter", "kind": "key", "key": "Enter", "label": "Press Enter"},
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]

    compacted, stats = compact_actions(
        actions, "Open Target", max_elements=1, max_actions=2,
    )

    assert len(compacted) == 2
    assert any(action.get("kind") == "click" for action in compacted)
    assert stats["sent"] == 2


def test_tiny_action_budget_prefers_fill_over_secondary_open_click():
    actions = [
        {"id": "fill", "node": 1, "kind": "fill", "role": "textbox",
         "label": "Destination", "value": ""},
        {"id": "open", "node": 1, "kind": "click", "role": "textbox",
         "label": "Open Destination", "value": ""},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]

    compacted, stats = compact_actions(
        actions, "Enter London as the destination", max_elements=1, max_actions=2,
    )

    assert [action["id"] for action in compacted] == ["fill", "wait"]
    assert stats["sent_elements"] == 1
