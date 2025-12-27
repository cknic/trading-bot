import os
import time
import json
import copy
import traceback
import sys
import yaml
import requests
from collections import defaultdict

# --- UTILS ---
from util.ledger import get_position, set_position, clear_position
from util.trade_log import append_trade
from exchange.kraken_client import KrakenClient
from exchange.kraken_orders import place_or_preview, resolve_pair_info
from exchange.kraken_marketdata import fetch_ohlc_closes
from risk.risk_engine import RiskEngine
from strategy.ma_crossover import decide

# --- PATHS ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
AI_LOG_PATH = os.path.join(DATA_DIR, "ai_log.jsonl")
PNL_PATH = os.path.join(DATA_DIR, "pnl.json")
BOT_STATUS_PATH = os.path.join(DATA_DIR, "bot_status.json")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

MANUAL_ORDER_PATH = "/run/trading/MANUAL_ORDER.json"
PAUSE_FILE = "/run/trading/PAUSE"
KILL_FILE = "/run/trading/KILL_SWITCH"
LIVE_LATCH_FILE = "/run/trading/LIVE_LATCH"

# Env Flags
REQUIRE_LIVE_LATCH = os.environ.get("REQUIRE_LIVE_LATCH", "1").strip().lower() not in ("0", "false", "")

# ==============================================================================
# 1. HELPERS & LOGGING
# ==============================================================================

def load_yaml(path):
    with open(path, "r") as f: return yaml.safe_load(f)

def log_ai(prompt, response, model):
    entry = {
        "ts": int(time.time()),
        "model": model,
        "prompt": prompt,
        "response": response
    }
    try:
        with open(AI_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Logging Error: {e}")

def ai_call(provider, model, prompt):
    response = {"status": "simulated", "analysis": "Trend is neutral. Holding pattern."}
    if provider == "openai" and "OPENAI_API_KEY" in os.environ:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
            r = requests.post(url, headers=headers, json=payload, timeout=5)
            if r.status_code == 200: response = r.json()
        except: pass
    log_ai(prompt, response, model)
    return response

def update_realized_pnl(profit_usd):
    try:
        data = {}
        if os.path.exists(PNL_PATH):
            with open(PNL_PATH, 'r') as f: data = json.load(f)
        
        if "portfolio" not in data: data["portfolio"] = {"net_pnl_usd": 0.0}
        if "equity_curve_realized" not in data: data["equity_curve_realized"] = []

        current_net = data["portfolio"]["net_pnl_usd"] + profit_usd
        data["portfolio"]["net_pnl_usd"] = current_net
        
        data["equity_curve_realized"].append([int(time.time()), current_net])
        
        with open(PNL_PATH, 'w') as f: json.dump(data, f)
        print(f">> PnL UPDATED: ${profit_usd:.6f} (Total Equity Change: ${current_net:.6f})")
    except Exception as e:
        print(f"PnL Update Error: {e}")

def log_trade_csv(pair, side, vol, price, cost, mode):
    append_trade(int(time.time()), pair, side, str(vol), str(price), cost, mode)

def write_bot_status(status):
    try:
        tmp = BOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f: json.dump(status, f)
        os.replace(tmp, BOT_STATUS_PATH)
    except: pass

def sync_risk_counters(risk_engine):
    count = 0
    try:
        if os.path.exists(STATE_JSON):
            with open(STATE_JSON, 'r') as f:
                state = json.load(f)
                for v in state.values():
                    if v.get("has_position"): count += 1
    except: pass
    risk_engine.update_open_positions(count)

# ==============================================================================
# 2. STATE & SAFETY
# ==============================================================================

def is_paused(): return os.path.exists(PAUSE_FILE)
def is_killed(): return os.path.exists(KILL_FILE)
def live_latch_present(): return os.path.exists(LIVE_LATCH_FILE)
def get_trading_mode(kcfg): return (kcfg.get("trading", {}).get("mode", "") or "").strip().lower()

def allow_live(kcfg): 
    if get_trading_mode(kcfg) != "live": return False
    if is_killed(): return False
    if REQUIRE_LIVE_LATCH and not live_latch_present(): return False
    return True

def safe_kcfg_for_orders(kcfg):
    mode = get_trading_mode(kcfg)
    if mode != "live": return kcfg
    if allow_live(kcfg): return kcfg
    tmp = copy.deepcopy(kcfg)
    tmp.setdefault("trading", {})["mode"] = "dry_run"
    return tmp

def cancel_all_open_orders(k):
    print("SAFETY: Canceling open orders...")
    try: 
        resp = k.private("CancelAll")
        if resp.get("error"): print(f"Warn: CancelAll failed: {resp['error']}")
        else: print(f"SAFETY: CancelAll complete. {resp.get('result', {}).get('count', 0)} orders canceled.")
    except Exception as e: 
        print(f"Warn: CancelAll exception: {e}")

def reconcile_positions(k, pairs):
    print("SAFETY: Reconciling positions...")
    try:
        resp = k.private("OpenPositions")
        if resp.get("error"): return f"Kraken Error: {resp['error']}"
        
        kraken_pos = resp.get("result", {})
        real_positions = defaultdict(float)
        for txid, info in kraken_pos.items():
            p = info['pair']
            vol = float(info['vol']) - float(info['vol_closed'])
            if vol > 0.000001: real_positions[p] += vol
        
        local_state = {}
        if os.path.exists(STATE_JSON):
            with open(STATE_JSON, 'r') as f: local_state = json.load(f)

        errors = []
        for pair in pairs:
            local_has = local_state.get(pair, {}).get("has_position", False)
            local_vol = float(local_state.get(pair, {}).get("base_volume", 0.0))
            real_vol = real_positions.get(pair, 0.0)
            real_has = real_vol > 0.0001

            if local_has != real_has:
                msg = f"MISMATCH on {pair}: Local={local_has}({local_vol:.4f}), Kraken={real_has}({real_vol:.4f})"
                print(f"CRITICAL: {msg}")
                errors.append(msg)
        return errors
    except Exception as e: return f"Reconciliation Exception: {e}"

# ==============================================================================
# 3. MAIN LOOP
# ==============================================================================

def try_read_manual_order():
    try:
        if not os.path.exists(MANUAL_ORDER_PATH): return None
        with open(MANUAL_ORDER_PATH, "r") as f: return json.load(f)
    except: return None

def clear_manual_order():
    try: os.remove(MANUAL_ORDER_PATH)
    except: pass

def execute_trade_logic(k, risk, kcfg, pair, side, amt=None):
    kcfg_orders = safe_kcfg_for_orders(kcfg)
    
    pos = get_position(pair)
    base_override = pos.get("base_volume") if side == "sell" else None
    entry_price = pos.get("average_price", 0.0) if pos.get("has_position") else 0.0
    
    od, m = place_or_preview(k, kcfg_orders, risk, pair, side, base_override)
    
    if od.should_place or od.reason in ("dry-run", "LIVE order placed"):
        executed_price = float(m.get("last", 0))
        executed_vol = float(od.volume)
        total_cost = executed_price * executed_vol
        
        print(f"------------------------------------------------")
        print(f" ACTION: {side.upper()} | PAIR: {pair}")
        print(f" PRICE:  ${executed_price:.2f}")
        print(f" VOL:    {executed_vol:.6f}")
        print(f" COST:   ${total_cost:.2f}")
        print(f" MODE:   {od.mode.upper()}")
        print(f"------------------------------------------------")
        
        log_trade_csv(pair, side, executed_vol, executed_price, total_cost, od.mode)
        
        # --- PNL & FEES ---
        if side == "sell" and entry_price > 0:
            gross_pnl = (executed_price - entry_price) * executed_vol
            
            # Fee Logic:
            # 1. Try to read explicit fee from Kraken response (Live Mode)
            # 2. Fallback to calculation using config (Dry Run)
            
            real_fee_usd = float(m.get("fee", 0.0))
            
            if real_fee_usd > 0:
                # We have real data (LIVE)
                fees = real_fee_usd
                fee_source = "KRAKEN_API"
            else:
                # We simulate (DRY_RUN)
                # Use configured pct or default to 0.26%
                fee_pct = float(kcfg.get("fees", {}).get("taker_fee_pct", 0.26)) / 100.0
                entry_fee = (entry_price * executed_vol) * fee_pct
                exit_fee = (executed_price * executed_vol) * fee_pct
                fees = entry_fee + exit_fee
                fee_source = f"SIMULATED ({fee_pct*100}%)"

            net_pnl = gross_pnl - fees
            
            print(f">> PnL CALC: Gross ${gross_pnl:.6f} - Fees ${fees:.6f} [{fee_source}] = Net ${net_pnl:.6f}")
            update_realized_pnl(net_pnl)
        
        if od.mode != "live" and od.reason == "dry-run":
            if side == "buy": set_position(pair, executed_vol, executed_price)
            if side == "sell": clear_position(pair)
            
        sync_risk_counters(risk)
        return True
    else:
        print(f"REJECTED: {od.reason}")
        return False

def main():
    print(">>> BOT STARTED <<<")
    
    try:
        risk_cfg = load_yaml("/config/risk.yaml")
        ai_cfg = load_yaml("/config/ai.yaml")
        kcfg = load_yaml("/config/kraken.yaml")
    except Exception as e:
        print(f"CRITICAL: Config error {e}")
        sys.exit(1)

    k = KrakenClient(os.environ["KRAKEN_API_KEY"], os.environ["KRAKEN_API_SECRET"], kcfg["kraken"]["base_url"])
    risk = RiskEngine(risk_cfg)
    
    poll_seconds = kcfg.get("trading", {}).get("poll_seconds", 60)
    strategy_interval = kcfg.get("strategy", {}).get("timeframe_minutes", 60)
    sma_short = kcfg.get("strategy", {}).get("sma_short", 10)
    sma_long = kcfg.get("strategy", {}).get("sma_long", 30)
    
    print(f"Poll Interval: {poll_seconds}s")
    print(f"Strategy Interval: {strategy_interval}m")
    
    pairs = []
    pair_map = {}
    for p in kcfg["kraken"]["pairs"]:
        try:
            pk, _ = resolve_pair_info(k, p)
            pairs.append(pk)
            pair_map[p] = pk
            pair_map[pk] = pk
        except Exception as e:
            print(f"Startup Warning: Could not resolve pair {p}: {e}")
        
    print(f"Pairs Resolved: {pairs}")

    mode = get_trading_mode(kcfg)
    if mode == "live":
        cancel_all_open_orders(k)
        errs = reconcile_positions(k, pairs)
        if errs:
            print(f"SAFETY ABORT: {errs}")
            write_bot_status({"killed": True, "last_error": str(errs)})
            sys.exit(1)

    print("Initializing Risk Engine State...")
    sync_risk_counters(risk)

    last_check_ts = 0

    while True:
        try:
            # A. Manual
            manual = try_read_manual_order()
            if manual:
                raw_pair = manual.get("pair")
                pk = pair_map.get(raw_pair)
                side = manual.get("side")
                if pk and side in ("buy", "sell"):
                    print(f"\n>>> MANUAL REQUEST: {side.upper()} {pk}")
                    execute_trade_logic(k, risk, kcfg, pk, side)
                clear_manual_order()

            # B. Strategy
            now = int(time.time())
            if now - last_check_ts > poll_seconds:
                print(f"\n--- STRATEGY CHECK ({time.strftime('%H:%M:%S')}) ---")
                
                prompt = ai_cfg.get("prompts", {}).get("strategy_decision", "Analyze market.")
                ai_resp = ai_call(ai_cfg.get("provider"), ai_cfg.get("model", "gpt-4"), prompt)
                print(f"AI Brain: {ai_resp.get('analysis', 'No analysis')}")

                for pk in pairs:
                    try:
                        closes = fetch_ohlc_closes(k, pk, interval=strategy_interval)
                        min_candles = kcfg.get("strategy", {}).get("min_candles", 50)
                        if len(closes) < min_candles:
                            print(f"{pk}: Not enough data ({len(closes)}/{min_candles})")
                            continue
                            
                        pos_data = get_position(pk)
                        has_pos = pos_data.get("has_position", False)
                        
                        signal, reason = decide(closes, sma_short, sma_long, has_pos)
                        print(f"{pk}: Signal={signal.upper()} | {reason}")
                        
                        if signal in ("buy", "sell"):
                            print(f">>> AUTO TRADING: {signal.upper()} {pk}")
                            execute_trade_logic(k, risk, kcfg, pk, signal)
                            
                    except Exception as e:
                        print(f"Strategy Error on {pk}: {e}")
                        traceback.print_exc()

                last_check_ts = now

            write_bot_status({
                "ts": int(time.time()),
                "mode_config": mode,
                "paused": is_paused(),
                "killed": is_killed(),
                "live_allowed": allow_live(kcfg)
            })
            
            sync_risk_counters(risk)
            time.sleep(2)

        except KeyboardInterrupt:
            print("Stopping...")
            break
        except Exception as e:
            print(f"LOOP ERROR: {e}")
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()