"""Structured JSONL traces and redacted public agent state.

The live loop needs exact field values in memory, but callers and persisted
diagnostics normally need only to know that text was supplied.  This module
keeps that boundary in one place so new integrations do not accidentally expose
generated text, prompts, screenshots, or raw model requests.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_DROP_KEYS = {
    "content",
    "goal",
    "messages",
    "prompt",
    "raw_answers",
    "request",
    "screenshot",
    "text",
    "value",
}


def goal_fingerprint(goal: str) -> dict:
    encoded = str(goal).encode("utf-8")
    return {
        "goal_sha256": hashlib.sha256(encoded).hexdigest(),
        "goal_chars": len(str(goal)),
    }


def safe_url(value: str | None) -> str:
    """Keep a useful destination while dropping credentials, query, and fragment."""
    text = str(value or "")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or ""
        if not host:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            return ""
        authority = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    if parsed.scheme:
        return f"{parsed.scheme}:"
    return text[:500]


def public_history(history: list[dict]) -> list[dict]:
    """Return action history safe for MCP/UI responses and trace exports."""
    out = []
    for item in history:
        row = {k: v for k, v in item.items() if k != "text"}
        row["text_supplied"] = bool(item.get("text"))
        if "url" in row:
            row["url"] = safe_url(row["url"])
        out.append(row)
    return out


def public_text_calls(calls: list[dict]) -> list[dict]:
    out = []
    for item in calls:
        row = {k: v for k, v in item.items() if k != "value"}
        row["value_supplied"] = bool(item.get("value"))
        out.append(row)
    return out


def public_decision(decision: dict | None) -> dict | None:
    if decision is None:
        return None
    return {k: v for k, v in decision.items() if k not in {"raw_answers", "request"}}


def observation_summary(page: dict) -> dict:
    return {
        "url": safe_url(page.get("url")),
        "title": str(page.get("title", ""))[:300],
        "fingerprint": page.get("fingerprint"),
        "actions": len(page.get("actions") or []),
        "omitted_actions": int(page.get("omitted_actions") or 0),
        "selected_controls": len(page.get("selected_controls") or []),
        "selected_controls_truncated": bool(page.get("selected_controls_truncated")),
        "alerts": len(page.get("alerts") or []),
        "text_complete": page.get("text_complete"),
        "elements_complete": page.get("elements_complete"),
    }


def _safe_payload(value, key: str | None = None):
    """Last-resort trace scrubber for data supplied by future call sites."""
    lowered = (key or "").lower()
    if lowered in _DROP_KEYS:
        if lowered in {"text", "value"}:
            return {"supplied": bool(value)}
        return "[redacted]"
    if "url" in lowered and isinstance(value, str):
        return safe_url(value)
    if isinstance(value, dict):
        return {str(k): _safe_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_payload(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


class TraceRecorder:
    """Append-only JSONL recorder. A missing path makes every call a no-op."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.sequence = 0
        self.closed = False
        self.error = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **payload) -> None:
        if not self.path or self.closed or self.error:
            return
        try:
            self.sequence += 1
            row = {
                "seq": self.sequence,
                "ts": round(time.time(), 6),
                "event": event,
                **_safe_payload(payload),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError) as error:
            # Diagnostics must never turn a completed browser mutation into an
            # application failure. Disable this recorder and surface the reason
            # through Agent.snapshot instead.
            self.error = f"{type(error).__name__}: {error}"

    def close(self, **payload) -> None:
        if self.closed:
            return
        self.record("run_stopped", **payload)
        self.closed = True
