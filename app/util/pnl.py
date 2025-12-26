import json
import os
import time
from typing import Dict, Any

def _append_csv(path: str, row: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a") as f:
        if new_file:
            f.write("ts,pair,side,volume,price,notional_usd,mode\n")
        f.write(row + "\n")

def record_trade(cfg: Dict[str, Any], pair: str, side: str, volume: float, price: float, notional: float, mode: str) -> None:
    pnl_cfg = cfg.get("pnl", {})
    if not pnl_cfg.get("write_csv", True):
        return
    path = pnl_cfg.get("csv_path", "/data/trades.csv")
    ts = int(time.time())
    _append_csv(path, f"{ts},{pair},{side},{volume:.10f},{price:.8f},{notional:.4f},{mode}")

def write_summary(cfg: Dict[str, Any], summary: Dict[str, Any]) -> None:
    pnl_cfg = cfg.get("pnl", {})
    path = pnl_cfg.get("summary_path", "/data/pnl.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)