"""Recognize raw tool-call replies without treating prose or code examples as calls."""

import json
import re
from uuid import uuid4

_NAME = r"[A-Za-z0-9_.-]+"
_OPEN = re.compile(r"<tool_call\s*>", re.I)
_CLOSE = re.compile(r"\s*</tool_call\s*>", re.I)
_FUNCTION = re.compile(rf"<function\s*=\s*({_NAME})\s*>(.*?)</function\s*>", re.I | re.S)
_PARAM = re.compile(rf"<parameter\s*=\s*({_NAME})\s*>(.*?)</(?:\1|parameter)\s*>", re.I | re.S)
_BARE = re.compile(rf"<({_NAME})\s*>(.*?)</\1\s*>", re.I | re.S)
_START = re.compile(rf"<(?:tool_call\s*>|(?:function|parameter)\s*=\s*{_NAME}\s*>)", re.I)
_PLACEHOLDERS = {"name", "tool", "function", "function_name", "tool_name", "tool_call", "tools"}
_DECODER = json.JSONDecoder(strict=False)


def _parameters(body):
    args = {}
    while body := body.lstrip():
        match = _PARAM.match(body) or _BARE.match(body)
        if not match or match[1] in args:
            raise ValueError("invalid or repeated parameter")
        args[match[1]] = match[2].strip()
        body = body[match.end():]
    return args


def parse_markup_tool_calls(text):
    """Parse a complete reply of JSON/XML calls; reject incomplete or mixed prose replies."""
    if not isinstance(text, str):
        return []
    calls = []
    try:
        while text := text.lstrip():
            opener = _OPEN.match(text)
            if opener:
                text = text[opener.end():].lstrip()
            if opener and text.startswith("{"):
                obj, end = _DECODER.raw_decode(text)
                func = obj.get("function", obj)
                if not isinstance(func, dict):
                    return []
                name = func.get("name")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    args = json.loads(args)
            else:
                match = _FUNCTION.match(text)
                if not match:
                    return []
                name, body = match.groups()
                args = _parameters(body)
                end = match.end()
            text = text[end:]
            if opener:
                closer = _CLOSE.match(text)
                if not closer:
                    return []
                text = text[closer.end():]
            if (not isinstance(name, str) or not re.fullmatch(_NAME, name)
                    or name.lower() in _PLACEHOLDERS or not isinstance(args, dict)):
                return []
            calls.append({"id": f"markup_{uuid4().hex}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args, allow_nan=False)}})
    except (ValueError, RecursionError):
        return []
    return calls


def extract_markup_tool_names(text):
    """Return distinct names from a fully parsed reply, in original order."""
    return list(dict.fromkeys(call["function"]["name"] for call in parse_markup_tool_calls(text)))


def looks_like_tool_markup(text):
    """Recognize a raw protocol reply, including malformed and nameless blocks."""
    return isinstance(text, str) and _START.match(text.lstrip()) is not None
