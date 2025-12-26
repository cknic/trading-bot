import json
import os
import time
from typing import Dict, Any

STATE_PATH = "/data/state.json"

def _load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}

def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["updated_at"] = int(time.time())
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

def get_position(pair: str) -> Dict[str, Any]:
    state = _load_state()
    pos = state.get("positions", {}).get(pair, {})
    return {
        "has_position": bool(pos.get("base_volume")) and float(pos.get("base_volume", 0)) > 0,
        "base_volume": float(pos.get("base_volume", 0) or 0),
        "entry_price": float(pos.get("entry_price", 0) or 0),
    }

def set_position(pair: str, base_volume: float, entry_price: float) -> None:
    state = _load_state()
    positions = state.get("positions", {})
    positions[pair] = {"base_volume": float(base_volume), "entry_price": float(entry_price)}
    state["positions"] = positions
    _save_state(state)

def clear_position(pair: str) -> None:
    state = _load_state()
    positions = state.get("positions", {})
    positions.pop(pair, None)
    state["positions"] = positions
    _save_state(state)

def get_cooldown_until(pair: str) -> int:
    state = _load_state()
    cds = state.get("cooldowns", {})
    return int(cds.get(pair, 0) or 0)

def set_cooldown(pair: str, seconds_from_now: int) -> int:
    state = _load_state()
    cds = state.get("cooldowns", {})
    until = int(time.time()) + int(seconds_from_now)
    cds[pair] = until
    state["cooldowns"] = cds
    _save_state(state)
    return until
