import os
import csv
import json
import time
import requests
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ==============================================================================
# 1. CONFIGURATION & PATHS
# ==============================================================================
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRADES_CSV = os.environ.get("TRADES_CSV", os.path.join(DATA_DIR, "trades.csv"))
PNL_JSON = os.environ.get("PNL_JSON", os.path.join(DATA_DIR, "pnl.json"))
EVENTS_JSONL = os.environ.get("EVENTS_JSONL", os.path.join(DATA_DIR, "events.jsonl"))
STATE_JSON = os.environ.get("STATE_JSON", os.path.join(DATA_DIR, "state.json"))
BOT_STATUS_JSON = os.environ.get("BOT_STATUS_JSON", os.path.join(DATA_DIR, "bot_status.json"))
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(DATA_DIR, "cache"))

RUN_DIR = os.environ.get("RUN_DIR", "/run/trading")
PAUSE_FILE = os.environ.get("PAUSE_FILE", os.path.join(RUN_DIR, "PAUSE"))
KILL_FILE = os.environ.get("KILL_FILE", os.path.join(RUN_DIR, "KILL_SWITCH"))
MANUAL_ORDER_PATH = os.environ.get("MANUAL_ORDER_PATH", os.path.join(RUN_DIR, "MANUAL_ORDER.json"))

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_KRAKEN = os.path.join(CONFIG_DIR, "kraken.yaml")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

START_TS = int(time.time())

# ==============================================================================
# 2. FASTAPI APP SETUP
# ==============================================================================
app = FastAPI(title="Trading Bot API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# 3. HELPER FUNCTIONS & MODELS
# ==============================================================================
class ReasonBody(BaseModel):
    reason: str = "manual"

class ManualExecuteBody(BaseModel):
    pair: str
    side: str
    notional_usd: float = 20.0

def _auth_ok(authorization: Optional[str]) -> bool:
    if not ADMIN_TOKEN: return True
    if not authorization or not authorization.startswith("Bearer "): return False
    return authorization.split(" ", 1)[1].strip() == ADMIN_TOKEN

def _require_auth(authorization: Optional[str]):
    if not _auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

def _read_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path, "r") as f: return json.load(f)
    except: return {}

def _tail_file(path, limit=50):
    if not os.path.exists(path): return []
    lines = []
    try:
        with open(path, "r") as f:
            if path.endswith(".csv"):
                reader = csv.DictReader(f)
                lines = list(reader)
            else:
                lines = [json.loads(line) for line in f if line.strip()]
    except: return []
    return lines[-limit:]

def _load_yaml(path):
    import yaml
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return yaml.safe_load(f) or {}

def _read_bot_status() -> Dict[str, Any]:
    bs = _read_json(BOT_STATUS_JSON)
    mode_cfg = (bs.get("mode_config") or "unknown").strip().lower()
    live_allowed = bs.get("live_allowed", True) 
    latch_required = bs.get("latch_required", False)
    latch_present = bs.get("latch_present", False)
    latch_file = bs.get("latch_file", "")
    killed = bool(bs.get("killed", False))
    paused = bool(bs.get("paused", False))
    
    mode_effective = mode_cfg
    blocked = []
    
    if mode_cfg == "live":
        if live_allowed is False:
            mode_effective = "dry_run"
            if killed: blocked.append("KILL_SWITCH")
            if latch_required and (not latch_present): blocked.append("LATCH_MISSING")

    return {
        "ts": bs.get("ts"),
        "mode_requested": mode_cfg,
        "mode_effective": mode_effective,
        "allow_live": live_allowed,
        "require_live_latch": latch_required,
        "live_latch_present": latch_present,
        "live_latch_file": latch_file,
        "blocked_reasons": blocked,
        "last_error": bs.get("last_error", ""),
        "killed": killed,
        "paused": paused
    }

# ==============================================================================
# 4. API ENDPOINTS
# ==============================================================================

@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    bs = _read_bot_status()
    pnl = _read_json(PNL_JSON)
    return {
        "ok": True,
        "uptime_s": int(time.time()) - START_TS,
        "bot_status": bs,
        "portfolio": pnl.get("portfolio", {}),
        "auth_required": bool(ADMIN_TOKEN),
        "auth_ok": _auth_ok(authorization)
    }

@app.get("/equity")
def equity(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    p = _read_json(PNL_JSON)
    # Ensure list format for frontend
    return {"items": p.get("equity_curve_realized", [])}

@app.get("/trades")
def trades(limit: int = 50): 
    return {"items": _tail_file(TRADES_CSV, limit)}

@app.get("/events")
def events(limit: int = 50): 
    return {"items": _tail_file(EVENTS_JSONL, limit)}

@app.get("/config/summary")
def config_summary():
    k = _load_yaml(CONFIG_KRAKEN)
    return {"pairs": k.get("kraken", {}).get("pairs", [])}

@app.get("/candles")
def get_candles(pair: str, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    safe_pair = "".join(c for c in pair if c.isalnum())
    cache_file = os.path.join(CACHE_DIR, f"{safe_pair}_ohlc.json")
    
    # Check cache (5 mins)
    if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file) < 300):
        return _read_json(cache_file)

    try:
        kcfg = _load_yaml(CONFIG_KRAKEN)
        base_url = kcfg.get("kraken", {}).get("base_url", "https://api.kraken.com")
        url = f"{base_url}/0/public/OHLC?pair={pair}&interval=60"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("error"): raise Exception(str(data["error"]))
        
        candles = []
        for k, v in data.get("result", {}).items():
            if k != "last" and isinstance(v, list):
                for c in v:
                    # Kraken: [time, open, high, low, close, vwap, vol, count]
                    candles.append({
                        "time": int(c[0]), 
                        "open": float(c[1]), 
                        "high": float(c[2]), 
                        "low": float(c[3]), 
                        "close": float(c[4])
                    })
                break
        
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f: json.dump({"pair": pair, "data": candles}, f)
        return {"pair": pair, "data": candles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- CONTROL ENDPOINTS ---

@app.post("/control/pause")
def pause(body: ReasonBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    with open(PAUSE_FILE, "w") as f: f.write(body.reason)
    return {"status": "paused"}

@app.post("/control/resume")
def resume(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if os.path.exists(PAUSE_FILE): os.remove(PAUSE_FILE)
    return {"status": "resumed"}

@app.post("/control/kill")
def kill(body: ReasonBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    with open(KILL_FILE, "w") as f: f.write(body.reason)
    return {"status": "killed"}

@app.post("/control/unkill")
def unkill(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if os.path.exists(KILL_FILE): os.remove(KILL_FILE)
    return {"status": "unkilled"}

@app.post("/control/reset_state")
def reset_state(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if os.path.exists(STATE_JSON): os.remove(STATE_JSON)
    return {"status": "cleared"}

@app.post("/manual/execute")
def manual_execute(body: ManualExecuteBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    req = {
        "ts": int(time.time()),
        "id": f"manual_{int(time.time())}",
        "pair": body.pair, 
        "side": body.side, 
        "notional_usd": body.notional_usd
    }
    os.makedirs(RUN_DIR, exist_ok=True)
    tmp = MANUAL_ORDER_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(req, f)
    os.replace(tmp, MANUAL_ORDER_PATH)
    return {"queued": True}

# ==============================================================================
# 5. UI ENDPOINT & HTML
# ==============================================================================

@app.get("/ui", response_class=HTMLResponse)
def ui():
    # This function uses the _UI_HTML string defined below.
    # Python resolves this at runtime, so it's safe to define it after.
    return HTMLResponse(content=_UI_HTML)

# This is the FULL HTML string. No truncation.
_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Trading Bot Dashboard</title>
  <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --text-muted: #94a3b8; --border: #334155; --primary: #3b82f6; --danger: #ef4444; --success: #10b981; --warn: #f59e0b; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 20px; margin: 0; }
    
    .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; }
    h1 { margin: 0 0 8px 0; font-size: 24px; }
    .subhead { font-size: 13px; color: var(--text-muted); }
    
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); display: flex; flex-direction: column; gap: 12px; }
    h2 { margin: 0 0 4px 0; font-size: 14px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; }
    
    .grid { display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); }
    
    input, select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; outline: none; font-size: 13px; }
    input:focus, select:focus { border-color: var(--primary); }
    
    button { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; cursor: pointer; font-weight: 500; transition: all 0.2s; }
    button:hover { background: #334155; }
    button.primary { background: var(--primary); border-color: var(--primary); color: white; }
    button.primary:hover { filter: brightness(110%); }
    button.danger { border-color: var(--danger); color: var(--danger); }
    button.danger:hover { background: var(--danger); color: white; }
    
    .time-btn { background: transparent; color: var(--text-muted); padding: 4px 8px; font-size: 11px; }
    .time-btn:hover { color: var(--text); border-color: var(--text); }
    .time-btn.active { background: var(--bg); color: var(--primary); border-color: var(--primary); }
    
    .auth-box { display: flex; align-items: center; gap: 8px; background: var(--card); padding: 8px; border-radius: 8px; border: 1px solid var(--border); }
    
    .pill { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; border: 1px solid transparent; background: #334155; color: var(--text-muted); }
    .pill.ok { background: rgba(16, 185, 129, 0.15); color: var(--success); border-color: rgba(16, 185, 129, 0.3); }
    .pill.bad { background: rgba(239, 68, 68, 0.15); color: var(--danger); border-color: rgba(239, 68, 68, 0.3); }
    .pill.warn { background: rgba(245, 158, 11, 0.15); color: var(--warn); border-color: rgba(245, 158, 11, 0.3); }
    
    .kv-table { display: grid; grid-template-columns: 140px 1fr; gap: 6px 16px; font-size: 13px; }
    .k { color: var(--text-muted); }
    .v { font-family: monospace; font-weight: 600; }
    
    .alert { background: rgba(239,68,68,0.15); border: 1px solid var(--danger); color: #fca5a5; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-weight: 600; display: none; }
    
    #debugLog { display:none; font-family:monospace; font-size:11px; color:#64748b; margin-top:20px; border-top:1px solid var(--border); padding-top:10px; }
    
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; color: var(--text-muted); font-weight: 500; padding: 8px; border-bottom: 1px solid var(--border); }
    td { padding: 8px; border-bottom: 1px solid var(--border); }
    .mono { font-family: monospace; }
    
    /* HIDE TRADINGVIEW BRANDING */
    a[href*="tradingview.com"] { display: none !important; opacity: 0 !important; visibility: hidden !important; pointer-events: none !important; }
    
    canvas { width: 100%; height: 250px; background: var(--bg); border-radius: 8px; border: 1px solid var(--border); }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Bot Dashboard 3.4</h1>
    <div class="subhead">
      Status: <span id="sysStatus">Connecting...</span> | Time: <span id="sysTime" class="mono">--:--:--</span>
    </div>
  </div>
  
  <div class="auth-box">
    <span id="authPill" class="pill">Checking Auth</span>
    <input id="token" type="password" placeholder="Paste ADMIN_TOKEN" style="width: 140px;">
    <button onclick="saveToken()" class="primary">Save</button>
    <button onclick="clearToken()">Clear</button>
    <button onclick="toggleToken()">Show</button>
  </div>
</div>

<div id="alertBox" class="alert"></div>

<div class="grid">
  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>System Health</h2>
      <div id="healthBadges" style="display:flex; gap:6px;"></div>
    </div>
    
    <div style="display:flex; gap:8px; margin-bottom: 10px;">
      <button onclick="api('/control/pause', 'POST', {reason:'UI'})">Pause</button>
      <button onclick="api('/control/resume', 'POST')">Resume</button>
      <button class="danger" onclick="api('/control/kill', 'POST', {reason:'UI'})">KILL</button>
      <button onclick="api('/control/unkill', 'POST')">Unkill</button>
      <button onclick="resetState()">Reset State</button>
    </div>

    <div class="kv-table">
      <div class="k">Uptime</div><div class="v" id="hUptime">-</div>
      <div class="k">Mode (Req/Eff)</div><div class="v"><span id="hModeReq">-</span> / <span id="hModeEff">-</span></div>
      <div class="k">Live Allowed</div><div class="v" id="hAllow">-</div>
      <div class="k">Live Latch</div><div class="v" id="hLatch">-</div>
      <div class="k">Blocked By</div><div class="v" id="hBlock" style="color:var(--warn)">-</div>
      <div class="k">PnL Updated</div><div class="v" id="hPnlTs">-</div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>Performance</h2>
      <div id="timeBtns" style="display:flex; gap:4px;">
        <button class="time-btn" onclick="setTimeRange('1d')">1D</button>
        <button class="time-btn" onclick="setTimeRange('1w')">1W</button>
        <button class="time-btn" onclick="setTimeRange('1m')">1M</button>
        <button class="time-btn active" onclick="setTimeRange('all')">ALL</button>
      </div>
    </div>
    <div style="font-size: 24px; font-weight: bold; font-family:monospace;" id="pNet">--</div>
    <div id="chartContainer" style="width:100%; height:200px;"></div>
  </div>

  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>Price Chart</h2>
      <select id="chartPair" onchange="loadCandles()"></select>
    </div>
    <div id="candleContainer" style="width:100%; height:250px;"></div>
  </div>

  <div class="card">
    <h2>Manual Trade</h2>
    <div style="display:flex; gap:8px; align-items:center;">
      <select id="mxPair"></select>
      <select id="mxSide"><option value="buy">Buy</option><option value="sell">Sell</option></select>
      <div style="position:relative;">
        <span style="position:absolute; left:8px; top:8px; font-size:12px; color:var(--text-muted)">$</span>
        <input id="mxNotional" value="20" style="width:60px; padding-left:20px;">
      </div>
      <button class="primary" onclick="manualEx()">Execute</button>
    </div>
    <div style="font-size:11px; color:var(--text-muted);">
      Queues a one-shot order.
    </div>
  </div>
</div>

<div class="grid" style="margin-top:20px;">
    <div class="card">
        <h2>Recent Trades</h2>
        <table id="tradesTbl"><tbody></tbody></table>
    </div>
    <div class="card">
        <h2>System Events</h2>
        <table id="eventsTbl"><tbody></tbody></table>
    </div>
</div>

<div id="debugLog" style="display:none; margin-top:20px; font-family:monospace; font-size:11px; color:#64748b;"></div>
<button onclick="document.getElementById('debugLog').style.display='block'" style="margin-top:20px; font-size:11px;">Show Debug Log</button>

<script>
// --- CHART SETUP ---
const chartOpts = {
  layout: { background: { color: '#1e293b' }, textColor: '#cbd5e1' },
  grid: { vertLines: { color: '#334155' }, horzLines: { color: '#334155' } },
  timeScale: { borderColor: '#475569', timeVisible: true },
  rightPriceScale: { borderColor: '#475569' },
  localization: { priceFormatter: p => '$' + p.toFixed(2) }
};
let pnlSeries, pnlChart, candleSeries, candleChart;

try {
  pnlChart = LightweightCharts.createChart(document.getElementById('chartContainer'), chartOpts);
  pnlSeries = pnlChart.addAreaSeries({ lineColor: '#3b82f6', topColor: 'rgba(59, 130, 246, 0.4)', bottomColor: 'rgba(59, 130, 246, 0)' });
  
  candleChart = LightweightCharts.createChart(document.getElementById('candleContainer'), chartOpts);
  candleSeries = candleChart.addCandlestickSeries();
} catch(e) { console.error("Chart init failed", e); }

const TOKEN_KEY = "trading_admin_token";
let timeRange = 'all';

function log(msg) {
    const d = document.getElementById('debugLog');
    d.innerText = `[${new Date().toLocaleTimeString()}] ${msg}\n` + d.innerText.substring(0, 1000);
}

function getToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
function saveToken() { localStorage.setItem(TOKEN_KEY, document.getElementById("token").value.trim()); alert("Saved"); refresh(); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); document.getElementById("token").value=""; alert("Cleared"); refresh(); }
function toggleToken() { const el=document.getElementById("token"); el.type = el.type==="password"?"text":"password"; }

function setTimeRange(r) {
    timeRange = r;
    document.querySelectorAll('.time-btn').forEach(b => b.className = 'time-btn' + (b.innerText.toLowerCase()===r ? ' active' : ''));
    refresh();
}

document.getElementById("token").value = getToken();

async function api(path, method='GET', body=null) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if(token) headers["Authorization"] = "Bearer " + token;
  const opts = { method, headers };
  if(body) opts.body = JSON.stringify(body);
  
  try {
      const res = await fetch(path, opts);
      const data = await res.json();
      if(!res.ok) throw new Error(data.detail || "Error " + res.status);
      return data;
  } catch(e) {
      log("API Error: " + path + " -> " + e.message);
      throw e;
  }
}

function renderBadges(h) {
  const bs = h.bot_status || {};
  const ok = (h.ok && !bs.killed);
  const badges = [
    `<span class="pill ${ok ? "ok":"bad"}">${ok ? "OK" : "ISSUE"}</span>`,
    `<span class="pill ${bs.paused ? "warn":"ok"}">${bs.paused ? "PAUSED" : "RUNNING"}</span>`,
    `<span class="pill ${bs.killed ? "bad":"ok"}">KILL=${bs.killed}</span>`
  ];
  document.getElementById("healthBadges").innerHTML = badges.join("");
}

async function refresh() {
  try {
    const h = await api('/health');
    const bs = h.bot_status || {};
    
    document.getElementById("sysStatus").textContent = "Online";
    document.getElementById("sysStatus").style.color = "var(--success)";
    document.getElementById("sysTime").textContent = new Date().toLocaleTimeString();
    
    const ap = document.getElementById("authPill");
    ap.textContent = h.auth_required ? (h.auth_ok ? "Auth: OK" : "Auth: Missing") : "No Auth";
    ap.className = "pill " + (h.auth_ok ? "ok" : "warn");

    const box = document.getElementById('alertBox');
    if(bs.last_error) { box.style.display='block'; box.textContent = "CRITICAL: " + bs.last_error; }
    else { box.style.display='none'; }

    renderBadges(h);
    document.getElementById('hUptime').textContent = h.uptime_s + "s";
    document.getElementById('hModeReq').textContent = bs.mode_requested || "-";
    document.getElementById('hModeEff').textContent = bs.mode_effective || "-";
    document.getElementById('hAllow').textContent = String(bs.allow_live);
    document.getElementById('hLatch').textContent = bs.require_live_latch ? (bs.live_latch_present ? "Present" : "Missing") : "Not Req";
    document.getElementById('hBlock').textContent = (bs.blocked_reasons||[]).join(", ") || "-";
    document.getElementById('hPnlTs').textContent = new Date().toLocaleTimeString();

    document.getElementById('pNet').textContent = "$" + (h.portfolio.net_pnl_usd||0).toFixed(2);

    // --- PNL CHART FIX: Force flat line if empty ---
    if(pnlSeries) {
        const eq = await api('/equity');
        let items = eq.items || [];
        
        let chartData = items.map(i => {
            if(Array.isArray(i)) return { time: i[0], value: i[1] };
            return i;
        });

        // FORCE DATA IF EMPTY OR SINGLE POINT
        if (chartData.length < 2) {
            const now = Math.floor(Date.now()/1000);
            const val = chartData.length > 0 ? chartData[0].value : 0;
            chartData = [
                { time: now - 86400, value: val },
                { time: now, value: val }
            ];
        }

        const now = Math.floor(Date.now()/1000);
        let start = 0;
        if(timeRange === '1d') start = now - 86400;
        if(timeRange === '1w') start = now - 604800;
        if(timeRange === '1m') start = now - 2592000;
        
        const filtered = chartData.filter(i => i.time >= start);
        
        // Use filtered data if enough points, else fallback to full synthetic
        if (filtered.length >= 2) {
             pnlSeries.setData(filtered);
        } else {
             pnlSeries.setData(chartData);
        }
        pnlChart.timeScale().fitContent();
    }

    const tr = await api('/trades?limit=10');
    document.getElementById('tradesTbl').querySelector('tbody').innerHTML = tr.items.reverse().map(t => 
      `<tr><td>${new Date(t.ts*1000).toLocaleTimeString()}</td><td>${t.pair}</td><td>${t.side}</td><td>${t.price}</td><td>$${t.notional_usd}</td><td>${t.mode||'-'}</td></tr>`
    ).join('');

    const ev = await api('/events?limit=10');
    document.getElementById('eventsTbl').querySelector('tbody').innerHTML = ev.items.reverse().map(e => 
      `<tr><td>${new Date(e.ts*1000).toLocaleTimeString()}</td><td>${e.event}</td><td>${e.pair||'-'}</td><td>${e.reason||e.action||''}</td></tr>`
    ).join('');

    const c = await api('/config/summary');
    const pairs = c.pairs || [];
    ['mxPair','chartPair'].forEach(id => {
       const el = document.getElementById(id);
       if(el.options.length === 0) {
         pairs.forEach(p => {
           const o = document.createElement('option'); o.value=p; o.text=p; el.appendChild(o);
         });
         if(id === 'chartPair') loadCandles(); 
       }
    });

  } catch(e) {
    document.getElementById("sysStatus").textContent = "Offline";
    document.getElementById("sysStatus").style.color = "var(--danger)";
    console.log("Poll error", e);
  }
}

async function loadCandles() {
  const pair = document.getElementById('chartPair').value;
  if(!pair) return;
  try {
    const data = await api(`/candles?pair=${pair}`);
    if(candleSeries && data.data && data.data.length > 0) {
        candleSeries.setData(data.data);
        candleChart.timeScale().fitContent();
    }
  } catch(e) { log("Candle error: " + e.message); }
}

async function resetState() {
  if(confirm("Delete state.json? Use this to fix mismatch errors.")) {
    await api('/control/reset_state', 'POST');
    alert("State cleared. Restart the bot.");
  }
}

async function manualEx() {
  const body = {
    pair: document.getElementById('mxPair').value,
    side: document.getElementById('mxSide').value,
    notional_usd: parseFloat(document.getElementById('mxNotional').value)
  };
  try { await api('/manual/execute', 'POST', body); alert("Order Queued"); }
  catch(e) { alert(e.message); }
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""