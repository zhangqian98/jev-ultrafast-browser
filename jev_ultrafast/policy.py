"""Deterministic policy gate over Jev's typed outputs.

Jev returns probabilities; code decides what they mean. Thresholds and
sensitive-label patterns are evaluated here, never delegated to the model.
Verdicts: proceed | done | confirm | escalate | stop.
"""

import re

SENSITIVE_LABEL_PATTERNS = [
    ("delete", r"删除|移除|清空|delete|remove"),
    ("send", r"发送|提交|发布|回复|send|submit|post|reply"),
    ("payment", r"支付|付款|购买|下单|充值|订阅|开通|pay|purchase|buy|subscribe|checkout"),
    ("auth", r"授权|权限|登录|密码|验证码|authorize|permission|sign in|login|password|captcha"),
    ("share", r"上传|分享|导出|upload|share|export"),
    ("install", r"安装|install"),
    ("settings", r"系统设置|偏好设置|安全设置|system settings|security settings"),
]
SENSITIVE_LABEL_PATTERNS = [(kind, re.compile(pattern, re.IGNORECASE))
                            for kind, pattern in SENSITIVE_LABEL_PATTERNS]

DEFAULT_THRESHOLDS = {
    "done_probability": 0.9,   # done noul >= this -> finish without executing
    "risk_confirm": 0.2,       # risk noul >= this -> hold for caller approval
    "min_confidence": 0.5,     # target confidence below this -> escalate
    "stop_confidence": 0.3,    # target confidence below this -> stop entirely
}

# Read-only exploration: a wrong guess costs one observation, nothing more. The
# confidence floor only gates operations that mutate state. Sensitive-label and
# risk checks still apply to everything.
SAFE_OPERATIONS = {"WAIT", "SCROLL_UP", "SCROLL_DOWN", "SCROLL_LEFT", "SCROLL_RIGHT", "HOVER"}


def match_sensitive(label):
    text = str(label or "")
    return next((kind for kind, pattern in SENSITIVE_LABEL_PATTERNS
                 if pattern.search(text)), None)


def evaluate_policy(*, operation, label=None, done=None, risk=None,
                    confidence=None, thresholds=None):
    """Map one decision's typed outputs to a verdict. Noul values absent from the
    response (older mocks) skip their threshold instead of blocking the step."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if done is not None and done >= t["done_probability"]:
        return {"verdict": "done", "reasons": [f"done probability {done:.2f}"]}
    if operation == "DONE" and done is not None and done < 0.5:
        return {"verdict": "escalate",
                "reasons": [f"chose DONE but done probability is only {done:.2f}"]}
    if operation == "REVIEW":
        return {"verdict": "confirm",
                "reasons": ["model chose REVIEW: the next action needs human judgment"]}
    reasons = []
    sensitive = match_sensitive(label)
    if sensitive:
        reasons.append(f"target looks like a sensitive '{sensitive}' action: {label}")
    if risk is not None and risk >= t["risk_confirm"]:
        reasons.append(f"risk judgment {risk:.2f} >= {t['risk_confirm']}")
    if reasons:
        return {"verdict": "confirm", "reasons": reasons}
    if confidence is not None and operation not in SAFE_OPERATIONS:
        if confidence < t["stop_confidence"]:
            return {"verdict": "stop",
                    "reasons": [f"confidence {confidence:.2f} < {t['stop_confidence']}"]}
        if confidence < t["min_confidence"]:
            return {"verdict": "escalate",
                    "reasons": [f"confidence {confidence:.2f} < {t['min_confidence']}"]}
    return {"verdict": "proceed", "reasons": []}


_INTERNAL_SCHEMES = ("about:", "data:", "vscode-file:", "chrome:", "chrome-extension:",
                     "devtools:", "file:")


def origin_allowed(url, patterns):
    """Caller-supplied origin allowlist, e.g. ["https://*.example.com"].
    Empty/absent list means unrestricted; non-web internal pages always pass."""
    if not patterns:
        return True
    url = str(url or "")
    if url.startswith(_INTERNAL_SCHEMES):
        return True
    if not url.startswith(("http://", "https://")):
        return False
    from urllib.parse import urlparse
    origin = urlparse(url).netloc
    if not origin:
        return False
    origin = urlparse(url).scheme + "://" + origin
    for pattern in patterns:
        expression = "^" + re.escape(str(pattern).strip()).replace(r"\*", ".*") + "$"
        if re.match(expression, origin, re.IGNORECASE):
            return True
    return False
