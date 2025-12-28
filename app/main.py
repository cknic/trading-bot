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
from strategy.ma_crossover import decide, calculate_sma
from strategy.exit import calculate_decaying_stop

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
    try:
        with open(path, "r") as f: return yaml.safe_load(f)
    except: return {}

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

def get_ai_model_config(ai_cfg):
    provider = ai_cfg.get("provider", "openai")
    model = ai_cfg.get("model")
    if not model and provider in ai_cfg:
        model = ai_cfg.get(provider, {}).get("model")
    if not model:
        model = "gpt-4o-mini"
    return provider, model

def ai_call(provider, model, base_prompt, market_data_str):
    full_prompt = f"{base_prompt}\n\nCURRENT MARKET DATA:\n{market_data_str}"
    
    clean_response = {"status": "error", "analysis": "AI Request Failed"}
    
    # 1. Validation
    if "OPENAI_API_KEY" not in os.environ and "OPENROUTER_API_KEY" not in os.environ:
         clean_response["analysis"] = "Configuration Error: API Keys missing."
         log_ai(full_prompt, clean_response, model)
         return clean_response

    # 2. Provider Routing
    try:
        url = "https://api.openai.com/v1/chat/completions"
        api_key = os.environ.get('OPENAI_API_KEY')
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        # OpenRouter Specific Overrides
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            # Prefer OPENROUTER_API_KEY, fallback to OPENAI_API_KEY
            api_key = os.environ.get('OPENROUTER_API_KEY', api_key)
            headers["Authorization"] = f"Bearer {api_key}"
            headers["HTTP-Referer"] = "http://sentinel-bot"
            headers["X-Title"] = "Sentinel Trading Bot"
        
        # 3. Payload Construction
        # [FIX] Removed 'temperature' to prevent HTTP 400 errors on strict models (Grok/o1)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}]
        }
        
        # 4. Execution
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if r.status_code == 200: 
            raw = r.json()
            try:
                content = raw['choices'][0]['message']['content']
                clean_response = {"status": "success", "analysis": content}
            except KeyError:
                clean_response = {"status": "error", "analysis": "Format Error", "raw": raw}
        else:
            err_msg = f"HTTP {r.status_code}: {r.text}"
            print(f"AI Error: {err_msg}")
            clean_response["analysis"] = f"AI Provider Error: {err_msg[:200]}"
            
    except Exception as e: 
        print(f"AI Exception: {e}")
        clean_response["analysis"] = f"AI Connection Failed: {str(e)}"

    log_ai(full_prompt, clean_response, model)
    return clean_response

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

def apply_daily_opex(kcfg):
    daily_cost = float(kcfg.get("fees", {}).get("operational_cost_daily_usd", 0.0))
    if daily_cost <= 0: return

    try:
        data = {}
        if os.path.exists(PNL_PATH):
            with open(PNL_PATH, 'r') as f: data = json.load(f)
        
        if "portfolio" not in data: data["portfolio"] = {"net_pnl_usd": 0.0}
        if "equity_curve_realized" not in data: data["equity_curve_realized"] = []
        
        last_opex = data.get("last_opex_ts", 0)
        now = int(time.time())
        
        if (now - last_opex) >= 86400:
            current_net = data["portfolio"]["net_pnl_usd"] - daily_cost
            data["portfolio"]["net_pnl_usd"] = current_net
            data["equity_curve_realized"].append([now, current_net])
            data["last_opex_ts"] = now
            
            with open(PNL_PATH, 'w') as f: json.dump(data, f)
            print(f">> OPEX DEDUCTION: -${daily_cost:.2f} (Daily Operational Cost)")
            
    except Exception as e:
        print(f"OpEx Update Error: {e}")

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

def reconcile_and_sync_positions(k, pairs):
    print("SAFETY: Syncing State with Kraken Reality...")
    try:
        resp = k.private("OpenPositions")
        if resp.get("error"): 
            print(f"Kraken Error during sync: {resp['error']}")
            return False

        kraken_pos = resp.get("result", {})
        
        real_positions = defaultdict(lambda: {"vol": 0.0, "cost": 0.0})
        for txid, info in kraken_pos.items():
            p = info['pair']
            vol = float(info['vol']) - float(info['vol_closed'])
            cost = float(info['cost']) 
            if vol > 0.000001: 
                real_positions[p]["vol"] += vol
                real_positions[p]["cost"] += cost
        
        local_state = {}
        if os.path.exists(STATE_JSON):
            with open(STATE_JSON, 'r') as f: local_state = json.load(f)

        updates_made = False
        
        for pair in pairs:
            real_vol = real_positions[pair]["vol"]
            real_cost = real_positions[pair]["cost"]
            real_has = real_vol > 0.0001
            real_avg = (real_cost / real_vol) if real_vol > 0 else 0.0

            local_has = local_state.get(pair, {}).get("has_position", False)
            local_vol = float(local_state.get(pair, {}).get("base_volume", 0.0))

            if abs(real_vol - local_vol) > 0.0001 or (real_has != local_has):
                print(f">> SYNC: Mismatch on {pair}. Local: {local_vol} | Kraken: {real_vol}")
                
                if real_has:
                    local_state[pair] = {
                        "has_position": True,
                        "base_volume": real_vol,
                        "average_price": real_avg,
                        "last_update": int(time.time()),
                        "entry_ts": int(time.time())
                    }
                    print(f"   -> ADOPTED: {pair} Vol: {real_vol:.6f} @ ${real_avg:.2f}")
                else:
                    if pair in local_state: del local_state[pair]
                    print(f"   -> CLEARED: {pair} (Not on Kraken)")
                
                updates_made = True

        if updates_made:
            with open(STATE_JSON, 'w') as f: json.dump(local_state, f, indent=2)
            print(">> SYNC COMPLETE: Local State updated to match Kraken.")
        else:
            print(">> SYNC OK: Local State matches Kraken.")
            
        return True

    except Exception as e: 
        print(f"Sync Exception: {e}")
        return False

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
        
        if side == "sell" and entry_price > 0:
            gross_pnl = (executed_price - entry_price) * executed_vol
            real_fee_usd = float(m.get("fee", 0.0))
            if real_fee_usd > 0:
                fees = real_fee_usd
                fee_source = "KRAKEN_API"
            else:
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

def generate_market_summary(k, pairs, interval, sma_short, sma_long):
    summary = []
    for pair in pairs:
        try:
            # 1. TACTICAL (Strategy Timeframe)
            closes = fetch_ohlc_closes(k, pair, interval)
            if not closes or len(closes) < sma_long: continue

            last_price = closes[-1]
            last_5 = closes[-5:]
            
            val_short = calculate_sma(closes, sma_short)
            val_long = calculate_sma(closes, sma_long)
            
            sma_str = f"SMA({sma_short}): {val_short:.2f} | SMA({sma_long}): {val_long:.2f}" if (val_short and val_long) else "SMA: N/A"
            prices_str = ", ".join([f"{p:.2f}" for p in last_5])

            # 2. STRATEGIC (Daily)
            try:
                daily_closes = fetch_ohlc_closes(k, pair, 1440) 
                if daily_closes and len(daily_closes) >= 1:
                    price_24h_ago = daily_closes[-2] if len(daily_closes) > 1 else daily_closes[0]
                    daily_change_pct = ((last_price - price_24h_ago) / price_24h_ago) * 100.0
                    
                    macro_str = f"24h Change: {daily_change_pct:+.2f}%"
                    if daily_change_pct > 3.0: macro_trend = "STRONG BULLISH"
                    elif daily_change_pct < -3.0: macro_trend = "STRONG BEARISH"
                    else: macro_trend = "NEUTRAL"
                else:
                    macro_str = "24h Data: N/A"
                    macro_trend = "UNKNOWN"
            except:
                macro_str = "24h Data: Error"
                macro_trend = "UNKNOWN"

            line = (f"PAIR: {pair}\n"
                    f" - TACTICAL (1h): Price ${last_price:.2f} | {sma_str}\n"
                    f" - RECENT ACTION: [{prices_str}]\n"
                    f" - STRATEGIC (24h): {macro_str} | Trend: {macro_trend}")
            summary.append(line)
        except Exception as e:
            print(f"Data Gen Error {pair}: {e}")
    return "\n\n".join(summary)

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
    
    ai_provider, ai_model = get_ai_model_config(ai_cfg)
    print(f"AI Provider: {ai_provider} | Model: {ai_model}")

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

    mode = get_trading_mode(kcfg)
    
    if mode == "live":
        cancel_all_open_orders(k)
        sync_ok = reconcile_and_sync_positions(k, pairs)
        if not sync_ok:
            print("CRITICAL: Failed to sync with Kraken. Aborting for safety.")
            write_bot_status({"killed": True, "last_error": "Startup Sync Failed"})
            sys.exit(1)

    print("Initializing Risk Engine State...")
    sync_risk_counters(risk)

    last_check_ts = 0

    while True:
        try:
            apply_daily_opex(kcfg)

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
                
                try: 
                    ai_cfg = load_yaml("/config/ai.yaml")
                    ai_provider, ai_model = get_ai_model_config(ai_cfg)
                except: pass

                market_data = generate_market_summary(k, pairs, strategy_interval, sma_short, sma_long)
                prompt_base = ai_cfg.get("prompts", {}).get("strategy_decision", "Analyze market.")
                
                ai_resp = ai_call(ai_provider, ai_model, prompt_base, market_data)
                
                analysis = ai_resp.get("analysis", "No Analysis")
                print(f"AI Brain ({ai_model}): {analysis[:100]}...")

                # [FIX] STRICT GUARDRAIL LOGIC
                # Default to BLOCKED unless "GO" is explicitly found.
                ai_entry_ban = True 
                
                if "VERDICT: GO" in analysis:
                    print(">>> AI GUARDRAIL: GREEN LIGHT. ENTRIES APPROVED.")
                    ai_entry_ban = False
                elif "VERDICT: STOP" in analysis:
                    print(">>> AI GUARDRAIL: STOP SIGNAL RECEIVED. ENTRIES BLOCKED.")
                    ai_entry_ban = True
                else:
                    # Catch-all for API Errors, "No Verdict", Ambiguity, or Hallucinations
                    print(">>> AI GUARDRAIL: NO CLEAR GO SIGNAL (OR ERROR). DEFAULTING TO BLOCK.")
                    ai_entry_ban = True

                # C. Algo Logic
                for pk in pairs:
                    try:
                        closes = fetch_ohlc_closes(k, pk, interval=strategy_interval)
                        min_candles = kcfg.get("strategy", {}).get("min_candles", 50)
                        if len(closes) < min_candles:
                            print(f"{pk}: Not enough data")
                            continue
                            
                        pos_data = get_position(pk)
                        has_pos = pos_data.get("has_position", False)
                        
                        # [NEW] DECAYING STOP-LOSS CHECK
                        if has_pos:
                            entry_price = float(pos_data.get("average_price", 0.0))
                            entry_ts = int(pos_data.get("entry_ts", time.time())) 
                            decay_cfg = kcfg.get("strategy", {}).get("decay_exit", {})
                            
                            should_exit, exit_reason = calculate_decaying_stop(
                                entry_price, 
                                entry_ts, 
                                closes[-1], 
                                decay_cfg
                            )
                            
                            if should_exit:
                                print(f">>> EXIT SIGNAL: {exit_reason}")
                                execute_trade_logic(k, risk, kcfg, pk, "sell")
                                continue # Skip SMA check, we are exiting.

                        # Standard SMA Logic
                        signal, reason = decide(closes, sma_short, sma_long, has_pos)
                        print(f"{pk}: Signal={signal.upper()} | {reason}")
                        
                        if signal == "buy":
                            if ai_entry_ban:
                                print(f">>> AUTO TRADING: BUY {pk} BLOCKED BY AI VERDICT (OR FAILURE).")
                                continue
                            print(f">>> AUTO TRADING: BUY {pk}")
                            execute_trade_logic(k, risk, kcfg, pk, signal)
                        
                        elif signal == "sell":
                            print(f">>> AUTO TRADING: SELL {pk}")
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