import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class RiskDecision:
    allowed: bool
    reason: str


class RiskEngine:
    """
    Enforces:
      - pause/kill file controls
      - max trades/day (global and per pair)
      - max_notional_usd_per_trade (per trade)
      - circuit breakers based on portfolio realized PnL (USD) and max drawdown (USD)

    NOTE:
      - circuit breaker inputs come from pnl_analytics output (pnl.json), which is USD-based.
      - pause is "soft stop": no trades, but bot continues to run/log/update analytics.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

        self.fail_closed = bool(cfg.get("safety", {}).get("fail_closed", True))

        # Controls
        controls = cfg.get("controls", {})
        self.pause_file = controls.get("pause_file", "/run/trading/PAUSE")
        self.kill_file = controls.get("kill_switch_file", "/run/trading/KILL_SWITCH")

        # Trade limits
        trade = cfg.get("trade", {})
        self.max_notional_usd_per_trade = float(trade.get("max_notional_usd_per_trade", 20.0))
        self.max_trades_per_day = int(trade.get("max_trades_per_day", 3))
        self.max_trades_per_day_per_pair = int(trade.get("max_trades_per_day_per_pair", self.max_trades_per_day))

        # Circuit breakers (USD-based, sourced from pnl.json)
        account = cfg.get("account", {})
        self.max_daily_loss_usd = float(account.get("max_daily_loss_usd", 0.0))
        self.max_drawdown_usd = float(account.get("max_drawdown_usd", 0.0))

        # Daily counters (UTC)
        self.day_key = self._utc_day_key()
        self.trades_today = 0
        self.trades_today_by_pair: Dict[str, int] = {}

        # Latest portfolio metrics (fed from pnl_analytics)
        self.portfolio_realized = 0.0
        self.portfolio_max_dd = 0.0

        # Sticky pause reason (also mirrors to pause_file)
        self.pause_reason: Optional[str] = None

    def _utc_day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_day_if_needed(self):
        dk = self._utc_day_key()
        if dk != self.day_key:
            self.day_key = dk
            self.trades_today = 0
            self.trades_today_by_pair = {}

    # --- Control state ---
    def kill_switch_active(self) -> bool:
        return os.path.exists(self.kill_file)

    def paused(self) -> bool:
        return os.path.exists(self.pause_file) or (self.pause_reason is not None)

    def get_pause_reason(self) -> str:
        if self.pause_reason:
            return self.pause_reason
        if os.path.exists(self.pause_file):
            return f"pause file present: {self.pause_file}"
        return ""

    def _touch_pause(self, reason: str):
        self.pause_reason = reason
        try:
            os.makedirs(os.path.dirname(self.pause_file), exist_ok=True)
            with open(self.pause_file, "w") as f:
                f.write(reason + "\n")
        except Exception:
            # If we cannot write pause file, we still keep pause_reason in memory.
            pass

    # --- Circuit breakers ---
    def update_portfolio_metrics(self, realized_pnl_usd: float, max_drawdown_usd: float):
        """
        Called from main loop after pnl_analytics runs.
        If circuit breakers are hit, trading is paused.
        """
        self.portfolio_realized = float(realized_pnl_usd)
        self.portfolio_max_dd = float(max_drawdown_usd)

        if self.max_daily_loss_usd > 0 and self.portfolio_realized <= -self.max_daily_loss_usd:
            self._touch_pause(
                f"circuit breaker: realized pnl {self.portfolio_realized:.6f} <= -{self.max_daily_loss_usd:.6f}"
            )
            return

        if self.max_drawdown_usd > 0 and self.portfolio_max_dd >= self.max_drawdown_usd:
            self._touch_pause(
                f"circuit breaker: max drawdown {self.portfolio_max_dd:.6f} >= {self.max_drawdown_usd:.6f}"
            )
            return

    # --- Core gating ---
    def can_trade(self, notional_usd: float, mode: str, pair: Optional[str] = None) -> RiskDecision:
        """
        Used by exchange layer before placing/previewing.
        This MUST be conservative (block if uncertain).
        """
        self._roll_day_if_needed()

        if self.kill_switch_active():
            return RiskDecision(False, "kill switch active")

        if self.paused():
            return RiskDecision(False, f"paused: {self.get_pause_reason()}")

        if notional_usd > self.max_notional_usd_per_trade:
            return RiskDecision(False, f"max_notional_usd_per_trade exceeded ({notional_usd:.2f} > {self.max_notional_usd_per_trade:.2f})")

        # Trade caps apply to both dry_run and live
        if self.trades_today >= self.max_trades_per_day:
            return RiskDecision(False, f"max trades/day reached ({self.trades_today}/{self.max_trades_per_day})")

        if pair:
            pt = self.trades_today_by_pair.get(pair, 0)
            if pt >= self.max_trades_per_day_per_pair:
                return RiskDecision(False, f"max trades/day per pair reached ({pair}: {pt}/{self.max_trades_per_day_per_pair})")

        return RiskDecision(True, "ok")

    def record_trade(self, pair: Optional[str] = None):
        """
        Call ONLY when a trade is considered "executed" (dry-run filled, or live placed).
        """
        self._roll_day_if_needed()
        self.trades_today += 1
        if pair:
            self.trades_today_by_pair[pair] = self.trades_today_by_pair.get(pair, 0) + 1