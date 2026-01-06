import csv
import os
import time
import json
import yaml
import requests
from typing import Dict, Any, List, Tuple

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRADES_DEFAULT = os.path.join(DATA_DIR, "trades.csv")
OUT_DEFAULT = os.path.join(DATA_DIR, "pnl.json")
CLOSED_TRADES_PATH = os.path.join(DATA_DIR, "closed_trades.jsonl")
IBKR_CONFIG_PATH = "config/ibkr.yaml"
KRAKEN_CONFIG_PATH = "config/kraken.yaml"
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# Default fee percentages
DEFAULT_CRYPTO_FEE_PCT = 0.26  # Kraken taker fee (0.26%)


def _load_yaml(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except:
        return {}


def _read_trades(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append(
                    {
                        "ts": int(row["ts"]),
                        "pair": row["pair"],
                        "side": row["side"],
                        "volume": float(row["volume"]),
                        "price": float(row["price"]),
                        "notional_usd": float(row["notional_usd"]),
                        "mode": row.get("mode", ""),
                        "reason": row.get("reason", ""),
                    }
                )
            except Exception:
                continue
    rows.sort(key=lambda x: x["ts"])
    return rows


def _pair_round(x: float) -> float:
    return float(f"{x:.6f}")


def _is_crypto_pair(pair: str) -> bool:
    """Determine if a pair is crypto based on naming patterns"""
    pair_upper = pair.upper()
    crypto_symbols = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "XBT", "LTC", "DOT", "AVAX", "MATIC", "LINK"]
    for sym in crypto_symbols:
        if sym in pair_upper:
            return True
    if pair_upper.startswith("X") and "USD" in pair_upper and len(pair_upper) >= 7:
        return True
    if pair_upper.endswith("USD") and len(pair_upper) >= 6:
        base = pair_upper.replace("USD", "")
        if len(base) >= 3:
            return True
    return False


def _fetch_kraken_prices(pairs: List[str]) -> Dict[str, float]:
    """Fetch live prices from Kraken API"""
    if not pairs:
        return {}
    
    prices = {}
    try:
        kraken_cfg = _load_yaml(KRAKEN_CONFIG_PATH)
        base_url = kraken_cfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
        
        pair_str = ",".join(pairs)
        url = f"{base_url}/0/public/Ticker?pair={pair_str}"
        
        resp = requests.get(url, timeout=5)
        data = resp.json()
        
        if not data.get("error"):
            for kraken_pair, ticker_data in data.get("result", {}).items():
                last_price = float(ticker_data["c"][0])
                for orig_pair in pairs:
                    if orig_pair.upper() in kraken_pair.upper() or kraken_pair.upper() in orig_pair.upper():
                        prices[orig_pair] = last_price
                        break
                else:
                    prices[kraken_pair] = last_price
    except Exception as e:
        print(f"Kraken price fetch error: {e}")
    
    return prices


def _load_stock_prices_from_cache() -> Dict[str, float]:
    """Load latest stock prices from OHLC cache files"""
    prices = {}
    if not os.path.exists(CACHE_DIR):
        return prices
    
    for filename in os.listdir(CACHE_DIR):
        if filename.endswith("_ohlc.json"):
            symbol = filename.replace("_ohlc.json", "")
            if _is_crypto_pair(symbol):
                continue
            try:
                filepath = os.path.join(CACHE_DIR, filename)
                with open(filepath, "r") as f:
                    cache_data = json.load(f)
                    candles = cache_data.get("data", [])
                    if candles:
                        last_candle = candles[-1]
                        y_values = last_candle.get("y", [0, 0, 0, 0])
                        close_price = y_values[3] if len(y_values) >= 4 else 0
                        if close_price > 0:
                            prices[symbol] = close_price
            except Exception:
                pass
    
    return prices


def _calculate_crypto_fee(notional: float, cfg: Dict[str, Any]) -> float:
    """Calculate Kraken fee (percentage based)"""
    fee_pct = cfg.get("fees", {}).get("taker_fee_pct", DEFAULT_CRYPTO_FEE_PCT) / 100.0
    return notional * fee_pct


def _calculate_ibkr_fee(shares: float, price: float, ibkr_cfg: Dict[str, Any]) -> float:
    """
    Calculate IBKR Pro Tiered fee structure:
    - $0.0035 per share
    - Minimum $0.35 per order
    - Maximum 1% of trade value
    """
    fees_cfg = ibkr_cfg.get("fees", {})
    
    per_share = fees_cfg.get("per_share", 0.0035)
    minimum_usd = fees_cfg.get("minimum_usd", 0.35)
    maximum_pct = fees_cfg.get("maximum_pct", 1.0) / 100.0
    
    share_fee = abs(shares) * per_share
    fee = max(share_fee, minimum_usd)
    notional = abs(shares * price)
    max_fee = notional * maximum_pct
    fee = min(fee, max_fee)
    
    return fee


def _get_fee(pair: str, volume: float, price: float, notional: float, 
             kraken_cfg: Dict[str, Any], ibkr_cfg: Dict[str, Any]) -> float:
    """Get the appropriate fee for a trade"""
    if _is_crypto_pair(pair):
        return _calculate_crypto_fee(notional, kraken_cfg)
    else:
        return _calculate_ibkr_fee(volume, price, ibkr_cfg)


def _get_friendly_name(pair: str) -> str:
    """Convert pair to friendly display name"""
    names = {
        "XXBTZUSD": "BTC", "XETHZUSD": "ETH", "SOLUSD": "SOL",
        "XXRPZUSD": "XRP", "ADAUSD": "ADA", "XDGUSD": "DOGE",
        "DOTUSD": "DOT", "LINKUSD": "LINK", "MATICUSD": "MATIC",
    }
    return names.get(pair, pair)


def _load_logged_close_ids(path: str) -> set:
    """Load set of already-logged close trade IDs to prevent duplicates"""
    logged = set()
    if not os.path.exists(path):
        return logged
    try:
        with open(path, "r") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    trade_id = f"{entry.get('entry_ts', 0)}_{entry.get('exit_ts', 0)}_{entry.get('pair', '')}"
                    logged.add(trade_id)
    except Exception:
        pass
    return logged


def _log_closed_trade(closed_trade: Dict[str, Any], path: str = CLOSED_TRADES_PATH) -> None:
    """Append a closed trade to the JSONL log"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(closed_trade) + "\n")
    except Exception as e:
        print(f"Failed to log closed trade: {e}")


def compute_pnl(
    trades: List[Dict[str, Any]],
    marks: Dict[str, float],
    cfg: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Compute PnL with accurate fee calculation for both Kraken and IBKR.
    Auto-fetches live prices for any pairs with missing marks.
    Logs closed trades to closed_trades.jsonl.
    """
    if cfg is None:
        cfg = {}
    
    # Load configs for fee calculation
    kraken_cfg = _load_yaml(KRAKEN_CONFIG_PATH)
    ibkr_cfg = _load_yaml(IBKR_CONFIG_PATH)
    
    # Load stock prices from cache as fallback
    cache_prices = _load_stock_prices_from_cache()
    
    for symbol, price in cache_prices.items():
        if symbol not in marks or marks.get(symbol, 0) == 0:
            marks[symbol] = price
    
    # Identify all pairs from trades that need prices
    all_pairs = set(t["pair"] for t in trades)
    crypto_pairs_needing_prices = []
    
    for pair in all_pairs:
        if _is_crypto_pair(pair) and (pair not in marks or marks.get(pair, 0) == 0):
            crypto_pairs_needing_prices.append(pair)
    
    if crypto_pairs_needing_prices:
        live_crypto_prices = _fetch_kraken_prices(crypto_pairs_needing_prices)
        for pair, price in live_crypto_prices.items():
            if price > 0:
                marks[pair] = price
    
    # Load already-logged closed trades to prevent duplicates
    logged_close_ids = _load_logged_close_ids(CLOSED_TRADES_PATH)
    
    per_pair: Dict[str, Any] = {}
    portfolio = {
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "net_pnl_usd": 0.0,
        "total_fees_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "trades_closed": 0,
        "win_rate": 0.0,
        "max_drawdown_usd": 0.0,
    }
    
    open_pos: Dict[str, Dict[str, Any]] = {}
    equity_points: List[Tuple[int, float]] = []
    realized_equity = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    total_fees = 0.0
    
    for t in trades:
        pair = t["pair"]
        side = t["side"]
        vol = t["volume"]
        px = t["price"]
        ts = t["ts"]
        notional = t["notional_usd"]
        reason = t.get("reason", "")
        
        if pair not in per_pair:
            per_pair[pair] = {
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "net_pnl_usd": 0.0,
                "fees_paid_usd": 0.0,
                "wins": 0,
                "losses": 0,
                "trades_closed": 0,
                "win_rate": 0.0,
                "open_position": False,
                "open_volume": 0.0,
                "entry_price": 0.0,
                "entry_fee_usd": 0.0,
                "mark_price": marks.get(pair, 0.0),
                "last_event_ts": 0,
            }
        
        per_pair[pair]["last_event_ts"] = ts
        
        if side == "buy":
            if pair not in open_pos or open_pos[pair].get("vol", 0.0) <= 0:
                entry_fee = _get_fee(pair, vol, px, notional, kraken_cfg, ibkr_cfg)
                open_pos[pair] = {
                    "vol": vol,
                    "entry_px": px,
                    "entry_ts": ts,
                    "cost_usd": notional,
                    "entry_fee": entry_fee,
                    "entry_reason": reason,
                }
                per_pair[pair]["open_position"] = True
                per_pair[pair]["open_volume"] = vol
                per_pair[pair]["entry_price"] = px
                per_pair[pair]["entry_fee_usd"] = entry_fee
                total_fees += entry_fee
                
        elif side == "sell":
            if pair in open_pos and open_pos[pair].get("vol", 0.0) > 0:
                entry_px = open_pos[pair]["entry_px"]
                entry_vol = open_pos[pair]["vol"]
                entry_fee = open_pos[pair].get("entry_fee", 0.0)
                entry_ts = open_pos[pair].get("entry_ts", 0)
                entry_reason = open_pos[pair].get("entry_reason", "")
                entry_notional = open_pos[pair].get("cost_usd", entry_px * entry_vol)
                close_vol = min(entry_vol, vol) if vol > 0 else entry_vol
                
                # Calculate exit fee
                exit_notional = px * close_vol
                exit_fee = _get_fee(pair, close_vol, px, exit_notional, kraken_cfg, ibkr_cfg)
                total_trade_fees = entry_fee + exit_fee
                
                # Gross and net PnL
                gross_pnl = (px - entry_px) * close_vol
                net_pnl = gross_pnl - total_trade_fees
                
                # Percentage return (based on entry cost)
                pnl_pct = (net_pnl / entry_notional * 100) if entry_notional > 0 else 0.0
                
                # ===== LOG CLOSED TRADE =====
                trade_id = f"{entry_ts}_{ts}_{pair}"
                if trade_id not in logged_close_ids:
                    closed_trade = {
                        "id": trade_id,
                        "pair": pair,
                        "symbol": _get_friendly_name(pair),
                        "asset_type": "crypto" if _is_crypto_pair(pair) else "stock",
                        "side": "LONG",
                        "volume": close_vol,
                        "entry_price": entry_px,
                        "exit_price": px,
                        "entry_ts": entry_ts,
                        "exit_ts": ts,
                        "entry_reason": entry_reason,
                        "exit_reason": reason,
                        "entry_notional": round(entry_notional, 2),
                        "exit_notional": round(exit_notional, 2),
                        "entry_fee": round(entry_fee, 4),
                        "exit_fee": round(exit_fee, 4),
                        "total_fees": round(total_trade_fees, 4),
                        "gross_pnl": round(gross_pnl, 4),
                        "net_pnl": round(net_pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "is_win": net_pnl >= 0,
                        "hold_duration_hours": round((ts - entry_ts) / 3600, 2) if entry_ts > 0 else 0,
                    }
                    _log_closed_trade(closed_trade)
                    logged_close_ids.add(trade_id)
                
                per_pair[pair]["realized_pnl_usd"] += net_pnl
                per_pair[pair]["fees_paid_usd"] += total_trade_fees
                per_pair[pair]["trades_closed"] += 1
                portfolio["realized_pnl_usd"] += net_pnl
                portfolio["trades_closed"] += 1
                total_fees += exit_fee
                
                if net_pnl >= 0:
                    per_pair[pair]["wins"] += 1
                    portfolio["wins"] += 1
                else:
                    per_pair[pair]["losses"] += 1
                    portfolio["losses"] += 1
                
                # Clear position
                open_pos[pair] = {"vol": 0.0, "entry_px": 0.0, "entry_ts": 0, "cost_usd": 0.0, "entry_fee": 0.0, "entry_reason": ""}
                per_pair[pair]["open_position"] = False
                per_pair[pair]["open_volume"] = 0.0
                per_pair[pair]["entry_price"] = 0.0
                per_pair[pair]["entry_fee_usd"] = 0.0
                
                # Equity curve
                realized_equity += net_pnl
                equity_points.append((ts, realized_equity))
                peak_equity = max(peak_equity, realized_equity)
                dd = peak_equity - realized_equity
                max_dd = max(max_dd, dd)
    
    # Calculate unrealized PnL (accounting for estimated exit fees)
    for pair, pos in open_pos.items():
        vol = pos.get("vol", 0.0)
        entry_px = pos.get("entry_px", 0.0)
        entry_fee = pos.get("entry_fee", 0.0)
        mark = marks.get(pair, 0.0)
        
        if vol > 0 and entry_px > 0:
            per_pair[pair]["mark_price"] = mark
            per_pair[pair]["open_position"] = True
            per_pair[pair]["open_volume"] = vol
            per_pair[pair]["entry_price"] = entry_px
            
            if mark > 0:
                gross_unrealized = (mark - entry_px) * vol
                exit_notional = mark * vol
                estimated_exit_fee = _get_fee(pair, vol, mark, exit_notional, kraken_cfg, ibkr_cfg)
                net_unrealized = gross_unrealized - entry_fee - estimated_exit_fee
                
                per_pair[pair]["unrealized_pnl_usd"] = net_unrealized
                portfolio["unrealized_pnl_usd"] += net_unrealized
    
    # Final rollups
    for pair, st in per_pair.items():
        st["net_pnl_usd"] = st["realized_pnl_usd"] + st["unrealized_pnl_usd"]
        tc = st["trades_closed"]
        st["win_rate"] = (st["wins"] / tc) if tc > 0 else 0.0
        
        for k in ("realized_pnl_usd", "unrealized_pnl_usd", "net_pnl_usd", "win_rate",
                  "mark_price", "entry_price", "open_volume", "fees_paid_usd", "entry_fee_usd"):
            if k in st:
                st[k] = _pair_round(st[k])
    
    portfolio["net_pnl_usd"] = portfolio["realized_pnl_usd"] + portfolio["unrealized_pnl_usd"]
    portfolio["total_fees_usd"] = total_fees
    portfolio["win_rate"] = (portfolio["wins"] / portfolio["trades_closed"]) if portfolio["trades_closed"] > 0 else 0.0
    portfolio["max_drawdown_usd"] = max_dd
    
    for k in ("realized_pnl_usd", "unrealized_pnl_usd", "net_pnl_usd", "win_rate", "max_drawdown_usd", "total_fees_usd"):
        portfolio[k] = _pair_round(portfolio[k])
    
    return {
        "ts": int(time.time()),
        "portfolio": portfolio,
        "pairs": per_pair,
        "equity_curve_realized": equity_points[-200:],
    }


def get_closed_trades(limit: int = 50) -> list:
    """Load closed trades from JSONL file"""
    if not os.path.exists(CLOSED_TRADES_PATH):
        return []
    
    trades = []
    try:
        with open(CLOSED_TRADES_PATH, "r") as f:
            for line in f:
                if line.strip():
                    trades.append(json.loads(line))
    except Exception as e:
        print(f"Error reading closed trades: {e}")
        return []
    
    # Sort by exit timestamp descending (most recent first)
    trades.sort(key=lambda x: x.get("exit_ts", 0), reverse=True)
    return trades[:limit]


def write_pnl_json(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    out = cfg.get("pnl", {}).get("summary_path", OUT_DEFAULT)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp_path = out + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, out)


def compute_and_write(cfg: Dict[str, Any], marks: Dict[str, float]) -> Dict[str, Any]:
    trades_path = cfg.get("pnl", {}).get("csv_path", TRADES_DEFAULT)
    trades = _read_trades(trades_path)
    payload = compute_pnl(trades, marks, cfg)
    write_pnl_json(cfg, payload)
    return payload