import json

from jev_ultrafast.trace import (
    TraceRecorder,
    public_decision,
    public_history,
    public_text_calls,
    safe_url,
)


def test_agent_snapshot_redacts_private_runtime_fields():
    from jev_ultrafast.agent import Agent

    agent = Agent.__new__(Agent)
    agent.state = {
        "browser": object(),
        "page": {
            "url": "https://example.test/",
            "title": "Example",
            "text": "Example",
            "actions": [{"id": "e1", "node": 1, "kind": "fill", "role": "textbox",
                         "label": "Search", "value": "private query"}],
        },
        "status": "ready",
        "history": [{"action": "Search", "text": "private query"}],
        "decision": {"operation": "TYPE_TEXT", "request": {"goal": "private"}},
        "decisions": [{"operation": "TYPE_TEXT", "raw_answers": {"secret": True}}],
        "text_calls": [{"field": "Search", "value": "private query"}],
    }
    snapshot = agent.snapshot()
    assert snapshot["history"][0]["text_supplied"] is True
    assert "text" not in snapshot["history"][0]
    assert "request" not in snapshot["decision"]
    assert "raw_answers" not in snapshot["decisions"][0]
    assert snapshot["text_calls"][0] == {"field": "Search", "value_supplied": True}


def test_agent_snapshot_uses_the_pending_decisions_exact_candidate_indices():
    from jev_ultrafast.agent import Agent

    agent = Agent.__new__(Agent)
    agent.state = {
        "browser": object(),
        "goal": "Open the target",
        "page": {"actions": [
            {"id": "e1", "node": 1, "kind": "click", "role": "button", "label": "Original first"},
            {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Target"},
        ]},
        "status": "predicted",
        "history": [],
        "decision": {
            "operation": "CLICK",
            "target": "1",
            "request": {"state": {"elements": [
                {"index": "1", "label": "Target", "operations": ["CLICK"]},
            ]}},
            "candidate_nodes": {"1": 2},
            "candidate_stats": {"observed_elements": 2, "sent_elements": 1},
        },
        "decisions": [],
        "text_calls": [],
    }
    snapshot = agent.snapshot()
    assert snapshot["elements"] == [
        {"index": "1", "label": "Target", "operations": ["CLICK"]},
    ]
    assert snapshot["candidate_stats"]["sent_elements"] == 1
    assert snapshot["element_nodes"] == {"1": 2}
    assert "request" not in snapshot["decision"]


def test_public_state_redacts_generated_text_and_raw_model_inputs():
    history = public_history([
        {"action": "Search", "text": "private query", "url": "https://example.test/x?token=secret#part"}
    ])
    assert history == [{"action": "Search", "url": "https://example.test/x", "text_supplied": True}]

    calls = public_text_calls([{"field": "Search", "value": "private query", "model": "test"}])
    assert calls == [{"field": "Search", "model": "test", "value_supplied": True}]

    decision = public_decision({"operation": "CLICK", "request": {"goal": "secret"}, "raw_answers": {}})
    assert decision == {"operation": "CLICK"}


def test_trace_recorder_scrubs_sensitive_payloads(tmp_path):
    path = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(path)
    recorder.record(
        "action_attempted",
        text="private query",
        value="private value",
        request={"goal": "private goal"},
        url="https://example.test/path?token=secret",
        target_label="Search",
    )
    recorder.close(status="done")

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["action_attempted", "run_stopped"]
    assert lines[0]["text"] == {"supplied": True}
    assert lines[0]["value"] == {"supplied": True}
    assert lines[0]["request"] == "[redacted]"
    assert lines[0]["url"] == "https://example.test/path"
    assert "private" not in path.read_text(encoding="utf-8")


def test_trace_write_failure_disables_diagnostics_without_raising(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    recorder.path = tmp_path  # Opening a directory as a file fails at write time.
    recorder.record("action_executed", choice="e1")
    assert recorder.error and "Error" in recorder.error
    recorder.record("observation", title="ignored after failure")
    recorder.close(status="ready")


def test_safe_url_handles_internal_and_invalid_urls():
    assert safe_url("https://example.test/a?q=1#x") == "https://example.test/a"
    assert safe_url("https://alice:secret@example.test/a?q=1") == "https://example.test/a"
    assert safe_url("https://[2001:db8::1]:8443/a?q=1") == "https://[2001:db8::1]:8443/a"
    assert safe_url("about:blank") == "about:"
    assert safe_url("") == ""
