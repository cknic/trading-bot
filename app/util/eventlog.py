import json
import os
import time
from typing import Any, Dict, Optional

DEFAULT_PATH = "/data/events.jsonl"
MAX_BYTES = 10_000_000  # rotate at ~10MB


def _rotate(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            ts = int(time.time())
            os.rename(path, f"{path}.{ts}.bak")
    except Exception:
        pass


def emit(event: str, **fields: Any) -> None:
    path = os.environ.get("EVENTS_JSONL", DEFAULT_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _rotate(path)

    payload: Dict[str, Any] = {"ts": int(time.time()), "event": event}
    payload.update(fields)

    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        # Never crash the bot due to logging
        pass