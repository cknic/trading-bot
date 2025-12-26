# app/util/equity_log.py
import os, csv
from pathlib import Path

PATH = os.environ.get("EQUITY_CSV_PATH", "/data/equity.csv")

def append_point(ts: int, net_pnl_usd: float):
    p = Path(PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "net_pnl_usd"])
        w.writerow([str(ts), f"{float(net_pnl_usd):.6f}"])