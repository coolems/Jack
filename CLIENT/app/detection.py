"""Tool detection utilities - moved from tools/tool_detection.py

Analyzes user messages to determine required tools.
Now lives in app/ since it's core logic, not a tool itself.
"""

import re

# Tool detection patterns
TOOL_PATTERNS = {
    'web': [
        r'\b(?:search|google|look up|find)\s+(?:for\s+)?(?:on\s+)?(?:the\s+)?(?:web|internet)\b',
        r'\b(?:search|find)\s+(?:for)\s+\S+',
        r'\b(?:on|in)\s+(?:the\s+)?(?:web|internet)\b',
        r'\b(?:browse|look up|lookup)\s+\S+',
        r'\b(?:search|find|look).*on\s+(?:the\s+)?internet\b',
        r'\bsearch.*date\b',
        r'\bsearch.*today\b',
        r'\bsearch.*weather\b',
        r'\bwhat(?:\'s| is) (?:the )?(?:date|time|day|weather|news|current|live|today|now|price|score)\b',
        r'\b(?:ce|data|ora|vremea|az|acum)\b.*\b(?:este|ea|e)\b',
        r'\b(?:when|where|who).*(?:live|current|now|today)\b',
    ],
    'file': [
        r'\b(?:read|open|show|display|view)\s+(?:a\s+)?(?:file|document|pdf|doc|excel|csv)\b',
        r'\b(?:list|show)\s+(?:files?|documents?)\b',
        r'\.(?:pdf|docx?|xlsx?|csv|txt|py|js|html)\b',
        r'\b(?:file|document|pdf|word|excel)\s+(?:named|called)\b',
    ],
    'pdf': [
        r'\b(?:create|make|generate|do|save|export)\s+(?:a\s+)?(?:pdf|document)\b',
        r'\b(?:pdf)\s+(?:from|with|of)\b',
        r'\b(?:as)\s+(?:a\s+)?(?:pdf)\b',
        r'\b(?:into|to)\s+(?:a\s+)?(?:pdf)\b',
        r'\b(?:save|export).*\b(?:pdf)\b',
        r'\b(?:pdf\s*file|\.pdf)\b',
        r'\b(?:images?|pictures?|photos?)\s+(?:to|into)\s+(?:a\s+)?(?:pdf)\b',
    ],
    'ocr': [
        r'\b(?:transcribe|extract|recognize|read\s+text|read\s+from\s+image|what\s+does\s+this\s+image\s+say)\b',
        r'\b(?:image)\s+(?:says|contains|shows)\b',
        r'\b(?:scan|ocr|text\s+from)\s+(?:image|picture|photo|screenshot)\b',
    ],
    'calc': [
        r'\b(?:calculate|compute|solve|what\s+is)\s+[\d\+\-\*\/\(\)]+',
        r'\b(?:math|arithmetic|add|subtract|multiply|divide)\b.*\b(?:numbers?|values?)\b',
    ],
    'time': [
        r'\b(?:what|what\'s|whats|tell me|current|now)\s+(?:time|date|day|week|month|year)\b',
        r'\b(?:time|date|clock)\s+(?:in|at|for)\s+\w+\b',
        r'\b(?:what time is it|what\'s the time|current time)\b',
        r'\b(?:what day is it|what\'s today|today\'s date)\b',
        r'\b(?:timezone|time zone|utc|gmt|local time)\b',
        r'\b(?:what movies are now|movies playing now|now playing|currently in theaters)\b',
        r'\b(?:what\'s on netflix now|netflix now|streaming now|currently on)\b',
        r'\b(?:what\'s happening now|current events|live now)\b',
        r'\b(?:shows? now|events? now|concerts? now)\b',
        r'\b(?:new releases? this week|released this week|out now)\b',
        r'\b(?:schedule for today|today\'s schedule|today\'s events)\b',
    ],
    'browser': [
        # Browser navigation commands
        r'\b(?:go to|open|navigate to|visit)\s+(?:https?://)?(?:www\.)?\S+\.\S+\b',
        r'\b(?:search|find|look up)\s+(?:for\s+)?[\"\']?(.+?)[\"\']?\s+(?:on|using)\s+(?:google|chrome|browser|web)\b',
        r'\bsearch\s+google\s+for\s+(.+)\b',
        r'\btype\s+(.+)\s+in\s+google\b',
        r'\b(?:click|press|select)\s+(?:on\s+)?(?:the\s+)?(?:button|link|element)\s+[\"\']?(.+?)[\"\']?\b',
        r'\b(?:fill|enter|input)\s+(?:the\s+)?(?:form|field|box)\s+[\"\']?(.+?)[\"\']?\s+with\s+[\"\']?(.+?)[\"\']?\b',
        r'\b(?:take|make|capture)\s+(?:a\s+)?screenshot\b',
        r'\b(?:scroll|move)\s+(?:up|down|to\s+top|to\s+bottom)\b',
        r'\b(?:go\s+back|back\s+page|previous\s+page)\b',
        r'\b(?:list|show)\s+(?:my|all)\s+(?:tabs|pages)\b',
        r'\b(?:switch|change)\s+(?:to\s+)?(?:tab|page)\s+(\d+)\b',
        r'\bconnect\s+to\s+(?:my\s+)?chrome\s+browser\b',
        r'\b(?:disconnect|close)\s+(?:from\s+)?chrome\b',
    ],
}


def detect_tool_requirement(message: str) -> tuple:
    """Detect if a message requires tool usage.

    Returns: (requires_tools, list_of_tool_types)
    """
    message_lower = message.lower()
    needed_tools = []

    for pattern in TOOL_PATTERNS['browser']:
        if re.search(pattern, message_lower, re.IGNORECASE):
            needed_tools.append('browser')
            break

    for pattern in TOOL_PATTERNS['pdf']:
        if re.search(pattern, message_lower):
            needed_tools.append('pdf')
            break

    for pattern in TOOL_PATTERNS['web']:
        if re.search(pattern, message_lower):
            needed_tools.append('web')
            break

    for pattern in TOOL_PATTERNS['file']:
        if re.search(pattern, message_lower):
            needed_tools.append('file')
            break

    for pattern in TOOL_PATTERNS['calc']:
        if re.search(pattern, message_lower):
            needed_tools.append('calc')
            break

    for pattern in TOOL_PATTERNS['ocr']:
        if re.search(pattern, message_lower):
            needed_tools.append('ocr')
            break

    for pattern in TOOL_PATTERNS['time']:
        if re.search(pattern, message_lower):
            needed_tools.append('time')
            break

    return len(needed_tools) > 0, needed_tools


def detect_dont_know_response(response: str) -> bool:
    """Detect if an AI response indicates it doesn't know the answer."""
    if not response:
        return False

    response_lower = response.lower()

    dont_know_patterns = [
        r"nu am acces", r"nu am informa?ii", r"nu pot s?", r"nu pot afla",
        r"nu ?tiu", r"nu stiu", r"nu am habar", r"nu pot verifica",
        r"nu am posibilitatea", r"nu dispun", r"f?r? acces",
        r"i don't have access", r"i don't know", r"i cannot access",
        r"i'm not sure about", r"i don't have", r"i can't provide",
        r"i cannot provide", r"i'm unable to", r"do not have access",
        r"don't have access", r"cannot determine", r"can't tell",
        r"could not determine", r"unsure", r"unknown",
        r"i have no idea", r"no way to know", r"no information",
        r"no access to", r"not able to find", r"can't access",
        r"cannot access", r"i don't have the ability to", r"i lack the ability",
    ]

    for pattern in dont_know_patterns:
        if re.search(pattern, response_lower):
            return True

    if len(response.strip()) < 50 and any(
        word in response_lower for word in [
            "nu", "nu pot", "i cannot", "i can't", "i do not"
        ]
    ):
        return True

    return False


def detect_needs_live_data(query: str) -> bool:
    """Detect if a query requires live/current data (needs web search)."""
    query_lower = query.lower()

    live_data_patterns = [
        r"ce data e azi", r"ce dat? e azi", r"what'?s the date",
        r"what day is it", r"today'?s date",
        r"current (weather|temperature|time|price)",
        r"latest news", r"recent (news|events)", r"stock price",
        r"how (much|many) (is|are) .* now", r"vremea acum", r"temperatura acum",
        r"news today", r"when (was|will|does)", r"cum e vremea", r"unde e",
        r"who (is|was) .* now", r"what is happening", r"breaking (news|alert)",
        r"search date of today", r"search on internet", r"date of today",
        r"what'?s today", r"what day (?:is it|today)", r"current date",
        r"today'?s day", r"live (weather|score|price|news)", r"real[- ]time",
        r"as of now", r"up to the minute",
    ]

    for pattern in live_data_patterns:
        if re.search(pattern, query_lower):
            return True

    question_indicators = [
        r"^ce ", r"^what ", r"^who ", r"^when ", r"^where ", r"^how ", r"\?$"
    ]
    has_question = any(re.search(p, query_lower) for p in question_indicators)
    has_time_words = any(w in query_lower for w in [
        "azi", "today", "now", "current", "latest", "recent",
        "acum", "curent", "live"
    ])

    return has_question and has_time_words


def detect_browser_command(message: str) -> tuple:
    """Detect if a message is a browser automation command.

    Returns: (is_browser_command, command_type, extracted_params)
    """
    message_lower = message.lower()

    if re.search(r'connect\s+to\s+(?:my\s+)?chrome\s+browser', message_lower):
        return True, 'connect', {}

    if re.search(r'(?:disconnect|close)\s+(?:from\s+)?chrome', message_lower):
        return True, 'disconnect', {}

    goto_match = re.search(
        r'(?:go to|open|navigate to|visit)\s+(?:https?://)?(?:www\.)?([^\s]+)',
        message_lower
    )
    if goto_match:
        url = goto_match.group(1)
        if not url.startswith('http'):
            url = 'https://' + url
        return True, 'goto', {'url': url}

    search_match = re.search(
        r'(?:search|find|look up)\s+(?:for\s+)?[\"\']?(.+?)[\"\']?\s+'
        r'(?:on|using)\s+(?:google|chrome)', message_lower
    )
    if not search_match:
        search_match = re.search(r'search\s+google\s+for\s+(.+)', message_lower)
    if not search_match:
        search_match = re.search(r'type\s+(.+)\s+in\s+google', message_lower)
    if not search_match:
        search_match = re.search(
            r'search\s+for\s+[\"\']?(.+?)[\"\']?$', message_lower
        )

    if search_match:
        query = search_match.group(1).strip()
        return True, 'search', {'query': query}

    click_match = re.search(
        r'(?:click|press|select)\s+(?:on\s+)?(?:the\s+)?'
        r'(?:button|link|element)?\s+[\"\']?(.+?)[\"\']?$', message_lower
    )
    if click_match:
        return True, 'click', {'selector': click_match.group(1).strip()}

    fill_match = re.search(
        r'(?:fill|enter|input)\s+(?:the\s+)?(?:form|field|box)?\s+'
        r'[\"\']?(.+?)[\"\']?\s+with\s+[\"\']?(.+?)[\"\']?$', message_lower
    )
    if fill_match:
        field = fill_match.group(1).strip()
        value = fill_match.group(2).strip()
        return True, 'fill', {'selector': field, 'value': value}

    if re.search(r'(?:take|make|capture)\s+(?:a\s+)?screenshot', message_lower):
        return True, 'screenshot', {}

    if re.search(r'scroll\s+(up|down)', message_lower):
        direction = 'down' if 'down' in message_lower else 'up'
        return True, 'scroll', {'direction': direction}

    if re.search(r'(?:go\s+back|back\s+page|previous\s+page)', message_lower):
        return True, 'back', {}

    if re.search(r'(?:list|show)\s+(?:my|all)\s+(?:tabs|pages)', message_lower):
        return True, 'list_pages', {}

    switch_match = re.search(
        r'(?:switch|change)\s+(?:to\s+)?(?:tab|page)\s+(\d+)', message_lower
    )
    if switch_match:
        index = int(switch_match.group(1)) - 1
        return True, 'switch_page', {'index': index}

    if re.search(
        r'(?:what\'s on|what is on|show me|tell me about)\s+'
        r'(?:this|the)\s+page', message_lower
    ):
        return True, 'get_state', {}

    return False, None, {}
