"""Attach to VS Code desktop over CDP and drive the workbench. No model calls."""

import json
import os
import time
import urllib.request

from jev_ultrafast.browser import Browser, press_key

CDP = os.environ.get("JEV_VSCODE_CDP_URL", "http://127.0.0.1:9333")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def pages():
    return json.loads(OPENER.open(f"{CDP}/json/list", timeout=2).read())


def main():
    assert any("workbench.html" in t.get("url", "") for t in pages()), \
        "VS Code is not listening on " + CDP
    browser = Browser(cdp_url=CDP, attach="workbench.html")
    passed = []
    try:
        page = browser.observe(screenshot=False)
        print(f"{len(page['actions'])} actions on {page['title']!r}")
        # First-run profiles can show an onboarding modal over the workbench.
        if browser.evaluate("!!document.querySelector('.onboarding-a-overlay.visible')"):
            close = next((a for a in page["actions"]
                          if a["kind"] == "click" and a["label"] == "Close"), None)
            if close:
                browser.act(close, page)
            else:
                press_key(browser.call, "Escape")
            page = browser.observe(screenshot=False)
        explorer = next(a for a in page["actions"] if "Explorer" in a["label"])
        first = page["fingerprint"]
        browser.act(explorer, page)
        page = browser.observe(screenshot=False)
        assert page["fingerprint"] != first
        passed.append(f"acted {explorer['label']!r}; fingerprint changed")

        # Open Quick Access (Ctrl+P) and type ">" so the command list overflows.
        for t in ("keyDown", "keyUp"):
            browser.call("Input.dispatchKeyEvent", type=t, key="p", code="KeyP", modifiers=2,
                         windowsVirtualKeyCode=80, nativeVirtualKeyCode=80)
        time.sleep(0.5)
        browser.call("Input.insertText", text=">")
        # The command list renders asynchronously; poll until it overflows.
        page = None
        for _ in range(20):
            time.sleep(0.3)
            page = browser.observe(screenshot=False)
            if any(a["id"].startswith(("scroll_down_", "scroll_up_")) for a in page["actions"]):
                break
        ids = {a["id"] for a in page["actions"]}
        keys = {a.get("key") for a in page["actions"] if a["kind"] == "key"}
        assert "Escape" in keys, keys
        assert any(i.startswith(("scroll_down_", "scroll_up_")) for i in ids), ids
        passed.append("quick pick offers key_escape and a container scroll")

        escape = next(a for a in page["actions"] if a["id"] == "key_escape")
        browser.act(escape, page)
        passed.append("key_escape dispatched")
    finally:
        browser.close()
    assert any("workbench.html" in t.get("url", "") for t in pages()), \
        "VS Code workbench target disappeared after detach"
    passed.append("detach left VS Code running")
    print("\n".join(passed))
    print(f"PASS: {len(passed)} VS Code attach checks; no model calls")


if __name__ == "__main__":
    main()
