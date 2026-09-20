"""Caller-owned, declarative success checks over an observed page.

Verification deliberately accepts data predicates only.  It never evaluates
selectors, JavaScript, model output, or executable code.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

_TOP_LEVEL_KEYS = {
    "all",
    "any",
    "not",
    "text",
    "text_contains",
    "text_absent",
    "url_contains",
    "url",
    "element",
}
_ELEMENT_EXACT = {
    "id",
    "kind",
    "role",
    "label",
    "value",
    "option_value",
    "checked",
    "selected",
    "expanded",
    "required",
    "href",
    "form_action",
}
_ELEMENT_CONTAINS = {"label_contains", "value_contains", "href_contains"}
_ELEMENT_COUNTS = {"count", "min_count", "max_count"}
_URL_KEYS = {"origin", "path", "path_contains", "query", "query_contains", "contains"}


def _domains(spec):
    if not isinstance(spec, dict):
        return set()
    domains = set()
    if set(spec) & {"text", "text_contains", "text_absent"}:
        domains.add("text")
    if "element" in spec:
        domains.add("elements")
    for key in ("all", "any"):
        children = spec.get(key)
        if isinstance(children, list):
            for child in children:
                domains |= _domains(child)
    if "not" in spec:
        domains |= _domains(spec["not"])
    return domains


def _domains_complete(page, domains):
    return (
        ("text" not in domains or page.get("text_complete") is True)
        and ("elements" not in domains or page.get("elements_complete") is True)
    )


def _require_string(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"verify.{name} must be a non-empty string")
    return value


def _strings(value, name):
    if isinstance(value, str):
        return [_require_string(value, name)]
    if isinstance(value, list) and value:
        return [_require_string(item, name) for item in value]
    raise ValueError(f"verify.{name} must be a non-empty string or list of strings")


def _as_boolish(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _equal(actual, expected):
    return _as_boolish(actual) == _as_boolish(expected)


def _page_elements(page):
    """Normalize action rows into element-like records without losing identity."""
    rows = []
    for action in page.get("actions") or []:
        label = str(action.get("label", "")).split(" → ", 1)[0]
        rows.append({
            **action,
            "label": label,
            "value": action.get("current_value", action.get("value", "")),
            "option_value": action.get("value") if action.get("kind") == "select" else None,
        })
    for selected in page.get("selected_controls") or []:
        options = selected.get("options") or []
        value = ", ".join(str(option.get("label", "")) for option in options) or selected.get("value", "")
        base = {
            **selected,
            "kind": "selected",
            "value": value,
            "option_value": None,
        }
        rows.append(base)
        # A multi-select remains one observed element for count predicates, but
        # each selected option must be independently addressable by value.
        rows.extend({**base, "option_value": option.get("value")} for option in options)
    return rows


def _match_element(row, predicate):
    for key in _ELEMENT_EXACT:
        if key in predicate and not _equal(row.get(key), predicate[key]):
            return False
    for key in _ELEMENT_CONTAINS:
        if key in predicate:
            source = key.removesuffix("_contains")
            if _require_string(predicate[key], f"element.{key}") not in str(row.get(source, "")):
                return False
    return True


def _verify_element(page, predicate):
    if not isinstance(predicate, dict) or not predicate:
        raise ValueError("verify.element must be a non-empty object")
    unknown = set(predicate) - _ELEMENT_EXACT - _ELEMENT_CONTAINS - _ELEMENT_COUNTS
    if unknown:
        raise ValueError(f"unknown verify.element keys: {sorted(unknown)}")
    if not (set(predicate) & (_ELEMENT_EXACT | _ELEMENT_CONTAINS)):
        raise ValueError("verify.element needs at least one element predicate")
    counts = {}
    for key in _ELEMENT_COUNTS:
        if key in predicate:
            value = predicate[key]
            if type(value) is not int or value < 0:
                raise ValueError(f"verify.element.{key} must be a non-negative integer")
            counts[key] = value
    if "count" in counts and ({"min_count", "max_count"} & counts.keys()):
        raise ValueError("verify.element.count cannot be combined with min_count/max_count")
    if counts.get("min_count", 0) > counts.get("max_count", float("inf")):
        raise ValueError("verify.element.min_count cannot exceed max_count")
    if ({"count", "max_count"} & counts.keys()) and page.get("elements_complete") is not True:
        return False

    matched = [row for row in _page_elements(page) if _match_element(row, predicate)]
    # Several supported operations can refer to the same observed DOM node. Count
    # that as one element unless no node identity exists.
    identities = {
        ("node", row["node"]) if row.get("node") is not None else ("id", row.get("id"), index)
        for index, row in enumerate(matched)
    }
    total = len(identities)
    if "count" in counts:
        return total == counts["count"]
    minimum = counts.get("min_count", 0 if "max_count" in counts else 1)
    return total >= minimum and total <= counts.get("max_count", float("inf"))


def _verify_url(value, predicate):
    if not isinstance(predicate, dict) or not predicate:
        raise ValueError("verify.url must be a non-empty object")
    unknown = set(predicate) - _URL_KEYS
    if unknown:
        raise ValueError(f"unknown verify.url keys: {sorted(unknown)}")
    parsed = urlsplit(str(value or ""))
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
        host = ""
    authority = f"{host}:{port}" if host and port is not None else host
    origin = f"{parsed.scheme}://{authority}" if parsed.scheme and authority else ""
    checks = []
    if "origin" in predicate:
        checks.append(origin == _require_string(predicate["origin"], "url.origin"))
    if "path" in predicate:
        checks.append(parsed.path == _require_string(predicate["path"], "url.path"))
    if "path_contains" in predicate:
        checks.append(_require_string(predicate["path_contains"], "url.path_contains") in parsed.path)
    if "query_contains" in predicate:
        checks.append(_require_string(predicate["query_contains"], "url.query_contains") in parsed.query)
    if "contains" in predicate:
        checks.append(_require_string(predicate["contains"], "url.contains") in str(value or ""))
    if "query" in predicate:
        expected = predicate["query"]
        if not isinstance(expected, dict) or not expected:
            raise ValueError("verify.url.query must be a non-empty object")
        actual = parse_qs(parsed.query, keep_blank_values=True)
        for key, wanted in expected.items():
            if not isinstance(key, str) or not key:
                raise ValueError("verify.url.query keys must be non-empty strings")
            wanted_values = [wanted] if isinstance(wanted, str) else wanted
            if not isinstance(wanted_values, list) or not wanted_values or not all(
                isinstance(item, str) for item in wanted_values
            ):
                raise ValueError("verify.url.query values must be strings or non-empty string lists")
            checks.append(actual.get(key) == wanted_values)
    return all(checks)


def verify_page(page, spec):
    """Evaluate a verify spec, returning None when verification is not configured."""
    if spec is None or spec == {}:
        return None
    if not isinstance(spec, dict):
        raise ValueError("verify must be an object")
    unknown = set(spec) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"unknown verify keys: {sorted(unknown)}")

    checks = []
    if "all" in spec:
        children = spec["all"]
        if not isinstance(children, list) or not children:
            raise ValueError("verify.all must be a non-empty list")
        values = [verify_page(page, child) for child in children]
        if any(value is None for value in values):
            raise ValueError("verify.all children must contain predicates")
        checks.append(all(values))
    if "any" in spec:
        children = spec["any"]
        if not isinstance(children, list) or not children:
            raise ValueError("verify.any must be a non-empty list")
        values = [verify_page(page, child) for child in children]
        if any(value is None for value in values):
            raise ValueError("verify.any children must contain predicates")
        checks.append(any(values))
    if "not" in spec:
        child = verify_page(page, spec["not"])
        if child is None:
            raise ValueError("verify.not needs a configured child predicate")
        checks.append(_domains_complete(page, _domains(spec["not"])) and not child)

    page_text = str(page.get("text", ""))
    if "text" in spec:  # backward-compatible alias
        checks.append(_require_string(spec["text"], "text") in page_text)
    if "text_contains" in spec:
        checks.extend(value in page_text for value in _strings(spec["text_contains"], "text_contains"))
    if "text_absent" in spec:
        values = _strings(spec["text_absent"], "text_absent")
        checks.append(page.get("text_complete") is True)
        checks.extend(value not in page_text for value in values)
    if "url_contains" in spec:
        checks.append(_require_string(spec["url_contains"], "url_contains") in str(page.get("url", "")))
    if "url" in spec:
        checks.append(_verify_url(page.get("url"), spec["url"]))
    if "element" in spec:
        checks.append(_verify_element(page, spec["element"]))
    if not checks:
        raise ValueError("verify needs at least one predicate")
    return all(checks)
