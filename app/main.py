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
PNL_JSON = os.path.join(DATA_DIR, "pnl.json")
BOT_STATUS_PATH = os.environ.get("BOT_STATUS_PATH", os.path.join(DATA_DIR, "bot_status.json"))

# ----------------------------
# Helpers
# ----------------------------
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
    if get_trading_mode(kcfg) != "live":
        return False
    if is_killed():
        return False
    if REQUIRE_LIVE_LATCH and not live_latch_present():
        return False
    return True

def safe_kcfg_for_orders(kcfg):
    if get_trading_mode(kcfg) != "live":
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
    try:
        os.makedirs(os.path.dirname(BOT_STATUS_PATH), exist_ok=True)
        tmp = BOT_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2, sort_keys=True)
        os.replace(tmp, BOT_STATUS_PATH)
    except Exception:
        pass

# ----------------------------
# Main
# ----------------------------
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
        print("LIVE requested but NOT allowed -> forcing dry_run")

    kraken_test_main()
    print("Kraken test: OK")

    risk = RiskEngine(risk_cfg)

    k = KrakenClient(
        api_key=os.environ["KRAKEN_API_KEY"],
        api_secret=os.environ["KRAKEN_API_SECRET"],
        base_url=kcfg["kraken"]["base_url"],
    )

    pair_keys = []
    for p in kcfg["kraken"]["pairs"]:
        pk, _ = resolve_pair_info(k, p)
        pair_keys.append(pk)

    poll = int(kcfg["trading"].get("poll_seconds", 60))
    tf = int(kcfg["strategy"]["timeframe_minutes"])
    sma_s = int(kcfg["strategy"]["sma_short"])
    sma_l = int(kcfg["strategy"]["sma_long"])
    min_c = int(kcfg["strategy"]["min_candles"])
    simulate = bool(kcfg["strategy"].get("simulate_fills_in_dry_run", True))

    cd_seconds = int(kcfg.get("cooldown", {}).get("hours_after_trade", 4)) * 3600
    configured_notional = float(kcfg.get("trading", {}).get("quote_notional_usd", 20.0))

    while True:
        loop_ts = int(time.time())
        last_loop_ok = True
        last_error = ""

        try:
            # -------------------------------------------------
            # CRITICAL: Update Risk Engine from pnl.json
            # -------------------------------------------------
            try:
                with open(PNL_JSON, "r") as f:
                    p = json.load(f) or {}
                port = p.get("portfolio") or {}

                risk.update_portfolio_metrics(
                    realized_pnl_usd=float(port.get("realized_pnl_usd") or 0.0),
                    max_drawdown_usd=float(port.get("max_drawdown_usd") or 0.0),
                )
            except Exception as e:
                if risk.fail_closed:
                    risk._touch_pause(f"risk input failure: {e}")

            # -------------------------------------------------
            # Strategy loop
            # -------------------------------------------------
            for pair_key in pair_keys:
                now = int(time.time())
                cooldown_until = get_cooldown_until(pair_key)
                if cooldown_until > now:
                    continue

                pos = get_position(pair_key)
                closes = fetch_ohlc_closes(k, pair_key, tf)
                if len(closes) < min_c:
                    continue

                sig = decide(closes, sma_s, sma_l, pos["has_position"])
                action = sig["action"]

                if action == "hold" or is_paused() or is_killed():
                    continue

                kcfg_orders = safe_kcfg_for_orders(kcfg)

                if action == "buy":
                    od, m = place_or_preview(k, kcfg_orders, risk, pair_key, "buy", None)
                    if od.reason in ("dry-run", "LIVE order placed"):
                        set_cooldown(pair_key, cd_seconds)
                        log_trade_csv(pair_key, "buy", od, m, configured_notional)
                        if simulate and od.mode != "live":
                            last = float(m.get("last") or 0)
                            if last > 0:
                                set_position(pair_key, float(od.volume), last)

                if action == "sell":
                    od, m = place_or_preview(k, kcfg_orders, risk, pair_key, "sell", pos["base_volume"])
                    if od.reason in ("dry-run", "LIVE order placed"):
                        set_cooldown(pair_key, cd_seconds)
                        log_trade_csv(pair_key, "sell", od, m, configured_notional)
                        if simulate and od.mode != "live":
                            clear_position(pair_key)

            out = ai_call(provider, model, 'Return JSON only: {"status":"ok"}')
            print("AI response (truncated):", json.dumps(out)[:120])

        except Exception as e:
            last_loop_ok = False
            last_error = f"{type(e).__name__}: {e}"
            print("Loop error:", last_error)
            print(traceback.format_exc())

        write_bot_status({
            "ts": int(time.time()),
            "boot_ts": boot_ts,
            "mode_requested": get_trading_mode(kcfg),
            "mode_effective": "live" if allow_live(kcfg) else "dry_run",
            "require_live_latch": REQUIRE_LIVE_LATCH,
            "live_latch_present": live_latch_present(),
            "live_latch_file": LIVE_LATCH_FILE,
            "allow_live": allow_live(kcfg),
            "paused": is_paused(),
            "killed": is_killed(),
            "last_loop_ok": last_loop_ok,
            "last_error": last_error,
        })

        time.sleep(poll)

if __name__ == "__main__":
    main()