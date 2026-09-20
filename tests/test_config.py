from pathlib import Path

import pytest

from jev_ultrafast.config import load_runtime_config


def test_runtime_config_reads_bounded_non_secret_settings(tmp_path):
    config = load_runtime_config({
        "JEV_MAX_CANDIDATE_ELEMENTS": "120",
        "JEV_MAX_CANDIDATE_ACTIONS": "240",
        "JEV_MAX_OPTIONS_PER_SELECT": "25",
        "JEV_STEP_TIMEOUT_MS": "45000",
        "JEV_TRACE_DIR": str(tmp_path),
        "TYPESAFE_API_KEY": "must-not-be-exposed",
    })
    assert config.candidates.elements == 120
    assert config.candidates.actions == 240
    assert config.candidates.options_per_select == 25
    assert config.default_step_timeout_ms == 45000
    assert config.trace_dir == Path(tmp_path)
    assert "must-not-be-exposed" not in str(config.public_dict())


@pytest.mark.parametrize("name,value", [
    ("JEV_MAX_CANDIDATE_ELEMENTS", "9"),
    ("JEV_MAX_CANDIDATE_ACTIONS", "many"),
    ("JEV_MAX_OPTIONS_PER_SELECT", "0"),
    ("JEV_STEP_TIMEOUT_MS", "120001"),
])
def test_runtime_config_rejects_invalid_bounds(name, value):
    with pytest.raises(ValueError):
        load_runtime_config({name: value})
