"""
IBKR Client - Connection and Account Management
Uses ib_insync library for TWS/Gateway connection
"""

import os
import logging
from ib_insync import IB, Stock, util

logger = logging.getLogger("IBKR_CLIENT")


class IBKRClient:
    """Wrapper for Interactive Brokers API connection"""
    
    def __init__(self, host="ib-gateway", port=4002, client_id=1):
        self.host = host
        self.port = int(os.environ.get("IBKR_API_PORT", port))
        self.client_id = client_id
        self.ib = IB()
        
        logger.info(f"IBKRClient initialized: {self.host}:{self.port} (Client ID: {self.client_id})")
    
    async def connect_async(self):
        """Async connect to IBKR Gateway/TWS"""
        if self.ib.isConnected():
            return True
        
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            if self.ib.isConnected():
                logger.info(f"Connected to IBKR at {self.host}:{self.port}")
                return True
            return False
        except Exception as e:
            logger.error(f"IBKR Connection failed: {e}")
            return False
    
    def connect(self):
        """Synchronous connect to IBKR Gateway/TWS"""
        if self.ib.isConnected():
            return True
        
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id)
            if self.ib.isConnected():
                logger.info(f"Connected to IBKR at {self.host}:{self.port}")
                return True
            return False
        except Exception as e:
            logger.error(f"IBKR Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from IBKR"""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IBKR")
    
    def is_connected(self):
        """Check if connected to IBKR"""
        return self.ib.isConnected()
    
    def get_positions(self):
        """Get all current positions"""
        if not self.is_connected():
            return []
        
        try:
            positions = self.ib.positions()
            result = []
            for pos in positions:
                result.append({
                    "symbol": pos.contract.symbol,
                    "quantity": float(pos.position),
                    "avg_cost": float(pos.avgCost),
                    "contract": pos.contract
                })
            return result
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []
    
    def get_account_summary(self):
        """Get account summary values"""
        if not self.is_connected():
            return {}
        
        try:
            summary = self.ib.accountSummary()
            result = {}
            for item in summary:
                result[item.tag] = {
                    "value": item.value,
                    "currency": item.currency
                }
            return result
        except Exception as e:
            logger.error(f"Error getting account summary: {e}")
            return {}
    
    def get_buying_power(self):
        """Get available buying power"""
        summary = self.get_account_summary()
        bp = summary.get("BuyingPower", {}).get("value")
        if bp:
            return float(bp)
        return 0.0
    
    def cancel_all_orders(self):
        """Cancel all open orders"""
        if not self.is_connected():
            return {"status": "not_connected"}
        
        try:
            self.ib.reqGlobalCancel()
            logger.info("All orders cancelled")
            return {"status": "success"}
        except Exception as e:
            logger.error(f"Cancel all orders failed: {e}")
            return {"status": "error", "message": str(e)}
    
    async def qualify_contract_async(self, symbol, exchange='SMART', currency='USD'):
        """Qualify a stock contract asynchronously"""
        contract = Stock(symbol, exchange, currency)
        try:
            await self.ib.qualifyContractsAsync(contract)
            return contract
        except Exception as e:
            logger.error(f"Failed to qualify {symbol}: {e}")
            return None
    
    def qualify_contract(self, symbol, exchange='SMART', currency='USD'):
        """Qualify a stock contract synchronously"""
        contract = Stock(symbol, exchange, currency)
        try:
            self.ib.qualifyContracts(contract)
            return contract
        except Exception as e:
            logger.error(f"Failed to qualify {symbol}: {e}")
            return None
    
    def sleep(self, seconds):
        """IB-safe sleep"""
        self.ib.sleep(seconds)
    
    @property
    def raw(self):
        """Access underlying IB object for advanced operations"""
        return self.ib