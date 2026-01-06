"""
Market Hours Validation for US Stocks
Handles weekends, holidays, and regular trading hours.
"""

from datetime import datetime, time as dt_time
from typing import Tuple

# US Eastern timezone offset handling without pytz dependency
# Note: This is a simplified approach. For production, consider using pytz or zoneinfo (Python 3.9+)
import os
import time as time_module

def _get_eastern_now() -> datetime:
    """
    Get current time in US Eastern timezone.
    Handles both EST (UTC-5) and EDT (UTC-4) based on date.
    """
    utc_now = datetime.utcnow()
    
    # Determine if we're in DST (rough approximation)
    # DST in US: Second Sunday of March to First Sunday of November
    year = utc_now.year
    
    # March: DST starts second Sunday
    march_first = datetime(year, 3, 1)
    march_second_sunday = march_first.day + (6 - march_first.weekday() + 7) % 7 + 7
    dst_start = datetime(year, 3, march_second_sunday, 2, 0)
    
    # November: DST ends first Sunday
    nov_first = datetime(year, 11, 1)
    nov_first_sunday = nov_first.day + (6 - nov_first.weekday()) % 7
    dst_end = datetime(year, 11, nov_first_sunday, 2, 0)
    
    # Check if we're in DST
    if dst_start <= utc_now < dst_end:
        offset_hours = 4  # EDT (UTC-4)
    else:
        offset_hours = 5  # EST (UTC-5)
    
    from datetime import timedelta
    return utc_now - timedelta(hours=offset_hours)


# NYSE/NASDAQ Regular Trading Hours
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)

# US Market Holidays 2024-2026
# Format: (year, month, day)
US_MARKET_HOLIDAYS = {
    # 2024
    (2024, 1, 1),    # New Year's Day
    (2024, 1, 15),   # MLK Day
    (2024, 2, 19),   # Presidents Day
    (2024, 3, 29),   # Good Friday
    (2024, 5, 27),   # Memorial Day
    (2024, 6, 19),   # Juneteenth
    (2024, 7, 4),    # Independence Day
    (2024, 9, 2),    # Labor Day
    (2024, 11, 28),  # Thanksgiving
    (2024, 12, 25),  # Christmas
    
    # 2025
    (2025, 1, 1),    # New Year's Day
    (2025, 1, 20),   # MLK Day
    (2025, 2, 17),   # Presidents Day
    (2025, 4, 18),   # Good Friday
    (2025, 5, 26),   # Memorial Day
    (2025, 6, 19),   # Juneteenth
    (2025, 7, 4),    # Independence Day
    (2025, 9, 1),    # Labor Day
    (2025, 11, 27),  # Thanksgiving
    (2025, 12, 25),  # Christmas
    
    # 2026
    (2026, 1, 1),    # New Year's Day
    (2026, 1, 19),   # MLK Day
    (2026, 2, 16),   # Presidents Day
    (2026, 4, 3),    # Good Friday
    (2026, 5, 25),   # Memorial Day
    (2026, 6, 19),   # Juneteenth
    (2026, 7, 3),    # Independence Day (observed)
    (2026, 9, 7),    # Labor Day
    (2026, 11, 26),  # Thanksgiving
    (2026, 12, 25),  # Christmas
}

# Early close days (1:00 PM ET) - day before/after major holidays
US_EARLY_CLOSE_DAYS = {
    # 2024
    (2024, 7, 3),    # Day before July 4th
    (2024, 11, 29),  # Day after Thanksgiving
    (2024, 12, 24),  # Christmas Eve
    
    # 2025
    (2025, 7, 3),    # Day before July 4th
    (2025, 11, 28),  # Day after Thanksgiving
    (2025, 12, 24),  # Christmas Eve
    
    # 2026
    (2026, 11, 27),  # Day after Thanksgiving
    (2026, 12, 24),  # Christmas Eve
}

EARLY_CLOSE_TIME = dt_time(13, 0)  # 1:00 PM ET


def is_us_market_open() -> Tuple[bool, str]:
    """
    Check if US stock markets (NYSE/NASDAQ) are currently open.
    
    Returns:
        Tuple[bool, str]: (is_open, reason_string)
        
    Example:
        >>> is_open, reason = is_us_market_open()
        >>> if not is_open:
        ...     print(f"Market closed: {reason}")
    """
    now_et = _get_eastern_now()
    current_date = (now_et.year, now_et.month, now_et.day)
    current_time = now_et.time()
    weekday = now_et.weekday()  # Monday=0, Sunday=6
    
    # 1. Weekend check
    if weekday >= 5:
        day_name = "Saturday" if weekday == 5 else "Sunday"
        return False, f"Weekend ({day_name})"
    
    # 2. Holiday check
    if current_date in US_MARKET_HOLIDAYS:
        return False, f"US Market Holiday ({now_et.strftime('%Y-%m-%d')})"
    
    # 3. Early close day check
    close_time = MARKET_CLOSE
    if current_date in US_EARLY_CLOSE_DAYS:
        close_time = EARLY_CLOSE_TIME
    
    # 4. Trading hours check
    if current_time < MARKET_OPEN:
        minutes_until = (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute) - (current_time.hour * 60 + current_time.minute)
        return False, f"Pre-market ({now_et.strftime('%H:%M')} ET, opens in {minutes_until}min)"
    
    if current_time >= close_time:
        close_str = close_time.strftime('%H:%M')
        return False, f"After-hours ({now_et.strftime('%H:%M')} ET, closed at {close_str})"
    
    # Market is open
    close_str = close_time.strftime('%H:%M')
    return True, f"Market Open ({now_et.strftime('%H:%M')} ET, closes {close_str})"


def get_next_market_open() -> Tuple[int, str]:
    """
    Calculate seconds until next market open.
    
    Returns:
        Tuple[int, str]: (seconds_until_open, human_readable_description)
    """
    now_et = _get_eastern_now()
    
    from datetime import timedelta
    
    # Start checking from today
    check_date = now_et.date()
    
    for _ in range(10):  # Check up to 10 days ahead
        date_tuple = (check_date.year, check_date.month, check_date.day)
        weekday = check_date.weekday()
        
        # Skip weekends and holidays
        if weekday < 5 and date_tuple not in US_MARKET_HOLIDAYS:
            # This is a valid trading day
            market_open_dt = datetime.combine(check_date, MARKET_OPEN)
            
            # If it's today and we haven't passed open yet
            if check_date == now_et.date() and now_et.time() < MARKET_OPEN:
                delta = market_open_dt - now_et.replace(tzinfo=None)
                return int(delta.total_seconds()), f"Opens today at 09:30 ET"
            
            # If it's a future day
            if check_date > now_et.date():
                # Calculate from current time to that day's open
                delta = market_open_dt - now_et.replace(tzinfo=None)
                hours = int(delta.total_seconds() // 3600)
                if hours < 24:
                    return int(delta.total_seconds()), f"Opens tomorrow at 09:30 ET"
                else:
                    return int(delta.total_seconds()), f"Opens {check_date.strftime('%A %m/%d')} at 09:30 ET"
        
        check_date += timedelta(days=1)
    
    return 86400 * 3, "Unable to determine next open"


def wait_for_market_open(logger=None) -> None:
    """
    Block execution until market opens. Useful for bot startup.
    
    Args:
        logger: Optional logger instance for status messages
    """
    import time as time_module
    
    is_open, reason = is_us_market_open()
    
    if is_open:
        if logger:
            logger.info(f"Market Status: {reason}")
        return
    
    seconds_until, description = get_next_market_open()
    
    if logger:
        logger.info(f"Market Closed: {reason}")
        logger.info(f"Next Open: {description}")
        logger.info(f"Sleeping for {seconds_until // 60} minutes...")
    
    # Sleep in chunks to allow for graceful shutdown
    while seconds_until > 0:
        sleep_chunk = min(300, seconds_until)  # 5 minute chunks max
        time_module.sleep(sleep_chunk)
        seconds_until -= sleep_chunk
        
        # Re-check in case of DST change or other edge cases
        is_open, _ = is_us_market_open()
        if is_open:
            break