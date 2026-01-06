import os
import json
import logging
import threading
import requests
from typing import Optional

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
WEBHOOK_TIMEOUT = int(os.environ.get("WEBHOOK_TIMEOUT", "5"))  # Reduced default
ALERT_EVENTS = os.environ.get("ALERT_EVENTS", "trade,stop_loss,error,bot_status").lower().split(",")


def _send_webhook(payload: dict) -> bool:
    """Internal blocking webhook send."""
    if not WEBHOOK_URL:
        return False
    
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json=payload,
            timeout=WEBHOOK_TIMEOUT,
            headers={"Content-Type": "application/json"}
        )
        
        if resp.status_code in (200, 201, 204):
            logger.info(f"Alert sent: {payload.get('event_type')} - {payload.get('title')}")
            return True
        else:
            logger.warning(f"Webhook returned {resp.status_code}: {resp.text[:100]}")
            return False
            
    except requests.exceptions.Timeout:
        logger.warning(f"Webhook timeout after {WEBHOOK_TIMEOUT}s")
        return False
    except requests.exceptions.RequestException as e:
        logger.warning(f"Webhook error: {e}")
        return False


def send_alert(
    event_type: str,
    title: str,
    message: str,
    data: Optional[dict] = None,
    blocking: bool = False
) -> bool:
    """
    Send alert to HomeAssistant webhook.
    
    Args:
        event_type: Type of event (trade, stop_loss, error, bot_status)
        title: Alert title
        message: Alert message
        data: Additional data dict
        blocking: If True, wait for response. If False, fire and forget.
    """
    if not WEBHOOK_URL:
        return False
    
    # Check if this event type is enabled
    if event_type not in ALERT_EVENTS and "all" not in ALERT_EVENTS:
        logger.debug(f"Alert type '{event_type}' not in ALERT_EVENTS, skipping")
        return False
    
    payload = {
        "event_type": event_type,
        "title": title,
        "message": message,
        "data": data or {}
    }
    
    if blocking:
        return _send_webhook(payload)
    else:
        # Fire and forget - don't block the trading loop
        thread = threading.Thread(target=_send_webhook, args=(payload,), daemon=True)
        thread.start()
        return True  # Assume success for async


# === Convenience Functions (All non-blocking by default) ===

def alert_trade(pair: str, side: str, volume: float, price: float, notional: float, reason: str, mode: str):
    """Alert on trade execution."""
    emoji = "🟢" if side.upper() == "BUY" else "🔴"
    title = f"{emoji} {side.upper()} {pair}"
    message = f"{side.upper()} {volume:.6f} {pair} @ ${price:.4f} (${notional:.2f})\nReason: {reason}"
    
    send_alert(
        event_type="trade",
        title=title,
        message=message,
        data={
            "pair": pair,
            "side": side,
            "volume": volume,
            "price": price,
            "notional_usd": notional,
            "reason": reason,
            "mode": mode
        }
    )


def alert_stop_loss(pair: str, stop_type: str, entry_price: float, exit_price: float, pnl_pct: float):
    """Alert on stop-loss trigger."""
    title = f"🛑 {stop_type}: {pair}"
    message = f"{pair} stopped out. Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f} ({pnl_pct:+.2f}%)"
    
    send_alert(
        event_type="stop_loss",
        title=title,
        message=message,
        data={
            "pair": pair,
            "stop_type": stop_type,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct
        }
    )


def alert_error(component: str, error_message: str):
    """Alert on bot errors."""
    title = f"❌ Error: {component}"
    message = error_message[:500]
    
    send_alert(
        event_type="error",
        title=title,
        message=message,
        data={
            "component": component,
            "error": error_message
        }
    )


def alert_bot_status(bot_name: str, status: str, details: str = ""):
    """Alert on bot start/stop."""
    emoji = "✅" if status == "started" else "⏹️"
    title = f"{emoji} {bot_name} {status}"
    message = details or f"{bot_name} has {status}"
    
    send_alert(
        event_type="bot_status",
        title=title,
        message=message,
        data={
            "bot": bot_name,
            "status": status
        }
    )