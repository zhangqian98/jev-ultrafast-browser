"""Live exercise of every operation on a local fixture. No model calls.

Launches a dedicated Chrome on a CDP port (DirectCDP, owned tab), then drives
the real browser_operation path end to end: click variants, drag, fill, select,
upload, scrolling, key combos, dialogs, shadow DOM, iframe, tab following.
"""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jev_ultrafast.browser import Browser, press_key  # noqa: E402

BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
PORT = 9340

HTML = """<!doctype html><title>Ops fixture</title>
<style>
  body{margin:20px;font-family:sans-serif}
  button,input,select{display:block;margin:6px 0}
  #hoverbtn{display:none}
  #tall{height:80px;width:200px;overflow-y:scroll}
  #wide{height:90px;width:200px;overflow-x:scroll;white-space:nowrap}
  #drop{border:1px solid #888;width:160px;height:40px;line-height:40px}
</style>
<div id="log"></div>
<button id="alertbtn" onclick="log('confirm=' + confirm('proceed?'))">Confirm dialog</button>
<button id="newtab" onclick="window.open('about:blank#newtab','_blank')">Open new tab</button>
<button id="click" onclick="log('clicked')">Click me</button>
<div id="ctx" role="button" style="width:160px;height:30px;background:#eee"
     oncontextmenu="log('context menu');return false">Right-click area</div>
<button id="dbl" ondblclick="log('double clicked')">Double-click me</button>
<div id="hoverzone" role="button" style="width:160px;height:30px;background:#dde">Hover zone</div>
<button id="hoverbtn" onclick="log('hover revealed clicked')">Hover revealed</button>
<div id="drag" role="button" style="width:80px;height:30px;background:#cfc">Drag source</div>
<div id="drop" role="button">Drop target</div>
<label>Name <input id="name"></label>
<label>File <input id="up" type="file"></label>
<select id="sel" aria-label="Pick"><option>A</option><option>B</option></select>
<div id="tall"><div style="height:400px">tall content</div></div>
<div id="wide"><span style="display:inline-block;width:600px">wide content</span></div>
<div id="shadowhost"></div>
<iframe id="frame" srcdoc="&lt;button id='inframe'
  onclick=&quot;parent.log('iframe clicked')&quot;&gt;In frame&lt;/button&gt;"></iframe>
<div style="height:900px"></div>
<button onclick="log('deep clicked')">Deep below</button>
<script>
  function log(m){document.getElementById('log').textContent += m + ';';}
  const shb = document.createElement('button');
  shb.textContent = 'Shadow button';
  shb.onclick = () => log('shadow clicked');
  shadowhost.attachShadow({mode:'open'}).appendChild(shb);
  hoverzone.onmouseover = () => { hoverbtn.style.display='block'; log('hovered'); };
  document.addEventListener('keydown', e => {
    if (e.ctrlKey && e.key==='s') { e.preventDefault(); log('ctrl+s'); }
    if (e.key==='Enter' && e.target.id==='name') log('enter in name');
  });
  drag.onmousedown = e => {
    const move = ev => {
      const r = drop.getBoundingClientRect();
      if (ev.clientX>=r.x && ev.clientX<=r.right && ev.clientY>=r.y && ev.clientY<=r.bottom)
        log('dropped inside');
    };
    const up = () => { removeEventListener('mousemove',move); removeEventListener('mouseup',up); };
    addEventListener('mousemove',move); addEventListener('mouseup',up);
  };
</script>
"""


def find(page, label, kind=None):
    return next(a for a in page["actions"]
                if label in a["label"] and (kind is None or a["kind"] == kind))


def main():
    binary = next((b for b in BROWSERS if os.path.isfile(b)), None)
    if not binary:
        raise SystemExit("no Chrome/Edge found")
    profile = tempfile.mkdtemp(prefix="jev-ops-profile-")
    subprocess.Popen([binary, f"--remote-debugging-port={PORT}",
                      f"--user-data-dir={profile}", "--no-first-run",
                      "--no-default-browser-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{base}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("CDP endpoint did not answer")

    tmp = tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False)
    tmp.write(b"png")
    tmp.close()

    browser = Browser("data:text/html," + quote(HTML), cdp_url=base)
    passed = []

    def step(page, action, text=None):
        browser.act(action, page, text=text)
        return browser.observe(screenshot=False)

    def log():
        return browser.evaluate("document.getElementById('log').textContent") or ""

    try:
        page = browser.observe(screenshot=False)

        below = page.get("offscreen", {}).get("below", [])
        assert any("Deep below" in label for label in below), f"offscreen below: {below}"
        assert all("Deep below" not in a["label"] for a in page["actions"])
        passed.append("OFFSCREEN hint")

        page = step(page, find(page, "Click me"))
        assert "clicked;" in log()
        passed.append("CLICK")

        page = step(page, {**find(page, "Right-click area"), "button": "right"})
        assert "context menu;" in log()
        passed.append("RIGHT_CLICK")

        page = step(page, {**find(page, "Double-click me"), "clicks": 2})
        assert "double clicked;" in log()
        passed.append("DOUBLE_CLICK")

        page = step(page, {**find(page, "Hover zone"), "hover": True})
        revealed = next((a for a in page["actions"] if "Hover revealed" in a["label"]), None)
        assert revealed, "hover did not reveal the hidden button"
        page = step(page, revealed)
        assert "hover revealed clicked;" in log()
        passed.append("HOVER + reveal-click")

        page = step(page, {**find(page, "Drag source"), "kind": "drag",
                           "target_node": find(page, "Drop target")["node"]})
        assert "dropped inside;" in log()
        passed.append("DRAG")

        page = step(page, find(page, "Name", "fill"), text="jev")
        assert browser.evaluate("document.getElementById('name').value") == "jev"
        passed.append("TYPE_TEXT")

        press_key(browser.call, "Enter")
        page = browser.observe(screenshot=False)
        assert "enter in name;" in log()
        passed.append("PRESS_KEY")

        press_key(browser.call, "Ctrl+S")
        page = browser.observe(screenshot=False)
        assert "ctrl+s;" in log()
        passed.append("COMBO Ctrl+S")

        page = step(page, find(page, "Pick", "select"))
        assert browser.evaluate("document.getElementById('sel').value") == "B"
        passed.append("SELECT")

        page = step(page, find(page, "File", "file"), text=tmp.name)
        assert "png" in (browser.evaluate("document.getElementById('up').files[0]?.name") or "")
        passed.append("UPLOAD_FILE")

        page = step(page, find(page, "Scroll down in"))
        assert browser.evaluate("document.getElementById('tall').scrollTop") > 0
        passed.append("SCROLL_DOWN container")

        page = step(page, find(page, "Scroll right"))
        assert browser.evaluate("document.getElementById('wide').scrollLeft") > 0
        passed.append("SCROLL_RIGHT container")

        page = step(page, find(page, "Shadow button"))
        assert "shadow clicked;" in log()
        passed.append("SHADOW_DOM")

        page = step(page, find(page, "In frame"))
        assert "iframe clicked;" in log()
        passed.append("IFRAME")

        browser.cdp.dialog_accept = {"accept": True}
        page = step(page, find(page, "Confirm dialog"))
        assert "confirm=true;" in log(), log()
        assert any(p.get("type") == "confirm" for p in page.get("dialogs", []))
        passed.append("DIALOG accept")

        browser.call("Page.navigate", url="data:text/html,<h1>one</h1>")
        time.sleep(0.4)
        browser.call("Page.navigate", url="data:text/html,<h1>two</h1>")
        time.sleep(0.4)
        page = browser.observe(screenshot=False)
        page = step(page, find(page, "Go back", "back"))
        assert "one" in (page["url"] or "")
        passed.append("BACK")

        target0 = browser.target
        browser.call("Page.navigate", url="data:text/html," + quote(HTML))
        time.sleep(0.5)
        page = browser.observe(screenshot=False)
        page = step(page, find(page, "Open new tab"))
        assert browser.target != target0, "did not follow the new tab"
        assert "newtab" in (page["url"] or "")
        passed.append("TAB follow (window.open)")
    finally:
        browser.close()
        os.unlink(tmp.name)
    print("\n".join(passed))
    print(f"PASS: {len(passed)} live operation checks; no model calls")


if __name__ == "__main__":
    main()
