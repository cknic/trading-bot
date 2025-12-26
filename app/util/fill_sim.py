import random
from typing import Tuple, Optional, Dict, Any

def simulate_fill(
    side: str,
    ordertype: str,
    volume: float,
    limit_price: Optional[float],
    m: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[bool, float]:
    """
    Returns (filled, fill_price). Deterministic if random_seed is set.
    """
    bid = float(m.get("bid", 0) or 0)
    ask = float(m.get("ask", 0) or 0)

    if bid <= 0 or ask <= 0 or ask <= bid:
        return False, 0.0

    mode = cfg.get("mode", "cross_only")
    seed = cfg.get("random_seed", 42)
    rng = random.Random(seed + int(bid * 100) + int(ask * 100))  # stable-ish deterministic

    # Market orders fill immediately at ask/bid
    if ordertype == "market":
        return True, ask if side == "buy" else bid

    # Limit orders need a limit_price
    if limit_price is None or limit_price <= 0:
        return False, 0.0

    # Immediate cross logic
    if side == "buy":
        if limit_price >= ask:
            return True, ask
        if mode == "cross_only":
            return False, 0.0
        # Probabilistic: closer to ask => higher fill probability
        p = max(0.0, min(1.0, (limit_price - bid) / (ask - bid)))
        return (rng.random() < p), (limit_price if rng.random() < 0.5 else ask)
    else:
        # sell
        if limit_price <= bid:
            return True, bid
        if mode == "cross_only":
            return False, 0.0
        # For sell: closer to bid => higher fill probability
        p = max(0.0, min(1.0, (ask - limit_price) / (ask - bid)))
        return (rng.random() < p), (limit_price if rng.random() < 0.5 else bid)