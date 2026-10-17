"""Small shared helpers: time, ids, truncation, stable JSON and secret redaction."""
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def trunc(text: Any, limit: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + f"... [truncated {len(s) - limit} chars]"


def tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "[...]" + text[-limit:]


def stable_json(obj: Any) -> str:
    # sort_keys keeps any JSON placed in prompts byte-identical across calls (prefix caching)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


_BEARER_RE = re.compile(r"(?i)(bearer\s+)(?!\[REDACTED\])[A-Za-z0-9._\-$+/=]{16,}")


def secret_values() -> list[str]:
    return [s for s in (config.CRUSOE_API_KEY, config.YDC_API_KEY) if s and len(s) >= 6]


def redact(text: Any) -> Any:
    """Replace configured key values and bearer tokens. Non-strings pass through unchanged."""
    if text is None or not isinstance(text, str):
        return text
    for s in secret_values():
        text = text.replace(s, "[REDACTED]")
    return _BEARER_RE.sub(r"\1[REDACTED]", text)
