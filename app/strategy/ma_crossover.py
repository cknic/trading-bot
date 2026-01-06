def calculate_sma(data, period):
    """Calculate Simple Moving Average"""
    if len(data) < period:
        return None
    return sum(data[-period:]) / period


def calculate_rsi(closes, period=14):
    """Calculate RSI (Relative Strength Index)"""
    if len(closes) < period + 1:
        return None
    
    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_volatility(closes, period=20):
    """Calculate price volatility (standard deviation as % of mean)"""
    if len(closes) < period:
        return None
    
    recent = closes[-period:]
    mean = sum(recent) / period
    if mean == 0:
        return None
    
    variance = sum((x - mean) ** 2 for x in recent) / period
    std_dev = variance ** 0.5
    
    return (std_dev / mean) * 100.0  # As percentage


def decide(closes, sma_short, sma_long, has_position, config=None):
    """
    Enhanced SMA Crossover with RSI and Distance filters.
    
    Args:
        closes: List of closing prices
        sma_short: Short SMA period (e.g., 10)
        sma_long: Long SMA period (e.g., 30)
        has_position: Whether we currently hold this asset
        config: Optional dict with filter settings:
            - rsi_period: RSI calculation period (default 14)
            - rsi_overbought: Don't buy above this RSI (default 70)
            - rsi_oversold: Oversold threshold (default 30)
            - rsi_take_profit: Sell when RSI exceeds this (default 80)
            - max_distance_pct: Max % above SMA to buy (default 8.0)
            - require_crossover: Only trade on actual cross (default False)
        
    Returns:
        tuple: (signal, reason, indicators)
            - signal: "buy", "sell", or "hold"
            - reason: Human-readable explanation
            - indicators: Dict of calculated values for AI/logging
    """
    # Default config
    if config is None:
        config = {}
    
    rsi_period = config.get("rsi_period", 14)
    rsi_overbought = config.get("rsi_overbought", 70)
    rsi_oversold = config.get("rsi_oversold", 30)
    rsi_take_profit = config.get("rsi_take_profit", 80)
    max_distance_pct = config.get("max_distance_pct", 8.0)
    require_crossover = config.get("require_crossover", False)
    
    # Safety check - need enough data for all calculations
    min_needed = max(sma_long + 1, rsi_period + 1, 20)  # 20 for volatility
    if not closes or len(closes) < min_needed:
        return "hold", f"Not enough data (need {min_needed}, got {len(closes) if closes else 0})", {}
    
    # ===== CALCULATE ALL INDICATORS =====
    current_price = closes[-1]
    
    # SMAs - current and previous (for crossover detection)
    short_now = calculate_sma(closes, sma_short)
    long_now = calculate_sma(closes, sma_long)
    short_prev = calculate_sma(closes[:-1], sma_short)
    long_prev = calculate_sma(closes[:-1], sma_long)
    
    # RSI
    rsi = calculate_rsi(closes, rsi_period)
    
    # Volatility
    volatility = calculate_volatility(closes, 20)
    
    # Validation
    if None in (short_now, long_now, short_prev, long_prev):
        return "hold", "SMA calculation failed", {}
    
    # ===== DERIVED METRICS =====
    
    # Distance from long SMA (positive = above, negative = below)
    distance_pct = ((current_price - long_now) / long_now) * 100.0
    
    # Trend determination
    trend = "bullish" if short_now > long_now else "bearish"
    
    # Crossover detection
    bullish_cross = (short_prev <= long_prev) and (short_now > long_now)
    bearish_cross = (short_prev >= long_prev) and (short_now < long_now)
    
    # Trend strength (how far apart are the SMAs)
    sma_spread_pct = ((short_now - long_now) / long_now) * 100.0
    
    # ===== PACKAGE INDICATORS =====
    indicators = {
        "price": round(current_price, 4),
        "sma_short": round(short_now, 4),
        "sma_long": round(long_now, 4),
        "sma_short_prev": round(short_prev, 4),
        "sma_long_prev": round(long_prev, 4),
        "rsi": round(rsi, 1) if rsi is not None else None,
        "volatility_pct": round(volatility, 2) if volatility is not None else None,
        "trend": trend,
        "trend_strength_pct": round(sma_spread_pct, 2),
        "distance_from_sma_pct": round(distance_pct, 2),
        "bullish_cross": bullish_cross,
        "bearish_cross": bearish_cross,
        "rsi_overbought": rsi is not None and rsi > rsi_overbought,
        "rsi_oversold": rsi is not None and rsi < rsi_oversold,
        "price_extended": distance_pct > max_distance_pct,
    }
    
    # Build stats string for logging
    rsi_str = f"RSI: {rsi:.1f}" if rsi is not None else "RSI: N/A"
    stats = (
        f"Price: ${current_price:.2f} | "
        f"SMA({sma_short}): ${short_now:.2f} | "
        f"SMA({sma_long}): ${long_now:.2f} | "
        f"{rsi_str} | "
        f"Dist: {distance_pct:+.1f}%"
    )
    
    # ===== EXIT LOGIC (has position) =====
    if has_position:
        
        # EXIT SIGNAL 1: RSI take-profit (strength exit)
        if rsi is not None and rsi > rsi_take_profit:
            return "sell", f"RSI TAKE PROFIT: RSI {rsi:.1f} > {rsi_take_profit}. {stats}", indicators
        
        # EXIT SIGNAL 2: Death cross (bearish crossover)
        if bearish_cross:
            return "sell", f"DEATH CROSS: SMA({sma_short}) crossed below SMA({sma_long}). {stats}", indicators
        
        # EXIT SIGNAL 3: Trend turned bearish (if not requiring crossover)
        if not require_crossover and short_now < long_now:
            return "sell", f"BEARISH TREND: SMA({sma_short}) < SMA({sma_long}). {stats}", indicators
        
        # No exit signal - hold position
        hold_reason = f"HOLDING: Trend {trend.upper()}"
        if rsi is not None:
            if rsi > 60:
                hold_reason += f", RSI strong ({rsi:.1f})"
            elif rsi < 40:
                hold_reason += f", RSI weak ({rsi:.1f}) - watch closely"
        
        return "hold", f"{hold_reason}. {stats}", indicators
    
    # ===== ENTRY LOGIC (no position) =====
    else:
        
        # Determine if we have a base buy signal
        if require_crossover:
            buy_signal = bullish_cross
            signal_type = "GOLDEN CROSS"
        else:
            buy_signal = (short_now > long_now)  # Trend is bullish
            signal_type = "BULLISH TREND"
        
        # No buy signal at all
        if not buy_signal:
            wait_reason = "Waiting for bullish signal"
            if bearish_cross:
                wait_reason = "DEATH CROSS just occurred - waiting"
            elif trend == "bearish":
                wait_reason = f"Trend is bearish (SMA{sma_short} < SMA{sma_long})"
            return "hold", f"{wait_reason}. {stats}", indicators
        
        # ===== APPLY FILTERS TO BUY SIGNAL =====
        
        # FILTER 1: RSI overbought - don't chase
        if rsi is not None and rsi > rsi_overbought:
            return "hold", f"FILTERED (RSI): Overbought at {rsi:.1f} > {rsi_overbought}. {stats}", indicators
        
        # FILTER 2: Price too extended above SMA - don't chase
        if distance_pct > max_distance_pct:
            return "hold", f"FILTERED (DISTANCE): Price +{distance_pct:.1f}% above SMA (max {max_distance_pct}%). {stats}", indicators
        
        # FILTER 3: Price below long SMA (even if short > long, price might have dipped)
        if current_price < long_now:
            return "hold", f"FILTERED (PRICE): Price ${current_price:.2f} below SMA({sma_long}) ${long_now:.2f}. {stats}", indicators
        
        # ===== ALL FILTERS PASSED - BUY SIGNAL =====
        
        # Add context to the buy reason
        buy_context = []
        if rsi is not None:
            if rsi < rsi_oversold:
                buy_context.append(f"RSI oversold ({rsi:.1f})")
            elif rsi < 50:
                buy_context.append(f"RSI neutral-low ({rsi:.1f})")
            else:
                buy_context.append(f"RSI {rsi:.1f}")
        
        if bullish_cross:
            buy_context.append("fresh crossover")
        
        if distance_pct < 2.0:
            buy_context.append("price near SMA")
        
        context_str = ", ".join(buy_context) if buy_context else ""
        
        return "buy", f"{signal_type}: {context_str}. {stats}", indicators


# Backward compatibility - simple version without indicators
def decide_simple(closes, sma_short, sma_long, has_position):
    """Simple version that returns just (signal, reason) for backward compatibility"""
    signal, reason, _ = decide(closes, sma_short, sma_long, has_position)
    return signal, reason