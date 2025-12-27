import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    # For UI / events: "block" (hard reject), "alert" (blocked but should alert)
    level: str = "block"


class RiskEngine:
    """
    Enforces:
      - pause/kill file controls
      - max trades/day (global and per pair)  [C: block+alert, no auto-pause]
      - max_notional_usd_per_trade (per trade) [A]
      - max_open_positions (portfolio-level)   [A]
      - circuit breakers based on portfolio realized PnL and drawdown (USD-based) [A -> auto-pause]
      - optional pct-based circuit breaker support if you supply an equity base

    NOTE:
      - portfolio metrics are fed from pnl_analytics output (pnl.json), USD-based today.
      - pause is "soft stop": no trades, bot keeps running/logging.
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
        self.max_open_positions = int(trade.get("max_open_positions", 0))  # 0 = disabled
        self.max_notional_usd_per_trade = float(trade.get("max_notional_usd_per_trade", 20.0))

        # Trades/day caps
        self.max_trades_per_day = int(trade.get("max_trades_per_day", 3))
        self.max_trades_per_day_per_pair = int(
            trade.get("max_trades_per_day_per_pair", self.max_trades_per_day)
        )

        # Choice C behavior: DO NOT count/enforce trades/day in dry-run by default
        self.count_trades_in_dry_run = bool(trade.get("count_trades_in_dry_run", False))

        # Circuit breakers (USD-based)
        account = cfg.get("account", {})
        self.max_daily_loss_usd = float(account.get("max_daily_loss_usd", 0.0))
        self.max_drawdown_usd = float(account.get("max_drawdown_usd", 0.0))

        # Optional pct-based breakers (only active if equity_base_usd > 0)
        self.max_daily_loss_pct = float(account.get("max_daily_loss_pct", 0.0))
        self.max_drawdown_pct = float(account.get("max_drawdown_pct", 0.0))
        self.equity_base_usd = float(account.get("equity_base_usd", 0.0))

        # Daily counters (UTC)
        self.day_key = self._utc_day_key()
        self.trades_today = 0
        self.trades_today_by_pair: Dict[str, int] = {}

        # Latest portfolio metrics
        self.portfolio_realized = 0.0
        self.portfolio_max_dd = 0.0

        # Latest position metrics
        self.open_positions: Optional[int] = None
        self.open_positions_ts: int = 0

        # Sticky pause reason
        self.pause_reason: Optional[str] = None

    # -----------------
    # Internals
    # -----------------
    def _utc_day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_day_if_needed(self):
        dk = self._utc_day_key()
        if dk != self.day_key:
            self.day_key = dk
            self.trades_today = 0
            self.trades_today_by_pair = {}

    def _is_live_mode(self, mode: str) -> bool:
        return (mode or "").strip().lower() == "live"

    def _count_trades_for_mode(self, mode: str) -> bool:
        # Always count trades in live
        # Only count in dry_run if explicitly enabled
        return self._is_live_mode(mode) or self.count_trades_in_dry_run

    # -----------------
    # Control state
    # -----------------
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
            pass

    # -----------------
    # Metrics updates
    # -----------------
    def update_portfolio_metrics(self, realized_pnl_usd: float, max_drawdown_usd: float):
        self.portfolio_realized = float(realized_pnl_usd)
        self.portfolio_max_dd = float(max_drawdown_usd)

        if self.max_daily_loss_usd > 0 and self.portfolio_realized <= -self.max_daily_loss_usd:
            self._touch_pause(
                f"circuit breaker: realized pnl {self.portfolio_realized:.6f} <= -{self.max_daily_loss_usd:.6f} USD"
            )
            return

        if self.max_drawdown_usd > 0 and self.portfolio_max_dd >= self.max_drawdown_usd:
            self._touch_pause(
                f"circuit breaker: max drawdown {self.portfolio_max_dd:.6f} >= {self.max_drawdown_usd:.6f} USD"
            )
            return

        if self.equity_base_usd > 0:
            if self.max_daily_loss_pct > 0:
                daily_loss_usd = -min(self.portfolio_realized, 0.0)
                daily_loss_pct = (daily_loss_usd / self.equity_base_usd) * 100.0
                if daily_loss_pct >= self.max_daily_loss_pct:
                    self._touch_pause(
                        f"circuit breaker: daily loss {daily_loss_pct:.4f}% >= {self.max_daily_loss_pct:.4f}%"
                    )
                    return

            if self.max_drawdown_pct > 0:
                dd_pct = (self.portfolio_max_dd / self.equity_base_usd) * 100.0
                if dd_pct >= self.max_drawdown_pct:
                    self._touch_pause(
                        f"circuit breaker: drawdown {dd_pct:.4f}% >= {self.max_drawdown_pct:.4f}%"
                    )
                    return

    def update_open_positions(self, open_positions: int):
        self.open_positions = int(open_positions)
        self.open_positions_ts = int(time.time())

    # -----------------
    # Core gating
    # -----------------
    def can_trade(self, notional_usd: float, mode: str, pair: Optional[str] = None) -> RiskDecision:
        self._roll_day_if_needed()

        if self.kill_switch_active():
            return RiskDecision(False, "kill switch active", level="block")

        if self.paused():
            return RiskDecision(False, f"paused: {self.get_pause_reason()}", level="block")

        if self.max_open_positions > 0:
            if self.open_positions is None:
                if self.fail_closed:
                    return RiskDecision(False, "open positions unknown (fail-closed)", level="block")
            elif self.open_positions >= self.max_open_positions:
                return RiskDecision(
                    False,
                    f"max_open_positions reached ({self.open_positions}/{self.max_open_positions})",
                    level="block",
                )

        if notional_usd > self.max_notional_usd_per_trade:
            return RiskDecision(
                False,
                f"max_notional_usd_per_trade exceeded ({notional_usd:.2f} > {self.max_notional_usd_per_trade:.2f})",
                level="block",
            )

        if self._count_trades_for_mode(mode):
            if self.max_trades_per_day > 0 and self.trades_today >= self.max_trades_per_day:
                return RiskDecision(
                    False,
                    f"ALERT: max trades/day reached ({self.trades_today}/{self.max_trades_per_day})",
                    level="alert",
                )

            if pair and self.max_trades_per_day_per_pair > 0:
                pt = self.trades_today_by_pair.get(pair, 0)
                if pt >= self.max_trades_per_day_per_pair:
                    return RiskDecision(
                        False,
                        f"ALERT: max trades/day per pair reached ({pair}: {pt}/{self.max_trades_per_day_per_pair})",
                        level="alert",
                    )

        return RiskDecision(True, "ok", level="block")

    def record_trade(self, pair: Optional[str] = None, mode: str = "dry_run"):
        self._roll_day_if_needed()

        if not self._count_trades_for_mode(mode):
            return

        self.trades_today += 1
        if pair:
            self.trades_today_by_pair[pair] = self.trades_today_by_pair.get(pair, 0) + 1