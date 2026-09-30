"""
CLI entry point for standalone TTS operation.

Usage:
    python -m static.voice --voice M1 --speed 1.0
    python -m static.voice --text "Hello world." --output out.wav
    python -m static.voice --interactive
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from static.voice.config import VoiceConfig
from static.voice.engine import TTSManager


logger = logging.getLogger("COOLEMS.Voice")


def cli_main():
    """CLI entry point for standalone TTS operation."""

    parser = argparse.ArgumentParser(
        description="COOLEMS TTS — Supertonic Text-to-Speech Engine"
    )
    parser.add_argument(
        "--voice", "-v", type=str, default="M1",
        help="Voice style name (M1, M2, F1, F2, etc.)",
    )
    parser.add_argument(
        "--speed", "-s", type=float, default=1.05,
        help="Speech speed multiplier (0.7-2.0)",
    )
    parser.add_argument(
        "--text", "-t", type=str, default=None,
        help="Text to synthesize (single use mode)",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output WAV file path (single use mode)",
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true",
        help="Interactive mode (read from stdin)",
    )
    parser.add_argument(
        "--steps", type=int, default=10,
        help="Synthesis quality steps (1-100)",
    )
    parser.add_argument(
        "--lang", type=str, default="en",
        help="Language code (en, de, ro, etc.)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    voice_config = VoiceConfig(
        voice_name=args.voice,
        speed=args.speed,
        total_steps=args.steps,
        lang=args.lang,
    )

    if args.text:
        # Single-use mode — create TTSManager inside asyncio.run()
        # so all asyncio.Lock objects bind to the correct event loop.
        async def run_single():
            tts = TTSManager()
            output = args.output or "tts_output.wav"
            result = await tts.synthesize_to_file(args.text, output, voice_config)
            if result:
                pass
            else:
                sys.exit(1)

        asyncio.run(run_single())

    elif args.interactive:
        # Interactive mode — same pattern: TTSManager created inside
        # the event loop so Locks bind correctly.

        async def run_interactive():
            tts = TTSManager()
            await tts.start()
            try:
                while True:
                    text = input("> ")
                    if text.lower() in ("quit", "exit", "q"):
                        break
                    if text.strip():
                        await tts.add_text(text)
            except (KeyboardInterrupt, EOFError):
                logger.debug("Non-critical exception caught at CLIENT/static/voice/cli.py:101")
            finally:
                await tts.stop()

        asyncio.run(run_interactive())

    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
