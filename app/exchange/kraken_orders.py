import time

# --- CACHE ---
# Stores pair info (decimals, min_size) so we don't spam Kraken API
_PAIR_INFO_CACHE = {}


class OrderDecision:
    def __init__(self, should_place, side=None, volume=None, price=None, reason="", mode="dry_run"):
        self.should_place = should_place
        self.side = side
        self.volume = volume
        self.price = price
        self.reason = reason
        self.mode = mode


def resolve_pair_info(k, pair):
    """
    Resolves friendly names (ETHUSD) to Kraken IDs (XETHZUSD) and fetches decimals.
    Now uses caching to prevent timeouts on repetitive calls.
    """
    global _PAIR_INFO_CACHE
    
    # 1. Check Cache
    if pair in _PAIR_INFO_CACHE:
        return _PAIR_INFO_CACHE[pair]

    # 2. Fetch from API (Only if not in cache)
    try:
        resp = k.public("AssetPairs", {"pair": pair})
        if resp.get("error"):
            raise Exception(f"Kraken error: {resp['error']}")
        
        result = resp["result"]
        # Kraken returns a dict key like 'XXBTZUSD' or 'XBTUSD'
        key = list(result.keys())[0]
        info = result[key]
        
        # Normalize Data
        normalized = {
            "pair_key": key,
            "ordermin": float(info.get("ordermin", "0.001")),
            "pair_decimals": int(info.get("pair_decimals", 2)),
            "lot_decimals": int(info.get("lot_decimals", 8)),
            "cost_decimals": int(info.get("cost_decimals", 5)),
            "wsname": info.get("wsname", key) 
        }
        
        # 3. Save to Cache (Cache both input name and resolved name)
        _PAIR_INFO_CACHE[pair] = (key, normalized)
        _PAIR_INFO_CACHE[key] = (key, normalized)
        
        return key, normalized

    except Exception as e:
        raise Exception(f"Failed to resolve pair {pair}: {e}")


def get_price(k, pair_key):
    # Always fetch fresh price (never cache this!)
    t = k.public("Ticker", {"pair": pair_key})
    if t.get("error"):
        raise Exception(str(t["error"]))
    # Ticker format: {"XXBTZUSD": {"c": ["price", "vol"], ...}}
    return float(t["result"][pair_key]["c"][0])


def build_order(k, cfg, pair_key, side, base_volume_override=None):
    """
    Calculates volume, price, and checks min-order constraints.
    """
    # 1. Get Rules (Cached)
    _, info = resolve_pair_info(k, pair_key)
    
    # 2. Get Price (Fresh)
    price = get_price(k, pair_key)
    
    # 3. Calculate Volume
    volume = 0.0
    
    if side == "sell":
        if base_volume_override is None:
            return OrderDecision(False, reason="Sell requested but no position volume provided"), {}
        volume = float(base_volume_override)
    else:
        # Buy: Calculate based on fixed USD amount
        notional = float(cfg["trading"]["quote_notional_usd"])
        volume = notional / price

    # 4. Enforce Min Order Size
    if volume < info["ordermin"]:
        return OrderDecision(False, reason=f"Volume {volume:.6f} < min {info['ordermin']}"), {"last": price}

    # 5. Format Volume (Fix decimals)
    fmt_vol = f"{volume:.{info['lot_decimals']}f}"
    
    mode = cfg["trading"]["mode"]
    return OrderDecision(True, side=side, volume=fmt_vol, price=price, reason=f"{mode} order planned", mode=mode), {"last": price}


def place_or_preview(k, cfg, risk_engine, pair, side, base_volume_override=None):
    """
    Main entry point for order placement.
    
    Args:
        k: Kraken API client
        cfg: Trading configuration
        risk_engine: RiskEngine instance
        pair: Trading pair (e.g., "XBTUSD")
        side: "buy" or "sell"
        base_volume_override: For sells, the volume to sell
    """
    # 1. Resolve pair
    pk, _ = resolve_pair_info(k, pair)
    
    # 2. Build Decision
    od, metrics = build_order(k, cfg, pk, side, base_volume_override)
    
    if not od.should_place:
        return od, metrics

    # 3. Risk Check - Pass side so sells aren't blocked by position limits
    print(f"[RISK] Checking: side={side}, volume={od.volume}, price={od.price}")
    
    if not risk_engine.check(side, float(od.volume), od.price):
        od.should_place = False
        od.reason = "Risk check failed"
        return od, metrics

    # 4. Execution
    if od.mode == "live":
        print(f"LIVE EXECUTION: {side} {od.volume} {pk}")
        
        req = {
            "pair": pk,
            "type": side,
            "ordertype": "market",
            "volume": od.volume,
            "validate": False 
        }
        
        try:
            resp = k.private("AddOrder", req)
            if resp.get("error"):
                od.reason = f"Kraken Reject: {resp['error']}"
            else:
                od.reason = "LIVE order placed"
        except Exception as e:
            od.reason = f"Execution Exception: {e}"
            
    else:
        # Dry Run Simulation
        od.reason = "dry-run"
    
    return od, metrics