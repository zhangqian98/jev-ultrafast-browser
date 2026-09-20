import pytest

from jev_ultrafast.verify import verify_page


@pytest.fixture
def observed_page():
    return {
        "url": "https://shop.example.test/results?q=ada&tag=math&tag=history",
        "text": "Search results\nAda Lovelace\n3 results",
        "text_complete": True,
        "elements_complete": True,
        "actions": [
            {"id": "e1", "node": 1, "kind": "fill", "role": "searchbox",
             "label": "Search", "value": "Ada Lovelace"},
            {"id": "e2", "node": 1, "kind": "click", "role": "searchbox",
             "label": "Open Search", "value": "Ada Lovelace"},
            {"id": "e3", "node": 2, "kind": "click", "role": "checkbox",
             "label": "In stock", "checked": "true"},
            {"id": "e4", "node": 3, "kind": "select", "role": "combobox",
             "label": "Sort → Price", "current_value": "Relevance", "value": "price"},
        ],
        "selected_controls": [
            {"node": 4, "role": "radio", "label": "One way", "checked": True, "value": "oneway"},
            {"node": 5, "role": "combobox", "label": "Cabin",
             "options": [{"label": "Economy", "value": "economy"}]},
            {"node": 6, "role": "combobox", "label": "Features",
             "options": [
                 {"label": "Refundable", "value": "refundable"},
                 {"label": "Breakfast", "value": "breakfast"},
             ]},
        ],
    }


def test_legacy_and_composed_predicates(observed_page):
    assert verify_page(observed_page, {"text": "Ada", "url_contains": "/results"}) is True
    assert verify_page(observed_page, {
        "all": [
            {"text_contains": ["Ada Lovelace", "3 results"]},
            {"not": {"text_contains": "No results"}},
            {"any": [{"text_contains": "missing"}, {"text_contains": "Search results"}]},
        ]
    }) is True
    assert verify_page(observed_page, {"text_absent": ["Error", "No results"]}) is True


def test_url_predicates(observed_page):
    assert verify_page(observed_page, {"url": {
        "origin": "https://shop.example.test",
        "path": "/results",
        "path_contains": "result",
        "query": {"q": "ada", "tag": ["math", "history"]},
        "query_contains": "tag=math",
    }}) is True
    assert verify_page(observed_page, {"url": {"path": "/other"}}) is False


def test_url_origin_predicate_ignores_basic_auth_credentials(observed_page):
    observed_page["url"] = "https://alice:secret@shop.example.test/results?q=ada"
    assert verify_page(observed_page, {
        "url": {"origin": "https://shop.example.test"}
    }) is True


def test_element_predicates_use_current_values_and_dedupe_nodes(observed_page):
    assert verify_page(observed_page, {"element": {
        "role": "searchbox", "value_contains": "Lovelace", "count": 1,
    }}) is True
    assert verify_page(observed_page, {"element": {
        "role": "checkbox", "label": "In stock", "checked": True,
    }}) is True
    assert verify_page(observed_page, {"element": {
        "role": "combobox", "label": "Sort", "value": "Relevance",
        "option_value": "price",
    }}) is True
    assert verify_page(observed_page, {"element": {
        "role": "radio", "label": "One way", "checked": "true",
    }}) is True
    assert verify_page(observed_page, {"element": {
        "role": "combobox", "label": "Cabin", "value": "Economy",
        "option_value": "economy",
    }}) is True
    assert verify_page(observed_page, {"element": {
        "role": "combobox", "label": "Features", "option_value": "breakfast",
        "count": 1,
    }}) is True
    assert verify_page(observed_page, {"not": {"element": {
        "role": "button", "label": "Place order",
    }}}) is True
    assert verify_page(observed_page, {"element": {
        "role": "button", "label": "Place order", "max_count": 0,
    }}) is True


@pytest.mark.parametrize("spec", [
    [],
    {"unknown": True},
    {"all": []},
    {"all": [{}]},
    {"url": {"bogus": "x"}},
    {"element": {"count": 1}},
    {"element": {"role": "button", "count": 1, "min_count": 1}},
    {"element": {"role": "button", "min_count": 2, "max_count": 1}},
])
def test_malformed_specs_are_rejected(observed_page, spec):
    with pytest.raises(ValueError):
        verify_page(observed_page, spec)


def test_missing_verify_is_not_configured(observed_page):
    assert verify_page(observed_page, None) is None
    assert verify_page(observed_page, {}) is None


def test_negative_and_upper_bound_checks_require_complete_observation(observed_page):
    incomplete = {**observed_page, "text_complete": False, "elements_complete": False}
    assert verify_page(incomplete, {"text_absent": "No results"}) is False
    assert verify_page(incomplete, {"not": {"text_contains": "No results"}}) is False
    assert verify_page(incomplete, {"not": {"element": {"role": "button", "label": "Place order"}}}) is False
    assert verify_page(incomplete, {"element": {
        "role": "button", "label": "Place order", "max_count": 0,
    }}) is False
    # Positive existence remains safe even when more page content is omitted.
    assert verify_page(incomplete, {"element": {"role": "searchbox", "label": "Search"}}) is True
    assert verify_page(incomplete, {"text_contains": "Ada Lovelace"}) is True


def test_omitted_selected_options_make_negative_element_verification_incomplete(observed_page):
    observed_page["selected_controls"] = [{
        "node": 9,
        "role": "combobox",
        "label": "Tags",
        "options": [{"label": f"O{index}", "value": f"o{index}"} for index in range(1, 21)],
        "omitted_options": 1,
    }]
    observed_page["selected_controls_truncated"] = True
    observed_page["elements_complete"] = False

    assert verify_page(observed_page, {"not": {"element": {
        "role": "combobox", "label": "Tags", "value_contains": "O21",
    }}}) is False
