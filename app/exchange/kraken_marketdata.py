def fetch_ohlc_closes(k, pair, interval):
    """
    Fetches OHLC data from Kraken and returns a list of closing prices.
    
    Args:
        k: KrakenClient instance
        pair: Pair string (e.g., 'XXBTZUSD')
        interval: Timeframe in minutes (required) - strictly respecting YAML config.
    
    Returns:
        List[float]: A list of closing prices, ordered from oldest to newest.
    """
    try:
        # Kraken public endpoint: OHLC
        # Params: pair, interval
        resp = k.public("OHLC", {"pair": pair, "interval": interval})
        
        if resp.get("error"):
            print(f"Market Data Error for {pair}: {resp['error']}")
            return []

        # Kraken returns: { "result": { "XXBTZUSD": [[time, open, high, low, close, ...], ...], "last": ... } }
        result = resp["result"]
        
        # The key is likely the resolved ID (e.g. XXBTZUSD)
        ohlc_data = []
        for key, val in result.items():
            if key != "last" and isinstance(val, list):
                ohlc_data = val
                break
        
        if not ohlc_data:
            return []

        # Extract Closing Prices (Index 4 in Kraken OHLC arrays)
        # Entry format: [int <time>, str <open>, str <high>, str <low>, str <close>, ...]
        closes = [float(candle[4]) for candle in ohlc_data]
        
        return closes

    except Exception as e:
        print(f"Exception fetching market data for {pair}: {e}")
        return []