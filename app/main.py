import os, time, json, copy, traceback, sys
import yaml
import requests
from collections import defaultdict

from util.test_kraken import main as kraken_test_main
from util.ledger import (
    get_position, set_position, clear_position,
    get_cooldown_until, set_cooldown
)
from util.trade_log import append_trade

from exchange.kraken_client import KrakenClient
from exchange.kraken_marketdata import fetch_ohlc_closes
from exchange.kraken_orders import place_or_preview, resolve_pair_info
from risk.risk_engine import RiskEngine
from strategy.ma_crossover import decide

# -----------------------------
# CONFIG / ENV
# -----------------------------
MANUAL_ORDER_PATH = os.environ.get("MANUAL_ORDER_PATH", "/run/trading/MANUAL_ORDER.json")
PAUSE_FILE = os.environ.get("PAUSE_FILE", "/run/trading/PAUSE")
KILL_FILE = os.environ.get("KILL_FILE", "/run/trading/KILL_SWITCH")

LIVE_LATCH_FILE = os.environ.get("LIVE_LATCH_FILE", "/run/trading/LIVE_LATCH")
REQUIRE_LIVE_LATCH = os.environ.get("REQUIRE_LIVE_LATCH", "1").strip().lower() not in ("0", "false", "")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BOT_STATUS_PATH = os.environ.get("BOT_STATUS_PATH", os.path.join(DATA_DIR, "bot_status.json"))

def load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)

def post_json(url, headers, payload):
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def ai_call(provider, model, prompt):
    if provider == "openai":
        key = os.environ["OPENAI_API_KEY"]
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "input": prompt}
        return post_json(url, headers, payload)
    raise ValueError("Unknown AI provider")

def is_paused(): return os.path.exists(PAUSE_FILE)
def is_killed(): return os.path.exists(KILL_FILE)
def live_latch_present() -> bool: return os.path.exists(LIVE_LATCH_FILE)

def get_trading_mode(kcfg) -> str:
    return (kcfg.get("trading", {}).get("mode", "") or "").strip().lower()

def allow_live(kcfg) -> bool:
    if get_trading_mode(kcfg) != "live": return False
    if is_killed(): return False
    if REQUIRE_LIVE_LATCH and (not live_latch_present()): return False
    return True

def safe_kcfg_for_orders(kcfg):
    mode = get_trading_mode(kcfg)
    if mode != "live": return kcfg
    if allow_live(kcfg): return kcfg
    tmp = copy.deepcopy(kcfg)
    tmp.setdefault("trading", {})
    tmp["trading"]["mode"] = "dry_run"
    return tmp

def try_read_manual_order():
    try:
        if not os.path.exists(MANUAL_ORDER_PATH): return None
        with open(MANUAL_ORDER_PATH, "r") as f: return json.load(f)
    except Exception: return None

def clear_manual_order():
    try: os.remove(MANUAL_ORDER_PATH)
    except FileNotFoundError: pass

def log_trade_csv(pair_key: str, side: str, od, m, notional_usd: float):
    ts = int(time.time())
    px = m.get("last") or od.price or ""
    append_trade(ts=ts, pair=pair_key, side=side, volume=str(od.volume), price=str(px), notional_usd=float(notional_usd), mode=str(od.mode))

def write_bot_status(status: dict):
    try:
        os.makedirs(os.path.dirname(BOT_STATUS_PATH), exist_ok=True)
        tmp = BOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f: json.dump(status, f, indent=2, sort_keys=True)
        os.replace(tmp, BOT_STATUS_PATH)
    except Exception: pass

def cancel_all_open_orders(k: KrakenClient):
    print("SAFETY: Canceling ALL open orders on Kraken to prevent orphan fills...")
    try:
        resp = k.private("CancelAll")
        count = resp.get("result", {}).get("count", 0)
        print(f"SAFETY: CancelAll complete. {count} orders canceled.")
    except Exception as e:
        print(f"SAFETY ERROR: Failed to cancel open orders: {e}")

def reconcile_positions(k: KrakenClient, pair_keys: list) -> list:
    print("SAFETY: Reconciling local ledger with Kraken OpenPositions...")
    try:
        resp = k.private("OpenPositions")
        k_positions = resp.get("result", {}) or {}
    except Exception as e:
        return [f"CRITICAL: Failed to fetch Kraken positions: {e}"]

    k_agg = defaultdict(float)
    for _, pdata in k_positions.items():
        pname = pdata.get("pair")
        vol = float(pdata.get("vol", 0.0)) - float(pdata.get("vol_closed", 0.0))
        if vol > 0.00000001: k_agg[pname] += vol

    mismatches = []
    for pair in pair_keys:
        local_pos = get_position(pair)
        local_vol = local_pos["base_volume"] if local_pos["has_position"] else 0.0
        remote_vol = k_agg.get(pair, 0.0)
        if abs(local_vol - remote_vol) > 0.0001:
            mismatches.append(f"{pair}: Local={local_vol:.6f} vs Kraken={remote_vol:.6f}")
    return mismatches

def main():
    boot_ts = int(time.time())
    last_loop_ok = True
    last_error = ""

    try:
        risk_cfg = load_yaml("/config/risk.yaml")
        ai_cfg = load_yaml("/config/ai.yaml")
        kcfg = load_yaml("/config/kraken.yaml")
    except Exception as e:
        print(f"CRITICAL: Failed to load config: {e}")
        sys.exit(1)

    provider = ai_cfg["provider"]
    model = ai_cfg[provider]["model"]
    print("Bot starting. fail_closed =", risk_cfg["safety"]["fail_closed"])
    print("AI provider =", provider, "model =", model)

    mode = get_trading_mode(kcfg)
    print(f"Trading mode = {mode}")

    if mode == "live" and not allow_live(kcfg):
        why = []
        if is_killed(): why.append("KILL_SWITCH present")
        if REQUIRE_LIVE_LATCH and not live_latch_present(): why.append("LIVE_LATCH missing")
        print("LIVE requested but NOT allowed -> forcing dry_run for all orders. Reasons:", ", ".join(why) or "unknown")

    write_bot_status({
        "ts": int(time.time()), "boot_ts": boot_ts, "mode_config": mode,
        "latch_required": REQUIRE_LIVE_LATCH, "latch_file": LIVE_LATCH_FILE,
        "latch_present": live_latch_present(), "live_allowed": allow_live(kcfg),
        "paused": is_paused(), "killed": is_killed(),
        "last_loop_ok": True, "last_error": "", "note": "booting..."
    })

    try:
        kraken_test_main()
        print("Kraken test: OK")

        risk = RiskEngine(risk_cfg)
        k = KrakenClient(os.environ["KRAKEN_API_KEY"], os.environ["KRAKEN_API_SECRET"], kcfg["kraken"]["base_url"])

        configured_pairs = kcfg["kraken"]["pairs"]
        pair_keys = []
        pair_map = {} 

        for p in configured_pairs:
            pk, _ = resolve_pair_info(k, p)
            pair_keys.append(pk)
            pair_map[p] = pk
            pair_map[pk] = pk 
            
        print("Trading pairs (normalized):", pair_keys)

        if mode == "live":
            cancel_all_open_orders(k)
            try:
                errors = reconcile_positions(k, pair_keys)
                if errors:
                    err_msg = "SAFETY ABORT: State Mismatch. " + "; ".join(errors)
                    print(err_msg)
                    write_bot_status({
                        "ts": int(time.time()), "boot_ts": boot_ts, "mode_config": mode,
                        "killed": True, "last_loop_ok": False, "last_error": err_msg,
                        "note": "startup_failed"
                    })
                    sys.exit(1)
            except Exception as e:
                print(f"SAFETY WARNING: Reconciliation failed due to network: {e}")
                pass
        else:
            print(f"Skipping strict reconciliation because mode={mode}")

    except Exception as e:
        print(f"Startup Exception: {e}")
        write_bot_status({
            "ts": int(time.time()), "boot_ts": boot_ts, "mode_config": mode,
            "killed": True, "last_loop_ok": False, "last_error": f"Startup Error: {str(e)}",
            "note": "crashed"
        })
        sys.exit(1)

    poll = int(kcfg["trading"].get("poll_seconds", 60))
    tf = int(kcfg["strategy"]["timeframe_minutes"])
    sma_s = int(kcfg["strategy"]["sma_short"])
    sma_l = int(kcfg["strategy"]["sma_long"])
    min_c = int(kcfg["strategy"]["min_candles"])
    simulate = bool(kcfg["strategy"].get("simulate_fills_in_dry_run", True))
    cd_hours = int(kcfg.get("cooldown", {}).get("hours_after_trade", 4))
    cd_seconds = cd_hours * 3600
    configured_notional = float(kcfg.get("trading", {}).get("quote_notional_usd", 20.0))

    while True:
        loop_ts = int(time.time())
        last_error = ""
        last_loop_ok = True

        try:
            current_open_pos_count = 0
            for pk in pair_keys:
                pos_data = get_position(pk)
                if pos_data.get("has_position"): current_open_pos_count += 1
            risk.update_open_positions(current_open_pos_count)

            manual = try_read_manual_order()
            if manual:
                if is_killed():
                    print("[manual] Ignoring manual order: KILL_SWITCH is enabled")
                    clear_manual_order()
                elif is_paused():
                    print("[manual] Manual order queued but bot is paused")
                else:
                    raw_pair = (manual.get("pair") or "").strip()
                    side = (manual.get("side") or "").strip().lower()
                    requested_notional = float(manual.get("notional_usd") or 0)
                    pair_key = pair_map.get(raw_pair)
                    
                    if not pair_key:
                        try: pair_key, _ = resolve_pair_info(k, raw_pair)
                        except: pair_key = None

                    if side not in ("buy", "sell") or not pair_key or pair_key not in pair_keys or requested_notional <= 0:
                         print(f"[manual] Invalid request (pair={raw_pair}); clearing")
                         clear_manual_order()
                    else:
                        kcfg_orders = safe_kcfg_for_orders(kcfg)
                        print(f"[manual] Processing {pair_key} ({raw_pair}) {side} ${requested_notional}")
                        pos = get_position(pair_key)
                        base_override = pos["base_volume"] if side == "sell" else None
                        
                        if side == "sell" and not pos.get("has_position"):
                            print("[manual] No position to sell")
                            clear_manual_order()
                        else:
                            od, m = place_or_preview(k, kcfg_orders, risk, pair_key, side, base_override)
                            print(f"[manual] Result: {od.reason}")
                            if od.reason in ("dry-run", "LIVE order placed"):
                                set_cooldown(pair_key, cd_seconds)
                                log_trade_csv(pair_key, side, od, m, configured_notional)
                            if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                                last = float(m.get("last", 0) or 0)
                                if side == "buy" and last > 0: set_position(pair_key, float(od.volume), last)
                                if side == "sell": clear_position(pair_key)
                            clear_manual_order()

            for pair_key in pair_keys:
                now = int(time.time())
                if get_cooldown_until(pair_key) > now:
                    # Optional: Uncomment if you want cooldown noise
                    # print(f"[{pair_key}] Cooldown active (skip)")
                    continue

                pos = get_position(pair_key)
                closes = fetch_ohlc_closes(k, pair_key, tf)
                if len(closes) < min_c:
                    print(f"[{pair_key}] Not enough data")
                    continue

                sig = decide(closes, sma_s, sma_l, pos["has_position"])
                action = sig["action"]
                
                # --- DETAILED LOGGING RESTORED ---
                if action == "hold":
                    print(f"[{pair_key}] hold -> {sig['reason']}")
                elif is_killed() or is_paused():
                    print(f"[{pair_key}] Paused/Killed (skip)")
                else:
                    kcfg_orders = safe_kcfg_for_orders(kcfg)
                    if action == "buy":
                        od, m = place_or_preview(k, kcfg_orders, risk, pair_key, "buy", None)
                        print(f"[{pair_key}] BUY -> {od.reason}")
                        if od.reason in ("dry-run", "LIVE order placed"):
                            set_cooldown(pair_key, cd_seconds)
                            log_trade_csv(pair_key, "buy", od, m, configured_notional)
                        if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                            last = float(m.get("last", 0) or 0)
                            if last > 0: set_position(pair_key, float(od.volume), last)

                    if action == "sell":
                        od, m = place_or_preview(k, kcfg_orders, risk, pair_key, "sell", pos["base_volume"])
                        print(f"[{pair_key}] SELL -> {od.reason}")
                        if od.reason in ("dry-run", "LIVE order placed"):
                            set_cooldown(pair_key, cd_seconds)
                            log_trade_csv(pair_key, "sell", od, m, configured_notional)
                        if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                            clear_position(pair_key)

            out = ai_call(provider, model, 'Return JSON only: {"status":"ok"}')
            # --- AI LOGGING RESTORED ---
            out_str = json.dumps(out)
            trunc = out_str[:120] + "..." if len(out_str) > 120 else out_str
            print(f"AI response (truncated): {trunc}")

        except Exception as e:
            last_loop_ok = False
            last_error = f"{type(e).__name__}: {e}"
            print("Loop error:", last_error)
            print(traceback.format_exc())

        cooldowns = {}
        now = int(time.time())
        for pk in pair_keys:
            cu = get_cooldown_until(pk)
            cooldowns[pk] = {"remaining_s": int(max(0, (cu or 0) - now))}

        write_bot_status({
            "ts": int(time.time()), "boot_ts": boot_ts, "mode_config": get_trading_mode(kcfg),
            "latch_required": REQUIRE_LIVE_LATCH, "latch_file": LIVE_LATCH_FILE,
            "latch_present": live_latch_present(), "live_allowed": allow_live(kcfg),
            "paused": is_paused(), "killed": is_killed(),
            "pairs": list(pair_keys), "cooldowns": cooldowns,
            "last_loop_ok": last_loop_ok, "last_error": last_error,
        })

        time.sleep(poll)

if __name__ == "__main__":
    main()