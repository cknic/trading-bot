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
# 1. DATA MODELS (MUST BE DEFINED FIRST)
# ==============================================================================
class ReasonBody(BaseModel):
    reason: str = "manual"

class ManualExecuteBody(BaseModel):
    pair: str
    side: str
    notional_usd: float = 20.0

# ==============================================================================
# 2. CONFIGURATION
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
# 3. HELPER FUNCTIONS
# ==============================================================================
def _auth_ok(authorization: Optional[str]) -> bool:
    if not ADMIN_TOKEN: return True
    if not authorization or not authorization.startswith("Bearer "): return False
    return authorization.split(" ", 1)[1].strip() == ADMIN_TOKEN

def _require_auth(authorization: Optional[str]):
    if not _auth_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

def _read_json_safe(path):
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
    bs = _read_json_safe(BOT_STATUS_JSON)
    ts = bs.get("ts")
    paused = bool(bs.get("paused", False))
    killed = bool(bs.get("killed", False))
    last_error = bs.get("last_error", "")

    mode_cfg = (bs.get("mode_config") or "unknown").strip().lower()
    live_allowed = bs.get("live_allowed", False)
    latch_required = bs.get("latch_required", True)
    latch_present = bs.get("latch_present", False)
    latch_file = bs.get("latch_file", "")
    
    mode_effective = mode_cfg
    blocked = []
    
    if mode_cfg == "live":
        if live_allowed is False:
            mode_effective = "dry_run"
            if killed: blocked.append("KILL_SWITCH")
            if latch_required and (not latch_present): blocked.append("LATCH_MISSING")

    return {
        "ts": ts,
        "mode_requested": mode_cfg,
        "mode_effective": mode_effective,
        "allow_live": live_allowed,
        "require_live_latch": latch_required,
        "live_latch_present": latch_present,
        "live_latch_file": latch_file,
        "blocked_reasons": blocked,
        "last_error": last_error,
        "killed": killed,
        "paused": paused
    }

# ==============================================================================
# 4. APP & UI HTML
# ==============================================================================
app = FastAPI(title="Trading Bot API", version="8.1-Fixed")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Trading Bot Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #f1f5f9; --border: #334155; --primary: #3b82f6; --danger: #ef4444; --success: #10b981; --warn: #f59e0b; }
    body { font-family: sans-serif; background: var(--bg); color: var(--text); padding: 20px; margin: 0; }
    .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; }
    h1 { margin: 0 0 5px 0; font-size: 24px; }
    .subhead { font-size: 13px; color: #94a3b8; }
    .grid { display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
    h2 { margin: 0 0 10px 0; font-size: 14px; text-transform: uppercase; color: #94a3b8; }
    button { background: #334155; border: none; color: white; padding: 6px 12px; border-radius: 4px; cursor: pointer; margin-right: 5px; }
    button:hover { background: #475569; }
    button.danger { background: rgba(239,68,68,0.2); color: var(--danger); border: 1px solid var(--danger); }
    button.primary { background: var(--primary); }
    input, select { background: #0f172a; border: 1px solid var(--border); color: white; padding: 6px; border-radius: 4px; }
    .pill { padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: bold; background: #334155; margin-left: 5px; }
    .pill.ok { color: var(--success); background: rgba(16,185,129,0.2); }
    .pill.bad { color: var(--danger); background: rgba(239,68,68,0.2); }
    .pill.warn { color: var(--warn); background: rgba(245,158,11,0.2); }
    .kv-table { display: grid; grid-template-columns: 140px 1fr; gap: 6px 16px; font-size: 13px; margin-top: 10px; }
    .k { color: #94a3b8; } .v { font-family: monospace; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    td, th { padding: 8px; border-bottom: 1px solid var(--border); text-align: left; }
    #pnlChart, #priceChart { min-height: 250px; }
    .auth-box { display: flex; align-items: center; gap: 8px; background: var(--card); padding: 8px; border-radius: 8px; border: 1px solid var(--border); }
  </style>
</head>
<body>

<div class="header">
  <div>
    <h1>Bot Dashboard 8.1</h1>
    <div class="subhead">Status: <span id="sysStatus">Connecting...</span> | <span id="sysTime">--:--:--</span></div>
  </div>
  <div class="auth-box">
    <span id="authPill" class="pill">Checking...</span>
    <input id="token" type="password" placeholder="ADMIN_TOKEN">
    <button onclick="saveToken()" class="primary">Save</button>
    <button onclick="clearToken()">Clear</button>
    <button onclick="toggleToken()">Show</button>
  </div>
</div>

<div id="alertBox" style="display:none; background:rgba(239,68,68,0.2); border:1px solid var(--danger); padding:10px; margin-bottom:20px; border-radius:6px;"></div>

<div class="grid">
  <div class="card">
    <div style="display:flex; justify-content:space-between;">
      <h2>System Health</h2>
      <div id="badges"></div>
    </div>
    <div style="margin-top:10px;">
        <button onclick="api('/control/pause', 'POST')">Pause</button>
        <button onclick="api('/control/resume', 'POST')">Resume</button>
        <button class="danger" onclick="api('/control/kill', 'POST')">KILL</button>
        <button onclick="api('/control/unkill', 'POST')">Unkill</button>
        <button onclick="api('/control/reset_state', 'POST')">Reset State</button>
    </div>
    <div class="kv-table">
      <div class="k">Uptime</div><div class="v" id="hUptime">-</div>
      <div class="k">Mode</div><div class="v" id="hMode">-</div>
      <div class="k">Latch</div><div class="v" id="hLatch">-</div>
      <div class="k">PnL</div><div class="v" id="hPnl" style="font-size:16px;">-</div>
    </div>
  </div>

  <div class="card">
    <h2>Performance</h2>
    <div id="pnlChart"></div>
  </div>

  <div class="card">
    <div style="display:flex; justify-content:space-between;">
      <h2>Price Action</h2>
      <select id="chartPair" onchange="loadCandles()"></select>
    </div>
    <div id="priceChart"></div>
  </div>

  <div class="card">
    <h2>Manual Trade</h2>
    <select id="mxPair"></select>
    <select id="mxSide"><option value="buy">Buy</option><option value="sell">Sell</option></select>
    <input id="mxAmt" value="20" style="width:50px;">
    <button class="primary" onclick="manualEx()">Execute</button>
  </div>
</div>

<div class="grid" style="margin-top:20px;">
  <div class="card"><h2>Recent Trades</h2><table id="tblTrades"><tbody></tbody></table></div>
  <div class="card"><h2>System Events</h2><table id="tblEvents"><tbody></tbody></table></div>
</div>

<script>
const commonOpts = {
    chart: { type: 'area', height: 250, background: 'transparent', toolbar: { show: false }, animations: { enabled: false } },
    theme: { mode: 'dark' },
    stroke: { curve: 'straight', width: 2 },
    dataLabels: { enabled: false },
    grid: { borderColor: '#334155' },
    xaxis: { type: 'datetime', tooltip: { enabled: false } },
    yaxis: { labels: { formatter: val => val.toFixed(2) } }
};

var pnlChart = new ApexCharts(document.querySelector("#pnlChart"), {
    ...commonOpts,
    series: [{ name: "Equity", data: [] }],
    colors: ['#3b82f6']
});
pnlChart.render();

var priceChart = new ApexCharts(document.querySelector("#priceChart"), {
    ...commonOpts,
    chart: { type: 'candlestick', height: 250, background: 'transparent', toolbar: { show: false } },
    series: [],
    plotOptions: { candlestick: { colors: { upward: '#10b981', downward: '#ef4444' } } }
});
priceChart.render();

const TOKEN_KEY = "trading_admin_token";
document.getElementById('token').value = localStorage.getItem(TOKEN_KEY) || "";

function saveToken() { localStorage.setItem(TOKEN_KEY, document.getElementById('token').value.trim()); alert("Saved"); refresh(); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); document.getElementById('token').value = ""; alert("Cleared"); refresh(); }
function toggleToken() { const el = document.getElementById('token'); el.type = el.type === 'password' ? 'text' : 'password'; }

async function api(path, method='GET', body=null) {
    const headers = { "Content-Type": "application/json" };
    const t = localStorage.getItem(TOKEN_KEY);
    if(t) headers["Authorization"] = "Bearer " + t;
    try {
        const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
        if(res.status === 401) { document.getElementById("sysStatus").innerText = "Auth Failed"; return null; }
        return await res.json();
    } catch(e) { console.error(e); return null; }
}

async function refresh() {
    const h = await api('/health');
    if(!h) { document.getElementById("sysStatus").innerText = "Offline"; return; }
    
    document.getElementById("sysStatus").innerText = "Online";
    document.getElementById("sysStatus").style.color = "#10b981";
    document.getElementById("sysTime").innerText = new Date().toLocaleTimeString();
    
    const bs = h.bot_status || {};
    
    // Auth Pill
    const ap = document.getElementById("authPill");
    ap.innerText = h.auth_required ? (h.auth_ok ? "Auth: OK" : "Auth: Missing") : "No Auth";
    ap.className = "pill " + (h.auth_ok ? "ok" : "warn");

    document.getElementById("hUptime").innerText = h.uptime_s + "s";
    document.getElementById("hMode").innerText = (bs.mode_requested||"?") + " / " + (bs.mode_effective||"?");
    document.getElementById("hLatch").innerText = bs.live_latch_present ? "Present" : "Missing";
    document.getElementById("hPnl").innerText = "$" + (h.portfolio.net_pnl_usd||0).toFixed(2);
    
    const badges = [];
    if(bs.killed) badges.push('<span class="pill bad">KILLED</span>');
    else badges.push('<span class="pill ok">RUNNING</span>');
    if(bs.paused) badges.push('<span class="pill warn">PAUSED</span>');
    document.getElementById("badges").innerHTML = badges.join(" ");
    
    if(bs.last_error) { 
        document.getElementById("alertBox").style.display = "block";
        document.getElementById("alertBox").innerText = "CRITICAL: " + bs.last_error;
    } else { document.getElementById("alertBox").style.display = "none"; }

    // Chart: PnL
    const eq = await api('/equity');
    if(eq && eq.items) {
        const data = eq.items.map(i => ({ x: i.time * 1000, y: i.value }));
        pnlChart.updateSeries([{ data: data }]);
    }

    // Trades
    const tr = await api('/trades?limit=5');
    document.getElementById("tblTrades").querySelector("tbody").innerHTML = (tr.items||[]).reverse().map(x => 
        `<tr><td>${new Date(x.ts*1000).toLocaleTimeString()}</td><td>${x.pair}</td><td>${x.side}</td><td>$${x.notional_usd}</td><td>${x.mode}</td></tr>`
    ).join("");

    // Events
    const ev = await api('/events?limit=5');
    document.getElementById("tblEvents").querySelector("tbody").innerHTML = (ev.items||[]).reverse().map(x => 
        `<tr><td>${new Date(x.ts*1000).toLocaleTimeString()}</td><td>${x.event}</td><td>${x.pair||""}</td><td>${x.reason||""}</td></tr>`
    ).join("");

    // Pairs
    const c = await api('/config/summary');
    const sel = document.getElementById("chartPair");
    if(sel.options.length === 0 && c.pairs) {
        c.pairs.forEach(p => {
            const opt = document.createElement("option"); opt.value=p; opt.text=p;
            sel.appendChild(opt); document.getElementById("mxPair").appendChild(opt.cloneNode(true));
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

async function manualEx() {
    const body = {
        pair: document.getElementById("mxPair").value,
        side: document.getElementById("mxSide").value,
        notional_usd: parseFloat(document.getElementById("mxAmt").value)
    };
    await api('/manual/execute', 'POST', body);
    alert("Queued");
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""

# ==============================================================================
# 5. ENDPOINTS (MUST BE DEFINED AFTER DATA MODELS)
# ==============================================================================

@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_UI_HTML)

@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    bs = _read_bot_status()
    pnl = _read_json_safe(PNL_JSON)
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
    p = _read_json_safe(PNL_JSON)
    raw_data = p.get("equity_curve_realized", [])
    
    clean_data = []
    for item in raw_data:
        try:
            if isinstance(item, list) and len(item) >= 2:
                clean_data.append({"time": int(item[0]), "value": float(item[1])})
            elif isinstance(item, dict):
                clean_data.append({"time": int(item["time"]), "value": float(item["value"])})
        except: continue
        
    clean_data.sort(key=lambda x: x["time"])
    
    # SAFETY: Force flat line if empty
    if len(clean_data) == 0:
        now = int(time.time())
        clean_data = [
            {"time": now - 86400, "value": 0.0},
            {"time": now, "value": 0.0}
        ]
        
    return {"items": clean_data}

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
    if not pair: return {"pair": "", "data": []}
    
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
                        "open": float(c[1]), 
                        "high": float(c[2]), 
                        "low": float(c[3]), 
                        "close": float(c[4])
                    })
                break
        
        candles.sort(key=lambda x: x["time"])
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f: json.dump({"pair": pair, "data": candles}, f)
        return {"pair": pair, "data": candles}
    except: return {"pair": pair, "data": []}

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
        "pair": body.pair, "side": body.side, "notional_usd": body.notional_usd
    }
    os.makedirs(RUN_DIR, exist_ok=True)
    tmp = MANUAL_ORDER_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(req, f)
    os.replace(tmp, MANUAL_ORDER_PATH)
    return {"queued": True}