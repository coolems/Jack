"""
Async-safe sentence pool for TTS.

Uses asyncio primitives (Lock, Condition) — no threading.
Sentences are added, processed, and removed dynamically.

DESIGN:
- Each add() call receives ONE complete sentence from JS (streaming mode)
  or RAW text with multiple sentences (click mode)
- Backend sanitizes and adds to pool
- No buffering across add() calls - JS handles that
"""

import asyncio
import logging
from collections import deque
from typing import Deque, List, Optional

from static.voice.text_utils import sanitize_text

logger = logging.getLogger("COOLEMS.Voice")


def find_sentence_boundaries(text: str) -> List[int]:
    """
    Find all sentence boundary positions in text.
    A boundary is the position RIGHT AFTER a sentence-ending character.
    
    Sentence-ending: .!? followed by whitespace/end, or a newline.
    
    Returns list of indices where new sentences start.
    """
    boundaries: List[int] = []
    
    for i, ch in enumerate(text):
        if ch in '.!?':
            # Check if followed by whitespace or end of string
            if i + 1 >= len(text) or text[i + 1] in ' \n\r\t':
                boundaries.append(i + 1)
        elif ch == '\n':
            boundaries.append(i + 1)
    
    return boundaries


def split_text_into_sentences(text: str) -> List[str]:
    """
    Split raw text into individual sentences.
    Each returned sentence is a raw string (not sanitized).
    """
    if not text.strip():
        return []
    
    boundaries = find_sentence_boundaries(text)
    
    if not boundaries:
        # No sentence boundaries found - return the whole text as one sentence
        return [text.strip()]
    
    sentences: List[str] = []
    start = 0
    
    for boundary in boundaries:
        chunk = text[start:boundary].strip()
        if chunk:
            sentences.append(chunk)
        start = boundary
    
    # Don't forget the last chunk (after the last boundary)
    last_chunk = text[start:].strip()
    if last_chunk:
        sentences.append(last_chunk)
    
    return sentences


class SentencePool:
    """Async-safe sentence pool for TTS."""

    def __init__(self, max_size: int = 100):
        self._pool: Deque[str] = deque()
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._read_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, text: str) -> int:
        """
        Add text to the pool.
        
        - Splits text into sentences (handles multiple sentences in one call)
        - Sanitizes each sentence
        - Adds to pool
        
        Returns number of sentences added.
        """
        if not text:
            return 0
        
        # Split raw text into sentences
        raw_sentences = split_text_into_sentences(text)
        
        if not raw_sentences:
            return 0
        
        # Sanitize and add each sentence to the pool
        added = 0
        async with self._condition:
            for raw_sentence in raw_sentences:
                if len(self._pool) >= self._max_size:
                    break
                
                # Sanitize the sentence
                sanitized = sanitize_text(raw_sentence)
                if not sanitized:
                    continue
                
                # Clean up: strip trailing commas, ensure period at end
                sanitized = sanitized.rstrip(",").strip()
                if not sanitized:
                    continue
                if not sanitized.endswith("."):
                    sanitized += "."
                
                self._pool.append(sanitized)
                added += 1
            
            self._condition.notify_all()
        
        return added

    async def add_sentence(self, sentence: str) -> bool:
        """Add a single pre-sanitized sentence to the pool."""
        sanitized = sanitize_text(sentence)
        if not sanitized:
            return False
        if not sanitized.endswith("."):
            sanitized += "."
        async with self._condition:
            if len(self._pool) < self._max_size:
                self._pool.append(sanitized)
                self._condition.notify_all()
                return True
        return False

    async def get(self, timeout: float = 1.0) -> Optional[str]:
        """
        Get the next sentence from the pool.
        Returns None if timeout expires.
        """
        try:
            async with self._condition:
                if await asyncio.wait_for(
                    self._condition.wait_for(lambda: len(self._pool) > 0),
                    timeout=timeout,
                ):
                    return self._pool.popleft()
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"SentencePool.get error: {e}")
            return None
        return None

    async def clear(self) -> int:
        """Clear all sentences from the pool. Returns count removed."""
        async with self._condition:
            count = len(self._pool)
            self._pool.clear()
            self._condition.notify_all()
        return count

    async def size(self) -> int:
        """Get current pool size."""
        async with self._read_lock:
            return len(self._pool)

    async def is_empty(self) -> bool:
        """Check if pool is empty."""
        async with self._read_lock:
            return len(self._pool) == 0

    async def peek(self) -> Optional[str]:
        """Peek at the next sentence without removing it."""
        async with self._read_lock:
            return self._pool[0] if self._pool else None
