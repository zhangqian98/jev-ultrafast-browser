"""Local-browser freshness/execution regressions. No model calls or external websites."""

import time
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        # Keep the move clear of the City input: a cover would invalidate semantics now.
        browser.evaluate("document.querySelector('#target').style.transform='translateX(40px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        # Clicking changes focus, which is now part of the structured model state.
        # Establish a fresh post-click baseline before testing an unrelated mutation.
        page = browser.observe(screenshot=False)
        assert page["focus"]["label"] == "Continue"
        assert any(a["label"] == "Continue" and a.get("focused") for a in page["actions"])
        passed.append("focused control is represented structurally")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # An overlay that covers controls is a semantic change: it must block the click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert not browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio" checked>Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <div role="status">Ready</div>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert not any(a.get("href", "").endswith("/null") for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        assert any(c["label"] == "Choice" and c["checked"] for c in page["selected_controls"])
        assert any(a["text"] == "Ready" for a in page["alerts"])
        passed.append("native controls expose only supported operations and safe values")
        passed.append("selected controls and visible status text are observed structurally")

        browser.evaluate("""(() => {
          const host=document.createElement('div'); host.id='shadow-host';
          host.attachShadow({mode:'open'}).innerHTML='<span>Shadow validation failure</span>';
          document.body.append(host);
        })()""")
        page = browser.observe(screenshot=False)
        assert "Shadow validation failure" in page["text"]
        assert page["text_complete"] is True
        passed.append("visible shadow-root text participates in complete observations")

        browser.evaluate("""(() => {
          const select=document.createElement('select');
          select.id='many-selected'; select.multiple=true; select.size=4;
          select.setAttribute('aria-label','Many selected tags');
          for (let i=1;i<=21;i++) {
            const option=document.createElement('option');
            option.value='o'+i; option.textContent='O'+i; option.selected=true;
            select.append(option);
          }
          document.body.append(select);
        })()""")
        page = browser.observe(screenshot=False)
        many = next(c for c in page["selected_controls"] if c["label"] == "Many selected tags")
        assert many["omitted_options"] == 1
        assert page["selected_controls_truncated"] is True
        assert page["elements_complete"] is False
        passed.append("truncated selected state marks element observations incomplete")
        browser.evaluate("document.querySelector('#many-selected').remove()")

        browser.evaluate("""(() => {
          const frame=document.createElement('iframe');
          frame.id='opaque-frame'; frame.setAttribute('sandbox','');
          frame.srcdoc='<input autofocus aria-label="Opaque secret field">';
          document.body.append(frame); frame.focus();
        })()""")
        time.sleep(0.1)
        page = browser.observe(screenshot=False)
        if page["cross_origin_frames"]:
            assert page["focus"] is None or page["focus"].get("label") != "iframe"
            assert not any(a.get("kind") == "key" for a in page["actions"])
            passed.append("opaque iframe focus does not expose keyboard actions")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        assert page["focus"]["label"] == "Search"
        assert any(a.get("kind") == "key" and a.get("key") == "Enter" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")
        passed.append("focused widgets expose bounded keyboard actions")
        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
