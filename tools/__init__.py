"""
Tools module for COOLEMS - COMPLETELY DYNAMIC auto-discovery

ZERO hardcoded lists. Everything is discovered at runtime.
"""

import os
import sys
import logging
import importlib
from typing import Dict, Any

logger = logging.getLogger(__name__)

_current_dir = os.path.dirname(os.path.abspath(__file__))

# ===== Core infrastructure (explicit imports - these are not tools) =====
from .utils import (
    is_text_file,
    is_image_file,
    encode_image_to_base64,
    read_text_file,
    get_file_info,
    MAX_FILE_SIZE,
    MAX_TEXT_CONTENT_SIZE,
    MAX_WRITE_SIZE,
    TEXT_EXTENSIONS,
    EXCLUDED_FOLDERS,
    IMAGE_EXTENSIONS
)
# NOTE: tool detection lives on the CLIENT side only (CLIENT/app/detection.py).
# The old server-side tools/tool_detection.py was removed as dead code (2026-08-23):
# nothing in the SERVER imported it and it duplicated the client copy.
from .tool_orchestrator import ToolOrchestrator
from .dna import AgentDNA, get_agent
from .agent_prompt import (
    GENERIC_TOOL_PROMPT,
    PERSISTENT_PLAN_PROMPT,
    get_time_awareness_instruction,
    get_agent_system_prompt
)


# ===== COMPLETELY DYNAMIC TOOL DISCOVERY =====

def _discover_tool_folders():
    """Auto-discover all subdirectories that contain tool files"""
    folders = []
    excluded = EXCLUDED_FOLDERS
    
    for item in os.listdir(_current_dir):
        item_path = os.path.join(_current_dir, item)
        if os.path.isdir(item_path) and item not in excluded:
            py_files = [f for f in os.listdir(item_path) 
                       if f.endswith('.py') and f != '__init__.py']
            if py_files:
                folders.append(item)
    return folders


def _get_class_name(folder_name):
    """Convert 'web_tools' -> 'WebTools'"""
    return ''.join(word.capitalize() for word in folder_name.split('_'))


# Auto-import all tool classes from all discovered folders
for folder in _discover_tool_folders():
    class_name = _get_class_name(folder)
    try:
        module = importlib.import_module(f'.{folder}', package='tools')
        tool_class = getattr(module, class_name, None)
        if tool_class:
            globals()[class_name] = tool_class
    except Exception as e:
        logger.debug("Non-critical exception caught at tools/__init__.py:74 - %s", e)


# Auto-discover all individual tool functions
ALL_TOOLS: Dict[str, Any] = {}
for folder in _discover_tool_folders():
    folder_path = os.path.join(_current_dir, folder)
    for file in os.listdir(folder_path):
        if file.endswith('.py') and file != '__init__.py':
            tool_name = file.replace('.py', '')
            try:
                module = importlib.import_module(f'.{folder}.{tool_name}', package='tools')
                func = getattr(module, tool_name, None)
                if func and callable(func):
                    ALL_TOOLS[tool_name] = func
            except Exception as e:
                logger.debug("Non-critical exception caught at tools/__init__.py:90 - %s", e)
