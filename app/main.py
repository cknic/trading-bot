import os, time, json, copy, traceback
import yaml
import requests

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

def is_paused():
    return os.path.exists(PAUSE_FILE)

def is_killed():
    return os.path.exists(KILL_FILE)

def live_latch_present() -> bool:
    return os.path.exists(LIVE_LATCH_FILE)

def get_trading_mode(kcfg) -> str:
    return (kcfg.get("trading", {}).get("mode", "") or "").strip().lower()

def allow_live(kcfg) -> bool:
    # live only allowed if mode is live AND latch exists (if required) AND not killed
    if get_trading_mode(kcfg) != "live":
        return False
    if is_killed():
        return False
    if REQUIRE_LIVE_LATCH and (not live_latch_present()):
        return False
    return True

def safe_kcfg_for_orders(kcfg):
    """
    Critical safety: if config says live but latch/kill blocks it,
    force dry_run before calling place_or_preview.
    """
    mode = get_trading_mode(kcfg)
    if mode != "live":
        return kcfg

    if allow_live(kcfg):
        return kcfg

    tmp = copy.deepcopy(kcfg)
    tmp.setdefault("trading", {})
    tmp["trading"]["mode"] = "dry_run"
    return tmp

def try_read_manual_order():
    try:
        if not os.path.exists(MANUAL_ORDER_PATH):
            return None
        with open(MANUAL_ORDER_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None

def clear_manual_order():
    try:
        os.remove(MANUAL_ORDER_PATH)
    except FileNotFoundError:
        pass

def log_trade_csv(pair_key: str, side: str, od, m, notional_usd: float):
    ts = int(time.time())
    px = m.get("last") or od.price or ""
    append_trade(
        ts=ts,
        pair=pair_key,
        side=side,
        volume=str(od.volume),
        price=str(px),
        notional_usd=float(notional_usd),
        mode=str(od.mode),
    )

def write_bot_status(status: dict):
    """
    Writes /data/bot_status.json (atomic).
    """
    try:
        os.makedirs(os.path.dirname(BOT_STATUS_PATH), exist_ok=True)
        tmp = BOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2, sort_keys=True)
        os.replace(tmp, BOT_STATUS_PATH)
    except Exception:
        # Don't crash bot for status writes
        pass

def main():
    boot_ts = int(time.time())
    last_loop_ok = True
    last_error = ""

    risk_cfg = load_yaml("/config/risk.yaml")
    ai_cfg = load_yaml("/config/ai.yaml")
    kcfg = load_yaml("/config/kraken.yaml")

    provider = ai_cfg["provider"]
    model = ai_cfg[provider]["model"]

    print("Bot starting. fail_closed =", risk_cfg["safety"]["fail_closed"])
    print("AI provider =", provider, "model =", model)

    mode = get_trading_mode(kcfg)
    print(f"Trading mode = {mode}")
    print(f"Live latch required = {REQUIRE_LIVE_LATCH} file={LIVE_LATCH_FILE} present={live_latch_present()}")

    if mode == "live" and not allow_live(kcfg):
        why = []
        if is_killed():
            why.append("KILL_SWITCH present")
        if REQUIRE_LIVE_LATCH and not live_latch_present():
            why.append("LIVE_LATCH missing")
        print("LIVE requested but NOT allowed -> forcing dry_run for all orders. Reasons:", ", ".join(why) or "unknown")

    # write initial status early (even before kraken test)
    write_bot_status({
        "ts": int(time.time()),
        "boot_ts": boot_ts,
        "mode_config": mode,
        "latch_required": REQUIRE_LIVE_LATCH,
        "latch_file": LIVE_LATCH_FILE,
        "latch_present": live_latch_present(),
        "live_allowed": allow_live(kcfg),
        "paused": is_paused(),
        "killed": is_killed(),
        "last_loop_ok": last_loop_ok,
        "last_error": last_error,
        "note": "boot"
    })

    kraken_test_main()
    print("Kraken test: OK")

    risk = RiskEngine(risk_cfg)
    k = KrakenClient(
        api_key=os.environ["KRAKEN_API_KEY"],
        api_secret=os.environ["KRAKEN_API_SECRET"],
        base_url=kcfg["kraken"]["base_url"],
    )

    configured_pairs = kcfg["kraken"]["pairs"]
    pair_keys = []
    for p in configured_pairs:
        pk, _ = resolve_pair_info(k, p)
        pair_keys.append(pk)
    print("Trading pairs (normalized):", pair_keys)

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
            # ---- Manual order consume ----
            manual = try_read_manual_order()
            if manual:
                if is_killed():
                    print("[manual] Ignoring manual order: KILL_SWITCH is enabled")
                    clear_manual_order()
                elif is_paused():
                    print("[manual] Manual order queued but bot is paused (will retry)")
                else:
                    pair_key = (manual.get("pair") or "").strip()
                    side = (manual.get("side") or "").strip().lower()
                    requested_notional = float(manual.get("notional_usd") or 0)

                    if side not in ("buy", "sell"):
                        print("[manual] Invalid side; clearing request")
                        clear_manual_order()
                    elif pair_key not in pair_keys:
                        print(f"[manual] Pair {pair_key} not configured; clearing request")
                        clear_manual_order()
                    elif requested_notional <= 0:
                        print("[manual] Invalid notional; clearing request")
                        clear_manual_order()
                    else:
                        mode = get_trading_mode(kcfg)
                        if mode == "live" and not allow_live(kcfg):
                            print("[manual] LIVE requested but latch/kill blocks it -> forcing dry_run")
                        kcfg_orders = safe_kcfg_for_orders(kcfg)

                        print(f"[manual] Processing manual order: pair={pair_key} side={side} requested_notional=${requested_notional:.2f} (using_config=${configured_notional:.2f})")

                        pos = get_position(pair_key)
                        if side == "sell" and not pos.get("has_position"):
                            print(f"[manual] Refusing SELL: no open position for {pair_key}")
                            clear_manual_order()
                        else:
                            base_override = pos["base_volume"] if side == "sell" else None

                            od, m = place_or_preview(
                                k, kcfg_orders, risk,
                                pair_key=pair_key,
                                side=side,
                                base_volume_override=base_override,
                            )
                            print(f"[manual] {side.upper()} | {od.reason} vol={od.volume} limit={od.price} spread%={m.get('spread_pct')} slip%={m.get('slippage_pct')}")

                            if od.reason in ("dry-run", "LIVE order placed"):
                                until = set_cooldown(pair_key, cd_seconds)
                                print(f"[manual] Cooldown set until {until} (unix)")
                                log_trade_csv(pair_key, side, od, m, configured_notional)

                            if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                                last = float(m.get("last", 0) or 0)
                                if side == "buy" and last > 0:
                                    set_position(pair_key, float(od.volume), last)
                                    print(f"[manual] Ledger: simulated BUY base_volume={od.volume} entry_price={last}")
                                if side == "sell":
                                    clear_position(pair_key)
                                    print(f"[manual] Ledger: simulated SELL (position cleared)")

                            clear_manual_order()

            # ---- Strategy loop ----
            for pair_key in pair_keys:
                now = int(time.time())
                cooldown_until = get_cooldown_until(pair_key)
                if cooldown_until > now:
                    remaining = cooldown_until - now
                    print(f"[{pair_key}] Cooldown active: {remaining}s remaining (skip)")
                    continue

                pos = get_position(pair_key)
                closes = fetch_ohlc_closes(k, pair_key, tf)
                if len(closes) < min_c:
                    print(f"[{pair_key}] hold (need {min_c} candles, have {len(closes)})")
                    continue

                sig = decide(closes, sma_s, sma_l, pos["has_position"])
                action = sig["action"]
                reason = sig["reason"]

                if action == "hold":
                    print(f"[{pair_key}] hold -> {reason}")
                    continue

                if is_killed():
                    print(f"[{pair_key}] kill switch active -> skip trading actions")
                    continue
                if is_paused():
                    print(f"[{pair_key}] paused -> skip trading actions")
                    continue

                mode = get_trading_mode(kcfg)
                if mode == "live" and not allow_live(kcfg):
                    print(f"[{pair_key}] LIVE requested but latch/kill blocks it -> forcing dry_run")
                kcfg_orders = safe_kcfg_for_orders(kcfg)

                if action == "buy":
                    od, m = place_or_preview(k, kcfg_orders, risk, pair_key=pair_key, side="buy", base_volume_override=None)
                    print(f"[{pair_key}] BUY -> {reason} | {od.reason} vol={od.volume} limit={od.price} spread%={m.get('spread_pct')} slip%={m.get('slippage_pct')}")
                    if od.reason in ("dry-run", "LIVE order placed"):
                        until = set_cooldown(pair_key, cd_seconds)
                        print(f"[{pair_key}] Cooldown set until {until} (unix)")
                        log_trade_csv(pair_key, "buy", od, m, configured_notional)
                    if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                        last = float(m.get("last", 0) or 0)
                        if last > 0:
                            set_position(pair_key, float(od.volume), last)
                            print(f"[{pair_key}] Ledger: simulated BUY base_volume={od.volume} entry_price={last}")

                if action == "sell":
                    od, m = place_or_preview(k, kcfg_orders, risk, pair_key=pair_key, side="sell", base_volume_override=pos["base_volume"])
                    print(f"[{pair_key}] SELL -> {reason} | {od.reason} vol={od.volume} limit={od.price} spread%={m.get('spread_pct')} slip%={m.get('slippage_pct')}")
                    if od.reason in ("dry-run", "LIVE order placed"):
                        until = set_cooldown(pair_key, cd_seconds)
                        print(f"[{pair_key}] Cooldown set until {until} (unix)")
                        log_trade_csv(pair_key, "sell", od, m, configured_notional)
                    if (od.mode != "live") and simulate and (od.reason == "dry-run"):
                        clear_position(pair_key)
                        print(f"[{pair_key}] Ledger: simulated SELL (position cleared)")

            out = ai_call(provider, model, 'Return JSON only: {"status":"ok"}')
            print("AI response (truncated):", json.dumps(out)[:120])

        except Exception as e:
            last_loop_ok = False
            last_error = f"{type(e).__name__}: {e}"
            print("Loop error:", last_error)
            print(traceback.format_exc())

        # ---- Write status every loop ----
        cooldowns = {}
        now = int(time.time())
        for pk in pair_keys:
            cu = get_cooldown_until(pk)
            cooldowns[pk] = {
                "cooldown_until": int(cu or 0),
                "remaining_s": int(max(0, (cu or 0) - now)),
            }

        mode = get_trading_mode(kcfg)
        write_bot_status({
            "ts": int(time.time()),
            "boot_ts": boot_ts,
            "mode_config": mode,
            "latch_required": REQUIRE_LIVE_LATCH,
            "latch_file": LIVE_LATCH_FILE,
            "latch_present": live_latch_present(),
            "live_allowed": allow_live(kcfg),
            "paused": is_paused(),
            "killed": is_killed(),
            "pairs": list(pair_keys),
            "cooldowns": cooldowns,
            "last_loop_ok": last_loop_ok,
            "last_error": last_error,
        })

        time.sleep(poll)

if __name__ == "__main__":
    main()