"""Nested scroll containers, tree roles, and key actions in Chrome. No model calls."""

import time
from urllib.parse import quote

from jev_ultrafast.browser import Browser

HTML = """<!doctype html><title>Container checks</title>
<style>body{margin:20px}
#list{height:200px;width:300px;overflow:auto;border:1px solid}
#list div{height:30px}
#tree{height:150px;width:300px;overflow:auto}
@keyframes fade{from{opacity:0}to{opacity:1}}
</style>
<input id="field" aria-label="Name">
<button id="under">Under</button>
<button id="anim" onclick="const m=document.createElement('div');m.id='menu';
m.style.cssText='opacity:0;animation:fade .3s forwards';
m.innerHTML='<button>Item A</button>';document.body.append(m)">Menu</button>
<div id="wrap" style="width:200px;height:24px;border:1px solid"
     onclick="document.getElementById('ta').focus()"><textarea id="ta"
     aria-label="Zero width editor" style="width:0;height:20px;border:0;padding:0"></textarea></div>
<div id="list" aria-label="Items">
""" + "".join(f"<div>Item {i}</div>" for i in range(50)) + """
</div>
<div id="tree" role="tree" aria-label="Files">
""" + "".join(f'<div role="treeitem" style="height:30px">file{i}.py</div>' for i in range(20)) + """
</div>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        visible_treeitems = sum(1 for a in page["actions"] if a.get("role") == "treeitem")
        assert visible_treeitems >= 3, visible_treeitems
        passed.append(f"{visible_treeitems} treeitem rows appear in actions")

        zw = next(a for a in page["actions"]
                  if a["label"] == "Zero width editor" and a["kind"] == "fill")
        assert "geom" in zw, zw
        browser.act(zw, page, text="hi")
        assert browser.evaluate("document.querySelector('#ta').value") == "hi"
        passed.append("zero-width editable fills through its visible ancestor")

        assert any(a["label"] == "Under" for a in page["actions"])
        browser.evaluate("const c=document.createElement('div');c.id='cover';"
                         "c.style.cssText='position:fixed;left:0;top:0;width:100%;height:100%;"
                         "background:transparent';document.body.append(c)")
        page = browser.observe(screenshot=False)
        assert not any(a["label"] == "Under" for a in page["actions"])
        browser.evaluate("document.querySelector('#cover').remove()")
        page = browser.observe(screenshot=False)
        assert any(a["label"] == "Under" for a in page["actions"])
        passed.append("occluded controls leave the action list and return when uncovered")

        # Background tabs throttle CSS animation ticks; make the tab foreground
        # for this check so the fade actually progresses.
        browser.call("Page.bringToFront")
        page = browser.observe(screenshot=False)
        menu = next(a for a in page["actions"] if a["label"] == "Menu")
        browser.act(menu, page)
        t0 = time.perf_counter()
        page = browser.observe(screenshot=False)
        anim_ms = (time.perf_counter() - t0) * 1000
        assert any(a["label"] == "Item A" for a in page["actions"])
        assert anim_ms < 500, anim_ms
        passed.append(f"observe waited {anim_ms:.0f}ms for the fade-in menu")

        under = next(a for a in page["actions"] if a["label"] == "Under")
        browser.act(under, page)
        t0 = time.perf_counter()
        page = browser.observe(screenshot=False)
        plain_ms = (time.perf_counter() - t0) * 1000
        assert plain_ms < 150, plain_ms
        passed.append(f"plain click observe stayed fast ({plain_ms:.0f}ms)")

        field = next(a for a in page["actions"]
                     if a["kind"] == "fill" and a["label"] == "Name")
        browser.act(field, page, text="hello")
        page = browser.observe(screenshot=False)
        keys = {a.get("key") for a in page["actions"] if a["kind"] == "key"}
        assert "Enter" in keys and "Escape" in keys, keys
        passed.append("focused input offers key actions")

        down = next(a for a in page["actions"] if a["id"].startswith("scroll_down_"))
        assert down["label"] == "Scroll down in Items"
        browser.act(down, page)
        top = 0
        for _ in range(10):
            top = browser.evaluate("document.querySelector('#list').scrollTop")
            if top > 0:
                break
            time.sleep(0.05)
        assert top > 0, top
        passed.append(f"container scroll moved the div (scrollTop={top})")

        page = browser.observe(screenshot=False)
        up = next(a for a in page["actions"] if a["id"].startswith("scroll_up_"))
        browser.act(up, page)
        passed.append("scroll_up action offered once scrolled")

        page = browser.observe(screenshot=False)
        enter = next(a for a in page["actions"] if a["id"] == "key_enter")
        browser.act(enter, page)
        passed.append("key_enter dispatches without error")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} container/key checks; no model calls")


if __name__ == "__main__":
    main()
