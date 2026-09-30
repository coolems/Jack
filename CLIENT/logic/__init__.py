"""
Logic module for COOLEMS - separates normal and agentic chat logic

This module provides:
- normal_mode(): Standard AI chat without tools
- agentic_mode(): AI chat with tool orchestration and ReAct loop
- BaseModeConfig: Shared base configuration class
"""

import logging
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass

from .base_mode import BaseModeConfig
from .normal import normal_mode, NormalModeConfig
from .agentic import agentic_mode, AgenticModeConfig
from .tool_executor import ToolExecutor
from .config_builder import build_normal_config, build_agentic_config

logger = logging.getLogger("COOLEMS.Logic")

__all__ = [
    'BaseModeConfig',
    'normal_mode',
    'agentic_mode',
    'NormalModeConfig',
    'AgenticModeConfig',
    'ToolExecutor',
    'build_normal_config',
    'build_agentic_config',
]

# Re-export commonly used types (backward compat)
from .normal import stream_ollama, call_ollama_non_streaming, get_ollama_status_message


__all__ += ['stream_ollama', 'call_ollama_non_streaming', 'get_ollama_status_message']
