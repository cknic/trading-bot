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
    Returns dict with position data including pyramiding support.
    
    Returns:
        dict: {
            'has_position': bool,
            'base_volume': float,
            'average_price': float,
            'first_entry_time': int,  # Original entry timestamp (for decay calc)
            'entry_ts': int           # Most recent activity timestamp
        }
    """
    state = _load_state()
    default = {
        "has_position": False, 
        "base_volume": 0.0, 
        "average_price": 0.0,
        "first_entry_time": 0,
        "entry_ts": 0
    }
    
    pos = state.get(pair, default)
    
    # Backwards compatibility: if old record has entry_ts but no first_entry_time
    if pos.get("has_position") and not pos.get("first_entry_time") and pos.get("entry_ts"):
        pos["first_entry_time"] = pos["entry_ts"]
    
    return pos

def set_position(pair, volume, price, first_entry_time=None):
    """
    Called after a BUY. Updates state to reflect we hold the asset.
    
    Args:
        pair: Trading pair/symbol
        volume: Volume being added
        price: Price of this entry
        first_entry_time: Optional - preserve original entry time for pyramiding.
                         If None and no existing position, uses current time.
                         If None and existing position, preserves existing first_entry_time.
    """
    state = _load_state()
    
    # Default structure if new
    current = state.get(pair, {"base_volume": 0.0, "average_price": 0.0, "has_position": False})
    
    new_vol = float(volume)
    # Weighted Average Price logic
    current_vol = current.get("base_volume", 0.0)
    current_avg = current.get("average_price", 0.0)
    
    total_cost = (current_vol * current_avg) + (new_vol * float(price))
    total_vol = current_vol + new_vol
    
    avg_price = total_cost / total_vol if total_vol > 0 else 0.0
    
    # PYRAMIDING LOGIC:
    # - first_entry_time: NEVER changes once set (for decaying stop-loss calculation)
    # - entry_ts: Updates on each add (for UI "last activity" display)
    
    if current.get("has_position") and current.get("first_entry_time"):
        # Existing position - preserve the original first_entry_time
        original_first_entry = current["first_entry_time"]
    elif first_entry_time:
        # Explicitly passed (e.g., from reconciliation)
        original_first_entry = first_entry_time
    else:
        # Fresh position - set first_entry_time to now
        original_first_entry = int(time.time())
    
    # entry_ts tracks the most recent activity (for UI/monitoring)
    # first_entry_time tracks the original entry (for decay calculations)
    state[pair] = {
        "has_position": True,
        "base_volume": total_vol,
        "average_price": avg_price,
        "first_entry_time": original_first_entry,  # NEVER changes after initial entry
        "entry_ts": int(time.time()),              # Updates on each pyramid add
        "last_update": int(time.time()),
        "cooldown_until": current.get("cooldown_until", 0)  # Preserve cooldown
    }
    _save_state(state)
    
    # Log pyramid vs fresh entry
    if current_vol > 0:
        print(f"LEDGER: PYRAMID ADD for {pair}. Vol: {current_vol:.6f} -> {total_vol:.6f} @ ${avg_price:.2f}")
    else:
        print(f"LEDGER: NEW POSITION for {pair}. Vol: {total_vol:.6f} @ ${avg_price:.2f}")

def clear_position(pair):
    """
    Called after a SELL. Marks position as closed.
    Resets first_entry_time so next entry starts fresh.
    """
    state = _load_state()
    if pair in state:
        # Keep cooldown info, clear ALL position info including first_entry_time
        cooldown = state[pair].get("cooldown_until", 0)
        state[pair] = {
            "has_position": False,
            "base_volume": 0.0,
            "average_price": 0.0,
            "first_entry_time": 0,  # Reset for next position
            "entry_ts": 0,
            "last_update": int(time.time()),
            "cooldown_until": cooldown
        }
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