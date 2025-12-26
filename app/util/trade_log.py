import os, csv
from pathlib import Path

PATH = os.environ.get("TRADES_CSV_PATH", "/data/trades.csv")
HEADER = ["ts","pair","side","volume","price","notional_usd","mode"]

def ensure():
    p = Path(PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        with p.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)

def append_trade(ts:int, pair:str, side:str, volume:str, price:str, notional_usd:float, mode:str):
    ensure()
    with open(PATH, "a", newline="") as f:
        csv.writer(f).writerow([
            str(ts), pair, side, str(volume), str(price),
            f"{float(notional_usd):.4f}", mode
        ])
