import os
import json
import csv
import time
import yaml
import re
import requests
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ==============================================================================
# 1. CONFIGURATION & PATHS
# ==============================================================================
DATA_DIR = os.environ.get("DATA_DIR", "/data")
RUN_DIR = os.environ.get("RUN_DIR", "/run/trading")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# Find template directory - check multiple possible locations
def _find_template_dir():
    """Search for templates directory in common locations"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Possible locations to check
    candidates = [
        os.path.join(script_dir, "templates"),                    # Same dir as server.py
        os.path.join(script_dir, "..", "templates"),              # One level up
        os.path.join(script_dir, "..", "..", "templates"),        # Two levels up
        "/app/templates",                                          # Docker absolute
        os.path.join(os.getcwd(), "templates"),                   # Current working dir
        os.path.join(os.getcwd(), "app", "templates"),            # CWD/app/templates
    ]
    
    for path in candidates:
        normalized = os.path.normpath(path)
        if os.path.isdir(normalized):
            dashboard = os.path.join(normalized, "dashboard.html")
            if os.path.exists(dashboard):
                print(f"[INFO] Found templates at: {normalized}")
                return normalized
    
    # Fallback - return first candidate (will show error in UI)
    print(f"[WARN] Templates not found. Searched: {candidates}")
    return candidates[0]

TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR") or _find_template_dir()
DASHBOARD_HTML = os.path.join(TEMPLATE_DIR, "dashboard.html")

TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")
PNL_JSON = os.path.join(DATA_DIR, "pnl.json")
STATE_JSON = os.path.join(DATA_DIR, "state.json")
AI_LOG_PATH = os.path.join(DATA_DIR, "ai_log.jsonl")
BOT_LOG_PATH = os.path.join(DATA_DIR, "bot_errors.jsonl")
STATUS_CRYPTO = os.path.join(DATA_DIR, "bot_status.json")
STATUS_STOCKS = os.path.join(DATA_DIR, "bot_stocks_status.json")

PAUSE_FILE = os.path.join(RUN_DIR, "PAUSE")
KILL_FILE = os.path.join(RUN_DIR, "KILL_SWITCH")
MANUAL_ORDER_PATH = os.path.join(RUN_DIR, "MANUAL_ORDER.json")

CONFIG_KRAKEN = os.path.join(CONFIG_DIR, "kraken.yaml")
CONFIG_IBKR = os.path.join(CONFIG_DIR, "ibkr.yaml")
AI_CONFIG_PATH = os.path.join(CONFIG_DIR, "ai.yaml")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
START_TS = int(time.time())

# Increased AI log limit for scrolling
AI_LOG_LIMIT = 200

FRIENDLY_NAMES = {
    "XXBTZUSD": "Bitcoin", "XETHZUSD": "Ethereum", "SOLUSD": "Solana",
    "XXRPZUSD": "XRP", "ADAUSD": "Cardano", "XDGUSD": "Dogecoin",
    "XBTUSD": "Bitcoin", "ETHUSD": "Ethereum",
    "NVDA": "NVIDIA", "TSLA": "Tesla", "AAPL": "Apple", "MSFT": "Microsoft",
    "AMD": "AMD", "INTC": "Intel", "F": "Ford", "GM": "GM",
    "PLTR": "Palantir", "SOFI": "SoFi", "COIN": "Coinbase",
    "BAC": "Bank of America", "T": "AT&T", "SPY": "S&P 500", "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000", "ARCC": "Ares Capital", "ET": "Energy Transfer",
    "EPD": "Enterprise Products", "PFE": "Pfizer", "IONQ": "IonQ", "UPST": "Upstart"
}

CRYPTO_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "XBT", "LTC", "DOT", "AVAX", "MATIC", "LINK"]


# ==============================================================================
# 2. DATA MODELS & API SETUP
# ==============================================================================
class ReasonBody(BaseModel):
    reason: str = "manual"


class PromptUpdate(BaseModel):
    new_prompt: str
    target_key: str


class ManualExecuteBody(BaseModel):
    pair: str
    side: str
    notional_usd: float = 20.0


app = FastAPI(title="Sentinel Command Center", version="12.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ==============================================================================
# 3. HELPER FUNCTIONS
# ==============================================================================
def _auth_ok(authorization: Optional[str]) -> bool:
    if not ADMIN_TOKEN:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return authorization.split(" ", 1)[1].strip() == ADMIN_TOKEN


def _require_auth(authorization: Optional[str]):
    if not _auth_ok(authorization):
        raise HTTPException(401, "Unauthorized")


def _read_json_safe(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_yaml(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _tail_file(path, limit=50):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            if path.endswith(".csv"):
                return list(csv.DictReader(f))[-limit:]
            else:
                return [json.loads(line) for line in f if line.strip()][-limit:]
    except Exception:
        return []


def _is_crypto_symbol(symbol):
    clean = symbol.replace("/", "").upper()
    if "USD" in clean:
        for cs in CRYPTO_SYMBOLS:
            if cs in clean:
                return True
        if clean.startswith("X") and len(clean) >= 7:
            return True
    return False


def _get_asset_type(symbol, kraken_pairs=None):
    if kraken_pairs and symbol in kraken_pairs:
        return "crypto"
    if _is_crypto_symbol(symbol):
        return "crypto"
    return "stocks"


def _get_friendly_name(symbol):
    fname = FRIENDLY_NAMES.get(symbol, symbol)
    if fname == symbol:
        return symbol
    return f"{fname} ({symbol})"


def _detect_log_type(entry: dict) -> tuple:
    """Detect log type from asset field - both bots now use same format"""
    asset = entry.get("asset", "")
    
    if not asset:
        return ("unknown", "Unknown")
    
    asset_lower = asset.lower()
    if "(stock)" in asset_lower:
        ticker = asset.split("(")[0].strip()
        return ("stocks", ticker)
    elif "(crypto)" in asset_lower:
        symbol = asset.split("(")[0].strip()
        return ("crypto", symbol)
    else:
        clean = asset.replace("(", "").replace(")", "").strip()
        if _is_crypto_symbol(clean):
            return ("crypto", clean)
        return ("stocks", clean)


def _load_dashboard_html():
    """Load dashboard HTML from file, with fallback"""
    if os.path.exists(DASHBOARD_HTML):
        try:
            with open(DASHBOARD_HTML, "r") as f:
                return f.read()
        except Exception as e:
            print(f"Error loading dashboard HTML: {e}")
    
    # Fallback minimal HTML
    return """<!DOCTYPE html>
<html>
<head><title>Sentinel</title></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:monospace;padding:20px;">
<h1>Sentinel Command Center</h1>
<p style="color:#ef4444;">Dashboard template not found at: """ + DASHBOARD_HTML + """</p>
<p>Please ensure templates/dashboard.html exists.</p>
</body>
</html>"""


# ==============================================================================
# 4. API ENDPOINTS
# ==============================================================================
@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    return {
        "status": "ok",
        "uptime": int(time.time() - START_TS),
        "auth_ok": _auth_ok(authorization)
    }


@app.get("/api/data")
def get_dashboard_data(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    
    market_open = True
    market_status_str = "Unknown"
    next_open_str = None
    
    try:
        try:
            from util.market_hours import is_us_market_open, get_next_market_open
        except ImportError:
            from app.util.market_hours import is_us_market_open, get_next_market_open
        
        market_open, market_status_str = is_us_market_open()
        if not market_open:
            _, next_open_str = get_next_market_open()
    except Exception as e:
        market_status_str = f"Unknown ({type(e).__name__})"
    
    st_crypto = _read_json_safe(STATUS_CRYPTO)
    st_stocks = _read_json_safe(STATUS_STOCKS)
    
    if st_crypto.get("mode_config") == "live" and not st_crypto.get("live_allowed", False):
        st_crypto["display_status"] = "LIVE (LATCHED)"
        st_crypto["display_color"] = "yellow"
    else:
        mode = (st_crypto.get("mode_config") or "PAPER").upper()
        st_crypto["display_status"] = mode
        st_crypto["display_color"] = "green" if mode == "PAPER" else "red"
    
    st_stocks["market_open"] = market_open
    st_stocks["market_status"] = market_status_str
    if not market_open and next_open_str:
        st_stocks["next_market_open"] = next_open_str
    
    kcfg = _load_yaml(CONFIG_KRAKEN)
    kraken_pairs = kcfg.get("kraken", {}).get("pairs", [])
    ibkr_stocks = _load_yaml(CONFIG_IBKR).get("universe", {}).get("stocks", [])
    state = _read_json_safe(STATE_JSON)
    pnl_data = _read_json_safe(PNL_JSON)
    pnl_pairs = pnl_data.get("pairs", {})
    
    positions = {}
    crypto_to_fetch = []
    price_map = {}
    
    total_cost_basis = 0.0
    total_value = 0.0
    total_unrealized = 0.0
    total_fees = 0.0
    
    crypto_cost_basis = 0.0
    crypto_value = 0.0
    crypto_unrealized = 0.0
    crypto_fees = 0.0
    
    stocks_cost_basis = 0.0
    stocks_value = 0.0
    stocks_unrealized = 0.0
    stocks_fees = 0.0
    
    for pair, pdata in pnl_pairs.items():
        if not pdata.get("open_position"):
            continue
            
        entry_price = pdata.get("entry_price", 0.0)
        volume = pdata.get("open_volume", 0.0)
        mark_price = pdata.get("mark_price", 0.0)
        pair_unrealized = pdata.get("unrealized_pnl_usd", 0.0)
        pair_entry_fee = pdata.get("entry_fee_usd", 0.0)
        pair_fees_paid = pdata.get("fees_paid_usd", 0.0)
        
        cost_basis = entry_price * volume
        current_value = mark_price * volume if mark_price > 0 else cost_basis
        pair_total_fees = pair_entry_fee + pair_fees_paid
        
        asset_type = _get_asset_type(pair, kraken_pairs)
        
        if asset_type == "crypto":
            crypto_cost_basis += cost_basis
            crypto_value += current_value
            crypto_unrealized += pair_unrealized
            crypto_fees += pair_total_fees
        else:
            stocks_cost_basis += cost_basis
            stocks_value += current_value
            stocks_unrealized += pair_unrealized
            stocks_fees += pair_total_fees
    
    total_cost_basis = crypto_cost_basis + stocks_cost_basis
    total_value = crypto_value + stocks_value
    total_unrealized = crypto_unrealized + stocks_unrealized
    total_fees = crypto_fees + stocks_fees
    
    def calc_pct(unrealized, cost_basis):
        if cost_basis > 0:
            return (unrealized / cost_basis) * 100
        return 0.0
    
    total_pct = calc_pct(total_unrealized, total_cost_basis)
    crypto_pct = calc_pct(crypto_unrealized, crypto_cost_basis)
    stocks_pct = calc_pct(stocks_unrealized, stocks_cost_basis)
    
    for symbol, data in state.items():
        if not data.get("has_position"):
            continue
        atype = _get_asset_type(symbol, kraken_pairs)
        data['asset_type'] = atype
        data['friendly_name'] = _get_friendly_name(symbol)
        if atype == 'crypto':
            data['mode'] = st_crypto.get("display_status", "PAPER")
            crypto_to_fetch.append(symbol)
        else:
            stock_mode = _load_yaml(CONFIG_IBKR).get("trading", {}).get("mode", "PAPER").upper()
            data['mode'] = stock_mode
            pnl_pair_data = pnl_pairs.get(symbol, {})
            mark = pnl_pair_data.get("mark_price", 0.0)
            if mark > 0:
                price_map[symbol] = mark
            else:
                price_map[symbol] = float(data.get("average_price", 0.0))
        positions[symbol] = data
    
    if crypto_to_fetch:
        try:
            base_url = kcfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
            pair_str = ",".join(crypto_to_fetch)
            url = f"{base_url}/0/public/Ticker?pair={pair_str}"
            resp = requests.get(url, timeout=2).json()
            if not resp.get("error"):
                for k, v in resp.get("result", {}).items():
                    price_map[k] = float(v["c"][0])
        except Exception as e:
            print(f"Price Fetch Error: {e}")
    
    for s, p in positions.items():
        curr = price_map.get(s, float(p.get("average_price", 0.0)))
        p['current_price'] = round(curr, 4)
        entry = float(p.get("average_price", 0.0))
        vol = float(p.get("base_volume", 0.0))
        val = curr * vol
        cost = entry * vol
    
        pnl_pair_data = pnl_pairs.get(s, {})
    
        if pnl_pair_data and pnl_pair_data.get("open_position"):
            unrealized = pnl_pair_data.get("unrealized_pnl_usd", 0.0)
            pct = calc_pct(unrealized, cost)
            # Get fees for this position
            entry_fee = pnl_pair_data.get("entry_fee_usd", 0.0)
            fees_paid = pnl_pair_data.get("fees_paid_usd", 0.0)
            position_fees = entry_fee + fees_paid
        else:
            unrealized = (curr - entry) * vol
            pct = ((curr - entry) / entry) * 100 if entry > 0 else 0
            position_fees = 0.0
    
        p['base_volume'] = round(vol, 4)
        p['average_price'] = round(entry, 4)
        p['current_value'] = round(val, 2)
        p['cost_basis'] = round(cost, 2)
        p['unrealized_usd'] = round(unrealized, 2)
        p['unrealized_pct'] = round(pct, 2)
        p['fees_usd'] = round(position_fees, 4)
    
    raw_trades = _tail_file(TRADES_CSV, 50)
    trades = []
    for t in raw_trades:
        t['friendly_name'] = _get_friendly_name(t.get('pair'))
        t['price'] = round(float(t.get('price', 0)), 4)
        t['vol'] = round(float(t.get('vol', t.get('volume', 0))), 4)
        t['asset_type'] = _get_asset_type(t.get('pair', ''), kraken_pairs)
        trades.append(t)
    
    # Increased AI log limit for scrolling
    ai_logs_raw = _tail_file(AI_LOG_PATH, AI_LOG_LIMIT)
    ai_logs = []
    for log in ai_logs_raw:
        log_type, asset = _detect_log_type(log)
        log['type'] = log_type
        type_suffix = "Crypto" if log_type == "crypto" else "Stock"
        log['asset'] = f"{asset} ({type_suffix})"
        ai_logs.append(log)
    
    return {
        "status_crypto": st_crypto,
        "status_stocks": st_stocks,
        "positions": positions,
        "trades": trades,
        "ai_logs": ai_logs,
        "prompts": _load_yaml(AI_CONFIG_PATH).get("prompts", {}),
        "totals": {
            "total_cost_basis": round(total_cost_basis, 2),
            "total_value": round(total_value, 2),
            "total_unrealized": round(total_unrealized, 2),
            "total_fees": round(total_fees, 2),
            "total_pct": round(total_pct, 2),
            "crypto_cost_basis": round(crypto_cost_basis, 2),
            "crypto_value": round(crypto_value, 2),
            "crypto_unrealized": round(crypto_unrealized, 2),
            "crypto_fees": round(crypto_fees, 2),
            "crypto_pct": round(crypto_pct, 2),
            "stocks_cost_basis": round(stocks_cost_basis, 2),
            "stocks_value": round(stocks_value, 2),
            "stocks_unrealized": round(stocks_unrealized, 2),
            "stocks_fees": round(stocks_fees, 2),
            "stocks_pct": round(stocks_pct, 2)
        },
        "global_config": {
            "kraken_pairs": kraken_pairs,
            "ibkr_stocks": ibkr_stocks,
            "friendly_map": FRIENDLY_NAMES
        },
        "market_hours": {
            "stocks_open": market_open,
            "status": market_status_str,
            "next_open": next_open_str,
            "crypto_open": True
        }
    }


@app.get("/api/equity")
def get_equity_curve(filter: str = "all", timeframe: str = "all", authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    
    pnl_data = _read_json_safe(PNL_JSON)
    
    if not pnl_data:
        return {"curve": [[int(time.time()), 0.0]], "label": "No PnL Data", "net_pnl": 0.0}
    
    now = int(time.time())
    if timeframe == "1d":
        cutoff = now - 86400
        tf_label = "24h"
    elif timeframe == "1w":
        cutoff = now - 604800
        tf_label = "7d"
    elif timeframe == "1m":
        cutoff = now - 2592000
        tf_label = "30d"
    else:
        cutoff = 0
        tf_label = "All Time"
    
    raw_curve = pnl_data.get("equity_curve_realized", [])
    filtered_curve = [[ts, val] for ts, val in raw_curve if ts >= cutoff]
    
    portfolio = pnl_data.get("portfolio", {})
    total_realized = portfolio.get("realized_pnl_usd", 0.0)
    
    kcfg = _load_yaml(CONFIG_KRAKEN)
    kraken_pairs = kcfg.get("kraken", {}).get("pairs", [])
    
    if filter in ("crypto", "stocks"):
        pairs_data = pnl_data.get("pairs", {})
        filtered_realized = 0.0
        
        for pair, data in pairs_data.items():
            asset_type = _get_asset_type(pair, kraken_pairs)
            if asset_type == filter:
                filtered_realized += data.get("realized_pnl_usd", 0.0)
        
        curve = [[now, round(filtered_realized, 2)]]
        if filtered_curve:
            curve = [[ts, round(val, 2)] for ts, val in filtered_curve[-200:]]
        
        label = f"{filter.upper()} Realized P&L ({tf_label})"
        return {"curve": curve, "label": label, "net_pnl": round(filtered_realized, 2)}
    
    else:
        if filtered_curve:
            rounded_curve = [[ts, round(float(val), 2)] for ts, val in filtered_curve[-200:]]
        else:
            rounded_curve = [[now, round(total_realized, 2)]]
        
        label = f"Total Realized P&L ({tf_label})"
        return {"curve": rounded_curve, "label": label, "net_pnl": round(total_realized, 2)}


@app.get("/api/errors")
def get_error_logs(limit: int = 50, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    
    errors = []
    if os.path.exists(BOT_LOG_PATH):
        try:
            with open(BOT_LOG_PATH, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            errors.append(json.loads(line))
                        except:
                            pass
        except:
            pass
    
    return {"errors": errors[-limit:][::-1]}


@app.post("/api/errors/clear")
def clear_errors(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    
    if os.path.exists(BOT_LOG_PATH):
        try:
            os.remove(BOT_LOG_PATH)
        except:
            pass
    
    return {"status": "cleared"}


@app.get("/candles")
def get_candles(pair: str, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if not pair:
        return {"pair": "", "data": []}
    safe_pair = "".join(c for c in pair if c.isalnum())
    cache_file = os.path.join(CACHE_DIR, f"{safe_pair}_ohlc.json")
    if os.path.exists(cache_file):
        is_fresh = (time.time() - os.path.getmtime(cache_file) < 3600)
        is_crypto = _is_crypto_symbol(pair)
        if is_fresh or not is_crypto:
            return _read_json_safe(cache_file)
    if _is_crypto_symbol(pair):
        try:
            kcfg = _load_yaml(CONFIG_KRAKEN)
            base_url = kcfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
            url = f"{base_url}/0/public/OHLC?pair={pair}&interval=60"
            r = requests.get(url, timeout=5)
            data = r.json()
            candles = []
            if not data.get("error"):
                for k, v in data.get("result", {}).items():
                    if k != "last" and isinstance(v, list):
                        for c in v:
                            candles.append({
                                "x": int(c[0]) * 1000,
                                "y": [float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                            })
                        break
            os.makedirs(CACHE_DIR, exist_ok=True)
            result = {"pair": pair, "data": sorted(candles, key=lambda i: i['x'])}
            with open(cache_file, "w") as f:
                json.dump(result, f)
            return result
        except Exception:
            pass
    return {"pair": pair, "data": []}


@app.post("/config/ai")
def update_ai_config(body: PromptUpdate, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    try:
        cfg = _load_yaml(AI_CONFIG_PATH)
        if "prompts" not in cfg:
            cfg["prompts"] = {}
        cfg["prompts"][body.target_key] = body.new_prompt
        with open(AI_CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f)
        return {"status": "saved", "key": body.target_key}
    except Exception as e:
        raise HTTPException(500, str(e))


# ==============================================================================
# 5. CONTROL ENDPOINTS
# ==============================================================================
@app.post("/control/pause")
def pause(body: ReasonBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(PAUSE_FILE, "w") as f:
        f.write(body.reason)
    return {"status": "paused"}


@app.post("/control/resume")
def resume(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    return {"status": "resumed"}


@app.post("/control/kill")
def kill(body: ReasonBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(KILL_FILE, "w") as f:
        f.write(body.reason)
    return {"status": "killed"}


@app.post("/control/restart")
def restart(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    os._exit(1)


@app.post("/control/factory_reset")
def factory_reset(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    for fpath in [STATE_JSON, PNL_JSON, TRADES_CSV, AI_LOG_PATH, STATUS_CRYPTO, STATUS_STOCKS, BOT_LOG_PATH]:
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass
    return {"status": "reset_complete"}


@app.post("/manual/execute")
def manual_execute(body: ManualExecuteBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    os.makedirs(RUN_DIR, exist_ok=True)
    req = {"ts": int(time.time()), "pair": body.pair, "side": body.side, "notional_usd": body.notional_usd}
    tmp = MANUAL_ORDER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(req, f)
    os.replace(tmp, MANUAL_ORDER_PATH)
    return {"queued": True}


# ==============================================================================
# 6. UI ROUTES
# ==============================================================================
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_load_dashboard_html())


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=_load_dashboard_html())