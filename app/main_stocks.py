import asyncio
import os
import yaml
import logging
from ib_insync import *

# --- CONFIG & PATHS ---
def load_config():
    with open("/config/ibkr.yaml", "r") as f:
        return yaml.safe_load(f)

async def main():
    print(">>> IBKR BOT: INITIALIZING <<<")
    
    # 1. Load Config
    try:
        cfg = load_config()
        ib_cfg = cfg.get("ibkr", {})
        host = ib_cfg.get("host", "ib-gateway")
        port = int(ib_cfg.get("port", 4001))
        client_id = int(ib_cfg.get("client_id", 1))
    except Exception as e:
        print(f"CRITICAL: Config Error: {e}")
        return

    # 2. Initialize Client
    ib = IB()
    
    print(f"Connecting to {host}:{port} (ClientId: {client_id})...")
    
    # 3. Connection Loop (Infinite Retry)
    while True:
        try:
            await ib.connectAsync(host, port, clientId=client_id, timeout=10)
            print(">>> CONNECTED TO IBKR GATEWAY <<<")
            
            # 4. Verify Account
            # Request managed accounts
            print(f"Managed Accounts: {ib.managedAccounts()}")
            
            # 5. Keep-Alive / Main Logic
            while ib.isConnected():
                # For now, just heartbeat
                ib.sleep(1)
                await asyncio.sleep(5)
                print("Heartbeat: Connected.")
                
        except Exception as e:
            print(f"Connection Failed: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopping...")