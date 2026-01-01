import os
import csv
import json
import time
import yaml
import requests
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ... [KEEP EXISTING IMPORTS AND CONFIG CONSTANTS] ...
# (Standard Config Paths - no changes needed there)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRADES_CSV = os.environ.get("TRADES_CSV", os.path.join(DATA_DIR, "trades.csv"))
PNL_JSON = os.environ.get("PNL_JSON", os.path.join(DATA_DIR, "pnl.json"))
EVENTS_JSONL = os.environ.get("EVENTS_JSONL", os.path.join(DATA_DIR, "events.jsonl"))
STATE_JSON = os.environ.get("STATE_JSON", os.path.join(DATA_DIR, "state.json"))
BOT_STATUS_JSON = os.environ.get("BOT_STATUS_JSON", os.path.join(DATA_DIR, "bot_status.json"))
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(DATA_DIR, "cache"))
AI_LOG_PATH = os.path.join(DATA_DIR, "ai_log.jsonl")
RUN_DIR = os.environ.get("RUN_DIR", "/run/trading")
PAUSE_FILE = os.environ.get("PAUSE_FILE", os.path.join(RUN_DIR, "PAUSE"))
KILL_FILE = os.environ.get("KILL_FILE", os.path.join(RUN_DIR, "KILL_SWITCH"))
MANUAL_ORDER_PATH = os.environ.get("MANUAL_ORDER_PATH", os.path.join(RUN_DIR, "MANUAL_ORDER.json"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_KRAKEN = os.path.join(CONFIG_DIR, "kraken.yaml")
CONFIG_IBKR = os.path.join(CONFIG_DIR, "ibkr.yaml") # Added IBKR Config
AI_CONFIG_PATH = os.path.join(CONFIG_DIR, "ai.yaml")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
START_TS = int(time.time())

# ... [KEEP MODELS AND APP SETUP] ...
class ReasonBody(BaseModel):
    reason: str = "manual"
class PromptUpdate(BaseModel):
    new_prompt: str
class ManualExecuteBody(BaseModel):
    pair: str
    side: str
    notional_usd: float = 20.0

app = FastAPI(title="Trading Bot API 9.7", version="9.7") # Bump version
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def _auth_ok(authorization: Optional[str]) -> bool:
    if not ADMIN_TOKEN: return True
    if not authorization or not authorization.startswith("Bearer "): return False
    return authorization.split(" ", 1)[1].strip() == ADMIN_TOKEN

def _require_auth(authorization: Optional[str]):
    if not _auth_ok(authorization): raise HTTPException(401, "Unauthorized")

def _read_json_safe(path):
    if not os.path.exists(path): return {}
    try:
        with open(path, "r") as f: return json.load(f)
    except: return {}

def _load_yaml(path):
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return yaml.safe_load(f) or {}

def _tail_file(path, limit=50):
    if not os.path.exists(path): return []
    try:
        with open(path, "r") as f:
            if path.endswith(".csv"): return list(csv.DictReader(f))[-limit:]
            else: return [json.loads(line) for line in f if line.strip()][-limit:]
    except: return []

def _get_detailed_status():
    bs = _read_json_safe(BOT_STATUS_JSON)
    mode_cfg = (bs.get("mode_config") or "unknown").strip().lower()
    live_allowed = bs.get("live_allowed", False)
    mode_effective = mode_cfg
    if mode_cfg == "live" and not live_allowed:
        mode_effective = "dry_run (safety)"
    return {
        "ts": bs.get("ts"),
        "mode_requested": mode_cfg,
        "mode_effective": mode_effective,
        "killed": bs.get("killed", False),
        "paused": bs.get("paused", False),
        "last_error": bs.get("last_error", ""),
        "live_allowed": live_allowed
    }

# --- UPDATED ENDPOINTS ---

@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    status = _get_detailed_status()
    pnl = _read_json_safe(PNL_JSON)
    return {
        "ok": True,
        "uptime_s": int(time.time()) - START_TS,
        "bot_status": status,
        "portfolio": pnl.get("portfolio", {}),
        "auth_required": bool(ADMIN_TOKEN),
        "auth_ok": _auth_ok(authorization)
    }

@app.get("/holdings")
def get_holdings(authorization: Optional[str] = Header(default=None)):
    state = _read_json_safe(STATE_JSON)
    holdings = []
    total_val_usd = 0.0
    total_unrealized_usd = 0.0
    
    active_pairs = [p for p, data in state.items() if data.get("has_position")]
    
    if not active_pairs:
        return {"items": [], "total_usd": 0.0, "total_unrealized_usd": 0.0}

    # Identify Crypto vs Stocks
    kcfg = _load_yaml(CONFIG_KRAKEN)
    kraken_pairs = kcfg.get("kraken", {}).get("pairs", [])
    # Also resolve friendly names if they are in cache map, but simple check is enough
    
    crypto_to_fetch = []
    
    # 1. Fetch Prices
    price_map = {}
    
    for p in active_pairs:
        # Heuristic: If it has "USD" in it and is in Kraken config, it's crypto
        if any(kp in p for kp in kraken_pairs) or "USD" in p:
             crypto_to_fetch.append(p)
        else:
             # Assume Stock: Use entry price as fallback for now
             # (Since Web API cannot talk to IBKR directly)
             price_map[p] = float(state[p].get("average_price", 0.0))

    if crypto_to_fetch:
        try:
            base_url = kcfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
            pair_str = ",".join(crypto_to_fetch)
            url = f"{base_url}/0/public/Ticker?pair={pair_str}"
            resp = requests.get(url, timeout=3).json()
            if not resp.get("error"):
                for k, v in resp.get("result", {}).items():
                    price_map[k] = float(v["c"][0])
        except: pass

    # 2. Build Response
    for pair in active_pairs:
        data = state[pair]
        vol = float(data.get("base_volume", 0.0))
        entry = float(data.get("average_price", 0.0))
        
        # If we failed to get a price (or it's a stock), use entry price to avoid 0 value
        current_price = price_map.get(pair, entry) 
        
        val_usd = vol * current_price
        unrealized = (current_price - entry) * vol
        pct = ((current_price - entry) / entry) * 100 if entry > 0 else 0.0

        total_val_usd += val_usd
        total_unrealized_usd += unrealized
        
        holdings.append({
            "pair": pair,
            "volume": vol,
            "entry_price": round(entry, 4),
            "current_price": round(current_price, 4),
            "value_usd": round(val_usd, 2),
            "unrealized_usd": round(unrealized, 2),
            "unrealized_pct": round(pct, 2)
        })
        
    return {
        "items": holdings, 
        "total_usd": round(total_val_usd, 2),
        "total_unrealized_usd": round(total_unrealized_usd, 2)
    }

# ... [KEEP REMAINING ENDPOINTS: equity, trades, ai/logs, config/ai, candles, controls] ...

@app.get("/equity")
def equity(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    p = _read_json_safe(PNL_JSON)
    raw = p.get("equity_curve_realized", [])
    data = [{"time": x[0], "value": round(x[1], 2)} for x in raw] if raw else []
    if not data:
        now = int(time.time())
        data = [{"time": now-86400, "value": 0}, {"time": now, "value": 0}]
    return {"items": data}

@app.get("/trades")
def trades():
    return {"items": _tail_file(TRADES_CSV, 20)}

@app.get("/ai/logs")
def get_ai_logs(limit: int = 10):
    return {"items": _tail_file(AI_LOG_PATH, limit)}

@app.get("/config/ai")
def get_ai_config():
    cfg = _load_yaml(AI_CONFIG_PATH)
    if not cfg: return {"prompts": {}}
    return cfg

@app.post("/config/ai")
def update_ai_config(body: PromptUpdate, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    try:
        cfg = _load_yaml(AI_CONFIG_PATH)
        if "prompts" not in cfg: cfg["prompts"] = {}
        cfg["prompts"]["strategy_decision"] = body.new_prompt
        with open(AI_CONFIG_PATH, "w") as f: yaml.dump(cfg, f)
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/config/summary")
def config_summary():
    k = _load_yaml(CONFIG_KRAKEN)
    i = _load_yaml(CONFIG_IBKR)
    
    # Combine Pairs and Stocks
    pairs = k.get("kraken", {}).get("pairs", [])
    stocks = i.get("universe", {}).get("stocks", [])
    
    return {"pairs": pairs + stocks}

@app.get("/candles")
def get_candles(pair: str, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if not pair: return {"pair": "", "data": []}
    
    # Simple check: If it looks like a stock (no USD), skip Kraken
    # Or load config to check strictly.
    if "USD" not in pair and "EUR" not in pair:
        return {"pair": pair, "data": []} # No candle support for IBKR in web UI yet

    safe_pair = "".join(c for c in pair if c.isalnum())
    cache_file = os.path.join(CACHE_DIR, f"{safe_pair}_ohlc.json")
    
    if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file) < 300):
        return _read_json_safe(cache_file)

    try:
        kcfg = _load_yaml(CONFIG_KRAKEN)
        base_url = kcfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
        url = f"{base_url}/0/public/OHLC?pair={pair}&interval=60"
        
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("error"): return {"pair": pair, "data": []}
            
        candles = []
        for k, v in data.get("result", {}).items():
            if k != "last" and isinstance(v, list):
                for c in v:
                    candles.append({
                        "time": int(c[0]), 
                        "open": round(float(c[1]), 4), 
                        "high": round(float(c[2]), 4), 
                        "low": round(float(c[3]), 4), 
                        "close": round(float(c[4]), 4)
                    })
                break
        
        candles.sort(key=lambda x: x["time"])
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f: json.dump({"pair": pair, "data": candles}, f)
        return {"pair": pair, "data": candles}
    except: return {"pair": pair, "data": []}

# ... [KEEP CONTROLS AND UI HTML] ...
@app.post("/manual/execute")
def manual_execute(body: ManualExecuteBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    req = {"ts": int(time.time()), "pair": body.pair, "side": body.side, "notional_usd": body.notional_usd}
    tmp = MANUAL_ORDER_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(req, f)
    os.replace(tmp, MANUAL_ORDER_PATH)
    return {"queued": True}

@app.post("/control/pause")
def pause(body: ReasonBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    with open(PAUSE_FILE, "w") as f: f.write(body.reason)
    return {"status": "paused"}

@app.post("/control/resume")
def resume(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    if os.path.exists(PAUSE_FILE): os.remove(PAUSE_FILE)
    return {"status": "resumed"}

@app.post("/control/kill")
def kill(body: ReasonBody, authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    with open(KILL_FILE, "w") as f: f.write(body.reason)
    return {"status": "killed"}

@app.post("/control/unkill")
def unkill(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    if os.path.exists(KILL_FILE): os.remove(KILL_FILE)
    return {"status": "unkilled"}

@app.post("/control/reset_state")
def reset_state(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    if os.path.exists(STATE_JSON): os.remove(STATE_JSON)
    return {"status": "state_cleared"}

@app.post("/control/factory_reset")
def factory_reset(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    for f in [STATE_JSON, PNL_JSON, TRADES_CSV, AI_LOG_PATH, BOT_STATUS_JSON]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass
    return {"status": "factory_reset_complete"}

@app.post("/control/restart")
def restart(authorization: Optional[str] = Header(None)):
    _require_auth(authorization)
    os._exit(1)

# --- REUSING EXISTING UI HTML (No changes needed, it handles dynamic data) ---
_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bot 9.7 (Hybrid)</title>
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --text: #f1f5f9; --border: #334155; --primary: #3b82f6; --danger: #ef4444; --success: #10b981; --warn: #f59e0b; }
  body { font-family: sans-serif; background: var(--bg); color: var(--text); padding: 20px; margin: 0; }
  .grid { display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--border); padding: 15px; border-radius: 8px; }
  h3 { margin-top:0; color: #94a3b8; font-size: 14px; text-transform: uppercase; }
  
  button { background: #334155; color: white; border: 1px solid var(--border); padding: 6px 12px; cursor: pointer; border-radius: 4px; margin-right:4px; }
  button:hover { background: #475569; }
  button.primary { background: var(--primary); border-color: var(--primary); }
  button.danger { border-color: var(--danger); color: var(--danger); background: rgba(239,68,68,0.1); }
  
  input, select, textarea { background: #020617; border: 1px solid var(--border); color: white; padding: 5px; border-radius: 4px; }
  textarea { width: 100%; height: 80px; resize: vertical; }

  table { width: 100%; font-size: 12px; border-collapse: collapse; margin-top: 10px; }
  th { text-align: left; color: #94a3b8; border-bottom: 1px solid var(--border); padding: 6px; }
  td { padding: 6px; border-bottom: 1px solid var(--border); }
  
  .pill { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; }
  .pill.ok { background: rgba(16,185,129,0.2); color: #10b981; }
  .pill.warn { background: rgba(245,158,11,0.2); color: #f59e0b; }
  .pill.bad { background: rgba(239,68,68,0.2); color: #ef4444; }
  
  .auth-box { display: flex; align-items: center; gap: 8px; float: right; }
  .kv-row { display: flex; justify-content: space-between; margin-bottom: 5px; font-size: 13px; border-bottom: 1px solid #334155; padding-bottom: 2px; }
  .kv-k { color: #94a3b8; }
  .kv-v { font-weight: bold; font-family: monospace; }
</style>
</head>
<body>

<div style="margin-bottom: 20px; overflow: hidden;">
  <div class="auth-box">
    <span id="authStatus" style="font-size:12px; color:#64748b;">Checking...</span>
    <input id="token" type="password" placeholder="ADMIN_TOKEN" style="width:100px;">
    <button onclick="saveToken()" class="primary">Save</button>
    <button onclick="clearToken()">Clear</button>
    <button onclick="toggleToken()">Show</button>
  </div>
  <h1 style="margin:0;">Trading Bot 9.7 (Hybrid)</h1>
  <div style="font-size:12px; color:#64748b;">System Status: <span id="sysStatus">--</span></div>
</div>

<div class="grid">
  <div class="card">
    <h3>System Controls</h3>
    <div style="margin-bottom:15px; display:flex; gap:5px; flex-wrap:wrap;">
        <button onclick="api('/control/pause', 'POST', {reason:'UI'})">Pause</button>
        <button onclick="api('/control/resume', 'POST')">Resume</button>
        <button class="danger" onclick="api('/control/kill', 'POST', {reason:'UI'})">KILL</button>
        <button onclick="api('/control/unkill', 'POST')">Unkill</button>
        <button class="danger" onclick="if(confirm('FACTORY RESET: Wipe ALL history?')) api('/control/factory_reset', 'POST')">Factory Reset</button>
    </div>
    
    <div class="kv-row"><span class="kv-k">Uptime</span><span class="kv-v" id="stUptime">-</span></div>
    <div class="kv-row"><span class="kv-k">Mode (Req/Eff)</span><span class="kv-v" id="stMode">-</span></div>
    <div class="kv-row"><span class="kv-k">Realized PnL</span><span class="kv-v" id="stPnl" style="color:#3b82f6;">-</span></div>
    <div class="kv-row"><span class="kv-k">Status</span><span class="kv-v" id="stBadges">-</span></div>
  </div>

  <div class="card">
    <h3 id="holdingsTitle">Current Holdings</h3>
    <div style="margin-bottom:10px; font-size:16px;">
       Total Value: <span id="holdingsTotal" style="color:#f1f5f9; font-weight:bold;">$0.00</span> 
       <span style="color:#64748b; margin:0 5px;">|</span>
       Unrealized: <span id="unrealizedTotal" style="font-weight:bold;">$0.00</span>
    </div>
    <table id="tblHoldings">
      <thead><tr><th>Pair</th><th>Vol</th><th>Entry</th><th>Price</th><th>Value</th><th>Unrealized</th></tr></thead>
      <tbody><tr><td colspan="6" style="text-align:center; color:#64748b;">Loading...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="grid" style="margin-top:20px;">
  <div class="card">
    <h3>Equity Curve (Realized)</h3>
    <div id="pnlChart" style="min-height:220px;"></div>
  </div>
  <div class="card">
    <div style="display:flex; justify-content:space-between; margin-bottom:10px;">
        <h3>Price Action</h3>
        <select id="chartPair" onchange="loadCandles()" style="padding:2px;"></select>
    </div>
    <div id="priceChart" style="min-height:220px;"></div>
  </div>
</div>

<div class="grid" style="margin-top:20px;">
  <div class="card">
    <h3>Recent Trades</h3>
    <table id="tblTrades">
      <thead><tr><th>Time</th><th>Pair</th><th>Side</th><th>Price</th><th>Vol</th><th>Cost</th><th>Mode</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="card">
    <h3>AI Brain</h3>
    <div id="aiLogs" style="max-height: 250px; overflow-y: auto;"></div>
  </div>
</div>

<div class="grid" style="margin-top:20px;">
  <div class="card">
    <h3>Strategy Config (AI Prompt)</h3>
    <textarea id="promptEditor"></textarea>
    <div style="margin-top:10px;">
      <button onclick="savePrompt()">Save Config</button>
      <button onclick="restartBot()" class="danger">Restart Bot</button>
    </div>
  </div>
    <div class="card">
    <h3>Manual Trade</h3>
    <div style="display:flex; gap:5px; align-items:center;">
      <select id="mxPair"></select>
      <select id="mxSide"><option value="buy">Buy</option><option value="sell">Sell</option></select>
      <input id="mxAmt" value="12" style="width:50px;">
      <button class="primary" onclick="manualEx()">Execute</button>
    </div>
  </div>
</div>

<script>
const TOKEN_KEY = "trading_admin_token";
document.getElementById('token').value = localStorage.getItem(TOKEN_KEY) || "";

const PAIR_MAP = {
    "XXBTZUSD": "Bitcoin", "XETHZUSD": "Ethereum", "SOLUSD": "Solana",
    "XRPUSD": "XRP", "XXRPZUSD": "XRP", "ADAUSD": "Cardano",
    "NVDA": "NVIDIA", "TSLA": "Tesla", "AAPL": "Apple", "MSFT": "Microsoft"
};

function fmtPair(p) {
    if(PAIR_MAP[p]) return `${PAIR_MAP[p]} <span style='color:#64748b; font-size:10px;'>(${p})</span>`;
    return p;
}

function saveToken() { localStorage.setItem(TOKEN_KEY, document.getElementById('token').value.trim()); alert("Saved"); refresh(); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); document.getElementById('token').value=""; alert("Cleared"); refresh(); }
function toggleToken() { const x=document.getElementById('token'); x.type = x.type==='password'?'text':'password'; }

async function api(path, method="GET", body=null) {
    const headers = {"Content-Type": "application/json"};
    const t = localStorage.getItem(TOKEN_KEY);
    if(t) headers["Authorization"] = "Bearer " + t;
    try {
        const res = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : null});
        if(res.status===401) { document.getElementById("authStatus").innerText="Auth Missing"; return null; }
        return res.json();
    } catch(e) { return null; }
}

const commonOpts = {
    chart: { height:220, toolbar:{show:false}, background:'transparent', zoom:{enabled:true, type:'x'}, pan:{enabled:true} },
    theme: { mode:'dark' },
    grid: { borderColor:'#334155' },
    dataLabels: { enabled: false }
};

const pnlChart = new ApexCharts(document.querySelector("#pnlChart"), {
    ...commonOpts,
    chart: { ...commonOpts.chart, type:'area' },
    stroke: { curve:'straight', width:2 },
    series: [{ name:"Equity", data:[] }],
    xaxis: { type:'datetime' },
    yaxis: { labels: { formatter: (val) => "$" + val.toFixed(4) } },
    colors: ['#3b82f6']
});
pnlChart.render();

const priceChart = new ApexCharts(document.querySelector("#priceChart"), {
    ...commonOpts,
    chart: { ...commonOpts.chart, type:'candlestick' },
    series: [],
    xaxis: { type:'datetime' },
    plotOptions: { candlestick: { colors: { upward:'#10b981', downward:'#ef4444' } } }
});
priceChart.render();

async function refresh() {
    const h = await api('/health');
    if(!h) { document.getElementById("sysStatus").innerText="Offline"; return; }
    
    document.getElementById("sysStatus").innerText="Online";
    document.getElementById("authStatus").innerText = h.auth_ok ? "Auth OK" : "Auth Required";
    
    const bs = h.bot_status || {};
    document.getElementById("stUptime").innerText = h.uptime_s + "s";
    
    const isDry = (bs.mode_effective || "").includes("dry");
    document.getElementById("stMode").innerText = (bs.mode_effective||"?").toUpperCase();
    document.getElementById("holdingsTitle").innerText = isDry ? "Current Holdings (Simulated)" : "Current Holdings (Live)";
    document.getElementById("holdingsTitle").style.color = isDry ? "#f59e0b" : "#f1f5f9";
    
    document.getElementById("stPnl").innerText = "$" + (h.portfolio.net_pnl_usd||0).toFixed(6);
    
    let bHtml = "";
    if(bs.killed) bHtml += '<span class="pill bad">KILLED</span> ';
    else bHtml += '<span class="pill ok">RUNNING</span> ';
    if(bs.paused) bHtml += '<span class="pill warn">PAUSED</span>';
    document.getElementById("stBadges").innerHTML = bHtml;

    const eq = await api('/equity');
    if(eq && eq.items) pnlChart.updateSeries([{data: eq.items.map(i => ({x: i.time*1000, y: i.value}))}]);

    const hold = await api('/holdings');
    if(hold) {
        document.getElementById("holdingsTotal").innerText = "$" + (hold.total_usd||0).toFixed(2);
        
        const upnl = hold.total_unrealized_usd || 0;
        const uEl = document.getElementById("unrealizedTotal");
        uEl.innerText = (upnl >= 0 ? "+" : "") + "$" + upnl.toFixed(4);
        uEl.style.color = upnl >= 0 ? "#10b981" : "#ef4444";

        const t = document.getElementById("tblHoldings").querySelector("tbody");
        if(!hold.items || hold.items.length===0) {
            t.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#64748b; padding:10px;">No open positions</td></tr>`;
        } else {
            t.innerHTML = hold.items.map(x => {
                const pColor = x.unrealized_usd >= 0 ? "#10b981" : "#ef4444";
                return `
                <tr>
                    <td>${fmtPair(x.pair)}</td>
                    <td>${x.volume.toFixed(6)}</td>
                    <td>$${x.entry_price.toFixed(4)}</td>
                    <td>$${x.current_price.toFixed(4)}</td>
                    <td>$${x.value_usd.toFixed(2)}</td>
                    <td style="color:${pColor}; font-weight:bold;">
                       ${x.unrealized_usd>=0?"+":""}$${x.unrealized_usd.toFixed(4)} 
                    </td>
                </tr>`;
            }).join("");
        }
    }

    const tr = await api('/trades?limit=8');
    const tbody = document.getElementById("tblTrades").querySelector("tbody");
    tbody.innerHTML = (tr.items||[]).reverse().map(x => {
        const p = parseFloat(x.price||0).toFixed(4);
        const v = parseFloat(x.volume||x.vol||0).toFixed(6); 
        const c = parseFloat(x.notional_usd||x.cost||0).toFixed(2);
        
        const color = x.side==='buy' ? '#10b981' : '#ef4444';
        return `<tr>
          <td>${new Date(x.ts*1000).toLocaleTimeString()}</td>
          <td>${fmtPair(x.pair)}</td>
          <td style="color:${color}; font-weight:bold;">${x.side.toUpperCase()}</td>
          <td>$${p}</td>
          <td>${v}</td>
          <td>$${c}</td>
          <td style="font-size:10px;">${x.mode}</td>
        </tr>`;
    }).join("");

    const ai = await api('/ai/logs');
    document.getElementById("aiLogs").innerHTML = (ai.items||[]).reverse().map(l => 
        `<div style="border-bottom:1px solid #334155; margin-bottom:8px; padding-bottom:4px;">
           <div style="display:flex; justify-content:space-between; color:#64748b; font-size:10px;">
             <span>${new Date(l.ts*1000).toLocaleTimeString()}</span>
             <span>${l.model}</span>
           </div>
           <div style="font-size:12px; color:#e2e8f0; margin-top:2px;">${(l.response.analysis || JSON.stringify(l.response))}</div>
         </div>`
    ).join("");

    const c = await api('/config/summary');
    const sel = document.getElementById("mxPair");
    const chartSel = document.getElementById("chartPair");
    if(sel.options.length === 0 && c.pairs) {
        c.pairs.forEach(p => {
             const friendly = PAIR_MAP[p] ? `${PAIR_MAP[p]} (${p})` : p;
             const o = document.createElement("option"); o.value=p; o.text=friendly; 
             sel.appendChild(o);
             chartSel.appendChild(o.cloneNode(true));
        });
        loadCandles(); 
    }
}

async function loadCandles() {
    const p = document.getElementById("chartPair").value;
    if(!p) return;
    const d = await api(`/candles?pair=${p}`);
    if(d && d.data) {
        const seriesData = d.data.map(c => ({
            x: c.time * 1000,
            y: [c.open, c.high, c.low, c.close]
        }));
        priceChart.updateSeries([{ data: seriesData }]);
    }
}

async function loadConfig() {
    const cfg = await api('/config/ai');
    if(cfg && cfg.prompts) document.getElementById("promptEditor").value = cfg.prompts.strategy_decision || "";
}

async function savePrompt() {
    await api('/config/ai', 'POST', {new_prompt: document.getElementById("promptEditor").value});
    alert("Saved");
}

async function restartBot() {
    if(confirm("Restart Bot Container?")) await api('/control/restart', 'POST');
}

async function manualEx() {
    const body = {
        pair: document.getElementById("mxPair").value,
        side: document.getElementById("mxSide").value,
        notional_usd: parseFloat(document.getElementById("mxAmt").value)
    };
    await api('/manual/execute', 'POST', body);
    alert("Queued");
}

loadConfig();
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""
# --- ROUTER ---
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_UI_HTML)