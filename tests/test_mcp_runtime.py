import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "jev_ultrafast_mcp_server_test",
    Path(__file__).resolve().parents[1] / "mcp_server.py",
)
mcp_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp_server)


def _fake_session():
    agent = SimpleNamespace(deadline_at=None)
    return mcp_server._session(agent, threading.Event())


def test_session_operations_are_mutually_exclusive():
    session = _fake_session()
    assert mcp_server._begin_operation(session, "first", 5000) is None
    assert session["active_operation"] == "first"
    assert session["agent"].deadline_at is not None

    busy = mcp_server._begin_operation(session, "second", 5000)
    assert busy == {"status": "operation_in_progress", "active_operation": "first"}

    mcp_server._end_operation(session)
    assert session["active_operation"] is None
    assert session["agent"].deadline_at is None


def test_child_environment_keeps_runtime_settings_out_of_gui_credentials():
    child = mcp_server._child_environment({
        "PATH": "bin",
        "LOCALAPPDATA": "data",
        "TYPESAFE_API_KEY": "secret",
        "TEXT_MODEL_API_KEY": "secret-too",
        "GITHUB_TOKEN": "token",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AZURE_STORAGE_CONNECTION_STRING": "azure-secret",
        "DATABASE_URL": "postgres://secret",
        "ELECTRON_RUN_AS_NODE": "1",
        "BU_CDP_URL": "http://127.0.0.1:9223",
    })
    assert child == {
        "PATH": "bin",
        "LOCALAPPDATA": "data",
        "BU_CDP_URL": "http://127.0.0.1:9223",
    }


def test_session_timeout_is_bounded():
    session = _fake_session()
    assert mcp_server._begin_operation(session, "x", 999)["status"] == "error"
    assert mcp_server._begin_operation(session, "x", 120001)["status"] == "error"
    assert not session["lock"].locked()


def test_browser_cancel_marks_only_an_active_operation(monkeypatch):
    idle = _fake_session()
    active = _fake_session()
    active["active_operation"] = "browser_step"
    monkeypatch.setitem(mcp_server._sessions, "idle-test", idle)
    monkeypatch.setitem(mcp_server._sessions, "active-test", active)

    assert mcp_server.browser_cancel("idle-test") == {"status": "idle", "cancelled": False}
    result = mcp_server.browser_cancel("active-test")
    assert result["status"] == "cancellation_requested"
    assert result["operation_generation"] is None
    assert active["cancel"].is_set()


def test_operation_generation_prevents_a_finished_cancel_from_leaking_forward():
    session = _fake_session()
    assert mcp_server._begin_operation(session, "first", 5000) is None
    first_generation = session["active_generation"]
    mcp_server._end_operation(session)

    assert mcp_server._begin_operation(session, "second", 5000) is None
    try:
        assert session["active_generation"] == first_generation + 1
        assert not session["cancel"].is_set()
    finally:
        mcp_server._end_operation(session)


def test_closing_session_rejects_new_operations_and_reads():
    session = _fake_session()
    session["closing"] = True
    assert mcp_server._begin_operation(session, "step", 5000)["status"] == "closing"
    assert mcp_server._begin_read(session)["status"] == "closing"
    assert not session["lock"].locked()


def test_interrupted_status_overrides_previous_agent_status(monkeypatch):
    agent = SimpleNamespace(stage="decision")
    monkeypatch.setattr(mcp_server, "_view", lambda _agent: {"status": "predicted", "url": "x"})
    result = mcp_server._interrupted(agent, "deadline")
    assert result["status"] == "interrupted"
    assert result["failure"] == {"stage": "decision", "category": "deadline"}
    assert result["url"] == "x"


def test_view_counts_every_typesafe_decision_including_terminal_calls():
    snapshot = {
        "page": {
            "url": "https://example.test/",
            "title": "Example",
            "text": "Ready",
            "selected_controls": [],
            "alerts": [],
        },
        "status": "done",
        "elements": [],
        "element_nodes": {},
        "candidate_stats": {},
        "history": [{"usage": {"input_tokens": 10}}],
        "decisions": [
            {"usage": {"input_tokens": 10}},
            {"usage": {"inputTokens": 20}},
        ],
    }
    agent = SimpleNamespace(snapshot=lambda: snapshot)

    view = mcp_server._view(agent)

    assert view["usage"]["typesafe_input_tokens"] == 30
    assert view["usage"]["typesafe_cost_usd"] == round(
        mcp_server.input_cost_usd({"input_tokens": 30}), 6
    )


def test_browser_tabs_does_not_race_an_active_cdp_operation(monkeypatch):
    session = _fake_session()
    session["active_operation"] = "browser_step"
    session["lock"].acquire()
    monkeypatch.setitem(mcp_server._sessions, "tabs-busy", session)
    try:
        assert mcp_server.browser_tabs("tabs-busy") == {
            "status": "operation_in_progress",
            "active_operation": "browser_step",
        }
    finally:
        session["lock"].release()


def test_browser_dialog_does_not_race_an_active_operation(monkeypatch):
    session = _fake_session()
    session["active_operation"] = "browser_step"
    session["lock"].acquire()
    monkeypatch.setitem(mcp_server._sessions, "dialog-busy", session)
    try:
        assert mcp_server.browser_dialog("dialog-busy", True) == {
            "status": "operation_in_progress",
            "active_operation": "browser_step",
        }
    finally:
        session["lock"].release()


def test_browser_dialog_rejects_oversized_prompt_before_locking(monkeypatch):
    session = _fake_session()
    monkeypatch.setitem(mcp_server._sessions, "dialog-large", session)
    result = mcp_server.browser_dialog("dialog-large", True, "x" * 2001)
    assert result["status"] == "error"
    assert not session["lock"].locked()


def test_manual_action_deadline_reports_possible_effect(monkeypatch):
    calls = []

    class FakeAgent:
        deadline_at = None
        stage = "ready"

        def __init__(self):
            self.state = {"browser": SimpleNamespace(call=lambda *args, **kwargs: calls.append((args, kwargs)))}

        def _checkpoint(self, stage):
            self.stage = stage
            if stage == "manual_result":
                raise mcp_server.AgentInterrupted(stage, "deadline")

    agent = FakeAgent()
    session = mcp_server._session(agent, threading.Event())
    monkeypatch.setitem(mcp_server._sessions, "manual-deadline", session)
    monkeypatch.setattr(mcp_server, "_view", lambda _agent: {"status": "ready"})
    monkeypatch.setattr(mcp_server.time, "sleep", lambda *_args: None)

    result = mcp_server.browser_click_xy("manual-deadline", 10, 20, timeout_ms=5000)
    assert result["status"] == "interrupted"
    assert result["action_may_have_applied"] is True
    assert len(calls) == 2


def test_text_fill_is_never_retried_after_post_action_observation_goes_stale(monkeypatch):
    page = {
        "fingerprint": "old",
        "url": "https://example.test/",
        "title": "Form",
        "text": "Form",
        "actions": [{"id": "fill", "node": 1, "kind": "fill", "role": "textbox",
                     "label": "Name", "value": ""}],
    }
    fresh = {**page, "fingerprint": "fresh"}

    class FakeAgent:
        deadline_at = None
        stage = "post_action_observation"
        pending_text = None

        def __init__(self):
            self.state = {
                "page": page,
                "decision": {"choice": "fill"},
                "status": "predicted",
                "goal": "Enter Ada",
                "history": [{"action": "Name", "kind": "fill", "text": "Ada"}],
                "browser": SimpleNamespace(observe=lambda screenshot=False: fresh),
            }

        def command(self, name, body):
            raise mcp_server.StalePage("post-action observation changed")

        def _checkpoint(self, stage):
            self.stage = stage

        def _record(self, *_args, **_kwargs):
            pass

        def _hold_security_block(self, *, action_may_have_applied):
            return False

        def _update_recovered_action(self, previous_page):
            self.state["history"][-1]["page_changed"] = (
                self.state["page"]["fingerprint"] != previous_page["fingerprint"]
            )

    agent = FakeAgent()
    session = mcp_server._session(agent, threading.Event())
    session["pending"] = {"goal": "Enter Ada", "field": {"label": "Name"}}
    session["pending_node"] = 1
    monkeypatch.setitem(mcp_server._sessions, "post-action-stale", session)
    monkeypatch.setattr(
        mcp_server,
        "_view",
        lambda current: {"status": current.state["status"], "url": current.state["page"]["url"]},
    )

    result = mcp_server.browser_supply_text(
        "post-action-stale", "Ada", timeout_ms=5000,
    )
    assert result["status"] == "stale_reobserved"
    assert result["action_may_have_applied"] is True
    assert session["pending"] is None and session["pending_node"] is None
    assert agent.pending_text is None
    assert agent.state["decision"] is None
    assert agent.state["history"][-1]["page_changed"] is True


def test_text_fill_is_not_reused_across_documents_with_recycled_node_ids(monkeypatch):
    old_page = {
        "document_id": "doc-one",
        "fingerprint": "old",
        "url": "https://one.test/form",
        "title": "Form",
        "text": "Name",
        "actions": [{"id": "fill", "node": 1, "kind": "fill", "role": "textbox",
                     "label": "Name", "value": ""}],
    }
    new_page = {
        **old_page,
        "document_id": "doc-two",
        "fingerprint": "new",
        "url": "https://two.test/form",
    }

    class FakeAgent:
        deadline_at = None
        stage = "action"
        pending_text = None

        def __init__(self):
            self.state = {
                "page": old_page,
                "decision": {"choice": "fill"},
                "status": "predicted",
                "goal": "Enter Ada",
                "history": [],
                "browser": SimpleNamespace(observe=lambda screenshot=False: new_page),
            }

        def command(self, name, body):
            raise mcp_server.StalePage("page changed before input")

        def _checkpoint(self, stage):
            self.stage = stage

        def _record(self, *_args, **_kwargs):
            pass

        def _hold_security_block(self, *, action_may_have_applied):
            return False

        def _hold_disallowed_origin(self, *, action_may_have_applied):
            return False

    agent = FakeAgent()
    session = mcp_server._session(agent, threading.Event())
    session["pending"] = mcp_server.field_context(
        agent.state["goal"], old_page["actions"][0], old_page, []
    )
    session["pending_node"] = 1
    monkeypatch.setitem(mcp_server._sessions, "cross-document-stale", session)
    monkeypatch.setattr(
        mcp_server,
        "_view",
        lambda current: {"status": current.state["status"], "url": current.state["page"]["url"]},
    )

    result = mcp_server.browser_supply_text(
        "cross-document-stale", "Ada", timeout_ms=5000,
    )

    assert result["status"] == "stale_reobserved"
    assert session["pending"] is None and session["pending_node"] is None
    assert agent.state["decision"] is None


def test_pending_text_rejects_empty_and_oversized_values_before_locking(monkeypatch):
    session = _fake_session()
    session["pending"] = {"field": {"label": "Name"}}
    monkeypatch.setitem(mcp_server._sessions, "invalid-text", session)

    assert mcp_server.browser_supply_text("invalid-text", "   ")["status"] == "error"
    assert mcp_server.browser_supply_text("invalid-text", "x" * 2001)["status"] == "error"
    assert not session["lock"].locked()
