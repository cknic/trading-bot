import asyncio
import os
import yaml
import time
import json
import logging
from datetime import datetime
from ib_insync import *
import requests
import pandas as pd
# --- NEW IMPORTS (Standard 'ta' library) ---
from ta.trend import EMAIndicator, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# --- IMPORTS FROM SHARED MODULES ---
from util.ledger import get_position, set_position, clear_position
from util.trade_log import append_trade
from risk.risk_engine import RiskEngine

# --- CONFIG ---
CONFIG_PATH = "/config/ibkr.yaml"
AI_CONFIG_PATH = "/config/ai.yaml"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
AI_LOG_PATH = os.path.join(DATA_DIR, "ai_log.jsonl")

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("IBKR_BOT")

# --- AI HELPERS ---
def get_ai_config():
    try:
        with open(AI_CONFIG_PATH, "r") as f: return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load AI config: {e}")
        return {}

def log_ai_decision(model, prompt, response):
    entry = {"ts": int(time.time()), "model": model, "prompt": prompt, "response": response}
    try:
        with open(AI_LOG_PATH, "a") as f: f.write(json.dumps(entry) + "\n")
    except: pass

def get_ai_model_config(cfg):
    """
    Strictly extracts provider and model from ai.yaml.
    Refuses to guess if config is missing.
    """
    provider = cfg.get("provider")
    
    if not provider:
        logger.error("AI Config Error: 'provider' is missing in ai.yaml")
        return None, None

    provider_settings = cfg.get(provider, {})
    model = provider_settings.get("model")
    
    if not model:
        model = cfg.get("model")

    if not model:
        logger.error(f"AI Config Error: Model not defined for provider '{provider}'")
        return provider, None
        
    return provider, model

def ai_call_stock(ticker, market_summary):
    cfg = get_ai_config()
    provider, model = get_ai_model_config(cfg)
    
    if not model:
        return "ERROR", "AI Configuration Invalid"

    # --- PROMPT SELECTION ---
    prompts = cfg.get("prompts", {})
    # Use 'stock_swing_decision' (Sentinel), fallback to 'strategy_decision' (Zeus)
    base_prompt = prompts.get("stock_swing_decision", prompts.get("strategy_decision", "Analyze market."))

    full_prompt = f"ASSET: {ticker} (STOCK)\n{base_prompt}\n\nCURRENT MARKET DATA:\n{market_summary}"

    # Prepare Keys
    api_key = os.environ.get("OPENROUTER_API_KEY") if provider == "openrouter" else os.environ.get("OPENAI_API_KEY")
    url = "https://openrouter.ai/api/v1/chat/completions" if provider == "openrouter" else "https://api.openai.com/v1/chat/completions"
    
    if not api_key:
        logger.error("AI Error: Missing API Key.")
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

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if r.status_code == 200:
            content = r.json()['choices'][0]['message']['content']
            log_ai_decision(model, full_prompt, content)
            
            if "VERDICT: GO" in content: return "GO", content
            if "VERDICT: STOP" in content: return "STOP", content
            
            return "ERROR", "AI response missing VERDICT"
        else:
            logger.error(f"AI Error {r.status_code}: {r.text}")
            return "ERROR", f"HTTP {r.status_code}"
            
    except Exception as e:
        logger.error(f"AI Exception: {e}")
        return "ERROR", str(e)

# --- MAIN CLASS ---
class StockBot:
    def __init__(self):
        self.ib = IB()
        self.config = self.load_config()
        self.risk = RiskEngine(yaml.safe_load(open("/config/risk.yaml", "r")))
        
        ib_cfg = self.config.get("ibkr", {})
        self.host = ib_cfg.get("host", "ib-gateway")
        
        # --- PORT SELECTION ---
        env_port = os.environ.get("IBKR_API_PORT")
        if env_port:
            self.port = int(env_port)
            logger.info(f"Config: Using Docker Environment Port: {self.port}")
        else:
            self.port = int(ib_cfg.get("port", 4001))
            logger.info(f"Config: Using YAML Config Port: {self.port}")
            
        self.client_id = int(ib_cfg.get("client_id", 1))
        
        trading_cfg = self.config.get("trading", {})
        self.poll_interval = trading_cfg.get("poll_seconds", 300)
        
        self.is_connected = False

    def load_config(self):
        with open(CONFIG_PATH, "r") as f: return yaml.safe_load(f)

    async def connect(self):
        logger.info(f"Connecting to IB Gateway at {self.host}:{self.port} (Client: {self.client_id})...")
        while not self.ib.isConnected():
            try:
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                logger.info(">>> CONNECTED TO IBKR <<<")
                self.is_connected = True
            except Exception as e:
                logger.warning(f"Connection failed: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def get_market_data_async(self, contract):
        # Request Historical Data
        bars = await self.ib.reqHistoricalDataAsync(
            contract, endDateTime='', durationStr='30 D',
            barSizeSetting='1 hour', whatToShow='TRADES', useRTH=True
        )
        
        if not bars: return None, 0.0
        
        df = util.df(bars)
        
        if df.empty: return None, 0.0

        # --- CALCULATE INDICATORS (Using 'ta' library) ---
        # 1. EMAs
        df['EMA_9'] = EMAIndicator(close=df['close'], window=9).ema_indicator()
        df['EMA_21'] = EMAIndicator(close=df['close'], window=21).ema_indicator()
        
        # 2. SMA
        df['SMA_50'] = SMAIndicator(close=df['close'], window=50).sma_indicator()
        
        # 3. RSI
        df['RSI_14'] = RSIIndicator(close=df['close'], window=14).rsi()
        
        # 4. ATR
        df['ATRr_14'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()

        # Handle NaNs (for early bars)
        df.fillna(0, inplace=True)

        current = df.iloc[-1]
        prev = df.iloc[-2]
        
        change_pct = ((current['close'] - prev['close']) / prev['close']) * 100
        
        return df, change_pct

    async def execute_trade(self, contract, action, quantity, price, reason):
        logger.info(f"EXECUTION: {action} {quantity} {contract.symbol} @ ${price:.2f} | {reason}")
        
        # 1. Risk Check
        notional = quantity * price
        decision = self.risk.can_trade(notional, "live", contract.symbol)
        
        if not decision.allowed:
            logger.warning(f"RISK BLOCK: {decision.reason}")
            return

        # 2. Place Order
        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(contract, order)
        
        # 3. Wait for Fill
        while not trade.isDone():
            await asyncio.sleep(1)
            
        fill = trade.orderStatus
        avg_price = fill.avgFillPrice
        
        # 4. Log & Update Ledger
        mode = "live"
        append_trade(int(time.time()), contract.symbol, action.lower(), quantity, avg_price, notional, mode)
        
        if action == "BUY":
            set_position(contract.symbol, quantity, avg_price)
        elif action == "SELL":
            clear_position(contract.symbol)
            
        self.risk.record_trade(contract.symbol, mode)
        logger.info(f"FILLED: {action} {quantity} @ {avg_price}")

    async def run_loop(self):
        universe = self.config["universe"]["stocks"]
        
        while True:
            logger.info(f"--- STARTING SCAN: {len(universe)} Tickers ---")
            
            for symbol in universe:
                try:
                    contract = Stock(symbol, 'SMART', 'USD')
                    await self.ib.qualifyContractsAsync(contract)
                    
                    df, change_24h = await self.get_market_data_async(contract)
                    
                    if df is None:
                        logger.warning(f"{symbol}: No data found or insufficient history.")
                        continue
                        
                    curr = df.iloc[-1]
                    
                    # --- INDICATOR VALUES ---
                    ema_9 = curr.get('EMA_9', 0)
                    ema_21 = curr.get('EMA_21', 0)
                    rsi = curr.get('RSI_14', 50)
                    sma_50 = curr.get('SMA_50', 0)
                    atr = curr.get('ATRr_14', 0)
                    price = curr['close']
                    
                    pos = get_position(symbol)
                    has_pos = pos.get("has_position", False)
                    
                    math_signal = "HOLD"
                    math_reason = "Neutral"
                    
                    # --- LOGIC RULES ---
                    if not has_pos:
                        if ema_9 > ema_21 and rsi > 50 and rsi < 70:
                            math_signal = "BUY"
                            math_reason = "EMA Bull Cross + Momentum"
                        elif price > sma_50 and rsi < 35:
                            math_signal = "BUY"
                            math_reason = "Oversold Pullback (Mean Rev)"
                            
                    elif has_pos:
                        if ema_9 < ema_21:
                            math_signal = "SELL"
                            math_reason = "Trend Break (EMA Cross Down)"
                        elif rsi > 75:
                            math_signal = "SELL"
                            math_reason = "RSI Overbought (>75)"
                        
                    # --- AI EXECUTIVE ---
                    market_summary = (
                        f"Price: ${price:.2f} (24h: {change_24h:.2f}%)\n"
                        f"Trend: EMA9={'Above' if ema_9 > ema_21 else 'Below'} EMA21\n"
                        f"RSI(14): {rsi:.2f}\n"
                        f"Volatility (ATR): {atr:.2f}\n"
                        f"Regime: {'Above' if price > sma_50 else 'Below'} SMA50"
                    )
                    
                    ai_verdict, ai_analysis = ai_call_stock(symbol, market_summary)
                    
                    # --- KILL SWITCH ---
                    if ai_verdict == "ERROR":
                        logger.error(f"HALT: AI unresponsive for {symbol}. Reason: {ai_analysis}. Trading blocked.")
                        continue 

                    logger.info(f"{symbol} | Math: {math_signal} | AI: {ai_verdict} | RSI: {rsi:.0f}")
                    
                    # --- FINAL EXECUTION LOGIC ---
                    final_action = "HOLD"
                    final_reason = math_reason
                    
                    if math_signal == "BUY":
                        if ai_verdict == "STOP":
                            final_action = "HOLD"
                            final_reason = f"VETOED: AI blocked trade. {ai_analysis[:40]}..."
                        else:
                            final_action = "BUY"
                            
                    elif math_signal == "SELL":
                        if ai_verdict == "GO":
                            final_action = "HOLD"
                            final_reason = f"SAVED: AI overruled Sell. {ai_analysis[:40]}..."
                        else:
                            final_action = "SELL"
                            
                    elif math_signal == "HOLD":
                        if ai_verdict == "GO" and not has_pos:
                            final_action = "BUY"
                            final_reason = "OVERRIDE: AI spotted setup invisible to SMA."

                    # --- ORDER SIZING ---
                    risk_cfg = self.config.get("risk", {})
                    max_pos_usd = risk_cfg.get("max_position_size_usd", 5000)

                    if final_action == "BUY":
                        qty = int(max_pos_usd / price)
                        if qty > 0:
                            await self.execute_trade(contract, "BUY", qty, price, final_reason)
                        else:
                            logger.warning(f"{symbol}: Price ${price} too high for max pos ${max_pos_usd}")
                            
                    elif final_action == "SELL":
                        qty = pos.get("base_volume", 0)
                        if qty > 0:
                            await self.execute_trade(contract, "SELL", qty, price, final_reason)
                        
                except Exception as e:
                    logger.error(f"Error processing {symbol}: {e}")
            
            logger.info(f"Scan complete. Sleeping {self.poll_interval}s...")
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