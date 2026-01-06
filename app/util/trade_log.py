import os
import csv
from pathlib import Path

PATH = os.environ.get("TRADES_CSV_PATH", "/data/trades.csv")
HEADER = ["ts", "pair", "side", "volume", "price", "notional_usd", "mode", "reason"]


def ensure():
    p = Path(PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        with p.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


def append_trade(ts: int, pair: str, side: str, volume, price, notional_usd: float, mode: str, reason: str = ""):
    """
    Append trade to CSV and send webhook alert (non-blocking).
    
    Args:
        ts: Unix timestamp
        pair: Trading pair/symbol
        side: "buy" or "sell"
        volume: Trade volume
        price: Execution price
        notional_usd: Total USD value
        mode: "live", "paper", "dry_run"
        reason: Trade reason (optional, for alerts)
    """
    ensure()
    
    # Write to CSV (this is fast, keep blocking)
    with open(PATH, "a", newline="") as f:
        csv.writer(f).writerow([
            str(ts), pair, side, str(volume), str(price),
            f"{float(notional_usd):.4f}", mode, reason
        ])
    
    # Send webhook alert (non-blocking)
    try:
        from util.webhook import alert_trade
        alert_trade(
            pair=pair,
            side=side,
            volume=float(volume),
            price=float(price),
            notional=float(notional_usd),
            reason=reason or "Manual/Unknown",
            mode=mode
        )
    except ImportError:
        pass  # webhook module not available
    except Exception as e:
        print(f"Webhook alert failed: {e}")