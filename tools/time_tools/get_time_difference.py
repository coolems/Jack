"""Get time difference function - returns time difference between two timezones"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_time_difference",
        "description": "Calculate time difference between two timezones or cities. Returns current time in both zones and hours/minutes offset. Use when comparing schedules across locations.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone1": {"type": "string", "description": "First timezone/city (e.g., 'London', 'UTC', 'America/New_York')"},
                "timezone2": {"type": "string", "description": "Second timezone/city (e.g., 'Tokyo', 'Europe/Paris')"}
            },
            "required": ["timezone1", "timezone2"]
        }
    }
}

import logging
import datetime
import pytz

logger = logging.getLogger("COOLEMS.Tools.Time")


def get_time_difference(timezone1: str, timezone2: str) -> str:
    """
    Get time difference between two timezones.
    
    Args:
        timezone1: First timezone (IANA format or city name)
        timezone2: Second timezone (IANA format or city name)
    
    Returns:
        Formatted string with time difference
    """
    try:
        # Try to resolve if city names were provided
        tz1 = _resolve_timezone(timezone1)
        tz2 = _resolve_timezone(timezone2)
        
        utc_now = datetime.datetime.now(pytz.UTC)
        time1 = utc_now.astimezone(pytz.timezone(tz1))
        time2 = utc_now.astimezone(pytz.timezone(tz2))
        
        # Calculate offset difference
        offset1 = time1.utcoffset()
        offset2 = time2.utcoffset()
        diff = offset2 - offset1
        
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        
        result = f"""⏰ **Time Difference**

**{tz1}:** {time1.strftime('%Y-%m-%d %H:%M:%S')}
**{tz2}:** {time2.strftime('%Y-%m-%d %H:%M:%S')}

**Difference:** {tz2} is {hours} hours {minutes} minutes {'ahead' if diff.total_seconds() > 0 else 'behind'} {tz1}"""
        
        return result
        
    except Exception as e:
        return f"Error: Could not calculate time difference: {str(e)}"


def _resolve_timezone(tz_input: str) -> str:
    """Resolve a timezone input (could be city name or IANA zone)"""
    # Check if it's already a valid IANA timezone
    try:
        pytz.timezone(tz_input)
        return tz_input
    except Exception:
        logger.debug("Non-critical exception caught at tools/time_tools/get_time_difference.py:73")
    
    # Try to resolve as city
    city_timezone_map = {
        'new york': 'America/New_York',
        'london': 'Europe/London',
        'paris': 'Europe/Paris',
        'tokyo': 'Asia/Tokyo',
        'sydney': 'Australia/Sydney',
        'los angeles': 'America/Los_Angeles',
        'chicago': 'America/Chicago',
        'toronto': 'America/Toronto',
        'berlin': 'Europe/Berlin',
        'rome': 'Europe/Rome',
        'madrid': 'Europe/Madrid',
        'amsterdam': 'Europe/Amsterdam',
        'moscow': 'Europe/Moscow',
        'beijing': 'Asia/Shanghai',
        'hong kong': 'Asia/Hong_Kong',
        'singapore': 'Asia/Singapore',
        'mumbai': 'Asia/Kolkata',
        'dubai': 'Asia/Dubai',
    }
    
    city_lower = tz_input.lower()
    if city_lower in city_timezone_map:
        return city_timezone_map[city_lower]
    
    # Return original if not found (will cause error later)
    return tz_input