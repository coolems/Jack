"""Supertonic local TTS engine (relocated from static/voice, 2026-07).

One model (supertonic-2/3) speaks EN/DE/RO. Synthesis is blocking, so it runs in an
executor. Text goes through the per-language sanitizer first (the German voice rejects
raw ss-umlauts and curly quotes - discovered by testing).
"""
