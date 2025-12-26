import os
from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional

from app.exchange.kraken_client import KrakenClient
from app.risk.risk_engine import RiskEngine


@dataclass
class OrderDecision:
    pair: str
    side: str
    ordertype: str
    volume: str
    price: Optional[str]
    mode: str
    notional_usd: float
    reason: str


def _live_latch_enabled() -> bool:
    return os.path.exists("/run/trading/ENABLE_LIVE_TRADING")


def resolve_pair_info(k: KrakenClient, pair: str) -> Tuple[str, Dict[str, Any]]:
    ap = k.public("AssetPairs", {"pair": pair})
    if ap.get("error"):
        raise RuntimeError(f"AssetPairs error: {ap['error']}")
    result = ap["result"]
    key = next(iter(result.keys()))
    return key, result[key]


def get_ticker(k: KrakenClient, pair: str) -> Dict[str, Any]:
    t = k.public("Ticker", {"pair": pair})
    if t.get("error"):
        raise RuntimeError(f"Ticker error: {t['error']}")
    result = t["result"]
    key = next(iter(result.keys()))
    return result[key]


def _pct(x: float) -> float:
    return x * 100.0


def _format_dec(val: float, decimals: int) -> str:
    fmt = "{:0." + str(decimals) + "f}"
    return fmt.format(val)


def _format_volume(vol: float, lot_decimals: int) -> str:
    fmt = "{:0." + str(lot_decimals) + "f}"
    return fmt.format(vol)


def _calc_spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 999.0
    return _pct((ask - bid) / mid)


def _slippage_pct(ref: float, current: float) -> float:
    if ref <= 0:
        return 999.0
    return _pct(abs(current - ref) / ref)


def build_order(
    k: KrakenClient,
    cfg: Dict[str, Any],
    pair_key: str,
    side: str,
    base_volume_override: Optional[float],
) -> Tuple[OrderDecision, Dict[str, Any]]:
    mode = cfg["trading"]["mode"]
    ordertype = cfg["trading"].get("order_type", "limit")
    quote = float(cfg["trading"]["quote_notional_usd"])

    max_spread = float(cfg["safety"]["max_spread_pct"])
    max_slip = float(cfg["safety"]["max_slippage_pct"])
    limit_off = float(cfg["safety"]["limit_offset_pct"])

    _, pair_info = resolve_pair_info(k, pair_key)
    ticker = get_ticker(k, pair_key)

    bid = float(ticker["b"][0])
    ask = float(ticker["a"][0])
    last = float(ticker["c"][0])

    spread = _calc_spread_pct(bid, ask)
    mid = (bid + ask) / 2.0
    slip = _slippage_pct(mid, last)

    metrics = {
        "bid": bid,
        "ask": ask,
        "last": last,
        "mid": mid,
        "spread_pct": spread,
        "slippage_pct": slip,
    }

    if spread > max_spread:
        return (
            OrderDecision(
                pair_key,
                side,
                ordertype,
                "0",
                None,
                mode,
                quote,
                f"blocked: spread {spread:.6f}% > max {max_spread:.6f}%",
            ),
            metrics,
        )

    if slip > max_slip:
        return (
            OrderDecision(
                pair_key,
                side,
                ordertype,
                "0",
                None,
                mode,
                quote,
                f"blocked: slippage {slip:.6f}% > max {max_slip:.6f}%",
            ),
            metrics,
        )

    lot_decimals = int(pair_info.get("lot_decimals", 8))
    cost_decimals = int(pair_info.get("cost_decimals", 2))

    if side == "buy":
        if last <= 0:
            return (
                OrderDecision(pair_key, side, ordertype, "0", None, mode, quote, "blocked: invalid last"),
                metrics,
            )

        vol = quote / last
        vol_str = _format_volume(vol, lot_decimals)
        notional = quote

    else:
        if not base_volume_override or base_volume_override <= 0:
            return (
                OrderDecision(pair_key, side, ordertype, "0", None, mode, 0.0, "blocked: no position volume"),
                metrics,
            )

        vol_str = _format_volume(float(base_volume_override), lot_decimals)
        notional = float(base_volume_override) * last

    ordermin = pair_info.get("ordermin")
    if ordermin is not None:
        try:
            if float(vol_str) < float(ordermin):
                return (
                    OrderDecision(
                        pair_key,
                        side,
                        ordertype,
                        vol_str,
                        None,
                        mode,
                        notional,
                        f"blocked: below ordermin ({ordermin})",
                    ),
                    metrics,
                )
        except Exception:
            pass

    price_str = None
    if ordertype == "limit":
        if side == "buy":
            px = ask * (1.0 + (limit_off / 100.0))
        else:
            px = bid * (1.0 - (limit_off / 100.0))
        price_str = _format_dec(px, cost_decimals)

    return (
        OrderDecision(pair_key, side, ordertype, vol_str, price_str, mode, float(notional), "ok"),
        metrics,
    )


def place_or_preview(
    k: KrakenClient,
    cfg: Dict[str, Any],
    risk: RiskEngine,
    pair_key: str,
    side: str,
    base_volume_override: Optional[float],
) -> Tuple[OrderDecision, Dict[str, Any]]:
    od, metrics = build_order(k, cfg, pair_key, side, base_volume_override)
    if od.reason != "ok":
        return od, metrics

    # IMPORTANT: pass pair so per-pair caps apply
    rd = risk.can_trade(notional_usd=od.notional_usd, mode=od.mode, pair=pair_key)
    if not rd.allowed:
        od.reason = f"blocked by risk: {rd.reason}"
        return od, metrics

    if od.mode != "live":
        od.reason = "dry-run"
        return od, metrics

    # Live protections
    if not _live_latch_enabled():
        od.reason = "blocked: live latch not enabled (/run/trading/ENABLE_LIVE_TRADING)"
        return od, metrics

    payload = {"pair": od.pair, "type": od.side, "ordertype": od.ordertype, "volume": od.volume}
    if od.ordertype == "limit":
        payload["price"] = od.price

    resp = k.private("AddOrder", payload)
    if resp.get("error"):
        od.reason = f"AddOrder error: {resp['error']}"
        return od, metrics

    # Count the trade as executed for rate limiting
    risk.record_trade(pair_key)

    od.reason = "LIVE order placed"
    return od, metrics