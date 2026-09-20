"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import threading
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import ActionMayHaveApplied, StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK", "RIGHT_CLICK",
                                       "DOUBLE_CLICK", "HOVER", "DRAG"]
    assert elements[1]["operations"] == ["CLICK", "RIGHT_CLICK",
                                         "DOUBLE_CLICK", "HOVER", "DRAG"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert d["candidate_nodes"] == {"1": 10, "2": 20}
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target",
                                         "drag_source", "drag_target", "done", "risk"}


@pytest.mark.parametrize("operation", ["RIGHT_CLICK", "DOUBLE_CLICK", "HOVER"])
def test_pointer_operations_reuse_the_click_target_head(monkeypatch, operation):
    def post(_url, _key, body):
        assert operation in body["questions"]["operation"]["criteria"]
        assert operation.lower() + "_target" not in body["questions"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], operation),
                "click_target": choice(["1", "2"], "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Operate on Go", [])
    assert d["operation"] == operation and d["choice"] == "e3" and d["target"] == "2"


def test_file_input_gets_an_upload_operation():
    p = page()
    p["actions"].insert(2, {"id": "e5", "kind": "file",
                            "label": "Upload file to Avatar", "node": 30})
    elements, targets, _controls = model.action_space(p["actions"])
    assert targets["UPLOAD_FILE"]["2"]["id"] == "e5"
    assert elements[1]["operations"] == ["UPLOAD_FILE"]


def test_drag_uses_two_target_heads_and_rejects_self_drop(monkeypatch):
    seen = {}

    def post(_url, _key, body):
        assert "DRAG" in body["questions"]["operation"]["criteria"]
        assert body["questions"]["drag_source"]["criteria"] == \
            body["questions"]["drag_target"]["criteria"]
        seen["self_drop"] = seen.get("self_drop", False)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "DRAG"),
                "drag_source": choice(["1", "2"], "1"),
                "drag_target": choice(["1", "2"], "1" if seen["self_drop"] else "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Drag the search box onto Go", [])
    assert d["operation"] == "DRAG" and d["choice"] == "e2" and d["drop"] == "e3"
    seen["self_drop"] = True
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Drag the search box onto itself", [])


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.text_cache = {}
    p = page()
    a.state = {
        "browser": Mock(
            fresh=Mock(return_value=True),
            observe=Mock(return_value=p),
            consume_security_block=Mock(return_value=None),
        ),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_right_click_act_passes_the_right_button(runner):
    runner.state["decision"] = {**decision("e3"), "operation": "RIGHT_CLICK", "target": "2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    action = runner.state["browser"].act.call_args.args[0]
    assert action["id"] == "e3" and action["button"] == "right"
    assert runner.state["history"][-1]["operation"] == "RIGHT_CLICK"


def test_pointer_operations_pass_execution_modifiers(runner):
    for operation, expect in (("DOUBLE_CLICK", {"clicks": 2}),
                              ("HOVER", {"hover": True})):
        runner.state["decision"] = {**decision("e3"), "operation": operation, "target": "2"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        action = runner.state["browser"].act.call_args.args[0]
        assert all(action[k] == v for k, v in expect.items())


def test_drag_act_builds_a_drag_action(runner):
    runner.state["decision"] = {**decision("e3"), "operation": "DRAG", "target": "2",
                                "drop": "e2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    action = runner.state["browser"].act.call_args.args[0]
    assert action["kind"] == "drag" and action["node"] == 20 and action["target_node"] == 10


def test_browser_operation_dispatches_the_right_button():
    sent = []

    def send(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 5, "y": 6}}}
        return {}

    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "e1", "kind": "click", "node": 1,
                                  "button": "right", "label": "x"},
                       "text": None, "cdp": send})
    clicks = [p for m, p in sent if m == "Input.dispatchMouseEvent"]
    assert [c["type"] for c in clicks] == ["mousePressed", "mouseReleased"]
    assert all(c["button"] == "right" for c in clicks)


def test_browser_operation_hover_and_drag_and_nav():
    sent = []

    def send(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            expr = params["expression"]
            if "sx" in expr:  # drag geometry probe
                return {"result": {"value": {"sx": 1, "sy": 1, "dx": 9, "dy": 9}}}
            return {"result": {"value": {"x": 5, "y": 6}}}
        return {}

    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "e1", "kind": "click", "node": 1,
                                  "hover": True, "label": "x"},
                       "text": None, "cdp": send})
    hover = [p["type"] for m, p in sent if m == "Input.dispatchMouseEvent"]
    assert hover == ["mouseMoved"]

    sent.clear()
    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "e1", "kind": "drag", "node": 1,
                                  "target_node": 2, "label": "x"},
                       "text": None, "cdp": send})
    drag = [p["type"] for m, p in sent if m == "Input.dispatchMouseEvent"]
    assert drag == ["mouseMoved", "mousePressed", "mouseMoved",
                    "mouseMoved", "mouseMoved", "mouseReleased"]

    sent.clear()
    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "back", "kind": "back", "label": "Back"},
                       "text": None, "cdp": send})
    assert sent[-1][1]["expression"] == "history.back()"


def test_upload_file_requires_an_existing_local_path(runner, monkeypatch, tmp_path):
    p = runner.state["page"]
    p["actions"].append({"id": "e5", "kind": "file",
                         "label": "Upload file to Avatar", "node": 30})
    p["fingerprint"] = fingerprint(p)
    f = tmp_path / "avatar.png"
    f.write_bytes(b"png")
    helper = Mock(side_effect=lambda ctxs: ([str(f) for _c in ctxs],
                                            {"model": "test", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_texts", helper)
    runner.state["decision"] = {**decision("e5"), "operation": "UPLOAD_FILE"}
    runner.command("act", {"fingerprint": p["fingerprint"]})
    action, _page = runner.state["browser"].act.call_args.args[:2]
    assert action["kind"] == "file"
    assert runner.state["browser"].act.call_args.kwargs["text"] == str(f)

    helper.side_effect = lambda ctxs: (["/no/such/file.png" for _c in ctxs],
                                     {"model": "test", "latency_ms": 1})
    runner.state["decision"] = {**decision("e5"), "operation": "UPLOAD_FILE"}
    with pytest.raises(ValueError, match="does not exist"):
        runner.command("act", {"fingerprint": p["fingerprint"]})
    assert runner.state["browser"].act.call_count == 1  # invalid path never reached the browser


def test_file_upload_sets_files_on_the_observed_input():
    sent = []

    def send(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "o1"}}
        return {}

    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "e1", "kind": "file", "node": 7, "label": "x"},
                       "text": "C:/tmp/a.png", "cdp": send})
    uploads = [p for m, p in sent if m == "DOM.setFileInputFiles"]
    assert uploads == [{"files": ["C:/tmp/a.png"], "objectId": "o1"}]


def test_horizontal_scroll_dispatches_delta_x():
    sent = []

    def send(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            v = send.pos
            send.pos = "0,560"
            return {"result": {"value": v}}
        return {}

    send.pos = "0,0"
    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "scroll_right", "kind": "scroll",
                                  "delta": 0, "dx": 560, "label": "Scroll right"},
                       "text": None, "cdp": send})
    wheels = [p for m, p in sent if m == "Input.dispatchMouseEvent"]
    assert [w["type"] for w in wheels] == ["mouseMoved", "mouseWheel"]
    assert wheels[-1] == {"type": "mouseWheel", "x": 550, "y": 650,
                          "deltaX": 560, "deltaY": 0}


def test_new_page_target_is_followed():
    from jev_ultrafast.browser import Browser
    b = Browser.__new__(Browser)
    b.follow, b.target, b.session, b.owned = True, "t0", "s0", True
    b._known_targets = {"t0"}
    b._owned_targets = {"t0"}
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "t0"},
                                    {"type": "page", "targetId": "t1", "openerId": "t0"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        return {}

    b.cdp = cdp
    b._maybe_retarget()
    assert (b.target, b.session) == ("t1", "s1")
    assert "t1" in b._owned_targets
    assert any(m == "Target.detachFromTarget" for m, _p in calls)


def test_press_key_sends_modifier_combinations():
    from jev_ultrafast.browser import press_key
    sent = []
    press_key(lambda m, **p: sent.append((m, p)), "Ctrl+Shift+P")
    events = [p for m, p in sent if m == "Input.dispatchKeyEvent"]
    assert [e["type"] for e in events] == ["rawKeyDown", "keyUp"]
    assert all(e["modifiers"] == 10 and e["key"] == "P" and e["code"] == "KeyP"
               for e in events)
    with pytest.raises(ValueError, match="Unsupported key"):
        press_key(lambda m, **p: None, "Ctrl+Alt+Delete")


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=(["book"], {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=(["book"], {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_field_context_binds_generated_text_to_the_observed_document():
    first = page()
    second = deepcopy(first)
    first["url"] = "https://one.test/form"
    second["url"] = "https://two.test/form"
    action = first["actions"][0]
    assert model.field_context("Enter Ada", action, first, []) != model.field_context(
        "Enter Ada", action, second, []
    )


def test_batch_field_text_parses_all_fields(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content":
        '{"texts":{"0":"Zurich","1":"London","2":null}}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    p = page()
    contexts = [model.field_context("Fly Zurich to London on Sep 20", a, p, [])
                for a in p["actions"][:2]]
    contexts.append(dict(contexts[0]))
    values, _helper = model.field_texts(contexts)
    assert values == ["Zurich", "London", None]
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert [f["index"] for f in sent["fields"]] == [0, 1, 2]


def test_batch_text_generation_fills_other_fields_from_one_call(runner, monkeypatch):
    p = runner.state["page"]
    p["actions"].insert(1, {"id": "e9", "kind": "fill", "label": "To", "role": "textbox",
                            "value": "", "node": 40})
    p["fingerprint"] = fingerprint(p)

    def helper(contexts):
        assert [c["field"]["label"] for c in contexts] == ["Search", "To"]
        return (["Zurich", "London"], {"model": "test", "latency_ms": 5})

    helper_mock = Mock(side_effect=helper)
    monkeypatch.setattr(loop, "field_texts", helper_mock)
    runner.command("act", {"fingerprint": p["fingerprint"]})
    assert runner.state["history"][-1]["text"] == "Zurich"
    assert helper_mock.call_count == 1

    runner.state["decision"] = decision("e9")
    runner.command("act", {"fingerprint": p["fingerprint"]})
    assert runner.state["history"][-1]["text"] == "London"
    assert helper_mock.call_count == 1  # second fill came from the batch cache
    assert runner.state["text_calls"][-1]["model"] == "cached"


def test_speculative_text_batch_is_bounded_and_respects_candidate_nodes(runner, monkeypatch):
    p = runner.state["page"]
    for index in range(30):
        p["actions"].append({
            "id": f"extra-{index}",
            "kind": "fill",
            "label": f"Extra {index}",
            "role": "textbox",
            "value": "",
            "node": 100 + index,
        })
    p["fingerprint"] = fingerprint(p)
    runner.state["decision"]["candidate_nodes"] = {
        "1": 10,
        **{str(index + 2): 100 + index for index in range(20)},
    }
    seen = {}

    def helper(contexts):
        seen["labels"] = [context["field"]["label"] for context in contexts]
        return (["chosen", *[f"value-{index}" for index in range(len(contexts) - 1)]],
                {"model": "test", "latency_ms": 1})

    monkeypatch.setattr(loop, "field_texts", helper)
    runner.command("act", {"fingerprint": p["fingerprint"]})

    assert seen["labels"][0] == "Search"
    assert len(seen["labels"]) == 16
    assert all(label == "Search" or label.startswith("Extra ") for label in seen["labels"])
    assert "Extra 20" not in seen["labels"]


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_tick_recovers_post_action_stale_without_replaying_mutation(runner, monkeypatch):
    fresh = page()
    fresh["text"] = "Search results"
    fresh["fingerprint"] = fingerprint(fresh)
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    runner.state["browser"].observe.side_effect = [StalePage("post-action read changed"), fresh]
    monkeypatch.setattr(loop, "choose", Mock(return_value={
        **decision("e3"),
        "operation": "CLICK",
        "target": "2",
        "operation_probabilities": {"CLICK": 1.0},
        "target_probabilities": {"2": 1.0},
        "candidate_stats": {"omitted": 0},
    }))

    snap = runner.command("tick")
    assert snap["status"] == "ready"
    assert runner.state["browser"].act.call_count == 1
    assert len(runner.state["history"]) == 1
    assert runner.state["history"][-1]["page_changed"] is True
    assert runner.state["history"][-1]["url"] == fresh["url"]


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize(("key", "value"), [
    ("title", "Changed title"),
    ("offscreen", {"above": [], "below": ["Checkout"]}),
    ("selected_controls", [{"node": 7, "role": "radio", "label": "One way", "checked": True}]),
    ("focus", {"node": 10, "role": "textbox", "label": "Search"}),
    ("alerts", [{"role": "status", "text": "Loaded"}]),
])
def test_fingerprint_tracks_structured_policy_state(key, value):
    p = page()
    other = deepcopy(p)
    other[key] = value
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def key_page():
    p = page()
    p["actions"].append({"id": "key_enter", "kind": "key", "key": "Enter", "label": "Press Enter"})
    p["actions"].append({"id": "key_escape", "kind": "key", "key": "Escape", "label": "Press Escape"})
    p["fingerprint"] = fingerprint(p)
    return p


def test_key_actions_are_press_key_targets_not_elements():
    elements, targets, controls = model.action_space(key_page()["actions"])
    assert len(elements) == 2
    assert set(targets["PRESS_KEY"]) == {"Enter", "Escape"}
    assert targets["PRESS_KEY"]["Enter"]["id"] == "key_enter"


def test_press_key_head_uses_key_criteria_and_executes_key_action(monkeypatch):
    def post(_url, _key, body):
        questions = body["questions"]
        assert questions["press_key_target"]["criteria"] == {
            "Enter": {"key": "Enter"}, "Escape": {"key": "Escape"}}
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "PRESS_KEY"),
                "press_key_target": choice(["Enter", "Escape"], "Enter"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(key_page(), "Dismiss the dialog", [])
    assert d["operation"] == "PRESS_KEY" and d["target"] == "Enter" and d["choice"] == "key_enter"


def test_key_action_dispatches_keydown_then_keyup():
    cdp = Mock(return_value={})
    result = browser_operation({"operation": "act", "session": "test", "cdp": cdp,
                                "action": {"id": "key_enter", "kind": "key", "key": "Enter"}})
    assert result == {"executed": "key_enter"}
    calls = [c for c in cdp.call_args_list if c.args[0] == "Input.dispatchKeyEvent"]
    assert len(calls) == 2
    down, up = (c.kwargs for c in calls)
    assert down["type"] == "keyDown" and down["text"] == "\r"
    assert down["key"] == down["code"] == "Enter"
    assert down["windowsVirtualKeyCode"] == down["nativeVirtualKeyCode"] == 13
    assert up["type"] == "keyUp" and up["windowsVirtualKeyCode"] == 13


def test_key_action_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unsupported key"):
        browser_operation({"operation": "act", "session": "test", "cdp": Mock(),
                           "action": {"id": "key_f1", "kind": "key", "key": "F1"}})


def test_scroll_in_container_resolves_center():
    def send(method, session_id=None, **params):
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "scrollTop" in expr:
                v = send.pos
                send.pos = "200,0"
                return {"result": {"value": v}}
            return {"result": {"value": {"x": 300, "y": 400}}}
        return {}

    send.pos = "0,0"
    cdp = Mock(side_effect=send)
    result = browser_operation({"operation": "act", "session": "test", "cdp": cdp,
                                "action": {"id": "scroll_down_5", "kind": "scroll",
                                           "node": 5, "delta": 200}})
    assert result == {"executed": "scroll_down_5"}
    wheels = [c for c in cdp.call_args_list
              if c.args[0] == "Input.dispatchMouseEvent" and c.kwargs["type"] == "mouseWheel"]
    assert len(wheels) == 1
    wheel = wheels[0]
    assert wheel.kwargs["x"] == 300 and wheel.kwargs["y"] == 400 and wheel.kwargs["deltaY"] == 200


def test_dropped_wheel_is_dispatched_again():
    sent = []

    def send(method, session_id=None, **params):
        sent.append(method)
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "scrollY" in expr or "scrollX" in expr:
                return {"result": {"value": "0,0"}}  # wheel never lands
            return {"result": {"value": None}}
        return {}

    browser_operation({"operation": "act", "session": "s",
                       "action": {"id": "scroll_down", "kind": "scroll",
                                  "delta": 560, "label": "Scroll down"},
                       "text": None, "cdp": send})
    assert sent.count("Input.dispatchMouseEvent") == 4  # two hover+wheel attempts


def test_scroll_that_loses_its_document_after_dispatch_is_not_retryable():
    sent = []
    reads = 0

    def send(method, session_id=None, **params):
        nonlocal reads
        sent.append(method)
        if method == "Runtime.evaluate":
            reads += 1
            if reads == 1:
                return {"result": {"value": "0,0"}}
            raise StalePage("document changed")
        return {}

    with pytest.raises(ActionMayHaveApplied, match="Scroll input was dispatched"):
        browser_operation({"operation": "act", "session": "s", "cdp": send,
                           "action": {"id": "scroll_down", "kind": "scroll",
                                      "delta": 560, "label": "Scroll down"}})
    assert "Input.dispatchMouseEvent" in sent


def test_scroll_in_gone_container_is_stale():
    cdp = Mock(return_value={"result": {"value": None}})
    with pytest.raises(StalePage, match="Scroll container"):
        browser_operation({"operation": "act", "session": "test", "cdp": cdp,
                           "action": {"id": "scroll_down_5", "kind": "scroll",
                                      "node": 5, "delta": 200}})


def test_stale_streak_blocks_after_five(runner):
    runner.state["stale"] = 0
    runner.state["browser"].act.side_effect = StalePage("stale")
    for _ in range(5):
        runner.state["decision"] = decision("e3")
        with pytest.raises(StalePage):
            runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["stale"] == 5 and runner.state["status"] == "blocked"


def test_successful_act_resets_stale_streak(runner):
    runner.state["stale"] = 3
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["stale"] == 0


def test_fill_uses_geom_element_for_geometry():
    sent = {}

    def send(method, session_id=None, **params):
        if method == "Runtime.evaluate":
            sent["expr"] = params["expression"]
            return {"result": {"value": {"x": 50, "y": 60}}}
        return {}

    cdp = Mock(side_effect=send)
    result = browser_operation({"operation": "act", "session": "test", "cdp": cdp,
                                "action": {"id": "e1", "kind": "fill", "node": 10, "geom": 11},
                                "text": "hi"})
    assert result == {"executed": "e1"}
    assert '"geom": 11' in sent["expr"]
    clicks = [c for c in cdp.call_args_list if c.args[0] == "Input.dispatchMouseEvent"]
    assert clicks and all(c.kwargs["x"] == 50 and c.kwargs["y"] == 60 for c in clicks)
    assert any(c.args[0] == "Input.insertText" and c.kwargs["text"] == "hi"
               for c in cdp.call_args_list)


def test_geom_must_be_an_observed_int():
    with pytest.raises(ValueError, match="Invalid observed node"):
        browser_operation({"operation": "act", "session": "test", "cdp": Mock(),
                           "action": {"id": "e1", "kind": "click", "node": 10, "geom": "11"}})


def test_transient_http_error_retries(monkeypatch):
    import httpx

    post = Mock(side_effect=[
        httpx.ConnectError("boom"),
        Mock(status_code=200, is_error=False, json=lambda: {"ok": 1}),
    ])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)
    assert model.post_json("https://x.test", "k", {}) == {"ok": 1}
    assert post.call_count == 2


def test_wait_executes_on_a_changed_page():
    from jev_ultrafast.browser import Browser

    b = Browser.__new__(Browser)
    b.fresh = Mock(return_value=False)
    b.cdp = Mock()
    b.session = "test"
    assert b.act({"id": "wait", "kind": "wait"}, {"marker": 1}) == {"executed": "wait"}
    with pytest.raises(StalePage):
        b.act({"id": "e1", "kind": "click", "node": 1}, {"marker": 1})


def test_key_freshness_tracks_focus_without_invalidating_general_page_freshness():
    from jev_ultrafast.browser import Browser

    b = Browser.__new__(Browser)
    focus = [1, "textbox", "Search", "true", None]
    b.evaluate = Mock(side_effect=[["page-marker", focus], "page-marker"])
    page_state = {
        "focus_guard": focus,
        "marker": "page-marker",
    }
    assert b.fresh(page_state, {"kind": "key", "key": "Enter"})
    assert b.fresh(page_state)


def test_direct_cdp_roundtrip_events_and_errors():
    import threading

    from websockets.sync.server import serve

    from jev_ultrafast.browser import DirectCDP

    received = []

    def handler(sock):
        for raw in sock:
            msg = json.loads(raw)
            received.append(msg)
            sock.send(json.dumps({"method": "Target.targetCreated", "params": {}}))
            if msg["method"] == "Target.getTargets":
                sock.send(json.dumps({"id": msg["id"], "result": {"targetInfos": []}}))
            elif msg["method"] == "Boom":
                sock.send(json.dumps({"id": msg["id"],
                                      "error": {"code": -32000, "message": "Not supported"}}))

    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        conn = DirectCDP.from_ws_url(f"ws://127.0.0.1:{port}")
        assert conn("Target.getTargets", session_id="s1") == {"targetInfos": []}
        assert received[0]["sessionId"] == "s1"
        with pytest.raises(RuntimeError, match="Not supported"):
            conn("Boom")
        conn.close()
    finally:
        server.shutdown()


def test_choose_asks_done_and_risk_nouls(monkeypatch):
    def post(_url, _key, body):
        assert body["questions"]["done"]["type"] == "noul"
        assert body["questions"]["risk"]["type"] == "noul"
        return {
            "model": "test",
            "usage": {"input_tokens": 1000},
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(["1", "2"], "2"),
                "done": {"noul": 0.05},
                "risk": {"noul": 0.01},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert d["done_p"] == 0.05 and d["risk_p"] == 0.01
    assert d["cost_usd"] == pytest.approx(0.042 / 1e6 * 1000)


def test_policy_verdicts():
    from jev_ultrafast.policy import evaluate_policy, match_sensitive
    assert evaluate_policy(operation="CLICK", done=0.95)["verdict"] == "done"
    assert evaluate_policy(operation="CLICK", label="Delete account",
                           confidence=0.9)["verdict"] == "confirm"
    assert evaluate_policy(operation="CLICK", risk=0.8, confidence=0.9)["verdict"] == "confirm"
    assert evaluate_policy(operation="CLICK", confidence=0.2)["verdict"] == "stop"
    assert evaluate_policy(operation="CLICK", confidence=0.4)["verdict"] == "escalate"
    assert evaluate_policy(operation="CLICK", confidence=0.9)["verdict"] == "proceed"
    # Read-only exploration is exempt from the confidence floor (LOW_RISK relaxation).
    assert evaluate_policy(operation="WAIT", confidence=0.2)["verdict"] == "proceed"
    assert evaluate_policy(operation="SCROLL_DOWN", confidence=0.1)["verdict"] == "proceed"
    d = evaluate_policy(operation="DONE", done=0.3)
    assert d["verdict"] == "escalate" and "done probability" in d["reasons"][0]
    assert match_sensitive("Permanently delete repository") == "delete"
    assert match_sensitive("立即支付") == "payment"
    assert match_sensitive("Search") is None


def test_gated_decision_is_held_then_force_executes(runner):
    d = {**decision("e3"), "operation": "CLICK",
         "gate": {"verdict": "confirm", "reasons": ["sensitive 'delete' action"]}}
    runner.state["decision"] = d
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "confirm" and snap["gate"]["verdict"] == "confirm"
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is d  # held for approval, not consumed

    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"],
                           "force": True})
    assert runner.state["browser"].act.call_count == 1


def test_done_failing_verify_escalates(runner):
    runner.state["verify"] = {"text": "Order placed"}
    runner.state["decision"] = {**decision("DONE"), "operation": "DONE"}
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "escalate"
    assert "verify" in snap["gate"]["reasons"][0]


def test_done_passing_verify_finishes(runner):
    runner.state["verify"] = {"text": "Search"}
    runner.state["decision"] = {**decision("DONE"), "operation": "DONE"}
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "done"


def test_verify_passing_after_action_finishes_without_done(runner):
    seen = page()
    seen["text"] = "Order placed"
    runner.state["verify"] = {"text": "Order placed"}
    runner.state["browser"].observe.return_value = seen
    runner.state["decision"] = decision("e3")
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "done"


def test_predict_short_circuits_on_verify(runner, monkeypatch):
    runner.state["verify"] = {"text": "Search"}
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    choose = Mock(side_effect=AssertionError("no paid call when verify already passes"))
    monkeypatch.setattr(loop, "choose", choose)
    snap = runner.command("predict", {})
    assert snap["status"] == "done" and snap["gate"]["verdict"] == "done"
    choose.assert_not_called()


def test_review_operation_returns_confirm_even_under_force(runner):
    runner.state["decision"] = {**decision("REVIEW"), "operation": "REVIEW",
                                "gate": {"verdict": "confirm", "reasons": ["model chose REVIEW"]}}
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"],
                                  "force": True})
    assert snap["status"] == "confirm"
    runner.state["browser"].act.assert_not_called()


def test_done_without_verify_is_reported_unverified(runner):
    runner.state["decision"] = {**decision("DONE"), "operation": "DONE"}
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "done" and snap["verified"] is False


def test_origin_allowlist():
    from jev_ultrafast.policy import origin_allowed
    patterns = ["https://*.example.com", "http://localhost:8080"]
    assert origin_allowed("https://shop.example.com/a", patterns)
    assert origin_allowed("http://localhost:8080/x", patterns)
    assert origin_allowed("data:text/html,<b>x</b>", patterns)  # internal fixtures pass
    assert not origin_allowed("https://evil.com/", patterns)
    assert not origin_allowed("https://example.com.evil.com/", patterns)
    assert not origin_allowed("javascript:alert(1)", patterns)
    assert origin_allowed("https://anything.test/", None)
    assert origin_allowed("https://anything.test/", [])


def test_post_click_security_block_is_recorded_as_executed_then_held(runner):
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "target": "2"}
    runner.state["browser"].consume_security_block.return_value = {
        "type": "security",
        "url": "https://outside.test/",
    }
    snap = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snap["status"] == "confirm"
    assert snap["gate"]["verdict"] == "confirm"
    assert len(runner.state["history"]) == 1
    runner.state["browser"].act.assert_called_once()
    runner.state["browser"].observe.assert_called_once()


def test_direct_cdp_captures_diagnostic_logs():
    from jev_ultrafast.browser import DirectCDP
    conn = DirectCDP.__new__(DirectCDP)
    conn.logs, conn._log_id = [], 0
    conn._capture_log("Runtime.consoleAPICalled",
                      {"type": "error", "args": [{"value": "boom"}]})
    conn._capture_log("Runtime.exceptionThrown",
                      {"exceptionDetails": {"text": "Uncaught TypeError"}})
    conn._capture_log("Network.loadingFailed", {"errorText": "net::ERR_FAILED"})
    conn._capture_log("Page.frameNavigated", {"frame": {"url": "https://x.test/"}})
    conn._capture_log("Page.frameNavigated",
                      {"frame": {"url": "https://x.test/f", "parentId": "p"}})
    conn._capture_log("Browser.downloadWillBegin", {"suggestedFilename": "a.exe"})
    conn._capture_log("Target.attachedToTarget", {})  # ignored
    types = [e["type"] for e in conn.logs]
    assert types == ["console", "pageerror", "requestfailed",
                     "navigation", "download"]
    assert conn.logs[0]["text"] == "boom" and conn.logs[1]["level"] == "error"


def test_offscreen_controls_reach_the_model_state(monkeypatch):
    p = page()
    p["offscreen"] = {"above": [], "below": ["Checkout", "Place order"]}
    seen = {}

    def post(_url, _key, body):
        seen.update(body["state"]["page"])
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(["1", "2"], "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(p, "Check out", [])
    assert seen["offscreen"]["below"] == ["Checkout", "Place order"]
    seen.clear()
    p["offscreen"] = {"above": [], "below": []}
    model.choose(p, "Check out", [])
    assert "offscreen" not in seen  # empty lists are not sent


def test_structured_selected_focus_and_alert_state_reaches_model(monkeypatch):
    p = page()
    p["selected_controls"] = [
        {"node": 30, "role": "radio", "label": "One way", "checked": True}
    ]
    p["focus"] = {"node": 10, "role": "textbox", "label": "Search", "expanded": "true"}
    p["alerts"] = [{"role": "status", "text": "3 results loaded"}]
    seen = {}

    def post(_url, _key, body):
        seen.update(body["state"]["page"])
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(["1", "2"], "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(p, "Open results", [])
    assert seen["selected_controls"][0]["label"] == "One way"
    assert seen["focus"]["label"] == "Search"
    assert seen["alerts"][0]["text"] == "3 results loaded"


def test_review_is_an_offered_operation(monkeypatch):
    def post(_url, _key, body):
        assert "REVIEW" in body["questions"]["operation"]["criteria"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "REVIEW"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Delete everything", [])
    assert d["operation"] == "REVIEW" and d["choice"] == "REVIEW"


@pytest.mark.parametrize(("operation", "selected"), [("DONE", "DONE"), ("CLICK", "e3")])
def test_terminal_choice_from_truncated_candidates_is_held(runner, monkeypatch, operation, selected):
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    monkeypatch.setattr(loop, "choose", Mock(return_value={
        "choice": selected,
        "operation": operation,
        "target": None if operation == "DONE" else "2",
        "confidence": 1.0,
        "target_confidence": None,
        "probabilities": {selected: 1.0},
        "operation_probabilities": {operation: 1.0},
        "target_probabilities": {},
        "done_p": 0.99,
        "risk_p": 0.0,
        "latency_ms": 1,
        "usage": {},
        "candidate_stats": {"omitted": 1},
    }))
    snap = runner.command("predict")
    assert snap["decision"]["gate"]["verdict"] == "escalate"
    assert "truncated" in snap["decision"]["gate"]["reasons"][0]


def test_cancel_after_provider_response_never_publishes_an_ungated_decision(runner, monkeypatch):
    cancel = threading.Event()
    runner.cancel_event = cancel
    runner.deadline_at = None
    runner.state["status"] = "ready"
    runner.state["decision"] = None

    def choose_then_cancel(*_args, **_kwargs):
        cancel.set()
        return {
            **decision("e3"),
            "operation": "CLICK",
            "target": "2",
            "operation_probabilities": {"CLICK": 1.0},
            "target_probabilities": {"2": 1.0},
            "candidate_stats": {"omitted": 0},
        }

    monkeypatch.setattr(loop, "choose", choose_then_cancel)
    with pytest.raises(loop.AgentInterrupted, match="decision_result"):
        runner.command("predict")
    assert runner.state["decision"] is None


@pytest.mark.parametrize("status", ["done", "blocked", "confirm", "escalate"])
def test_tick_never_advances_a_terminal_or_held_run(runner, monkeypatch, status):
    held = runner.state["decision"]
    runner.state["status"] = status
    choose = Mock(side_effect=AssertionError("terminal tick must not predict"))
    monkeypatch.setattr(loop, "choose", choose)

    snapshot = runner.command("tick")

    assert snapshot["status"] == status
    assert runner.state["decision"] is held
    choose.assert_not_called()
    runner.state["browser"].act.assert_not_called()


def test_pending_security_block_stops_before_verify_or_model_call(runner, monkeypatch):
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    runner.state["browser"].consume_security_block.return_value = {
        "type": "security",
        "url": "https://outside.test/path?token=secret#fragment",
    }
    choose = Mock(side_effect=AssertionError("blocked navigation must stop before the model"))
    monkeypatch.setattr(loop, "choose", choose)

    snapshot = runner.command("predict")

    assert snapshot["status"] == "confirm"
    assert snapshot["decision"] is None
    reason = snapshot["gate"]["reasons"][0]
    assert reason.endswith("https://outside.test/path")
    assert "secret" not in reason and "fragment" not in reason
    choose.assert_not_called()


def test_reobserved_disallowed_origin_stops_before_verify_or_model_call(runner, monkeypatch):
    outside = deepcopy(runner.state["page"])
    outside["url"] = "https://outside.test/path?token=secret"
    outside["fingerprint"] = fingerprint(outside)
    runner.state.update(
        status="ready",
        decision=None,
        allowed_origins=["https://*.example.test"],
        verify={"text": "Search"},
    )
    runner.state["browser"].fresh.return_value = False
    runner.state["browser"].observe.return_value = outside
    choose = Mock(side_effect=AssertionError("outside-origin state must not reach the model"))
    monkeypatch.setattr(loop, "choose", choose)

    snapshot = runner.command("predict")

    assert snapshot["status"] == "confirm"
    assert "outside.test/path" in snapshot["gate"]["reasons"][0]
    assert snapshot.get("verified") is not True
    choose.assert_not_called()


def test_action_result_unknown_is_not_classified_as_pre_input_stale(runner):
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "target": "2"}
    runner.state["browser"].act.side_effect = ActionMayHaveApplied("input dispatched")

    with pytest.raises(ActionMayHaveApplied):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    assert runner.state["decision"] is None
    assert runner.state["status"] == "escalate"
    assert runner.state.get("stale", 0) == 0


def test_policy_failure_never_publishes_the_raw_model_decision(runner, monkeypatch):
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    predicted = {
        **decision("e3"),
        "operation": "CLICK",
        "target": "2",
        "operation_probabilities": {"CLICK": 1.0},
        "target_probabilities": {"2": 1.0},
        "candidate_stats": {"omitted": 0},
    }
    monkeypatch.setattr(loop, "choose", Mock(return_value=predicted))
    monkeypatch.setattr(loop, "evaluate_policy", Mock(side_effect=RuntimeError("policy failed")))

    with pytest.raises(RuntimeError, match="policy failed"):
        runner.command("predict")

    assert runner.state["decision"] is None
    assert runner.state["decisions"] == []


def test_agent_passes_the_remaining_deadline_to_the_model(runner, monkeypatch):
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    runner.deadline_at = time.monotonic() + 5
    seen = {}

    def choose_with_timeout(_page, _goal, _history, *, timeout, candidate_limits):
        seen["timeout"] = timeout
        seen["candidate_limits"] = candidate_limits
        return {
            **decision("e3"),
            "operation": "CLICK",
            "target": "2",
            "operation_probabilities": {"CLICK": 1.0},
            "target_probabilities": {"2": 1.0},
            "candidate_stats": {"omitted": 0},
        }

    monkeypatch.setattr(loop, "choose", choose_with_timeout)
    runner.command("predict")

    assert 0 < seen["timeout"] <= 5
    assert seen["candidate_limits"].elements > 0
