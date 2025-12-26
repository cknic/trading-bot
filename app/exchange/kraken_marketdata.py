from typing import List
from app.exchange.kraken_client import KrakenClient

def fetch_ohlc_closes(k: KrakenClient, pair: str, interval_minutes: int) -> List[float]:
    # Kraken OHLC is a public endpoint: /0/public/OHLC
    resp = k.public("OHLC", {"pair": pair, "interval": interval_minutes})
    if resp.get("error"):
        raise RuntimeError(f"OHLC error: {resp['error']}")
    result = resp["result"]
    # result includes a "last" key and one key for the pair
    pair_key = next(k for k in result.keys() if k != "last")
    candles = result[pair_key]
    # candle schema: [time, open, high, low, close, vwap, volume, count]
    closes = [float(c[4]) for c in candles]
    return closes
