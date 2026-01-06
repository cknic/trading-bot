"""
IBKR Order Placement Module
"""

import logging
from ib_insync import MarketOrder, LimitOrder

logger = logging.getLogger("IBKR_ORDERS")


async def place_order_async(client, contract, action, quantity, order_type="market", limit_price=None):
    """
    Place an order asynchronously.
    
    Args:
        client: IBKRClient instance
        contract: Qualified IB contract
        action: "BUY" or "SELL"
        quantity: Number of shares (can be fractional)
        order_type: "market" or "limit"
        limit_price: Price for limit orders
    
    Returns:
        (success: bool, result: dict)
    """
    if not client.is_connected():
        return False, {"reason": "Not connected to IBKR"}
    
    try:
        if order_type == "limit" and limit_price:
            order = LimitOrder(action, quantity, limit_price)
        else:
            order = MarketOrder(action, quantity)
        
        trade = client.raw.placeOrder(contract, order)
        
        # Wait for fill (with timeout)
        timeout = 30
        elapsed = 0
        while not trade.isDone() and elapsed < timeout:
            await client.raw.sleep(1)
            elapsed += 1
        
        if trade.isDone():
            fill_price = trade.orderStatus.avgFillPrice
            filled_qty = trade.orderStatus.filled
            
            return True, {
                "status": "filled",
                "symbol": contract.symbol,
                "action": action,
                "qty": float(filled_qty),
                "price": float(fill_price),
                "order_id": trade.order.orderId
            }
        else:
            # Order not filled in time - still pending
            return True, {
                "status": "pending",
                "symbol": contract.symbol,
                "action": action,
                "qty": float(quantity),
                "price": limit_price or 0,
                "order_id": trade.order.orderId
            }
            
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False, {"reason": str(e)}


def place_order_sync(client, contract, action, quantity, order_type="market", limit_price=None):
    """Synchronous order placement"""
    if not client.is_connected():
        return False, {"reason": "Not connected to IBKR"}
    
    try:
        if order_type == "limit" and limit_price:
            order = LimitOrder(action, quantity, limit_price)
        else:
            order = MarketOrder(action, quantity)
        
        trade = client.raw.placeOrder(contract, order)
        client.sleep(2)  # Wait briefly for fill
        
        fill_price = trade.orderStatus.avgFillPrice or 0
        filled_qty = trade.orderStatus.filled or quantity
        
        return True, {
            "status": "placed",
            "symbol": contract.symbol,
            "action": action,
            "qty": float(filled_qty),
            "price": float(fill_price),
            "order_id": trade.order.orderId
        }
        
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False, {"reason": str(e)}


def simulate_order(symbol, action, quantity, price, mode="paper"):
    """
    Simulate an order for paper/dry-run trading.
    
    Returns:
        (success: bool, result: dict)
    """
    return True, {
        "status": "simulated",
        "mode": mode,
        "symbol": symbol,
        "action": action,
        "qty": float(quantity),
        "price": float(price)
    }


def calculate_quantity(notional_usd, price, min_qty=0.0001):
    """
    Calculate order quantity from notional value.
    
    Args:
        notional_usd: Dollar amount to trade
        price: Current price per share
        min_qty: Minimum quantity (for fractional shares)
    
    Returns:
        Quantity (float for fractional, or int for whole shares)
    """
    if price <= 0:
        return 0
    
    qty = notional_usd / price
    
    # Round to 4 decimal places for fractional shares
    qty = round(qty, 4)
    
    if qty < min_qty:
        return 0
    
    return qty