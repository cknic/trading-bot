import os
import csv
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


# -----------------------------
# Paths / Env
# -----------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRADES_CSV = os.environ.get("TRADES_CSV", os.path.join(DATA_DIR, "trades.csv"))
PNL_JSON = os.environ.get("PNL_JSON", os.path.join(DATA_DIR, "pnl.json"))
EVENTS_JSONL = os.environ.get("EVENTS_JSONL", os.path.join(DATA_DIR, "events.jsonl"))
STATE_JSON = os.environ.get("STATE_JSON", os.path.join(DATA_DIR, "state.json"))

RUN_DIR = os.environ.get("RUN_DIR", "/run/trading")
PAUSE_FILE = os.environ.get("PAUSE_FILE", os.path.join(RUN_DIR, "PAUSE"))
KILL_FILE = os.environ.get("KILL_FILE", os.path.join(RUN_DIR, "KILL_SWITCH"))
MANUAL_ORDER_PATH = os.environ.get("MANUAL_ORDER_PATH", os.path.join(RUN_DIR, "MANUAL_ORDER.json"))

# Live latch (matches bot defaults)
LIVE_LATCH_FILE = os.environ.get("LIVE_LATCH_FILE", os.path.join(RUN_DIR, "LIVE_LATCH"))
REQUIRE_LIVE_LATCH = os.environ.get("REQUIRE_LIVE_LATCH", "1").strip().lower() not in ("0", "false", "")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_KRAKEN = os.path.join(CONFIG_DIR, "kraken.yaml")
CONFIG_RISK = os.path.join(CONFIG_DIR, "risk.yaml")
CONFIG_AI = os.path.join(CONFIG_DIR, "ai.yaml")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

BOT_STATUS_JSON = os.environ.get("BOT_STATUS_PATH", os.path.join(DATA_DIR, "bot_status.json"))

app = FastAPI(title="Trading Bot API", version="0.10")

origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in origins if o.strip()] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TS = int(time.time())


# -----------------------------
# Models
# -----------------------------
class ReasonBody(BaseModel):
    reason: str = "manual"


class PreviewBody(BaseModel):
    pair: str
    side: str  # buy|sell
    notional_usd: float = 20.0


class ManualExecuteBody(BaseModel):
    pair: str
    side: str  # buy|sell
    notional_usd: float = 20.0


# -----------------------------
# Auth helpers
# -----------------------------
def _require_auth(authorization: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def _auth_ok(authorization: Optional[str]) -> bool:
    if not ADMIN_TOKEN:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return authorization.split(" ", 1)[1].strip() == ADMIN_TOKEN


# -----------------------------
# File helpers
# -----------------------------
def _read_json(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not os.path.exists(path):
        return default or {}
    with open(path, "r") as f:
        return json.load(f)


def _read_text(path: str, max_bytes: int = 200_000) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        b = f.read(max_bytes)
    return b.decode("utf-8", errors="replace")


def _append_event(obj: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(EVENTS_JSONL), exist_ok=True)
        with open(EVENTS_JSONL, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def _tail_trades(limit: int = 100) -> List[Dict[str, Any]]:
    if not os.path.exists(TRADES_CSV):
        return []
    rows: List[Dict[str, Any]] = []
    with open(TRADES_CSV, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    limit = max(1, min(int(limit), 5000))
    return rows[-limit:]


def _tail_events(limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(EVENTS_JSONL):
        return []
    limit = max(1, min(int(limit), 5000))
    out: List[Dict[str, Any]] = []
    with open(EVENTS_JSONL, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 4096
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= limit:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
        lines = data.splitlines()[-limit:]
        for ln in lines:
            try:
                out.append(json.loads(ln.decode("utf-8", errors="replace")))
            except Exception:
                continue
    return out


def _touch(path: str, content: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content + "\n")


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return


def _load_yaml(path: str) -> Dict[str, Any]:
    import yaml
    txt = _read_text(path)
    if not txt.strip():
        return {}
    return yaml.safe_load(txt) or {}


def _trading_mode() -> str:
    kcfg = _load_yaml(CONFIG_KRAKEN)
    return ((kcfg.get("trading", {}) or {}).get("mode", "") or "").strip().lower()


def _latch_present() -> bool:
    return os.path.exists(LIVE_LATCH_FILE)


def _live_allowed() -> bool:
    # mirror the bot’s allow_live behavior at a high level
    if _trading_mode() != "live":
        return False
    if os.path.exists(KILL_FILE):
        return False
    if REQUIRE_LIVE_LATCH and (not _latch_present()):
        return False
    return True


# -----------------------------
# Core endpoints
# -----------------------------
@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    pnl = _read_json(PNL_JSON)
    mode = _trading_mode()
    latch_present = _latch_present()
    live_allowed = _live_allowed()

    return {
        "ok": True,
        "uptime_s": int(time.time()) - START_TS,
        "paused": os.path.exists(PAUSE_FILE),
        "kill_switch": os.path.exists(KILL_FILE),
        "pnl_ts": pnl.get("ts"),
        "portfolio": (pnl.get("portfolio") or {}),
        "auth_ok": _auth_ok(authorization),
        "auth_required": bool(ADMIN_TOKEN),
        # new:
        "trading_mode": mode,
        "live_latch_required": bool(REQUIRE_LIVE_LATCH),
        "live_latch_present": bool(latch_present),
        "live_allowed": bool(live_allowed),
    }


@app.get("/pnl")
def pnl():
    return _read_json(PNL_JSON)


@app.get("/trades")
def trades(limit: int = 100):
    items = _tail_trades(limit)
    return {"count": len(items), "items": items}


@app.get("/events")
def events(limit: int = 200):
    items = _tail_events(limit)
    return {"count": len(items), "items": items}


@app.get("/equity")
def equity(limit: int = 200, authorization: Optional[str] = Header(default=None)):
    """
    Returns realized equity curve points as: [[ts, realized_pnl], ...]
    Prefer pnl.json['equity_curve_realized'] if present.
    Fallback: derive points from trades.csv if it contains realized_pnl_usd (optional).
    """
    _require_auth(authorization)

    p = _read_json(PNL_JSON)
    curve = p.get("equity_curve_realized") or p.get("equity_curve") or None
    out: List[Tuple[int, float]] = []

    if isinstance(curve, list):
        for it in curve:
            try:
                ts = int(it[0])
                val = float(it[1])
                out.append((ts, val))
            except Exception:
                continue

    if not out and os.path.exists(TRADES_CSV):
        try:
            with open(TRADES_CSV, "r", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    if "realized_pnl_usd" in row and row.get("realized_pnl_usd") not in (None, ""):
                        ts = int(float(row.get("ts") or "0"))
                        val = float(row.get("realized_pnl_usd") or 0.0)
                        out.append((ts, val))
        except Exception:
            pass

    out = sorted(out, key=lambda x: x[0])
    limit = max(1, min(int(limit), 5000))
    out = out[-limit:]
    return {"ok": True, "count": len(out), "items": [[ts, v] for ts, v in out]}


@app.get("/config/summary")
def config_summary():
    kraken = _load_yaml(CONFIG_KRAKEN)
    risk = _load_yaml(CONFIG_RISK)
    ai = _load_yaml(CONFIG_AI)

    k = kraken.get("kraken", {}) or {}
    t = kraken.get("trading", {}) or {}
    s = kraken.get("strategy", {}) or {}
    cd = kraken.get("cooldown", {}) or {}
    saf = kraken.get("safety", {}) or {}

    acct = risk.get("account", {}) or {}
    tr = risk.get("trade", {}) or {}
    lev = risk.get("leverage_caps", {}) or {}
    r_saf = risk.get("safety", {}) or {}

    provider = ai.get("provider", "openai")
    model = (ai.get(provider, {}) or {}).get("model", "")

    return {
        "pairs": (k.get("pairs") or []),
        "base_url": k.get("base_url", ""),
        "mode": t.get("mode", ""),
        "order_type": t.get("order_type", ""),
        "quote_notional_usd": t.get("quote_notional_usd", ""),
        "poll_seconds": t.get("poll_seconds", ""),
        "timeframe_minutes": s.get("timeframe_minutes", ""),
        "sma_short": s.get("sma_short", ""),
        "sma_long": s.get("sma_long", ""),
        "min_candles": s.get("min_candles", ""),
        "cooldown_hours": cd.get("hours_after_trade", ""),
        "safety_spread_pct": saf.get("max_spread_pct", ""),
        "safety_slippage_pct": saf.get("max_slippage_pct", ""),
        "limit_offset_pct": saf.get("limit_offset_pct", ""),
        "risk_max_drawdown_pct": acct.get("max_drawdown_pct", ""),
        "risk_max_daily_loss_pct": acct.get("max_daily_loss_pct", ""),
        "risk_max_open_positions": tr.get("max_open_positions", ""),
        "risk_max_notional_usd_per_trade": tr.get("max_notional_usd_per_trade", ""),
        "risk_max_trades_per_day": tr.get("max_trades_per_day", ""),
        "leverage_caps": lev,
        "fail_closed": bool(r_saf.get("fail_closed", True)),
        "ai_provider": provider,
        "ai_model": model,
        # new:
        "live_latch_required": bool(REQUIRE_LIVE_LATCH),
        "live_latch_present": bool(_latch_present()),
        "live_allowed": bool(_live_allowed()),
        "live_latch_file": LIVE_LATCH_FILE,
    }


# -----------------------------
# Controls
# -----------------------------
@app.post("/control/pause")
def pause(body: ReasonBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    _touch(PAUSE_FILE, body.reason)
    _append_event({"ts": int(time.time()), "event": "paused", "reason": body.reason})
    return {"paused": True, "reason": body.reason}


@app.post("/control/resume")
def resume(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    _rm(PAUSE_FILE)
    _append_event({"ts": int(time.time()), "event": "resumed"})
    return {"paused": False}


@app.post("/control/kill")
def kill(body: ReasonBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    _touch(KILL_FILE, body.reason)
    _append_event({"ts": int(time.time()), "event": "kill_switch_on", "reason": body.reason})
    return {"kill_switch": True, "reason": body.reason}


@app.post("/control/unkill")
def unkill(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    _rm(KILL_FILE)
    _append_event({"ts": int(time.time()), "event": "kill_switch_off"})
    return {"kill_switch": False}


# -----------------------------
# Preview + Manual Execute (queue for bot)
# -----------------------------
def _compute_order_preview(pair: str, side: str, notional_usd: float) -> Dict[str, Any]:
    from app.exchange.kraken_client import KrakenClient
    from app.exchange.kraken_orders import resolve_pair_info, get_ticker

    kcfg = _load_yaml(CONFIG_KRAKEN)
    base_url = (kcfg.get("kraken", {}) or {}).get("base_url", "https://api.kraken.com")
    k = KrakenClient(api_key="x", api_secret="x", base_url=base_url)

    pair_in = pair.strip()
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be buy or sell")

    pair_key, pair_info = resolve_pair_info(k, pair_in)
    t = get_ticker(k, pair_key)
    bid = float(t["b"][0])
    ask = float(t["a"][0])
    last = float(t["c"][0])
    mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    slip_pct = (abs(last - mid) / mid) * 100.0 if mid > 0 else 999.0

    safety = kcfg.get("safety", {}) or {}
    max_spread = float(safety.get("max_spread_pct", 0.30))
    max_slip = float(safety.get("max_slippage_pct", 0.50))
    limit_off = float(safety.get("limit_offset_pct", 0.02))
    order_type = (kcfg.get("trading", {}) or {}).get("order_type", "limit")

    blocked_reasons = []
    if spread_pct > max_spread:
        blocked_reasons.append(f"spread {spread_pct:.6f}% > max {max_spread:.6f}%")
    if slip_pct > max_slip:
        blocked_reasons.append(f"slippage {slip_pct:.6f}% > max {max_slip:.6f}%")

    lot_decimals = int(pair_info.get("lot_decimals", 8))
    cost_decimals = int(pair_info.get("cost_decimals", 2))
    notional = float(notional_usd)

    vol = (notional / last) if (side == "buy" and last > 0) else 0.0

    def fmt_dec(v: float, d: int) -> str:
        return ("{:0." + str(d) + "f}").format(v)

    def fmt_vol(v: float, d: int) -> str:
        return ("{:0." + str(d) + "f}").format(v)

    price = None
    if order_type == "limit":
        if side == "buy":
            price = fmt_dec(ask * (1.0 + (limit_off / 100.0)), cost_decimals)
        else:
            price = fmt_dec(bid * (1.0 - (limit_off / 100.0)), cost_decimals)

    return {
        "pair_input": pair_in,
        "pair_resolved": pair_key,
        "side": side,
        "order_type": order_type,
        "notional_usd": notional,
        "volume_est": fmt_vol(vol, lot_decimals),
        "limit_price": price,
        "market": {"bid": bid, "ask": ask, "last": last, "spread_pct": spread_pct, "slippage_pct": slip_pct},
        "safety": {"max_spread_pct": max_spread, "max_slippage_pct": max_slip, "limit_offset_pct": limit_off},
        "would_block": bool(blocked_reasons),
        "block_reasons": blocked_reasons,
        "note": "Preview compute only. No order is placed by the API.",
    }


@app.post("/preview/order")
def preview_order(body: PreviewBody):
    return _compute_order_preview(body.pair, body.side, body.notional_usd)

@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    pnl = _read_json(PNL_JSON)
    bot = _read_json(BOT_STATUS_JSON, default={})

    return {
        "ok": True,
        "uptime_s": int(time.time()) - START_TS,
        "paused": os.path.exists(PAUSE_FILE),
        "kill_switch": os.path.exists(KILL_FILE),
        "pnl_ts": pnl.get("ts"),
        "portfolio": (pnl.get("portfolio") or {}),
        "auth_ok": _auth_ok(authorization),
        "auth_required": bool(ADMIN_TOKEN),

        # bot-reported truth (preferred)
        "bot": bot,
    }


@app.post("/manual/execute")
def manual_execute(body: ManualExecuteBody, authorization: Optional[str] = Header(default=None)):
    """
    Queues a one-shot request file for the bot to consume.

    Safety: still requires trading.mode == dry_run.
    (We can later allow live manual behind latch + extra confirmations + limits.)
    """
    _require_auth(authorization)

    kcfg = _load_yaml(CONFIG_KRAKEN)
    mode = ((kcfg.get("trading", {}) or {}).get("mode", "") or "").strip().lower()
    if mode != "dry_run":
        raise HTTPException(status_code=400, detail="Refusing manual execute: trading.mode is not dry_run")

    out = _compute_order_preview(body.pair, body.side, body.notional_usd)

    req = {
        "ts": int(time.time()),
        "id": f"manual_{int(time.time())}",
        "pair": out.get("pair_resolved") or body.pair,
        "side": (out.get("side") or body.side).lower(),
        "notional_usd": float(body.notional_usd),
        "requested_from": "ui",
    }

    os.makedirs(RUN_DIR, exist_ok=True)
    path = MANUAL_ORDER_PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(req, f)
    os.replace(tmp, path)

    _append_event({
        "ts": int(time.time()),
        "event": "manual_order_queued",
        "pair": req["pair"],
        "side": req["side"],
        "notional_usd": req["notional_usd"],
        "id": req["id"],
    })

    out["queued"] = True
    out["queue_file"] = path
    out["manual_id"] = req["id"]
    return out


# -----------------------------
# UI
# -----------------------------
_UI_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Trading Bot Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; }
    .row { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-start; }
    .card { border:1px solid #ddd; border-radius: 10px; padding: 14px; min-width: 320px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
    h1 { margin:0 0 8px 0; }
    h2 { margin:0 0 10px 0; font-size: 16px; }
    button { padding:8px 10px; border-radius:8px; border:1px solid #ccc; background:white; cursor:pointer; }
    button.primary { border-color:#111; }
    button:disabled { opacity:0.5; cursor:not-allowed; }
    input, select { padding:8px; border-radius:8px; border:1px solid #ccc; }
    input { width: 360px; }
    .muted { color:#666; font-size: 12px; }
    .ok { color: #0a7; font-weight: 600; }
    .bad { color: #c22; font-weight: 600; }
    .pill { display:inline-block; padding:2px 8px; border:1px solid #ddd; border-radius:999px; font-size:12px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #eee; padding: 6px 8px; font-size: 12px; text-align:left; }
    th { background:#fafafa; position: sticky; top: 0; }
    .scroll { max-height: 260px; overflow:auto; border:1px solid #eee; border-radius:8px; }
    .kv { display:grid; grid-template-columns: 160px 1fr; gap:6px 10px; font-size:13px; }
    .k { color:#444; }
    .v { font-weight:600; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; }
    canvas { border:1px solid #eee; border-radius:8px; width:100%; height:140px; }
  </style>
</head>
<body>
  <h1>Trading Bot Dashboard</h1>
  <p class="muted">Tunnel-only via SSH port-forward. Controls require ADMIN_TOKEN (stored locally in this browser). Times shown in US Eastern.</p>

  <div class="card">
    <h2>Admin Token</h2>
    <div class="row">
      <input id="token" type="password" placeholder="Paste ADMIN_TOKEN once (stored in localStorage)" />
      <button class="primary" onclick="saveToken()">Save</button>
      <button onclick="clearToken()">Clear</button>
      <button onclick="toggleToken()">Show</button>
      <span id="authPill" class="pill muted">auth: unknown</span>
    </div>
    <div class="muted">Stored in this browser only: <code>localStorage.trading_admin_token</code></div>
  </div>

  <div class="row">
    <div class="card" style="min-width:420px;">
      <h2>Health</h2>
      <div id="healthBadges" class="row"></div>
      <div class="kv" style="margin-top:10px;">
        <div class="k">uptime</div><div class="v" id="hUptime">—</div>
        <div class="k">paused</div><div class="v" id="hPaused">—</div>
        <div class="k">kill switch</div><div class="v" id="hKill">—</div>
        <div class="k">pnl_ts</div><div class="v mono" id="hPnlTs">—</div>
      </div>
      <div class="row" style="margin-top:12px;">
        <button id="btnPause" onclick="pause()">Pause</button>
        <button id="btnResume" onclick="resume()">Resume</button>
        <button id="btnKill" onclick="kill()">Kill</button>
        <button id="btnUnkill" onclick="unkill()">Unkill</button>
      </div>
      <div class="muted" style="margin-top:10px;">Raw: <span class="mono" id="healthRawMini">—</span></div>
    </div>

    <div class="card" style="min-width:560px;">
      <h2>PnL Summary</h2>
      <div class="kv">
        <div class="k">net PnL</div><div class="v" id="pNet">—</div>
        <div class="k">realized</div><div class="v" id="pRealized">—</div>
        <div class="k">unrealized</div><div class="v" id="pUnrealized">—</div>
        <div class="k">wins / losses</div><div class="v" id="pWL">—</div>
        <div class="k">win rate</div><div class="v" id="pWR">—</div>
        <div class="k">max drawdown</div><div class="v" id="pDD">—</div>
      </div>

      <h2 style="margin-top:14px;">Equity Curve (realized)</h2>
      <canvas id="eqCanvas" width="900" height="180"></canvas>
      <div class="scroll" style="margin-top:10px;"><table id="eqTbl"></table></div>
    </div>

    <div class="card" style="min-width:520px;">
      <h2>Manual Execute (dry-run only)</h2>
      <div class="row">
        <select id="mxPair"></select>
        <select id="mxSide">
          <option value="buy">buy</option>
          <option value="sell">sell</option>
        </select>
        <input id="mxNotional" value="20" style="width:120px;" />
        <button class="primary" onclick="manualExecute()">Execute</button>
      </div>
      <div class="muted" style="margin-top:6px;">Queues a one-shot request for the bot. Refuses unless <code>trading.mode</code> is <code>dry_run</code>.</div>
      <div class="scroll" style="max-height:220px;"><table id="mxTbl"></table></div>
    </div>

    <div class="card" style="min-width:520px;">
      <h2>Recent Trades</h2>
      <div class="scroll"><table id="tradesTbl"></table></div>
    </div>

    <div class="card" style="min-width:560px;">
      <h2>Recent Events</h2>
      <div class="scroll"><table id="eventsTbl"></table></div>
    </div>

    <div class="card" style="min-width:520px;">
      <h2>Preview Order (compute only)</h2>
      <div class="row">
        <select id="pvPair"></select>
        <select id="pvSide">
          <option value="buy">buy</option>
          <option value="sell">sell</option>
        </select>
        <input id="pvNotional" value="20" style="width:120px;" />
        <button class="primary" onclick="preview()">Preview</button>
      </div>
      <div class="muted" style="margin-top:6px;">Preview never places orders.</div>
      <div class="scroll" style="max-height:220px;"><table id="pvTbl"></table></div>
    </div>
  </div>

<script>
const TZ = "America/New_York";
function fmtTs(ts) {
  const n = Number(ts||0);
  if (!n) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    year:"numeric", month:"2-digit", day:"2-digit",
    hour:"2-digit", minute:"2-digit", second:"2-digit"
  }).format(new Date(n*1000));
}
function getToken() { return localStorage.getItem("trading_admin_token") || ""; }
function saveToken() { localStorage.setItem("trading_admin_token", document.getElementById("token").value.trim()); alert("Saved."); }
function clearToken() { localStorage.removeItem("trading_admin_token"); document.getElementById("token").value=""; alert("Cleared."); }
function toggleToken() {
  const el = document.getElementById("token");
  el.type = (el.type === "password") ? "text" : "password";
}
async function api(path, opts={}) {
  const token = getToken();
  const headers = Object.assign({"Content-Type":"application/json"}, (opts.headers||{}));
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const txt = await res.text();
  let data; try { data = JSON.parse(txt); } catch(e) { data = {raw: txt}; }
  if (!res.ok) throw new Error((data && data.detail) ? data.detail : ("HTTP " + res.status));
  return data;
}
function money(x) {
  const n = Number(x || 0);
  const sign = n >= 0 ? "" : "-";
  return sign + "$" + Math.abs(n).toFixed(2);
}
function pct(x) { return (Number(x||0)).toFixed(2) + "%"; }

function setAuthUI(authRequired, authOk) {
  const pill = document.getElementById("authPill");
  pill.className = "pill " + (authOk ? "ok" : "bad");
  pill.textContent = `auth: ${authRequired ? "required" : "not required"} · ${authOk ? "OK" : "FAIL"}`;
  const enableControls = (!authRequired) || authOk;
  ["btnPause","btnResume","btnKill","btnUnkill"].forEach(id => document.getElementById(id).disabled = !enableControls);
}
function renderBadges(h) {
  const el = document.getElementById("healthBadges");
  const ok = (h.ok && !h.kill_switch);

  const mode = (h.trading_mode || "unknown").toLowerCase();
  const modeClass = (mode === "live") ? "bad" : "ok";

  const latchReq = !!h.live_latch_required;
  const latchPresent = !!h.live_latch_present;
  const liveAllowed = !!h.live_allowed;

  const latchClass = (!latchReq) ? "ok" : (latchPresent ? "ok" : "bad");
  const allowClass = liveAllowed ? "ok" : "bad";

  el.innerHTML = `
    <span class="pill ${ok ? "ok":"bad"}">${ok ? "OK":"NOT OK"}</span>
    <span class="pill ${h.paused ? "bad":"ok"}">paused=${h.paused}</span>
    <span class="pill ${h.kill_switch ? "bad":"ok"}">kill=${h.kill_switch}</span>
    <span class="pill ${modeClass}">mode=${mode}</span>
    <span class="pill ${latchClass}">latch=${latchReq ? (latchPresent ? "present" : "missing") : "not required"}</span>
    <span class="pill ${allowClass}">live_allowed=${liveAllowed}</span>
  `;
}
function renderTrades(items) {
  const tbl = document.getElementById("tradesTbl");
  const head = `<tr><th>time (ET)</th><th>pair</th><th>side</th><th>price</th><th>notional</th><th>mode</th></tr>`;
  const rows = (items||[]).slice().reverse().map(t => (
    `<tr>
      <td class="mono">${fmtTs(t.ts||0)}</td>
      <td>${t.pair||""}</td>
      <td>${t.side||""}</td>
      <td class="mono">${t.price||""}</td>
      <td class="mono">${t.notional_usd||""}</td>
      <td>${t.mode||""}</td>
    </tr>`
  )).join("");
  tbl.innerHTML = head + rows;
}
function renderEvents(items) {
  const tbl = document.getElementById("eventsTbl");
  const head = `<tr><th>time (ET)</th><th>event</th><th>pair</th><th>action</th><th>reason</th></tr>`;
  const rows = (items||[]).slice().reverse().map(e => (
    `<tr>
      <td class="mono">${fmtTs(e.ts||0)}</td>
      <td>${e.event||""}</td>
      <td>${e.pair||""}</td>
      <td>${e.action||e.side||""}</td>
      <td>${e.reason||""}</td>
    </tr>`
  )).join("");
  tbl.innerHTML = head + rows;
}
function renderEqTable(curve) {
  const tbl = document.getElementById("eqTbl");
  const head = `<tr><th>time (ET)</th><th>realized_pnl</th></tr>`;
  const rows = (curve||[]).slice().reverse().map(pt => (
    `<tr><td class="mono">${fmtTs(pt[0])}</td><td class="mono">${money(pt[1])}</td></tr>`
  )).join("");
  tbl.innerHTML = head + rows;
}
function drawEquity(curve) {
  const c = document.getElementById("eqCanvas");
  const ctx = c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);

  const pts = (curve||[]).map(p => [Number(p[0]||0), Number(p[1]||0)]).filter(p => p[0]>0);
  if (pts.length < 2) { ctx.fillText("Not enough data yet", 10, 20); return; }

  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = Math.min(...ys), ymax = Math.max(...ys);

  const pad = 18, W = c.width, H = c.height;
  const xscale = (x) => pad + ((x - xmin) / (xmax - xmin)) * (W - pad*2);
  const yscale = (y) => {
    const denom = (ymax - ymin) || 1;
    return H - pad - ((y - ymin) / denom) * (H - pad*2);
  };

  ctx.beginPath();
  ctx.moveTo(pad, H-pad); ctx.lineTo(W-pad, H-pad);
  ctx.strokeStyle = "#ddd"; ctx.stroke();

  if (ymin <= 0 && ymax >= 0) {
    const y0 = yscale(0);
    ctx.beginPath(); ctx.moveTo(pad, y0); ctx.lineTo(W-pad, y0);
    ctx.strokeStyle = "#eee"; ctx.stroke();
  }

  ctx.beginPath();
  pts.forEach((p,i)=>{ const x=xscale(p[0]), y=yscale(p[1]); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.strokeStyle = "#111"; ctx.lineWidth = 2; ctx.stroke();

  const last = pts[pts.length-1][1];
  ctx.fillStyle = "#111";
  ctx.font = "12px ui-monospace, Menlo, monospace";
  ctx.fillText(`last: ${money(last)}`, pad, pad);
}
function renderPreview(out, tableId) {
  const tbl = document.getElementById(tableId);
  const rows = [
    ["pair", out.pair_resolved],
    ["side", out.side],
    ["order_type", out.order_type],
    ["notional_usd", money(out.notional_usd)],
    ["volume_est", out.volume_est],
    ["limit_price", out.limit_price || ""],
    ["bid/ask/last", `${out.market.bid} / ${out.market.ask} / ${out.market.last}`],
    ["spread%", out.market.spread_pct.toFixed(6)],
    ["slippage%", out.market.slippage_pct.toFixed(6)],
    ["would_block", String(out.would_block)],
    ["block_reasons", (out.block_reasons||[]).join("; ")],
    ["queued", String(out.queued||false)],
    ["manual_id", out.manual_id || ""],
  ];
  tbl.innerHTML = `<tr><th>field</th><th>value</th></tr>` + rows.map(r => `<tr><td>${r[0]}</td><td class="mono">${r[1]}</td></tr>`).join("");
}

async function refresh() {
  const h = await api("/health", {method:"GET", headers:{}});
  setAuthUI(h.auth_required, h.auth_ok);
  renderBadges(h);
  document.getElementById("hUptime").textContent = `${h.uptime_s}s`;
  document.getElementById("hPaused").textContent = String(h.paused);
  document.getElementById("hKill").textContent = String(h.kill_switch);
  document.getElementById("hPnlTs").textContent = fmtTs(h.pnl_ts || 0);
  document.getElementById("healthRawMini").textContent = `ok=${h.ok} paused=${h.paused} kill=${h.kill_switch} mode=${h.trading_mode} live_allowed=${h.live_allowed}`;

  const p = await api("/pnl");
  const port = p.portfolio || {};
  document.getElementById("pNet").textContent = money(port.net_pnl_usd);
  document.getElementById("pRealized").textContent = money(port.realized_pnl_usd);
  document.getElementById("pUnrealized").textContent = money(port.unrealized_pnl_usd);
  document.getElementById("pWL").textContent = `${port.wins||0} / ${port.losses||0}`;
  document.getElementById("pWR").textContent = pct((port.win_rate||0) * 100);
  document.getElementById("pDD").textContent = money(port.max_drawdown_usd);

  const eq = await api("/equity?limit=200", {method:"GET"});
  const curve = eq.items || [];
  drawEquity(curve);
  renderEqTable(curve);

  const cs = await api("/config/summary");
  const pairs = (cs.pairs||["XBTUSD","ETHUSD"]);
  ["pvPair","mxPair"].forEach(id => {
    const sel = document.getElementById(id);
    if (sel.options.length === 0) {
      pairs.forEach(p => { const o=document.createElement("option"); o.value=p; o.textContent=p; sel.appendChild(o); });
    }
  });

  const t = await api("/trades?limit=50");
  renderTrades(t.items||[]);
  const ev = await api("/events?limit=120");
  renderEvents(ev.items||[]);
}

async function pause() {
  const reason = prompt("Pause reason:", "manual pause") || "manual pause";
  await api("/control/pause", {method:"POST", body: JSON.stringify({reason})});
  await refresh();
}
async function resume() { await api("/control/resume", {method:"POST"}); await refresh(); }
async function kill() {
  const reason = prompt("Kill reason:", "manual kill") || "manual kill";
  await api("/control/kill", {method:"POST", body: JSON.stringify({reason})});
  await refresh();
}
async function unkill() { await api("/control/unkill", {method:"POST"}); await refresh(); }

async function preview() {
  const pair = document.getElementById("pvPair").value;
  const side = document.getElementById("pvSide").value;
  const notional_usd = parseFloat(document.getElementById("pvNotional").value || "20");
  try {
    const out = await api("/preview/order", {method:"POST", body: JSON.stringify({pair, side, notional_usd})});
    renderPreview(out, "pvTbl");
  } catch(e) {
    document.getElementById("pvTbl").innerHTML = `<tr><th>Error</th></tr><tr><td class="mono">${e.message}</td></tr>`;
  }
}
async function manualExecute() {
  const pair = document.getElementById("mxPair").value;
  const side = document.getElementById("mxSide").value;
  const notional_usd = parseFloat(document.getElementById("mxNotional").value || "20");
  try {
    const out = await api("/manual/execute", {method:"POST", body: JSON.stringify({pair, side, notional_usd})});
    renderPreview(out, "mxTbl");
    await refresh();
  } catch(e) {
    document.getElementById("mxTbl").innerHTML = `<tr><th>Error</th></tr><tr><td class="mono">${e.message}</td></tr>`;
  }
}

document.getElementById("token").value = getToken();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_UI_HTML)