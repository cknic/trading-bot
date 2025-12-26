import csv
import os
import time
from typing import Dict, Any, List, Tuple

TRADES_DEFAULT = "/data/trades.csv"
OUT_DEFAULT = "/data/pnl.json"


def _read_trades(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append(
                    {
                        "ts": int(row["ts"]),
                        "pair": row["pair"],
                        "side": row["side"],
                        "volume": float(row["volume"]),
                        "price": float(row["price"]),
                        "notional_usd": float(row["notional_usd"]),
                        "mode": row.get("mode", ""),
                    }
                )
            except Exception:
                continue
    rows.sort(key=lambda x: x["ts"])
    return rows


def _pair_round(x: float) -> float:
    # Keep numbers readable in JSON
    return float(f"{x:.6f}")


def compute_pnl(
    trades: List[Dict[str, Any]],
    marks: Dict[str, float],
) -> Dict[str, Any]:
    """
    Assumptions (matches current bot):
    - one position max per pair
    - buys open position, sells close position
    - position volume on sell matches open volume (or close enough)
    """
    per_pair: Dict[str, Any] = {}
    portfolio = {
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "net_pnl_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "trades_closed": 0,
        "win_rate": 0.0,
        "max_drawdown_usd": 0.0,
    }

    # Build per-pair state machines
    open_pos: Dict[str, Dict[str, float]] = {}  # pair -> {vol, entry_px, entry_ts, cost_usd}
    equity_points: List[Tuple[int, float]] = []  # (ts, realized_equity)

    realized_equity = 0.0
    peak_equity = 0.0
    max_dd = 0.0

    for t in trades:
        pair = t["pair"]
        side = t["side"]
        vol = t["volume"]
        px = t["price"]
        ts = t["ts"]

        if pair not in per_pair:
            per_pair[pair] = {
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "net_pnl_usd": 0.0,
                "wins": 0,
                "losses": 0,
                "trades_closed": 0,
                "win_rate": 0.0,
                "open_position": False,
                "open_volume": 0.0,
                "entry_price": 0.0,
                "mark_price": marks.get(pair, 0.0),
                "last_event_ts": 0,
            }

        per_pair[pair]["last_event_ts"] = ts

        if side == "buy":
            # Open if none; if one is open already, ignore (bot shouldn't do this, but safe)
            if pair not in open_pos or open_pos[pair].get("vol", 0.0) <= 0:
                open_pos[pair] = {"vol": vol, "entry_px": px, "entry_ts": float(ts), "cost_usd": vol * px}
                per_pair[pair]["open_position"] = True
                per_pair[pair]["open_volume"] = vol
                per_pair[pair]["entry_price"] = px

        elif side == "sell":
            # Close if open
            if pair in open_pos and open_pos[pair].get("vol", 0.0) > 0:
                entry_px = open_pos[pair]["entry_px"]
                entry_vol = open_pos[pair]["vol"]
                close_vol = min(entry_vol, vol) if vol > 0 else entry_vol

                pnl = (px - entry_px) * close_vol

                per_pair[pair]["realized_pnl_usd"] += pnl
                per_pair[pair]["trades_closed"] += 1

                portfolio["realized_pnl_usd"] += pnl
                portfolio["trades_closed"] += 1

                if pnl >= 0:
                    per_pair[pair]["wins"] += 1
                    portfolio["wins"] += 1
                else:
                    per_pair[pair]["losses"] += 1
                    portfolio["losses"] += 1

                # Clear position (single-lot model)
                open_pos[pair] = {"vol": 0.0, "entry_px": 0.0, "entry_ts": 0.0, "cost_usd": 0.0}
                per_pair[pair]["open_position"] = False
                per_pair[pair]["open_volume"] = 0.0
                per_pair[pair]["entry_price"] = 0.0

                # Equity curve (realized only)
                realized_equity += pnl
                equity_points.append((ts, realized_equity))
                peak_equity = max(peak_equity, realized_equity)
                dd = peak_equity - realized_equity
                max_dd = max(max_dd, dd)

    # Unrealized (from open positions + marks)
    for pair, pos in open_pos.items():
        vol = pos.get("vol", 0.0)
        entry_px = pos.get("entry_px", 0.0)
        mark = marks.get(pair, 0.0)
        if vol > 0 and entry_px > 0 and mark > 0:
            u = (mark - entry_px) * vol
            per_pair[pair]["unrealized_pnl_usd"] = u
            per_pair[pair]["open_position"] = True
            per_pair[pair]["open_volume"] = vol
            per_pair[pair]["entry_price"] = entry_px
            per_pair[pair]["mark_price"] = mark

            portfolio["unrealized_pnl_usd"] += u

    # Final rollups
    for pair, st in per_pair.items():
        st["net_pnl_usd"] = st["realized_pnl_usd"] + st["unrealized_pnl_usd"]
        tc = st["trades_closed"]
        st["win_rate"] = (st["wins"] / tc) if tc > 0 else 0.0

        # pretty numbers
        for k in ("realized_pnl_usd", "unrealized_pnl_usd", "net_pnl_usd", "win_rate", "mark_price", "entry_price", "open_volume"):
            st[k] = _pair_round(st[k])

    portfolio["net_pnl_usd"] = portfolio["realized_pnl_usd"] + portfolio["unrealized_pnl_usd"]
    portfolio["win_rate"] = (portfolio["wins"] / portfolio["trades_closed"]) if portfolio["trades_closed"] > 0 else 0.0
    portfolio["max_drawdown_usd"] = max_dd

    for k in ("realized_pnl_usd", "unrealized_pnl_usd", "net_pnl_usd", "win_rate", "max_drawdown_usd"):
        portfolio[k] = _pair_round(portfolio[k])

    return {
        "ts": int(time.time()),
        "portfolio": portfolio,
        "pairs": per_pair,
        "equity_curve_realized": equity_points[-200:],  # keep last 200 points
    }


def write_pnl_json(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    out = cfg.get("pnl", {}).get("summary_path", OUT_DEFAULT)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import json
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)


def compute_and_write(cfg: Dict[str, Any], marks: Dict[str, float]) -> Dict[str, Any]:
    trades_path = cfg.get("pnl", {}).get("csv_path", TRADES_DEFAULT)
    trades = _read_trades(trades_path)
    payload = compute_pnl(trades, marks)
    write_pnl_json(cfg, payload)
    return payload