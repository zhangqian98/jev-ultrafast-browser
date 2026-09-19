"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


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
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
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
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


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
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
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
            return {"result": {"value": {"x": 300, "y": 400}}}
        return {}

    cdp = Mock(side_effect=send)
    result = browser_operation({"operation": "act", "session": "test", "cdp": cdp,
                                "action": {"id": "scroll_down_5", "kind": "scroll",
                                           "node": 5, "delta": 200}})
    assert result == {"executed": "scroll_down_5"}
    wheel = next(c for c in cdp.call_args_list if c.args[0] == "Input.dispatchMouseEvent")
    assert wheel.kwargs["x"] == 300 and wheel.kwargs["y"] == 400 and wheel.kwargs["deltaY"] == 200


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
