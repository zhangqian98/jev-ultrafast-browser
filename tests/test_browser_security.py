import json
from unittest import mock
from unittest.mock import Mock

import pytest

from jev_ultrafast.browser import Browser, DirectCDP, NavigationBlocked


class _FakeSocket:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self, timeout=None):
        return json.dumps(next(self.replies))


def test_direct_cdp_blocks_disallowed_main_frame_before_navigation():
    socket = _FakeSocket([
        {
            "method": "Fetch.requestPaused",
            "sessionId": "s1",
            "params": {
                "requestId": "r1",
                "frameId": "main",
                "request": {"url": "https://evil.test/path"},
            },
        },
        {"id": 1, "result": {"ok": True}},
    ])
    conn = DirectCDP.__new__(DirectCDP)
    conn.ws = socket
    conn.timeout, conn.next_id = 1, 0
    conn.events, conn.dialog_accept = [], {"accept": False}
    conn.logs, conn._log_id = [], 0
    conn.allowed_origins = {"s1": ("https://*.example.test",)}
    conn.main_frames = {"s1": "main"}

    assert conn("Runtime.evaluate", session_id="s1") == {"ok": True}
    assert socket.sent[0]["id"] == 1
    assert socket.sent[1]["method"] == "Fetch.failRequest"
    assert socket.sent[1]["params"]["requestId"] == "r1"
    assert conn.logs[-1]["type"] == "security"


def test_direct_cdp_continues_subframe_documents():
    socket = _FakeSocket([
        {
            "method": "Fetch.requestPaused",
            "sessionId": "s1",
            "params": {
                "requestId": "r2",
                "frameId": "subframe",
                "request": {"url": "https://cdn.test/embed"},
            },
        },
        {"id": 1, "result": {}},
    ])
    conn = DirectCDP.__new__(DirectCDP)
    conn.ws = socket
    conn.timeout, conn.next_id = 1, 0
    conn.events, conn.dialog_accept = [], {"accept": False}
    conn.logs, conn._log_id = [], 0
    conn.allowed_origins = {"s1": ("https://app.example.test",)}
    conn.main_frames = {"s1": "main"}

    conn("Runtime.evaluate", session_id="s1")
    assert socket.sent[1]["method"] == "Fetch.continueRequest"


def test_direct_cdp_ignores_auxiliary_replies_until_the_main_response():
    socket = _FakeSocket([
        {
            "method": "Fetch.requestPaused",
            "sessionId": "s1",
            "params": {
                "requestId": "r1",
                "frameId": "main",
                "request": {"url": "https://evil.test/path"},
            },
        },
        {"id": 2, "result": {}},
        {"id": 1, "result": {"main": True}},
    ])
    conn = DirectCDP.__new__(DirectCDP)
    conn.ws = socket
    conn.timeout, conn.next_id = 1, 0
    conn.events, conn.dialog_accept = [], {"accept": False}
    conn.logs, conn._log_id = [], 0
    conn.allowed_origins = {"s1": ("https://*.example.test",)}
    conn.main_frames = {"s1": "main"}

    assert conn("Runtime.evaluate", session_id="s1") == {"main": True}
    assert [message["id"] for message in socket.sent] == [1, 2]


def test_direct_cdp_deadline_expires_even_while_events_keep_arriving(monkeypatch):
    socket = _FakeSocket([
        {"method": "Target.targetCreated", "params": {}},
    ])
    conn = DirectCDP.__new__(DirectCDP)
    conn.ws = socket
    conn.timeout, conn.next_id = 1, 0
    conn.events, conn.dialog_accept = [], {"accept": False}
    conn.logs, conn._log_id = [], 0
    conn.allowed_origins, conn.main_frames = {}, {}
    monkeypatch.setattr(
        "jev_ultrafast.browser.time.monotonic",
        mock.Mock(side_effect=[0.0, 0.5, 1.01]),
    )

    with pytest.raises(TimeoutError, match="CDP Runtime.evaluate timed out"):
        conn("Runtime.evaluate")


def test_browser_consumes_the_latched_security_block_for_its_session():
    browser = Browser.__new__(Browser)
    cdp = DirectCDP.__new__(DirectCDP)
    cdp.security_blocks = {
        "s1": {"id": 3, "type": "security", "url": "https://outside.test/"},
    }
    browser.cdp = cdp
    browser.session = "s1"
    assert browser.consume_security_block()["url"] == "https://outside.test/"
    assert browser.consume_security_block() is None


def test_browser_security_cursor_keeps_background_blocks_for_the_next_check():
    browser = Browser.__new__(Browser)
    cdp = DirectCDP.__new__(DirectCDP)
    cdp.logs, cdp._log_id = [], 0
    cdp.security_blocks = {}
    browser.cdp = cdp
    browser.session = None

    assert browser.consume_security_block() is None
    cdp._append_log("security", "blocked", "blocked", "https://outside.test/path")
    assert browser.consume_security_block()["url"] == "https://outside.test/path"
    assert browser.consume_security_block() is None


def test_security_latch_survives_diagnostic_ring_eviction():
    browser = Browser.__new__(Browser)
    cdp = DirectCDP.__new__(DirectCDP)
    cdp.logs, cdp._log_id, cdp.security_blocks = [], 0, {}
    browser.cdp, browser.session = cdp, "s1"

    cdp._append_log("security", "blocked", "blocked", "https://outside.test/", "s1")
    for index in range(1001):
        cdp._append_log("console", "log", f"noise {index}")

    assert cdp.logs[0]["id"] > 1
    assert browser.consume_security_block()["url"] == "https://outside.test/"


def test_security_latch_survives_retargeting_to_a_new_cdp_session():
    browser = Browser.__new__(Browser)
    cdp = DirectCDP.__new__(DirectCDP)
    cdp.security_blocks = {
        "old-session": {"id": 7, "type": "security", "url": "https://outside.test/"},
    }
    browser.cdp, browser.session = cdp, "new-session"

    assert browser.consume_security_block()["url"] == "https://outside.test/"
    assert browser.consume_security_block() is None


def test_browser_lists_only_targets_owned_by_the_session():
    browser = Browser.__new__(Browser)
    browser.owned = True
    browser.target = "owned"
    browser._owned_targets = {"owned"}

    def cdp(method, **_params):
        assert method == "Target.getTargets"
        return {"targetInfos": [
            {"type": "page", "targetId": "owned", "url": "https://one.test/"},
            {"type": "page", "targetId": "other", "url": "https://two.test/"},
        ]}

    browser.cdp = cdp
    assert [page["targetId"] for page in browser._pages()] == ["owned"]


def test_unrelated_new_target_is_not_followed():
    browser = Browser.__new__(Browser)
    browser.owned = browser.follow = True
    browser.target = "owned"
    browser._owned_targets = {"owned"}
    browser._known_targets = {"owned"}
    browser.timeout_provider = None
    browser.switch_to = Mock()
    browser.cdp = Mock(return_value={"targetInfos": [
        {"type": "page", "targetId": "owned"},
        {"type": "page", "targetId": "unrelated", "openerId": "someone-else"},
    ]})

    browser._maybe_retarget()

    browser.switch_to.assert_not_called()
    assert browser._owned_targets == {"owned"}


def test_direct_cdp_calls_receive_the_agent_deadline(monkeypatch):
    browser = Browser.__new__(Browser)
    browser.cdp = DirectCDP.__new__(DirectCDP)
    browser.timeout_provider = lambda: 0.25
    seen = {}

    def call(_self, method, **kwargs):
        seen.update(method=method, kwargs=kwargs)
        return {}

    monkeypatch.setattr(DirectCDP, "__call__", call)
    browser._send("Runtime.evaluate", session_id="s1", expression="1")
    assert seen["kwargs"]["_response_timeout"] == pytest.approx(0.25)


def test_browser_harness_calls_receive_the_agent_deadline():
    browser = Browser.__new__(Browser)
    seen = {}

    def cdp(method, **kwargs):
        seen.update(method=method, kwargs=kwargs)
        return {}

    browser.cdp = cdp
    browser.timeout_provider = lambda: 0.25
    browser._send("Runtime.evaluate", session_id="s1", expression="1")
    assert seen["kwargs"]["_response_timeout"] == pytest.approx(0.25)


@pytest.mark.parametrize("destination", [
    "https://evil.test/a",
    "javascript:",
    "mailto:",
])
def test_observed_link_destination_is_checked_before_input(monkeypatch, destination):
    browser = Browser.__new__(Browser)
    browser.allowed_origins = ("https://*.example.test",)
    browser.fresh = Mock(return_value=True)
    operation = Mock()
    monkeypatch.setattr("jev_ultrafast.browser.browser_operation", operation)

    with pytest.raises(NavigationBlocked, match="outside allowed origins"):
        browser.act(
            {"id": "e1", "kind": "click", "node": 1, "href": destination},
            {"url": "https://app.example.test/", "marker": 1},
        )
    operation.assert_not_called()


def test_navigation_block_message_drops_basic_auth_credentials():
    error = NavigationBlocked("https://alice:secret@example.test/private?q=token#fragment")
    assert str(error).endswith("https://example.test/private")
    assert "alice" not in str(error) and "secret" not in str(error)
