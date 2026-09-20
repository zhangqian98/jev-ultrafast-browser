"""Validated non-secret runtime configuration.

Credentials remain in their existing server-side environment variables.  This
module only centralizes bounded operational settings that are safe to expose in
diagnostics and tests.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _bounded_int(env, name, default, minimum, maximum):
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} to {maximum}")
    return value


@dataclass(frozen=True)
class CandidateLimits:
    elements: int = 180
    actions: int = 300
    options_per_select: int = 40


@dataclass(frozen=True)
class RuntimeConfig:
    candidates: CandidateLimits
    trace_dir: Path
    default_step_timeout_ms: int = 30000

    def public_dict(self):
        return {
            "candidates": {
                "elements": self.candidates.elements,
                "actions": self.candidates.actions,
                "options_per_select": self.candidates.options_per_select,
            },
            "trace_dir": str(self.trace_dir),
            "default_step_timeout_ms": self.default_step_timeout_ms,
        }


def load_runtime_config(env=None):
    env = os.environ if env is None else env
    trace_dir = Path(env.get("JEV_TRACE_DIR") or Path(tempfile.gettempdir()) / "jev-ultrafast").expanduser()
    return RuntimeConfig(
        candidates=CandidateLimits(
            elements=_bounded_int(env, "JEV_MAX_CANDIDATE_ELEMENTS", 180, 10, 1000),
            actions=_bounded_int(env, "JEV_MAX_CANDIDATE_ACTIONS", 300, 20, 2000),
            options_per_select=_bounded_int(env, "JEV_MAX_OPTIONS_PER_SELECT", 40, 1, 500),
        ),
        trace_dir=trace_dir,
        default_step_timeout_ms=_bounded_int(env, "JEV_STEP_TIMEOUT_MS", 30000, 1000, 120000),
    )
