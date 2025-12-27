def calculate_sma(data, period):
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

def decide(closes, sma_short, sma_long, has_position):
    """
    Analyzes closing prices to determine Buy/Sell signals based on SMA Crossover.
    
    Args:
        closes (list): List of closing prices (floats).
        sma_short (int): Short window (e.g., 10).
        sma_long (int): Long window (e.g., 30).
        has_position (bool): Whether we currently hold this asset.
        
    Returns:
        (str, str): Tuple of (signal, reason). Signal is "buy", "sell", or "hold".
    """
    # 1. Safety Checks
    if not closes or len(closes) < sma_long:
        return "hold", f"Not enough data (Need {sma_long}, got {len(closes)})"

    # 2. Calculate Indicators
    short_val = calculate_sma(closes, sma_short)
    long_val = calculate_sma(closes, sma_long)
    
    current_price = closes[-1]
    
    if short_val is None or long_val is None:
        return "hold", "SMA calculation failed"

    # 3. Decision Logic
    # FORMAT: "Price: $95000 | SMA10: $94000 | SMA30: $93000"
    stats = f"Price: ${current_price:.2f} | SMA({sma_short}): ${short_val:.2f} | SMA({sma_long}): ${long_val:.2f}"

    if has_position:
        # LOOKING TO SELL (Exit)
        # Sell if Short crosses BELOW Long (Bearish)
        if short_val < long_val:
            return "sell", f"Bearish Cross (Short < Long). {stats}"
        else:
            return "hold", f"Holding (Trend is Bullish). {stats}"
            
    else:
        # LOOKING TO BUY (Entry)
        # Buy if Short crosses ABOVE Long (Bullish)
        if short_val > long_val:
            return "buy", f"Bullish Cross (Short > Long). {stats}"
        else:
            return "hold", f"Waiting (Trend is Bearish). {stats}"