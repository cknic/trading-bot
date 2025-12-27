import os
import json
import requests
import time

# You will put your Discord Webhook URL in secrets.env later
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def send(message: str, level: str = "info"):
    """
    Sends a message to Discord.
    level: 'info', 'warning', 'critical', 'trade'
    """
    if not DISCORD_WEBHOOK_URL:
        return

    color = 0x3b82f6 # Blue (Info)
    if level == "warning": color = 0xf59e0b # Orange
    if level == "critical": color = 0xef4444 # Red
    if level == "trade": color = 0x10b981 # Green

    payload = {
        "embeds": [{
            "title": f"Trading Bot: {level.upper()}",
            "description": message,
            "color": color,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }]
    }

    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send notification: {e}")