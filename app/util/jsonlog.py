import json
import time
from typing import Any, Dict


def jlog(event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"ts": int(time.time()), "event": event}
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)