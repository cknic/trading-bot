"""
IBKR Market Data - Historical bars and price fetching
"""

import os
import json
import logging
import pandas as pd
from ib_insync import util

# Technical Analysis
from ta.trend import EMAIndicator, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

logger = logging.getLogger("IBKR_MARKETDATA")

# Cache directory for UI charts
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")


async def fetch_historical_data_async(client, contract, duration='30 D', bar_size='1 hour'):
    """
    Fetch historical OHLCV data asynchronously.
    
    Args:
        client: IBKRClient instance
        contract: Qualified IB contract
        duration: How far back to fetch (e.g., '30 D', '1 W')
        bar_size: Bar size (e.g., '1 hour', '1 day', '5 mins')
    
    Returns:
        DataFrame with OHLCV + technical indicators, or None
    """
    if not client.is_connected():
        logger.warning("Not connected to IBKR")
        return None
    
    try:
        bars = await client.raw.reqHistoricalDataAsync(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True
        )
        
        if not bars:
            logger.warning(f"No bars returned for {contract.symbol}")
            return None
        
        df = util.df(bars)
        if df.empty:
            return None
        
        # Add technical indicators
        df = add_technical_indicators(df)
        
        # Save for UI
        save_ohlc_for_ui(contract.symbol, df)
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching historical data for {contract.symbol}: {e}")
        return None


def fetch_historical_data_sync(client, contract, duration='30 D', bar_size='1 hour'):
    """Synchronous version of historical data fetch"""
    if not client.is_connected():
        return None
    
    try:
        bars = client.raw.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True
        )
        
        if not bars:
            return None
        
        df = util.df(bars)
        if df.empty:
            return None
        
        df = add_technical_indicators(df)
        save_ohlc_for_ui(contract.symbol, df)
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching historical data: {e}")
        return None


def add_technical_indicators(df):
    """Add all technical indicators to DataFrame"""
    
    # EMAs
    df['EMA_9'] = EMAIndicator(close=df['close'], window=9).ema_indicator()
    df['EMA_21'] = EMAIndicator(close=df['close'], window=21).ema_indicator()
    
    # SMAs
    df['SMA_10'] = SMAIndicator(close=df['close'], window=10).sma_indicator()
    df['SMA_30'] = SMAIndicator(close=df['close'], window=30).sma_indicator()
    df['SMA_50'] = SMAIndicator(close=df['close'], window=50).sma_indicator()
    
    # RSI
    df['RSI_14'] = RSIIndicator(close=df['close'], window=14).rsi()
    
    # ATR for volatility
    df['ATR_14'] = AverageTrueRange(
        high=df['high'], 
        low=df['low'], 
        close=df['close'], 
        window=14
    ).average_true_range()
    
    # Fill NaN values
    df.fillna(0, inplace=True)
    
    return df


def get_closes_from_df(df):
    """Extract close prices as list from DataFrame"""
    if df is None or df.empty:
        return []
    return df['close'].tolist()


def calculate_price_distance_pct(price, sma_value):
    """Calculate how far price is from SMA as percentage"""
    if not sma_value or sma_value == 0:
        return 0.0
    return ((price - sma_value) / sma_value) * 100.0


def calculate_24h_change(df):
    """Calculate 24-hour price change percentage"""
    if df is None or len(df) < 2:
        return 0.0
    
    current = df.iloc[-1]['close']
    
    # For hourly bars, ~24 bars ago is yesterday
    if len(df) >= 24:
        prev = df.iloc[-24]['close']
    else:
        prev = df.iloc[0]['close']
    
    if prev == 0:
        return 0.0
    
    return ((current - prev) / prev) * 100.0


def save_ohlc_for_ui(symbol, df):
    """Saves candles to JSON for the Web UI Chart"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = []
        for row in df.itertuples():
            ts = int(row.date.timestamp() * 1000)
            data.append({
                "x": ts,
                "y": [round(row.open, 2), round(row.high, 2), round(row.low, 2), round(row.close, 2)]
            })
        
        path = os.path.join(CACHE_DIR, f"{symbol}_ohlc.json")
        with open(path, "w") as f:
            json.dump({"pair": symbol, "data": data}, f)
            
    except Exception as e:
        logger.warning(f"Failed to save chart data for {symbol}: {e}")


def build_market_summary(symbol, df, has_position=False):
    """
    Build a market summary string for AI analysis.
    
    Returns:
        (summary_string, indicators_dict, current_price)
    """
    if df is None or df.empty:
        return None, {}, 0.0
    
    curr = df.iloc[-1]
    price = curr['close']
    
    # Extract indicators
    ema_9 = curr.get('EMA_9', 0)
    ema_21 = curr.get('EMA_21', 0)
    sma_10 = curr.get('SMA_10', 0)
    sma_30 = curr.get('SMA_30', 0)
    sma_50 = curr.get('SMA_50', 0)
    rsi = curr.get('RSI_14', 50)
    atr = curr.get('ATR_14', 0)
    
    # Calculate derived values
    change_24h = calculate_24h_change(df)
    distance_from_sma30 = calculate_price_distance_pct(price, sma_30)
    volatility_pct = (atr / price * 100) if price > 0 else 0
    
    # Determine trend
    if sma_10 > sma_30:
        trend = "bullish"
        trend_strength = ((sma_10 - sma_30) / sma_30 * 100) if sma_30 > 0 else 0
    elif sma_10 < sma_30:
        trend = "bearish"
        trend_strength = ((sma_30 - sma_10) / sma_30 * 100) if sma_30 > 0 else 0
    else:
        trend = "neutral"
        trend_strength = 0
    
    # Recent prices
    recent_prices = df['close'].tail(5).tolist()
    recent_str = ", ".join([f"{p:.2f}" for p in recent_prices])
    
    # Build indicators dict
    indicators = {
        "rsi": round(rsi, 1),
        "ema_9": round(ema_9, 2),
        "ema_21": round(ema_21, 2),
        "sma_10": round(sma_10, 2),
        "sma_30": round(sma_30, 2),
        "sma_50": round(sma_50, 2),
        "trend": trend,
        "trend_strength_pct": round(trend_strength, 2),
        "distance_from_sma_pct": round(distance_from_sma30, 2),
        "volatility_pct": round(volatility_pct, 2),
        "change_24h_pct": round(change_24h, 2),
        "atr": round(atr, 4),
        "rsi_overbought": rsi > 70,
        "rsi_oversold": rsi < 30,
        "price_extended": abs(distance_from_sma30) > 5.0,
        "bullish_cross": ema_9 > ema_21 and sma_10 > sma_30,
        "bearish_cross": ema_9 < ema_21 and sma_10 < sma_30,
    }
    
    # Build summary string
    summary = (
        f"SYMBOL: {symbol}\n"
        f" - PRICE: ${price:.2f} | 24h Change: {change_24h:+.2f}%\n"
        f" - TACTICAL (1h): EMA(9): {ema_9:.2f} | EMA(21): {ema_21:.2f} | RSI: {rsi:.0f}\n"
        f" - STRATEGIC: SMA(10): {sma_10:.2f} | SMA(30): {sma_30:.2f} | SMA(50): {sma_50:.2f}\n"
        f" - TREND: {trend.upper()} | Distance from SMA30: {distance_from_sma30:+.1f}%\n"
        f" - VOLATILITY: ATR: {atr:.2f} ({volatility_pct:.1f}% of price)\n"
        f" - RECENT: [{recent_str}]\n"
        f" - POSITION: {'HOLDING' if has_position else 'FLAT'}"
    )
    
    return summary, indicators, price