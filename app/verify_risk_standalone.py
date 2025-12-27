import sys
import os
import shutil
import yaml
import time
from typing import Dict, Any

# Adjust path to ensure we can import app modules
sys.path.append(os.getcwd())

try:
    from app.risk.risk_engine import RiskEngine, RiskDecision
except ImportError:
    # Fallback for running inside container structure
    sys.path.append(os.path.dirname(os.getcwd()))
    from app.risk.risk_engine import RiskEngine, RiskDecision

def run_test(name, success, details=""):
    if success:
        print(f"✅ PASS: {name}")
    else:
        print(f"❌ FAIL: {name}")
        if details:
            print(f"   Details: {details}")
        sys.exit(1)

def main():
    print("--- STARTING RISK ENGINE DIAGNOSTIC (STRICT MODE) ---")
    
    # 1. Load Config
    config_path = "config/risk.yaml"
    if not os.path.exists(config_path):
        print(f"CRITICAL: {config_path} not found.")
        sys.exit(1)
        
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # SAFETY OVERRIDE: Redirect control files to /tmp/ to avoid pausing the real bot
    if 'controls' not in config: config['controls'] = {}
    config['controls']['pause_file'] = '/tmp/TEST_PAUSE_STRICT'
    config['controls']['kill_switch_file'] = '/tmp/TEST_KILL_STRICT'

    # Cleanup previous test artifacts
    if os.path.exists('/tmp/TEST_PAUSE_STRICT'): os.remove('/tmp/TEST_PAUSE_STRICT')
    if os.path.exists('/tmp/TEST_KILL_STRICT'): os.remove('/tmp/TEST_KILL_STRICT')

    print(f"Loaded Limits (Test Mode): {config}")

    # 2. Initialize Engine
    risk = RiskEngine(config)
    
    # --- TEST 1: FAIL-CLOSED CHECK (Unknown Positions) ---
    print("\n--- TEST CASE 1: FAIL-CLOSED (Unknown Positions) ---")
    # Should block because we haven't sent positions yet and fail_closed=True
    decision = risk.can_trade(10.0, "live", "XBTUSD")
    run_test("Block when open_positions is None", decision.allowed is False, decision.reason)
    
    # --- TEST 2: MAX OPEN POSITIONS ---
    print("\n--- TEST CASE 2: MAX OPEN POSITIONS ---")
    limit_pos = config['trade'].get('max_open_positions', 8)
    
    # Simulate that we are AT the limit
    risk.update_open_positions(limit_pos)
    
    decision = risk.can_trade(10.0, "live", "XBTUSD")
    run_test(f"Block when open_positions ({limit_pos}) >= limit", decision.allowed is False, decision.reason)

    # Reset positions to 0 for remaining tests so they don't block us
    risk.update_open_positions(0)
    
    # --- TEST 3: MAX NOTIONAL ---
    print("\n--- TEST CASE 3: MAX NOTIONAL ---")
    limit_notional = config['trade'].get('max_notional_usd_per_trade', 20.0)
    huge_amt = limit_notional + 100.0
    
    decision = risk.can_trade(huge_amt, "live", "XBTUSD")
    run_test(f"Block excessive notional (${huge_amt})", decision.allowed is False, decision.reason)

    # --- TEST 4: DAILY LOSS CAP (Circuit Breaker) ---
    print("\n--- TEST CASE 4: DAILY LOSS CAP ---")
    max_loss = config['account'].get('max_daily_loss_usd', 3.0)
    
    # Simulate a portfolio update that triggers the breaker
    print(f"   Simulating portfolio loss of ${max_loss + 1.0}...")
    risk.update_portfolio_metrics(realized_pnl_usd=-(max_loss + 1.0), max_drawdown_usd=0.0)
    
    # Verify the engine entered PAUSE state
    run_test("Engine entered PAUSE state", risk.paused() is True)
    
    # Verify trading is blocked due to pause
    decision = risk.can_trade(10.0, "live", "XBTUSD")
    run_test("Block trading when Paused", decision.allowed is False, decision.reason)

    # --- TEST 5: TRADE FREQUENCY ---
    print("\n--- TEST CASE 5: TRADE FREQUENCY ---")
    # Reset pause state manually for this specific test
    if os.path.exists('/tmp/TEST_PAUSE_STRICT'): os.remove('/tmp/TEST_PAUSE_STRICT')
    risk.pause_reason = None
    
    max_trades = config['trade'].get('max_trades_per_day', 3)
    
    # Simulate hitting the cap
    print(f"   Simulating {max_trades} executed trades...")
    risk.trades_today = max_trades
    
    decision = risk.can_trade(10.0, "live", "XBTUSD")
    # The code returns allowed=False (with level="alert" or "block")
    run_test(f"Block/Alert after {max_trades} trades", decision.allowed is False, decision.reason)
    
    print("\n--- ALL SAFETY CHECKS PASSED ---")

if __name__ == "__main__":
    main()