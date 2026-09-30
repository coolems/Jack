"""Get current time function - returns local time and GMT time with timezone offset"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Get current local time, UTC time, timezone offset, and date info. Use for any time/date queries. Returns formatted time block with weekday, DST status, and timestamp.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone (e.g., 'America/New_York', 'Europe/London'). Optional - omit for system local time. For a city name, use the get_time_for_city tool instead; this tool only accepts IANA zones."}
            },
            "required": []
        }
    }
}

import logging
import datetime
import pytz

logger = logging.getLogger("COOLEMS.Tools.Time")


def get_current_time(timezone: str = None, system_timezone: str = "UTC") -> str:
    """
    Get current local time and GMT time with timezone offset.

    Args:
        timezone: Optional IANA timezone name (e.g., 'America/New_York', 'Europe/London').
                  If not provided, uses system local time. NOTE: this is an IANA zone
                  only - a bare city name will be rejected as unknown; use the
                  get_time_for_city tool for city names.
        system_timezone: Fallback label used in the output when the resolved tzinfo
                  cannot be named (rare); defaults to "UTC".

    Returns:
        Formatted string with local time, GMT time, and timezone offset
    
    Returns:
        Formatted string with local time, GMT time, and timezone offset
    """
    try:
        # Get current UTC time
        utc_now = datetime.datetime.now(pytz.UTC)
        
        # Get local time
        if timezone:
            try:
                local_tz = pytz.timezone(timezone)
                local_now = utc_now.astimezone(local_tz)
            except pytz.exceptions.UnknownTimeZoneError:
                return f"Error: Unknown timezone '{timezone}'. Use IANA format like 'America/New_York', 'Europe/London', 'Asia/Tokyo'"
        else:
            # Use system local time
            local_now = datetime.datetime.now().astimezone()
            local_tz = local_now.tzinfo
        
        # Calculate UTC offset
        utc_offset = local_now.strftime('%z')
        # Format offset as +HH:MM or -HH:MM
        if len(utc_offset) == 5:
            formatted_offset = f"{utc_offset[:3]}:{utc_offset[3:]}"
        else:
            formatted_offset = utc_offset
        
        # Get timezone name
        tz_name = str(local_tz) if local_tz else system_timezone
        
        # Format local time with milliseconds
        local_time_str = local_now.strftime('%Y-%m-%d %H:%M:%S')
        local_ms = local_now.strftime('%f')[:3]
        local_full = f"{local_time_str}.{local_ms}"
        
        # Format UTC time
        utc_time_str = utc_now.strftime('%Y-%m-%d %H:%M:%S')
        utc_ms = utc_now.strftime('%f')[:3]
        utc_full = f"{utc_time_str}.{utc_ms}"
        
        # Build response
        result = f"""📅 **Current Time Information**

**Local Time:** {local_full}
**Time Zone:** {tz_name}
**UTC Offset:** {formatted_offset}

**GMT/UTC Time:** {utc_full}

**Timestamp:** {int(local_now.timestamp())}

**Additional Info:**
- Weekday: {local_now.strftime('%A')}
- Day of Year: {local_now.strftime('%j')}
- Week Number: {local_now.strftime('%W')}
- DST Active: {bool(local_now.dst())}"""
        
        # Add suggestion if timezone was auto-detected
        if not timezone:
            result += f"\n\n💡 Tip: For specific timezones, ask for time in a city (e.g., 'What time is it in Tokyo?')"
        
        logger.info(f"Time tool executed: Local={local_full}, Offset={formatted_offset}")
        return result
        
    except Exception as e:
        logger.error(f"Time tool error: {e}")
        return f"Error: Could not get current time: {str(e)}"