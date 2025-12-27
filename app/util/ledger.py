import json
import os
import time

# --- PATHS ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def _save_state(state):
    # Atomic write to prevent corruption
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_file, STATE_FILE)

def get_position(pair):
    """
    Returns dict: { 'has_position': bool, 'base_volume': float, 'average_price': float }
    """
    state = _load_state()
    return state.get(pair, {
        "has_position": False, 
        "base_volume": 0.0, 
        "average_price": 0.0
    })

def set_position(pair, volume, price):
    """
    Called after a BUY. Updates state to reflect we hold the asset.
    """
    state = _load_state()
    
    # In a real bot, you might average down if you already hold it.
    # For simplicity, we overwrite or add to it.
    current = state.get(pair, {"base_volume": 0.0, "average_price": 0.0})
    
    new_vol = float(volume)
    # Weighted Average Price logic
    total_cost = (current["base_volume"] * current["average_price"]) + (new_vol * float(price))
    total_vol = current["base_volume"] + new_vol
    
    avg_price = total_cost / total_vol if total_vol > 0 else 0.0
    
    state[pair] = {
        "has_position": True,
        "base_volume": total_vol,
        "average_price": avg_price,
        "last_update": int(time.time())
    }
    _save_state(state)
    print(f"LEDGER: Position Updated for {pair}. Vol: {total_vol:.6f} @ ${avg_price:.2f}")

def clear_position(pair):
    """
    Called after a SELL. Marks position as closed.
    """
    state = _load_state()
    if pair in state:
        state[pair]["has_position"] = False
        state[pair]["base_volume"] = 0.0
        state[pair]["average_price"] = 0.0
        state[pair]["last_update"] = int(time.time())
        _save_state(state)
        print(f"LEDGER: Position Cleared for {pair}")

def get_cooldown_until(pair):
    state = _load_state()
    return state.get(pair, {}).get("cooldown_until", 0)

def set_cooldown(pair, ts):
    state = _load_state()
    if pair not in state: state[pair] = {}
    state[pair]["cooldown_until"] = ts
    _save_state(state)