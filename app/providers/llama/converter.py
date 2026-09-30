"""
Vision message converter for llama.cpp.

Converts Ollama-style 'images' key into OpenAI-compatible multimodal format
that llama.cpp expects via /v1/chat/completions.
"""
from typing import List, Dict


def convert_messages_for_vision(messages: List[Dict]) -> List[Dict]:
    """
    Convert messages that use the Ollama-style 'images' key into the
    OpenAI-compatible multimodal format that llama.cpp expects.

    Ollama format:
        {"role": "user", "content": "text", "images": ["base64data", ...]}

    OpenAI / llama.cpp format:
        {"role": "user", "content": [
            {"type": "text", "text": "text"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,base64data"}}
        ]}

    Messages without 'images' are returned unchanged.
    Messages that already have content as a list are returned unchanged.
    """
    converted = []
    for msg in messages:
        if not isinstance(msg, dict):
            converted.append(msg)
            continue

        new_msg = dict(msg)  # shallow copy

        # Only convert if there's an 'images' key with data
        images = new_msg.pop("images", None)
        if not images:
            converted.append(new_msg)
            continue

        # If content is already a list (e.g., tool messages), merge images in
        content = new_msg.get("content", "")
        if isinstance(content, list):
            image_entries = [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                for img in images
            ]
            new_msg["content"] = image_entries + content
        else:
            text_part = [{"type": "text", "text": content}] if content else []
            image_entries = [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                for img in images
            ]
            new_msg["content"] = text_part + image_entries

        converted.append(new_msg)

    return converted