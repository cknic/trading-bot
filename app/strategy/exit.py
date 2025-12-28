import time

def calculate_decaying_stop(
    entry_price: float, 
    entry_ts: int, 
    current_price: float, 
    config: dict
) -> tuple[bool, str]:
    """
    Calculates if a position should be closed based on a decaying stop-loss.
    
    Logic:
    1. Start with a wide stop (e.g., 10%).
    2. Every 24h, tighten the stop by 'decay_daily_pct' (e.g., 1%).
    3. Floor the stop at 'min_stop_pct' (e.g., 0.5% below entry).
    """
    
    # 1. Config & Defaults
    initial_stop_pct = config.get("initial_stop_pct", 0.10)  # Start 10% away
    decay_daily_pct = config.get("decay_daily_pct", 0.01)    # Tighten 1% per day
    min_stop_pct = config.get("min_stop_pct", 0.005)         # Never tighter than 0.5%
    max_hold_days = config.get("max_hold_days", 14)          # Hard exit after 2 weeks
    
    # 2. Time Calculations
    if entry_ts <= 0: return False, "" # Safety check
    
    now = int(time.time())
    seconds_held = now - entry_ts
    days_held = seconds_held / 86400.0
    
    # 3. Hard Stall Exit (Time-based)
    if days_held >= max_hold_days:
        return True, f"STALL EXIT: Held for {days_held:.1f} days (Limit: {max_hold_days})"

    # 4. Decaying Stop Calculation
    # Formula: Current Stop Distance = Initial - (DaysHeld * DecayRate)
    current_stop_dist = max(min_stop_pct, initial_stop_pct - (days_held * decay_daily_pct))
    
    stop_price = entry_price * (1.0 - current_stop_dist)
    
    # 5. Check Price
    if current_price < stop_price:
        return True, f"DECAY STOP: Price ${current_price:.4f} < Dynamic Stop ${stop_price:.4f} (Dist: {current_stop_dist*100:.2f}%)"
        
    return False, ""