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
from util.pnl_analytics import compute_and_write
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
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except:
        return {}


def log_ai(response, model, asset, indicators=None):
    """Log AI decision with asset identifier and indicators"""
    if isinstance(response, dict):
        response_text = response.get("analysis", json.dumps(response))
    elif not isinstance(response, str):
        response_text = str(response)
    else:
        response_text = response
    
    entry = {
        "ts": int(time.time()),
        "asset": asset,
        "model": model,
        "response": response_text,
    }
    
    if indicators:
        entry["indicators"] = indicators
    
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


def ai_call_single(provider, model, base_prompt, market_data_str, asset_name, indicators=None, position_context=None):
    """AI call for a single asset - logs with asset identifier and indicators"""
    
    # Build the full prompt
    full_prompt = base_prompt
    
    # Add position context if available
    if position_context:
        position_block = (
            f"\n\nCURRENT POSITION STATUS:\n"
            f"- Status: {'HOLDING' if position_context.get('has_position') else 'NO POSITION'}\n"
        )
        if position_context.get('has_position'):
            position_block += (
                f"- Entry Price: ${position_context.get('entry_price', 0):.4f}\n"
                f"- Current Price: ${position_context.get('current_price', 0):.4f}\n"
                f"- Position Size: {position_context.get('volume', 0):.6f}\n"
                f"- Cost Basis: ${position_context.get('cost_basis', 0):.2f}\n"
                f"- Unrealized P&L: ${position_context.get('unrealized_pnl', 0):.2f} ({position_context.get('unrealized_pct', 0):+.2f}%)\n"
                f"- Hold Duration: {position_context.get('hold_duration_hours', 0):.1f} hours ({position_context.get('hold_duration_days', 0):.1f} days)\n"
                f"- Entry Fee Paid: ${position_context.get('entry_fee', 0):.4f}\n"
            )
        full_prompt += position_block
    
    full_prompt += f"\n\nCURRENT MARKET DATA:\n{market_data_str}"
    
    # Include indicators in prompt if available
    if indicators:
        indicator_summary = (
            f"\n\nTECHNICAL INDICATORS:\n"
            f"- RSI(14): {indicators.get('rsi', 'N/A')}\n"
            f"- Trend: {indicators.get('trend', 'N/A').upper()}\n"
            f"- Trend Strength: {indicators.get('trend_strength_pct', 'N/A')}%\n"
            f"- Distance from SMA: {indicators.get('distance_from_sma_pct', 'N/A')}%\n"
            f"- Volatility: {indicators.get('volatility_pct', 'N/A')}%\n"
            f"- Golden Cross: {'YES' if indicators.get('bullish_cross') else 'No'}\n"
            f"- Death Cross: {'YES' if indicators.get('bearish_cross') else 'No'}\n"
            f"- RSI Overbought: {'YES' if indicators.get('rsi_overbought') else 'No'}\n"
            f"- RSI Oversold: {'YES' if indicators.get('rsi_oversold') else 'No'}\n"
            f"- Price Extended: {'YES' if indicators.get('price_extended') else 'No'}"
        )
        full_prompt += indicator_summary
    
    clean_response = {"status": "error", "analysis": "AI Request Failed"}
    
    if "OPENAI_API_KEY" not in os.environ and "OPENROUTER_API_KEY" not in os.environ:
        clean_response["analysis"] = "Configuration Error: API Keys missing."
        log_ai(clean_response, model, asset_name, indicators)
        return clean_response
    
    try:
        url = "https://api.openai.com/v1/chat/completions"
        api_key = os.environ.get('OPENAI_API_KEY')
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            api_key = os.environ.get('OPENROUTER_API_KEY', api_key)
            headers["Authorization"] = f"Bearer {api_key}"
            headers["HTTP-Referer"] = "http://sentinel-bot"
            headers["X-Title"] = "Sentinel Trading Bot"
        
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}]
        }
        
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
    
    log_ai(clean_response, model, asset_name, indicators)
    return clean_response


def refresh_pnl_json(current_prices):
    """Recompute pnl.json from trades.csv with current mark prices"""
    try:
        kcfg = load_yaml("/config/kraken.yaml")
        compute_and_write(kcfg, current_prices)
    except Exception as e:
        print(f"PnL Refresh Error: {e}")


def apply_daily_opex(kcfg):
    daily_cost = float(kcfg.get("fees", {}).get("operational_cost_daily_usd", 0.0))
    if daily_cost <= 0:
        return
    try:
        data = {}
        if os.path.exists(PNL_PATH):
            with open(PNL_PATH, 'r') as f:
                data = json.load(f)
        
        if "portfolio" not in data:
            data["portfolio"] = {"realized_pnl_usd": 0.0}
        if "equity_curve_realized" not in data:
            data["equity_curve_realized"] = []
        
        last_opex = data.get("last_opex_ts", 0)
        now = int(time.time())
        
        if (now - last_opex) >= 86400:
            current_realized = data["portfolio"].get("realized_pnl_usd", 0.0)
            new_realized = current_realized - daily_cost
            data["portfolio"]["realized_pnl_usd"] = new_realized
            data["equity_curve_realized"].append([now, new_realized])
            data["last_opex_ts"] = now
            
            with open(PNL_PATH, 'w') as f:
                json.dump(data, f)
            print(f">> OPEX DEDUCTION: -${daily_cost:.2f} (Daily Operational Cost)")
        
    except Exception as e:
        print(f"OpEx Update Error: {e}")


def log_trade_csv(pair, side, vol, price, cost, mode, reason=""):
    append_trade(int(time.time()), pair, side, str(vol), str(price), cost, mode, reason)


def write_bot_status(status):
    try:
        tmp = BOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, BOT_STATUS_PATH)
    except:
        pass


def sync_risk_counters(risk_engine):
    count = 0
    try:
        if os.path.exists(STATE_JSON):
            with open(STATE_JSON, 'r') as f:
                state = json.load(f)
                for v in state.values():
                    if v.get("has_position"):
                        count += 1
    except:
        pass
    risk_engine.update_open_positions(count)


# ==============================================================================
# 2. STATE & SAFETY
# ==============================================================================
def is_paused():
    return os.path.exists(PAUSE_FILE)


def is_killed():
    return os.path.exists(KILL_FILE)


def live_latch_present():
    return os.path.exists(LIVE_LATCH_FILE)


def get_trading_mode(kcfg):
    return (kcfg.get("trading", {}).get("mode", "") or "").strip().lower()


def allow_live(kcfg):
    if get_trading_mode(kcfg) != "live":
        return False
    if is_killed():
        return False
    if REQUIRE_LIVE_LATCH and not live_latch_present():
        return False
    return True


def safe_kcfg_for_orders(kcfg):
    mode = get_trading_mode(kcfg)
    if mode != "live":
        return kcfg
    if allow_live(kcfg):
        return kcfg
    tmp = copy.deepcopy(kcfg)
    tmp.setdefault("trading", {})["mode"] = "dry_run"
    return tmp


def cancel_all_open_orders(k):
    print("SAFETY: Canceling open orders...")
    try:
        resp = k.private("CancelAll")
        if resp.get("error"):
            print(f"Warn: CancelAll failed: {resp['error']}")
        else:
            print(f"SAFETY: CancelAll complete. {resp.get('result', {}).get('count', 0)} orders canceled.")
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
            with open(STATE_JSON, 'r') as f:
                local_state = json.load(f)
        
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
                        "entry_ts": int(time.time()),
                        "first_entry_time": int(time.time())
                    }
                    print(f"   -> ADOPTED: {pair} Vol: {real_vol:.6f} @ ${real_avg:.2f}")
                else:
                    if pair in local_state:
                        del local_state[pair]
                    print(f"   -> CLEARED: {pair} (Not on Kraken)")
                
                updates_made = True
        
        if updates_made:
            with open(STATE_JSON, 'w') as f:
                json.dump(local_state, f, indent=2)
            print(">> SYNC COMPLETE: Local State updated to match Kraken.")
        else:
            print(">> SYNC OK: Local State matches Kraken.")
            
        return True
    except Exception as e:
        print(f"Sync Exception: {e}")
        return False


# ==============================================================================
# 3. TRADE EXECUTION
# ==============================================================================
def execute_trade_logic(k, risk, kcfg, pair, side, amt=None, current_prices=None, reason=""):
    kcfg_orders = safe_kcfg_for_orders(kcfg)
    pos = get_position(pair)
    base_override = pos.get("base_volume") if side == "sell" else None
    entry_price = pos.get("average_price", 0.0) if pos.get("has_position") else 0.0
    first_entry_time = pos.get("first_entry_time", 0) if pos.get("has_position") else 0
    
    od, m = place_or_preview(k, kcfg_orders, risk, pair, side, base_override)
    
    if od.should_place or od.reason in ("dry-run", "LIVE order placed"):
        executed_price = float(m.get("last", 0))
        executed_vol = float(od.volume)
        total_cost = executed_price * executed_vol
        
        print(f"================================================")
        print(f"  TRADE EXECUTED")
        print(f"------------------------------------------------")
        print(f"  Action: {side.upper()}")
        print(f"  Pair:   {pair}")
        print(f"  Price:  ${executed_price:,.2f}")
        print(f"  Volume: {executed_vol:.6f}")
        print(f"  Cost:   ${total_cost:,.2f}")
        print(f"  Mode:   {od.mode.upper()}")
        print(f"================================================")
        
        log_trade_csv(pair, side, executed_vol, executed_price, total_cost, od.mode, reason)
        
        if od.mode != "live" and od.reason == "dry-run":
            if side == "buy":
                if first_entry_time > 0:
                    set_position(pair, executed_vol, executed_price, first_entry_time=first_entry_time)
                else:
                    set_position(pair, executed_vol, executed_price)
            if side == "sell":
                clear_position(pair)
        
        # Refresh PnL after trade
        if current_prices is not None:
            refresh_pnl_json(current_prices)
        
        sync_risk_counters(risk)
        return True
    else:
        print(f"ORDER REJECTED: {od.reason}")
        return False


# ==============================================================================
# 4. MAIN LOOP
# ==============================================================================
def try_read_manual_order():
    try:
        if not os.path.exists(MANUAL_ORDER_PATH):
            return None
        with open(MANUAL_ORDER_PATH, "r") as f:
            return json.load(f)
    except:
        return None


def clear_manual_order():
    try:
        os.remove(MANUAL_ORDER_PATH)
    except:
        pass


def get_friendly_name(pair):
    """Convert Kraken pair to friendly name"""
    names = {
        "XXBTZUSD": "BTC", "XETHZUSD": "ETH", "SOLUSD": "SOL",
        "XXRPZUSD": "XRP", "ADAUSD": "ADA", "XDGUSD": "DOGE",
        "DOTUSD": "DOT", "LINKUSD": "LINK", "MATICUSD": "MATIC",
        "UNIUSD": "UNI", "AVAXUSD": "AVAX", "ATOMUSD": "ATOM"
    }
    return names.get(pair, pair.replace("USD", "").replace("XX", "").replace("X", ""))

def build_position_context(pair, pos_data, current_price, pnl_data=None):
    """Build position context dict for AI prompt"""
    if not pos_data.get("has_position"):
        return {"has_position": False}
    
    entry_price = float(pos_data.get("average_price", 0))
    volume = float(pos_data.get("base_volume", 0))
    first_entry = int(pos_data.get("first_entry_time", pos_data.get("entry_ts", 0)))
    
    cost_basis = entry_price * volume
    current_value = current_price * volume
    unrealized_pnl = current_value - cost_basis
    unrealized_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    
    now = time.time()
    hold_hours = (now - first_entry) / 3600.0 if first_entry > 0 else 0
    hold_days = hold_hours / 24.0
    
    # Try to get entry fee from pnl.json
    entry_fee = 0.0
    if pnl_data and "pairs" in pnl_data:
        pair_pnl = pnl_data.get("pairs", {}).get(pair, {})
        entry_fee = float(pair_pnl.get("entry_fee_usd", 0))
    
    return {
        "has_position": True,
        "entry_price": entry_price,
        "current_price": current_price,
        "volume": volume,
        "cost_basis": cost_basis,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": unrealized_pct,
        "hold_duration_hours": hold_hours,
        "hold_duration_days": hold_days,
        "entry_fee": entry_fee,
    }

def generate_single_market_summary(k, pair, interval, sma_short, sma_long):
    """Generate market data for a single pair with multi-timeframe context"""
    try:
        closes = fetch_ohlc_closes(k, pair, interval)
        if not closes or len(closes) < sma_long:
            return None, None, None
        
        last_price = closes[-1]
        last_5 = closes[-5:]
        
        val_short = calculate_sma(closes, sma_short)
        val_long = calculate_sma(closes, sma_long)
        
        sma_str = f"SMA({sma_short}): {val_short:.2f} | SMA({sma_long}): {val_long:.2f}" if (val_short and val_long) else "SMA: N/A"
        prices_str = ", ".join([f"{p:.2f}" for p in last_5])
        
        # Multi-timeframe context
        try:
            daily_closes = fetch_ohlc_closes(k, pair, 1440)
            if daily_closes and len(daily_closes) >= 7:
                price_24h_ago = daily_closes[-2] if len(daily_closes) > 1 else daily_closes[0]
                price_7d_ago = daily_closes[-7] if len(daily_closes) >= 7 else daily_closes[0]
                
                daily_change_pct = ((last_price - price_24h_ago) / price_24h_ago) * 100.0
                weekly_change_pct = ((last_price - price_7d_ago) / price_7d_ago) * 100.0
                
                if weekly_change_pct > 10.0:
                    trend = "STRONG BULLISH (7d)"
                elif weekly_change_pct > 3.0:
                    trend = "BULLISH (7d)"
                elif weekly_change_pct < -10.0:
                    trend = "STRONG BEARISH (7d)"
                elif weekly_change_pct < -3.0:
                    trend = "BEARISH (7d)"
                else:
                    trend = "NEUTRAL (7d)"
                
                macro_str = f"24h: {daily_change_pct:+.2f}% | 7d: {weekly_change_pct:+.2f}% | Trend: {trend}"
            elif daily_closes and len(daily_closes) >= 2:
                price_24h_ago = daily_closes[-2]
                daily_change_pct = ((last_price - price_24h_ago) / price_24h_ago) * 100.0
                macro_str = f"24h: {daily_change_pct:+.2f}%"
            else:
                macro_str = "Timeframe Data: N/A"
        except:
            macro_str = "Timeframe Data: Error"
        
        summary = (
            f"PAIR: {pair}\n"
            f"  Price: ${last_price:.4f} | {sma_str}\n"
            f"  Recent: [{prices_str}]\n"
            f"  {macro_str}"
        )
        
        return summary, closes, last_price
    except Exception as e:
        print(f"Data Gen Error {pair}: {e}")
        return None, None, None


def get_strategy_config(kcfg):
    """Extract strategy filter config from kraken.yaml"""
    filters = kcfg.get("strategy", {}).get("filters", {})
    return {
        "rsi_period": filters.get("rsi_period", 14),
        "rsi_overbought": filters.get("rsi_overbought", 70),
        "rsi_oversold": filters.get("rsi_oversold", 30),
        "rsi_take_profit": filters.get("rsi_take_profit", 80),
        "max_distance_pct": filters.get("max_distance_pct", 8.0),
        "require_crossover": filters.get("require_crossover", False),
    }


def get_ai_override_limits(kcfg):
    """Extract AI override safety limits from config"""
    ai_override = kcfg.get("strategy", {}).get("ai_override", {})
    return {
        "max_rsi_for_force_buy": ai_override.get("max_rsi_for_force_buy", 75),
        "max_distance_for_force_buy": ai_override.get("max_distance_for_force_buy", 10.0),
        "allow_ai_force_buy": ai_override.get("allow_ai_force_buy", True),
        "allow_ai_block_sell": ai_override.get("allow_ai_block_sell", True),
    }


def synthesize_decision(signal, reason, indicators, ai_vote, has_pos, ai_limits):
    """
    Combine strategy signal with AI vote to determine final action.
    
    Returns: (final_action, final_reason)
    """
    final_action = "HOLD"
    final_reason = reason
    
    # Extract relevant indicators
    rsi = indicators.get("rsi")
    distance_pct = indicators.get("distance_from_sma_pct", 0)
    is_stop_loss = "stop" in reason.lower() or "take profit" in reason.lower()
    
    # ===== BUY SIGNAL =====
    if signal == "buy":
        if ai_vote == "STOP":
            final_action = "HOLD"
            final_reason = f"AI VETO: {reason}"
        elif ai_vote == "NEUTRAL":
            final_action = "HOLD"
            final_reason = f"AI UNCERTAIN: {reason}"
        else:  # GO
            final_action = "BUY"
            final_reason = f"CONFIRMED: {reason}"
    
    # ===== HOLD SIGNAL =====
    elif signal == "hold":
        # AI can force entry on hold (within safety limits)
        if ai_vote == "GO" and not has_pos and ai_limits.get("allow_ai_force_buy", True):
            # Safety checks for AI override
            max_rsi = ai_limits.get("max_rsi_for_force_buy", 75)
            max_dist = ai_limits.get("max_distance_for_force_buy", 10.0)
            
            blocked_reasons = []
            
            if rsi is not None and rsi > max_rsi:
                blocked_reasons.append(f"RSI {rsi:.1f} > {max_rsi}")
            
            if distance_pct > max_dist:
                blocked_reasons.append(f"Distance +{distance_pct:.1f}% > {max_dist}%")
            
            if blocked_reasons:
                final_action = "HOLD"
                final_reason = f"AI OVERRIDE BLOCKED: {', '.join(blocked_reasons)}"
            else:
                final_action = "BUY"
                final_reason = f"AI OVERRIDE: Force entry ({reason})"
        else:
            final_action = "HOLD"
            final_reason = reason
    
    # ===== SELL SIGNAL =====
    elif signal == "sell":
        # AI can save position from sell, BUT NOT if it's a stop-loss/take-profit
        if ai_vote == "GO" and ai_limits.get("allow_ai_block_sell", True) and not is_stop_loss:
            final_action = "HOLD"
            final_reason = f"AI SAVE: Position protected ({reason})"
        else:
            final_action = "SELL"
            if is_stop_loss:
                final_reason = f"SAFETY EXIT: {reason}"
            else:
                final_reason = f"CONFIRMED: {reason}"
    
    return final_action, final_reason


def main():
    print("=" * 60)
    print("  SENTINEL TRADING BOT - CRYPTO")
    print("  Enhanced Strategy with RSI + Distance Filters")
    print("=" * 60)
    
    try:
        risk_cfg = load_yaml("/config/risk.yaml")
        ai_cfg = load_yaml("/config/ai.yaml")
        kcfg = load_yaml("/config/kraken.yaml")
    except Exception as e:
        print(f"CRITICAL: Config error {e}")
        sys.exit(1)
    
    k = KrakenClient(
        os.environ["KRAKEN_API_KEY"],
        os.environ["KRAKEN_API_SECRET"],
        kcfg["kraken"]["base_url"]
    )
    risk = RiskEngine(risk_cfg)
    
    # Strategy parameters
    poll_seconds = kcfg.get("trading", {}).get("poll_seconds", 60)
    strategy_interval = kcfg.get("strategy", {}).get("timeframe_minutes", 60)
    sma_short = kcfg.get("strategy", {}).get("sma_short", 10)
    sma_long = kcfg.get("strategy", {}).get("sma_long", 30)
    min_candles = kcfg.get("strategy", {}).get("min_candles", 50)
    
    # Filter and AI override configs
    strategy_config = get_strategy_config(kcfg)
    ai_limits = get_ai_override_limits(kcfg)
    
    print(f"\nStrategy Config:")
    print(f"  SMA: {sma_short}/{sma_long} on {strategy_interval}min candles")
    print(f"  RSI: Period={strategy_config['rsi_period']}, OB={strategy_config['rsi_overbought']}, OS={strategy_config['rsi_oversold']}")
    print(f"  Max Distance: {strategy_config['max_distance_pct']}%")
    print(f"  Require Crossover: {strategy_config['require_crossover']}")
    print(f"  AI Force Buy Limit: RSI<{ai_limits['max_rsi_for_force_buy']}, Dist<{ai_limits['max_distance_for_force_buy']}%")
    
    ai_provider, ai_model = get_ai_model_config(ai_cfg)
    print(f"\nAI Provider: {ai_provider} | Model: {ai_model}")
    
    # Resolve pairs
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
    
    print(f"Trading Pairs: {pairs}")
    
    mode = get_trading_mode(kcfg)
    print(f"Trading Mode: {mode.upper()}")
    
    if mode == "live":
        cancel_all_open_orders(k)
        sync_ok = reconcile_and_sync_positions(k, pairs)
        if not sync_ok:
            print("CRITICAL: Failed to sync with Kraken. Aborting for safety.")
            write_bot_status({"killed": True, "last_error": "Startup Sync Failed"})
            sys.exit(1)
    
    print("Initializing Risk Engine State...")
    sync_risk_counters(risk)
    
    # Track current prices for PnL marks
    current_prices = {}
    
    # Initial PnL refresh
    refresh_pnl_json(current_prices)
    
    last_check_ts = 0
    
    print("\n" + "=" * 60)
    print("  BOT RUNNING - Waiting for signals...")
    print("=" * 60 + "\n")
    
    while True:
        try:
            apply_daily_opex(kcfg)
            
            # A. Manual Orders
            manual = try_read_manual_order()
            if manual:
                raw_pair = manual.get("pair")
                pk = pair_map.get(raw_pair)
                side = manual.get("side")
                if pk and side in ("buy", "sell"):
                    print(f"\n>>> MANUAL ORDER: {side.upper()} {pk}")
                    execute_trade_logic(k, risk, kcfg, pk, side, current_prices=current_prices, reason="Manual Order")
                clear_manual_order()
            
            # B. Strategy Check
            now = int(time.time())
            if now - last_check_ts > poll_seconds:
                print(f"\n{'─' * 50}")
                print(f"  STRATEGY SCAN - {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'─' * 50}")
                
                # Reload configs (hot reload support)
                try:
                    kcfg = load_yaml("/config/kraken.yaml")
                    ai_cfg = load_yaml("/config/ai.yaml")
                    strategy_config = get_strategy_config(kcfg)
                    ai_limits = get_ai_override_limits(kcfg)
                    ai_provider, ai_model = get_ai_model_config(ai_cfg)
                except:
                    pass
                
                prompt_base = ai_cfg.get("prompts", {}).get("strategy_decision", "Analyze market.")
                
                # C. Process each pair individually
                for pk in pairs:
                    try:
                        friendly_name = get_friendly_name(pk)
                        asset_label = f"{friendly_name} (Crypto)"
                        
                        print(f"\n  [{friendly_name}] Analyzing...")
                        
                        # Get market data for this pair
                        market_data, closes, last_price = generate_single_market_summary(
                            k, pk, strategy_interval, sma_short, sma_long
                        )
                        
                        if not market_data or not closes:
                            print(f"  [{friendly_name}] ⚠ Not enough data")
                            continue
                        
                        # Store current price for PnL marks
                        if last_price and last_price > 0:
                            current_prices[pk] = last_price
                        
                        if len(closes) < min_candles:
                            print(f"  [{friendly_name}] ⚠ Not enough candles ({len(closes)}/{min_candles})")
                            continue
                        
                        pos_data = get_position(pk)
                        has_pos = pos_data.get("has_position", False)
                        
                        # ===== DECAYING STOP-LOSS CHECK (Priority) =====
                        if has_pos:
                            entry_price = float(pos_data.get("average_price", 0.0))
                            first_entry = int(pos_data.get("first_entry_time", 0))
                            if first_entry == 0:
                                first_entry = int(pos_data.get("entry_ts", time.time()))
                            
                            decay_cfg = kcfg.get("strategy", {}).get("decay_exit", {})
                            
                            should_exit, exit_reason = calculate_decaying_stop(
                                entry_price, first_entry, closes[-1], decay_cfg
                            )
                            
                            if should_exit:
                                print(f"  [{friendly_name}] 🛑 STOP-LOSS: {exit_reason}")
                                execute_trade_logic(k, risk, kcfg, pk, "sell", current_prices=current_prices, reason=exit_reason)
                                continue
                        
                        # ===== STRATEGY DECISION =====
                        signal, reason, indicators = decide(
                            closes, sma_short, sma_long, has_pos, strategy_config
                        )
                        
                        # Log indicators
                        ind_summary = (
                            f"RSI:{indicators.get('rsi', 'N/A')} | "
                            f"Trend:{indicators.get('trend', '?')} | "
                            f"Dist:{indicators.get('distance_from_sma_pct', 0):+.1f}%"
                        )
                        print(f"  [{friendly_name}] Indicators: {ind_summary}")
                        print(f"  [{friendly_name}] Strategy: {signal.upper()} - {reason[:60]}...")
                        
                        # ===== BUILD POSITION CONTEXT =====
                        # Load pnl.json for fee data
                        pnl_data = {}
                        try:
                            if os.path.exists(PNL_PATH):
                                with open(PNL_PATH, 'r') as f:
                                    pnl_data = json.load(f)
                        except:
                            pass        
                        position_context = build_position_context(pk, pos_data, last_price, pnl_data)

                        # ===== AI CALL =====
                        ai_resp = ai_call_single(
                            ai_provider, ai_model, prompt_base,
                            market_data, asset_label, indicators, position_context
                        )
                        analysis = ai_resp.get("analysis", "No Analysis")
                        
                        # Parse AI verdict
                        ai_vote = "NEUTRAL"
                        if "VERDICT: GO" in analysis.upper():
                            ai_vote = "GO"
                        elif "VERDICT: STOP" in analysis.upper():
                            ai_vote = "STOP"
                        
                        print(f"  [{friendly_name}] AI Vote: {ai_vote}")
                        
                        # ===== SYNTHESIZE FINAL DECISION =====
                        final_action, final_reason = synthesize_decision(
                            signal, reason, indicators, ai_vote, has_pos, ai_limits
                        )
                        
                        # ===== EXECUTE =====
                        if final_action == "BUY":
                            print(f"  [{friendly_name}] ✅ BUYING: {final_reason}")
                            execute_trade_logic(k, risk, kcfg, pk, "buy", current_prices=current_prices, reason=final_reason)
                        elif final_action == "SELL":
                            print(f"  [{friendly_name}] 🔴 SELLING: {final_reason}")
                            execute_trade_logic(k, risk, kcfg, pk, "sell", current_prices=current_prices, reason=final_reason)
                        else:
                            print(f"  [{friendly_name}] ⏸ HOLD: {final_reason[:60]}...")
                        
                    except Exception as e:
                        print(f"  [{pk}] ❌ Error: {e}")
                        traceback.print_exc()
                
                # Refresh PnL after each scan cycle
                refresh_pnl_json(current_prices)
                
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
            print("\nShutting down gracefully...")
            break
        except Exception as e:
            print(f"LOOP ERROR: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()