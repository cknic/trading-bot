import os
import json
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "bot_errors.jsonl")


def log_error(bot: str, level: str, message: str):
    """
    Log an error/warning to the shared error log file.
    
    Args:
        bot: "crypto" or "stocks"
        level: "ERROR", "WARN", or "INFO"
        message: The log message
    """
    entry = {
        "ts": int(time.time()),
        "bot": bot,
        "level": level.upper(),
        "message": message
    }
    
    try:
        with open(ERROR_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Failed to write error log: {e}")


def log_crypto_error(message: str):
    log_error("crypto", "ERROR", message)


def log_crypto_warn(message: str):
    log_error("crypto", "WARN", message)


def log_crypto_info(message: str):
    log_error("crypto", "INFO", message)


def log_stocks_error(message: str):
    log_error("stocks", "ERROR", message)


def log_stocks_warn(message: str):
    log_error("stocks", "WARN", message)


def log_stocks_info(message: str):
    log_error("stocks", "INFO", message)