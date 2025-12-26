from typing import List, Dict, Any

def sma(vals: List[float], n: int) -> float:
    if len(vals) < n:
        raise ValueError("not enough values for SMA")
    return sum(vals[-n:]) / n

def decide(prices: List[float], sma_short: int, sma_long: int, has_position: bool) -> Dict[str, Any]:
    if len(prices) < max(sma_short, sma_long):
        return {"action": "hold", "reason": "not enough candles"}

    s = sma(prices, sma_short)
    l = sma(prices, sma_long)

    if (s > l) and (not has_position):
        return {"action": "buy", "reason": f"sma_short {s:.2f} > sma_long {l:.2f}"}
    if (s < l) and has_position:
        return {"action": "sell", "reason": f"sma_short {s:.2f} < sma_long {l:.2f}"}

    return {"action": "hold", "reason": f"no cross condition (s={s:.2f}, l={l:.2f})"}
