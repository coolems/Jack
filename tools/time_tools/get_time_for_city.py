"""Get time for city function - returns current time for a specific city"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_time_for_city",
        "description": "Get current time for a specific city worldwide. Resolves city name to IANA timezone automatically. Returns full time info block. Use when user asks 'what time is it in [city]'.",
        "parameters": {
            "type": "object",
            "properties": {
                "city_name": {"type": "string", "description": "Name of the city (e.g., 'New York', 'London', 'Tokyo', 'Dubai'). Case-insensitive."}
            },
            "required": ["city_name"]
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.Time")


def get_time_for_city(city_name: str) -> str:
    """
    Get current time for a specific city.
    
    Args:
        city_name: Name of the city (e.g., 'New York', 'London', 'Tokyo')
    
    Returns:
        Formatted string with time information for that city
    """
    # Map common cities to timezones
    city_timezone_map = {
        # Americas
        'new york': 'America/New_York',
        'nyc': 'America/New_York',
        'los angeles': 'America/Los_Angeles',
        'la': 'America/Los_Angeles',
        'chicago': 'America/Chicago',
        'toronto': 'America/Toronto',
        'vancouver': 'America/Vancouver',
        'mexico city': 'America/Mexico_City',
        'sao paulo': 'America/Sao_Paulo',
        'buenos aires': 'America/Argentina/Buenos_Aires',
        
        # Europe
        'london': 'Europe/London',
        'paris': 'Europe/Paris',
        'berlin': 'Europe/Berlin',
        'rome': 'Europe/Rome',
        'madrid': 'Europe/Madrid',
        'amsterdam': 'Europe/Amsterdam',
        'brussels': 'Europe/Brussels',
        'vienna': 'Europe/Vienna',
        'zurich': 'Europe/Zurich',
        'stockholm': 'Europe/Stockholm',
        'oslo': 'Europe/Oslo',
        'copenhagen': 'Europe/Copenhagen',
        'helsinki': 'Europe/Helsinki',
        'warsaw': 'Europe/Warsaw',
        'prague': 'Europe/Prague',
        'budapest': 'Europe/Budapest',
        'bucharest': 'Europe/Bucharest',
        'sofia': 'Europe/Sofia',
        'athens': 'Europe/Athens',
        'istanbul': 'Europe/Istanbul',
        'moscow': 'Europe/Moscow',
        
        # Asia
        'tokyo': 'Asia/Tokyo',
        'seoul': 'Asia/Seoul',
        'beijing': 'Asia/Shanghai',
        'shanghai': 'Asia/Shanghai',
        'hong kong': 'Asia/Hong_Kong',
        'singapore': 'Asia/Singapore',
        'mumbai': 'Asia/Kolkata',
        'delhi': 'Asia/Kolkata',
        'bangkok': 'Asia/Bangkok',
        'jakarta': 'Asia/Jakarta',
        'kuala lumpur': 'Asia/Kuala_Lumpur',
        'manila': 'Asia/Manila',
        'taipei': 'Asia/Taipei',
        
        # Australia & Pacific
        'sydney': 'Australia/Sydney',
        'melbourne': 'Australia/Melbourne',
        'brisbane': 'Australia/Brisbane',
        'perth': 'Australia/Perth',
        'auckland': 'Pacific/Auckland',
        'wellington': 'Pacific/Auckland',
        
        # Africa
        'cairo': 'Africa/Cairo',
        'johannesburg': 'Africa/Johannesburg',
        'nairobi': 'Africa/Nairobi',
        'lagos': 'Africa/Lagos',
        'casablanca': 'Africa/Casablanca',
        
        # Middle East
        'dubai': 'Asia/Dubai',
        'abudhabi': 'Asia/Dubai',
        'riyadh': 'Asia/Riyadh',
        'doha': 'Asia/Qatar',
        'tel aviv': 'Asia/Jerusalem',
        'jerusalem': 'Asia/Jerusalem',
    }
    
    city_lower = city_name.lower().strip()
    
    # Try direct match
    if city_lower in city_timezone_map:
        timezone = city_timezone_map[city_lower]
        from .get_current_time import get_current_time
        return get_current_time(timezone)
    
    # Try partial match
    for city_key, tz in city_timezone_map.items():
        if city_key in city_lower or city_lower in city_key:
            from .get_current_time import get_current_time
            return get_current_time(tz)
    
    # If city not found, return error with available cities
    major_cities = list(city_timezone_map.keys())[:20]
    return f"ERROR: Could not find timezone for '{city_name}'. Available cities: {', '.join(major_cities)}... or use exact timezone name."