"""
Sentinel Trading Bot - Stocks (IBKR)
Enhanced with RSI + Distance filters and AI override safety limits
"""

import asyncio
import os
import yaml
import time
import json
import logging
from datetime import datetime
from ib_insync import Stock

import requests

# --- EXCHANGE MODULES ---
from exchange.ibkr_client import IBKRClient
from exchange.ibkr_marketdata import (
    fetch_historical_data_async, 
    build_market_summary,
    calculate_price_distance_pct
)
from exchange.ibkr_orders import (
    place_order_async, 
    simulate_order, 
    calculate_quantity
)

# --- MARKET HOURS ---
from util.market_hours import is_us_market_open, get_next_market_open

# --- SHARED MODULES ---
from util.ledger import get_position, set_position, clear_position
from util.trade_log import append_trade
from util.pnl_analytics import compute_and_write
from risk.risk_engine import RiskEngine

# --- CONFIG PATHS ---
CONFIG_PATH = "/config/ibkr.yaml"
RISK_CONFIG_PATH = "/config/risk.yaml"
AI_CONFIG_PATH = "/config/ai.yaml"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
AI_LOG_PATH = os.path.join(DATA_DIR, "ai_log.jsonl")
STATUS_FILE = os.path.join(DATA_DIR, "bot_stocks_status.json")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("IBKR_BOT")


# ==============================================================================
# HELPERS
# ==============================================================================
def load_yaml(path):
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return {}


def write_bot_status(status_dict):
    try:
        tmp_path = STATUS_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(status_dict, f, indent=2)
        os.replace(tmp_path, STATUS_FILE)
    except Exception as e:
        logger.error(f"Failed to write status: {e}")


def get_ai_config():
    return load_yaml(AI_CONFIG_PATH)


def log_ai(response, model, asset, indicators=None):
    """Log AI decision with asset identifier and indicators"""
    if isinstance(response, dict):
        response_text = response.get("analysis", json.dumps(response))
    elif not isinstance(response, str):
        response_text = str(response)
    else:
        response_text = response
    
    entry = {
        "ts": int(time.time()),
        "asset": asset,
        "asset_type": "stock",
        "model": model,
        "response": response_text
    }
    
    if indicators:
        entry["indicators"] = indicators
    
    try:
        with open(AI_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"AI Log Error: {e}")

def build_position_context(symbol, pos_data, current_price):
    """Build position context dict for AI prompt"""
    if not pos_data.get("has_position"):
        return {"has_position": False}
    
    entry_price = float(pos_data.get("average_price", 0))
    volume = float(pos_data.get("base_volume", 0))
    first_entry = int(pos_data.get("first_entry_time", pos_data.get("entry_ts", 0)))
    
    cost_basis = entry_price * volume
    current_value = current_price * volume
    unrealized_pnl = current_value - cost_basis
    unrealized_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    
    now = time.time()
    hold_hours = (now - first_entry) / 3600.0 if first_entry > 0 else 0
    hold_days = hold_hours / 24.0
    
    # Try to get entry fee from pnl.json
    entry_fee = 0.0
    pnl_path = os.path.join(DATA_DIR, "pnl.json")
    try:
        if os.path.exists(pnl_path):
            with open(pnl_path, 'r') as f:
                pnl_data = json.load(f)
                pair_pnl = pnl_data.get("pairs", {}).get(symbol, {})
                entry_fee = float(pair_pnl.get("entry_fee_usd", 0))
    except:
        pass
    
    return {
        "has_position": True,
        "entry_price": entry_price,
        "current_price": current_price,
        "volume": volume,
        "cost_basis": cost_basis,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": unrealized_pct,
        "hold_duration_hours": hold_hours,
        "hold_duration_days": hold_days,
        "entry_fee": entry_fee,
    }

def get_ai_model_config(cfg):
    provider = cfg.get("provider")
    if not provider:
        return None, None
    provider_settings = cfg.get(provider, {})
    model = provider_settings.get("model")
    if not model:
        model = cfg.get("model")
    return provider, model


def ai_call_stock(ticker, market_summary, indicators=None, position_context=None):
    """AI call for a single stock - logs with asset identifier and indicators"""
    cfg = get_ai_config()
    provider, model = get_ai_model_config(cfg)
    
    if not model:
        return "ERROR", "AI Configuration Invalid"
    
    prompts = cfg.get("prompts", {})
    base_prompt = prompts.get("stock_swing_decision", prompts.get("strategy_decision", "Analyze market."))
    
    # Start building full prompt
    full_prompt = f"ASSET: {ticker} (STOCK)\n{base_prompt}"
    
    # Add position context if available
    if position_context:
        position_block = (
            f"\n\nCURRENT POSITION STATUS:\n"
            f"- Status: {'HOLDING' if position_context.get('has_position') else 'NO POSITION'}\n"
        )
        if position_context.get('has_position'):
            position_block += (
                f"- Entry Price: ${position_context.get('entry_price', 0):.2f}\n"
                f"- Current Price: ${position_context.get('current_price', 0):.2f}\n"
                f"- Position Size: {position_context.get('volume', 0):.4f} shares\n"
                f"- Cost Basis: ${position_context.get('cost_basis', 0):.2f}\n"
                f"- Unrealized P&L: ${position_context.get('unrealized_pnl', 0):.2f} ({position_context.get('unrealized_pct', 0):+.2f}%)\n"
                f"- Hold Duration: {position_context.get('hold_duration_hours', 0):.1f} hours ({position_context.get('hold_duration_days', 0):.1f} days)\n"
                f"- Entry Fee Paid: ${position_context.get('entry_fee', 0):.2f}\n"
            )
        full_prompt += position_block
    
    full_prompt += f"\n\nCURRENT MARKET DATA:\n{market_summary}"
    
    # Build prompt with indicators if available
    if indicators:
        indicator_block = (
            f"\n\nTECHNICAL INDICATORS:\n"
            f"- RSI(14): {indicators.get('rsi', 'N/A')}\n"
            f"- Trend: {indicators.get('trend', 'N/A').upper()}\n"
            f"- Trend Strength: {indicators.get('trend_strength_pct', 'N/A')}%\n"
            f"- Distance from SMA: {indicators.get('distance_from_sma_pct', 'N/A')}%\n"
            f"- Volatility: {indicators.get('volatility_pct', 'N/A')}%\n"
            f"- RSI Overbought: {'YES' if indicators.get('rsi_overbought') else 'No'}\n"
            f"- RSI Oversold: {'YES' if indicators.get('rsi_oversold') else 'No'}\n"
            f"- Price Extended: {'YES' if indicators.get('price_extended') else 'No'}\n"
            f"- Bullish Cross: {'YES' if indicators.get('bullish_cross') else 'No'}\n"
            f"- Bearish Cross: {'YES' if indicators.get('bearish_cross') else 'No'}"
        )
        full_prompt += indicator_block
    
    api_key = os.environ.get("OPENROUTER_API_KEY") if provider == "openrouter" else os.environ.get("OPENAI_API_KEY")
    url = "https://openrouter.ai/api/v1/chat/completions" if provider == "openrouter" else "https://api.openai.com/v1/chat/completions"
    
    if not api_key:
        return "ERROR", "Missing API Key"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://sentinel-bot"
        headers["X-Title"] = "Sentinel IBKR"
    
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": full_prompt}]
    }
    
    asset_label = f"{ticker} (Stock)"
    
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            content = r.json()['choices'][0]['message']['content']
            
            # Log with asset identifier and indicators
            log_ai(content, model, asset_label, indicators)
            
            content_upper = content.upper()
            if "VERDICT: GO" in content_upper:
                return "GO", content
            if "VERDICT: STOP" in content_upper:
                return "STOP", content
            return "NEUTRAL", content
        else:
            error_msg = f"HTTP {r.status_code}: {r.text}"
            log_ai(f"Error: {error_msg}", model, asset_label, indicators)
            return "ERROR", error_msg
    except Exception as e:
        error_msg = str(e)
        log_ai(f"Exception: {error_msg}", model, asset_label, indicators)
        return "ERROR", error_msg


# ==============================================================================
# STRATEGY HELPERS
# ==============================================================================
def get_strategy_config(config):
    """Extract strategy filter config from ibkr.yaml"""
    filters = config.get("strategy", {}).get("filters", {})
    return {
        "rsi_period": filters.get("rsi_period", 14),
        "rsi_overbought": filters.get("rsi_overbought", 70),
        "rsi_oversold": filters.get("rsi_oversold", 30),
        "rsi_take_profit": filters.get("rsi_take_profit", 75),
        "max_distance_pct": filters.get("max_distance_pct", 5.0),
        "require_crossover": filters.get("require_crossover", False),
    }


def get_ai_override_limits(config):
    """Extract AI override safety limits from config"""
    ai_override = config.get("strategy", {}).get("ai_override", {})
    return {
        "max_rsi_for_force_buy": ai_override.get("max_rsi_for_force_buy", 65),
        "max_distance_for_force_buy": ai_override.get("max_distance_for_force_buy", 6.0),
        "allow_ai_force_buy": ai_override.get("allow_ai_force_buy", True),
        "allow_ai_block_sell": ai_override.get("allow_ai_block_sell", True),
    }


def get_decay_exit_config(config):
    """Extract decay exit config"""
    decay = config.get("strategy", {}).get("decay_exit", {})
    return {
        "enabled": decay.get("enabled", True),
        "initial_stop_pct": decay.get("initial_stop_pct", 3.0),
        "min_stop_pct": decay.get("min_stop_pct", 1.5),
        "decay_hours": decay.get("decay_hours", 72),
        "take_profit_pct": decay.get("take_profit_pct", 10.0),
    }


def calculate_math_signal(indicators, has_position, strategy_config):
    """
    Calculate the mathematical/technical signal.
    
    Returns:
        (signal: str, reason: str)
        signal is one of: "buy", "sell", "hold"
    """
    rsi = indicators.get("rsi", 50)
    ema_9 = indicators.get("ema_9", 0)
    ema_21 = indicators.get("ema_21", 0)
    sma_10 = indicators.get("sma_10", 0)
    sma_30 = indicators.get("sma_30", 0)
    sma_50 = indicators.get("sma_50", 0)
    distance_pct = indicators.get("distance_from_sma_pct", 0)
    
    # Config thresholds
    rsi_overbought = strategy_config.get("rsi_overbought", 70)
    rsi_oversold = strategy_config.get("rsi_oversold", 30)
    rsi_take_profit = strategy_config.get("rsi_take_profit", 75)
    max_distance = strategy_config.get("max_distance_pct", 5.0)
    
    # ===== NO POSITION - Looking to BUY =====
    if not has_position:
        # Check filters first
        blocked_reasons = []
        
        # RSI filter
        if rsi > rsi_overbought:
            blocked_reasons.append(f"RSI {rsi:.0f} > {rsi_overbought}")
        
        # Distance filter
        if distance_pct > max_distance:
            blocked_reasons.append(f"Price +{distance_pct:.1f}% extended")
        
        if blocked_reasons:
            return "hold", f"FILTERED: {', '.join(blocked_reasons)}"
        
        # EMA crossover bullish + healthy RSI
        if ema_9 > ema_21 and rsi_oversold < rsi < rsi_overbought:
            return "buy", f"Bull Trend (EMA9 {ema_9:.2f} > EMA21) + RSI {rsi:.0f}"
        
        # Oversold dip in uptrend
        if sma_10 > sma_30 and rsi < 35:
            return "buy", f"Oversold Dip (RSI {rsi:.0f}) in Uptrend"
        
        # No setup
        if ema_9 < ema_21:
            return "hold", f"Bearish Trend (EMA9 < EMA21)"
        elif rsi >= rsi_overbought:
            return "hold", f"RSI Overbought ({rsi:.0f})"
        elif rsi <= rsi_oversold + 10:
            return "hold", f"Weak Momentum (RSI {rsi:.0f})"
        else:
            return "hold", f"No Setup (EMA Flat, RSI {rsi:.0f})"
    
    # ===== HAS POSITION - Looking to SELL or HOLD =====
    else:
        # RSI take profit
        if rsi > rsi_take_profit:
            return "sell", f"RSI Take Profit ({rsi:.0f} > {rsi_take_profit})"
        
        # Trend broken
        if ema_9 < ema_21:
            return "sell", f"Trend Broken (EMA Cross Down)"
        
        # Still healthy
        return "hold", f"Trend Intact (EMA9 > EMA21, RSI {rsi:.0f})"


def check_decay_exit(entry_price, entry_ts, current_price, decay_config):
    """
    Check if position should exit due to decaying stop-loss or take profit.
    
    Returns:
        (should_exit: bool, reason: str)
    """
    if not decay_config.get("enabled", True):
        return False, ""
    
    if entry_price <= 0:
        return False, ""
    
    now = time.time()
    hours_held = (now - entry_ts) / 3600.0
    
    initial_stop = decay_config.get("initial_stop_pct", 3.0)
    min_stop = decay_config.get("min_stop_pct", 1.5)
    decay_hours = decay_config.get("decay_hours", 72)
    take_profit_pct = decay_config.get("take_profit_pct", 10.0)
    
    # Calculate current stop level
    if hours_held >= decay_hours:
        current_stop_pct = min_stop
    else:
        decay_progress = hours_held / decay_hours
        current_stop_pct = initial_stop - (initial_stop - min_stop) * decay_progress
    
    # Calculate P&L
    pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
    
    # Take profit check
    if pnl_pct >= take_profit_pct:
        return True, f"TAKE PROFIT: +{pnl_pct:.1f}% >= {take_profit_pct}%"
    
    # Stop loss check
    if pnl_pct <= -current_stop_pct:
        return True, f"STOP LOSS: {pnl_pct:.1f}% <= -{current_stop_pct:.1f}% (held {hours_held:.1f}h)"
    
    return False, ""


def synthesize_decision(math_signal, math_reason, indicators, ai_vote, has_position, ai_limits):
    """
    Combine math signal with AI vote to determine final action.
    
    Returns:
        (final_action: str, final_reason: str)
    """
    rsi = indicators.get("rsi", 50)
    distance_pct = indicators.get("distance_from_sma_pct", 0)
    is_stop_loss = "stop" in math_reason.lower() or "take profit" in math_reason.lower()
    
    # ===== BUY SIGNAL =====
    if math_signal == "buy":
        if ai_vote == "STOP":
            return "HOLD", f"AI VETO: {math_reason}"
        elif ai_vote == "NEUTRAL":
            return "HOLD", f"AI UNCERTAIN: {math_reason}"
        else:  # GO
            return "BUY", f"CONFIRMED: {math_reason}"
    
    # ===== HOLD SIGNAL =====
    elif math_signal == "hold":
        # AI can force entry on hold (within safety limits)
        if ai_vote == "GO" and not has_position and ai_limits.get("allow_ai_force_buy", True):
            max_rsi = ai_limits.get("max_rsi_for_force_buy", 65)
            max_dist = ai_limits.get("max_distance_for_force_buy", 6.0)
            
            blocked_reasons = []
            
            if rsi > max_rsi:
                blocked_reasons.append(f"RSI {rsi:.0f} > {max_rsi}")
            
            if distance_pct > max_dist:
                blocked_reasons.append(f"Distance +{distance_pct:.1f}% > {max_dist}%")
            
            if blocked_reasons:
                return "HOLD", f"AI OVERRIDE BLOCKED: {', '.join(blocked_reasons)}"
            else:
                return "BUY", f"AI OVERRIDE: Force entry ({math_reason})"
        else:
            return "HOLD", math_reason
    
    # ===== SELL SIGNAL =====
    elif math_signal == "sell":
        # AI can save position BUT NOT if it's a stop-loss
        if ai_vote == "GO" and ai_limits.get("allow_ai_block_sell", True) and not is_stop_loss:
            return "HOLD", f"AI SAVE: Position protected ({math_reason})"
        else:
            if is_stop_loss:
                return "SELL", f"SAFETY EXIT: {math_reason}"
            else:
                return "SELL", f"CONFIRMED: {math_reason}"
    
    return "HOLD", math_reason


# ==============================================================================
# MAIN CLASS
# ==============================================================================
class StockBot:
    def __init__(self):
        self.config = load_yaml(CONFIG_PATH)
        self.risk_config = load_yaml(RISK_CONFIG_PATH)
        self.risk = RiskEngine(self.risk_config)
        
        # IBKR Connection
        ib_cfg = self.config.get("ibkr", {})
        self.client = IBKRClient(
            host=ib_cfg.get("host", "ib-gateway"),
            port=ib_cfg.get("port", 4002),
            client_id=ib_cfg.get("client_id", 1)
        )
        
        # Trading config
        trading_cfg = self.config.get("trading", {})
        self.mode = trading_cfg.get("mode", "paper").lower()
        self.poll_interval = trading_cfg.get("poll_seconds", 300)
        
        # Trade size from risk.yaml
        trade_cfg = self.risk_config.get("trade", {})
        self.max_notional_per_trade = trade_cfg.get("max_notional_usd_per_trade", 20.0)
        
        # Universe
        self.active_universe = self.config.get("universe", {}).get("stocks", [])
        
        # Strategy configs
        self.strategy_config = get_strategy_config(self.config)
        self.ai_limits = get_ai_override_limits(self.config)
        self.decay_config = get_decay_exit_config(self.config)
        
        # Track prices for PnL
        self.current_prices = {}
        
        logger.info(f"Config: Mode={self.mode}, Notional=${self.max_notional_per_trade}")
        logger.info(f"Strategy: RSI OB={self.strategy_config['rsi_overbought']}, Max Dist={self.strategy_config['max_distance_pct']}%")
        logger.info(f"AI Limits: Max RSI={self.ai_limits['max_rsi_for_force_buy']}, Max Dist={self.ai_limits['max_distance_for_force_buy']}%")
    
    def sync_risk_counters(self):
        count = 0
        try:
            if os.path.exists(STATE_JSON):
                with open(STATE_JSON, 'r') as f:
                    state = json.load(f)
                    for v in state.values():
                        if v.get("has_position"):
                            count += 1
        except Exception as e:
            logger.warning(f"Risk Sync Warning: {e}")
        self.risk.update_open_positions(count)
    
    def refresh_pnl_json(self):
        """Recompute pnl.json from trades.csv with current mark prices"""
        try:
            compute_and_write(self.config, self.current_prices)
            logger.info(">> PnL JSON refreshed from trades.csv")
        except Exception as e:
            logger.error(f"PnL Refresh Error: {e}")
    
    async def connect(self):
        logger.info(f"Connecting to IB Gateway...")
        while not self.client.is_connected():
            try:
                connected = await self.client.connect_async()
                if connected:
                    logger.info(">>> CONNECTED TO IBKR <<<")
                    break
            except Exception as e:
                logger.warning(f"Connection failed: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
    
    async def execute_trade(self, contract, action, quantity, price, reason):
        """Execute a trade with risk checks"""
        notional = quantity * price
        logger.info(f"EXECUTION REQUEST: {action} {quantity:.4f} {contract.symbol} @ ${price:.2f} (${notional:.2f})")
        
        decision = self.risk.can_trade(notional, self.mode, contract.symbol)
        
        if not decision.allowed:
            logger.warning(f"RISK BLOCK: {decision.reason}")
            return False
        
        if self.mode == "paper" or self.mode == "dry_run":
            # Simulate the fill
            success, result = simulate_order(contract.symbol, action, quantity, price, self.mode)
            avg_price = price
            logger.info(f"PAPER FILL: {action} {quantity:.4f} {contract.symbol} @ ${avg_price:.2f}")
        else:
            # Live trading
            success, result = await place_order_async(
                self.client, contract, action, quantity, "market"
            )
            
            if not success:
                logger.error(f"ORDER FAILED: {result.get('reason')}")
                return False
            
            avg_price = result.get("price", price)
            logger.info(f"LIVE FILL: {action} {quantity:.4f} {contract.symbol} @ ${avg_price:.2f}")
        
        # Record the trade
        final_notional = quantity * avg_price
        append_trade(int(time.time()), contract.symbol, action.lower(), quantity, avg_price, final_notional, self.mode)
        
        if action == "BUY":
            set_position(contract.symbol, quantity, avg_price)
        elif action == "SELL":
            clear_position(contract.symbol)
        
        self.risk.record_trade(contract.symbol, self.mode)
        self.sync_risk_counters()
        self.refresh_pnl_json()
        
        logger.info(f"RECORDED: {action} {quantity:.4f} @ ${avg_price:.2f} | {reason}")
        return True
    
    async def run_loop(self):
        # Initial PnL refresh
        self.refresh_pnl_json()
        
        while True:
            # Reload configs for hot-reload support
            try:
                self.config = load_yaml(CONFIG_PATH)
                self.strategy_config = get_strategy_config(self.config)
                self.ai_limits = get_ai_override_limits(self.config)
                self.decay_config = get_decay_exit_config(self.config)
            except:
                pass
            
            # ============================================================
            # MARKET HOURS CHECK
            # ============================================================
            is_open, market_status = is_us_market_open()
            
            if not is_open:
                seconds_until, next_open_desc = get_next_market_open()
                sleep_time = min(seconds_until, self.poll_interval)
                
                logger.info(f"--- MARKET CLOSED: {market_status} ---")
                logger.info(f"Next: {next_open_desc} | Sleeping {sleep_time}s...")
                
                write_bot_status({
                    "ts": int(time.time()),
                    "status": "market_closed",
                    "market_status": market_status,
                    "next_open": next_open_desc,
                    "tickers": len(self.active_universe),
                    "mode_config": self.mode,
                    "next_poll": int(time.time()) + sleep_time
                })
                
                await asyncio.sleep(sleep_time)
                continue
            
            # ============================================================
            # MARKET IS OPEN - Run Scan
            # ============================================================
            logger.info(f"--- STARTING SCAN ({market_status}): {len(self.active_universe)} Tickers ---")
            self.sync_risk_counters()
            
            write_bot_status({
                "ts": int(time.time()),
                "status": "scanning",
                "market_status": market_status,
                "tickers": len(self.active_universe),
                "mode_config": self.mode,
                "next_poll": int(time.time()) + self.poll_interval
            })
            
            for symbol in self.active_universe:
                try:
                    contract = Stock(symbol, 'SMART', 'USD')
                    await self.client.raw.qualifyContractsAsync(contract)
                    
                    # Fetch market data
                    df = await fetch_historical_data_async(self.client, contract)
                    
                    if df is None or df.empty:
                        logger.warning(f"{symbol}: Insufficient Data")
                        continue
                    
                    # Build summary and get indicators
                    pos = get_position(symbol)
                    has_pos = pos.get("has_position", False)
                    
                    market_summary, indicators, price = build_market_summary(symbol, df, has_pos)
                    
                    if not market_summary:
                        continue
                    
                    # Store current price for PnL marks
                    self.current_prices[symbol] = price
                    
                    # ===== DECAY EXIT CHECK (Priority) =====
                    if has_pos:
                        entry_price = float(pos.get("average_price", 0))
                        entry_ts = int(pos.get("first_entry_time", pos.get("entry_ts", 0)))
                        
                        should_exit, exit_reason = check_decay_exit(
                            entry_price, entry_ts, price, self.decay_config
                        )
                        
                        if should_exit:
                            logger.info(f"{symbol} | 🛑 {exit_reason}")
                            qty = float(pos.get("base_volume", 0))
                            if qty > 0:
                                await self.execute_trade(contract, "SELL", qty, price, exit_reason)
                            continue
                    
                    # ===== MATH SIGNAL =====
                    math_signal, math_reason = calculate_math_signal(
                        indicators, has_pos, self.strategy_config
                    )
                    
                    # Log indicators summary
                    rsi = indicators.get("rsi", 0)
                    distance = indicators.get("distance_from_sma_pct", 0)
                    trend = indicators.get("trend", "?")
                    logger.info(f"{symbol} | RSI:{rsi:.0f} | Dist:{distance:+.1f}% | Trend:{trend} | Math:{math_signal.upper()}")
                    
                    # ===== BUILD POSITION CONTEXT =====
                    position_context = build_position_context(symbol, pos, price)
                    
                    # ===== AI CALL =====
                    ai_verdict, ai_analysis = ai_call_stock(symbol, market_summary, indicators, position_context)
                                    
                    if ai_verdict == "ERROR":
                        logger.error(f"{symbol} | AI Error: {ai_analysis[:50]}...")
                        continue
                    
                    logger.info(f"{symbol} | AI Vote: {ai_verdict}")
                    
                    # ===== SYNTHESIZE DECISION =====
                    final_action, final_reason = synthesize_decision(
                        math_signal, math_reason, indicators, ai_verdict, has_pos, self.ai_limits
                    )
                    
                    logger.info(f"{symbol} | FINAL: {final_action} | {final_reason}")
                    
                    # ===== EXECUTE =====
                    if final_action == "BUY":
                        qty = calculate_quantity(self.max_notional_per_trade, price)
                        if qty > 0:
                            await self.execute_trade(contract, "BUY", qty, price, final_reason)
                        else:
                            logger.warning(f"{symbol}: Quantity too small for ${self.max_notional_per_trade}")
                    
                    elif final_action == "SELL":
                        qty = float(pos.get("base_volume", 0))
                        if qty > 0:
                            await self.execute_trade(contract, "SELL", qty, price, final_reason)
                    
                except Exception as e:
                    logger.error(f"Error processing {symbol}: {e}")
            
            # Refresh PnL after scan
            self.refresh_pnl_json()
            
            logger.info(f"Scan complete. Sleeping {self.poll_interval}s...")
            
            write_bot_status({
                "ts": int(time.time()),
                "status": "sleeping",
                "market_status": market_status,
                "tickers": len(self.active_universe),
                "mode_config": self.mode,
                "last_scan_count": len(self.active_universe),
                "next_poll": int(time.time()) + self.poll_interval
            })
            
            await asyncio.sleep(self.poll_interval)


async def main_wrapper():
    bot = StockBot()
    await bot.connect()
    await bot.run_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main_wrapper())
    except KeyboardInterrupt:
        print("Bot stopped by user.")