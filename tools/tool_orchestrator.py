"""Main tool orchestrator - coordinates all tools (COMPLETELY DYNAMIC - NO HARDCODED FOLDERS)

CLIENT-SIDE: Returns ALL tools unfiltered. SERVER enforces real role-based restrictions via WebSocket protocol.
"""

import logging
import os
import importlib
import inspect
from typing import Dict, Optional, Callable, Any, List

from .utils import EXCLUDED_FOLDERS

logger = logging.getLogger("COOLEMS.Tools.Orchestrator")


class ToolOrchestrator:
    """Orchestrates all available tools. CLIENT returns ALL tools - SERVER enforces restrictions."""
    
    def __init__(self, provider=None, ollama_url: str = None):
        self.provider = provider
        self.ollama_url = ollama_url
        
        # COMPLETELY DYNAMIC - auto-discovers ALL tool classes from ALL folders
        self.tool_instances: Dict[str, Any] = {}
        self.tools: Dict[str, Callable] = {}
        self._discover_all_tool_classes()
        
        # Cache for all tool definitions (unfiltered) - None until first load
        self._all_tool_definitions: Optional[List[Dict]] = None
        
        logger.info(f"ToolOrchestrator initialized with {len(self.tools)} tools")
    
    def _discover_all_tool_classes(self) -> None:
        """COMPLETELY DYNAMIC tool discovery. Scans EVERY subfolder in tools/."""
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        
        for item in os.listdir(tools_dir):
            item_path = os.path.join(tools_dir, item)
            
            if not os.path.isdir(item_path):
                continue
            if item in EXCLUDED_FOLDERS:
                continue
            
            class_name = ''.join(word.capitalize() for word in item.split('_'))
            
            try:
                module = importlib.import_module(f'tools.{item}')
                tool_class = getattr(module, class_name, None)
                
                if tool_class:
                    instance = tool_class()
                    self.tool_instances[item] = instance
                    
                    if hasattr(instance, 'get_all_tools'):
                        for tool_name, tool_func in instance.get_all_tools().items():
                            self.tools[tool_name] = tool_func
                    elif hasattr(instance, '_tools'):
                        for tool_name, tool_func in instance._tools.items():
                            self.tools[tool_name] = tool_func
                            
            except Exception as e:
                logger.warning(f"Failed to load tool class from folder '{item}': {e}")
    
    def _load_all_tool_definitions(self) -> List[Dict]:
        """Load all __tool_description__ definitions. Cached after first load."""
        if self._all_tool_definitions is not None:
            return self._all_tool_definitions
        
        tool_definitions = []
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        
        for item in os.listdir(tools_dir):
            item_path = os.path.join(tools_dir, item)
            if not os.path.isdir(item_path) or item in EXCLUDED_FOLDERS:
                continue
            
            folder_path = os.path.join(tools_dir, item)
            for file in os.listdir(folder_path):
                if not file.endswith('.py') or file == '__init__.py':
                    continue
                
                tool_name = file.replace('.py', '')
                
                try:
                    module = importlib.import_module(f'tools.{item}.{tool_name}')
                    
                    if hasattr(module, '__tool_description__'):
                        tool_definitions.append(module.__tool_description__)
                    else:
                        func = getattr(module, tool_name, None)
                        if func and callable(func):
                            ollama_tool = self._build_tool_from_func(tool_name, func)
                            if ollama_tool:
                                tool_definitions.append(ollama_tool)
                                
                except Exception as e:
                    logger.warning(f"Failed to load tool definition '{tool_name}': {e}")
        
        self._all_tool_definitions = tool_definitions
        return tool_definitions
    
    def get_ollama_tools(self, api_key: str = None) -> List[Dict]:
        """Get ALL tool definitions. CLIENT does NOT filter - SERVER enforces restrictions."""
        all_tools = self._load_all_tool_definitions()
        logger.info(f"get_ollama_tools: returning all {len(all_tools)} tools (SERVER will enforce restrictions)")
        return all_tools
    
    def is_tool_allowed(self, tool_name: str, api_key: str = None) -> bool:
        """CLIENT allows ALL tools. SERVER enforces real restrictions."""
        return tool_name in self.tools
    
    def _build_tool_from_func(self, tool_name: str, func: Callable) -> Optional[Dict]:
        """Fallback: build provider tool definition from function signature and docstring"""
        sig = inspect.signature(func)
        properties = {}
        required = []
        
        for param_name, param in sig.parameters.items():
            if param_name == 'self':
                continue
            
            param_type = "string"
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == bool:
                    param_type = "boolean"
                elif param.annotation == float:
                    param_type = "number"
            
            prop = {"type": param_type, "description": f"Parameter: {param_name}"}
            
            if param.default != inspect.Parameter.empty:
                prop["default"] = param.default
            else:
                required.append(param_name)
            
            properties[param_name] = prop
        
        description = ""
        if func.__doc__:
            description = func.__doc__.strip().split('\n')[0][:200]
        
        if not description:
            description = f"Tool: {tool_name}"
        
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}
            }
        }
    
    def get_tool_names(self, api_key: str = None) -> list:
        """Get ALL tool names. CLIENT does NOT filter."""
        return sorted(self.tools.keys())
    
    def get_tool_count(self, api_key: str = None) -> int:
        """Get total number of tools."""
        return len(self.tools)
    
    def _auto_inject_context_params(self, tool_name: str, tool_func: Callable, tool_params: Dict, api_key: str = None) -> None:
        """Dynamically inject work_folder and working_root based on function signature.
        
        COMPLETELY DYNAMIC - inspects the actual function parameters to decide what to inject.
        No hardcoded tool names required.
        """
        from .utils import get_working_root
        
        try:
            sig = inspect.signature(tool_func)
            param_names = list(sig.parameters.keys())
            
            # SECURITY (2026-07-15): ALWAYS force the SYSTEM working_root value.
            # Previously these were only injected when ABSENT, which let a
            # model-supplied 'work_folder'/'working_root' override the real root
            # and break containment. The system value now always wins.
            if 'work_folder' in param_names:
                tool_params['work_folder'] = get_working_root()

            if 'working_root' in param_names:
                tool_params['working_root'] = get_working_root()
                
        except Exception as e:
            logger.debug(f"Could not inspect signature for {tool_name}: {e}")
    
    def execute_tool(self, tool_name: str, tool_params: Dict, conversation_history: list = None, api_key: str = None) -> str:
        """Execute a tool. CLIENT allows ALL - SERVER enforces restrictions."""
        
        # Get the tool function first (needed for dynamic param injection)
        tool_func = self.tools.get(tool_name)
        
        if not tool_func:
            return f"ERROR: Unknown tool: {tool_name}"
        
        # COMPLETELY DYNAMIC parameter injection based on function signature
        self._auto_inject_context_params(tool_name, tool_func, tool_params, api_key=api_key)
        
        try:
            logger.info(f"Executing tool '{tool_name}'")
            result = tool_func(**tool_params)
        # (2026-08-29 threadless refactor): web_interact tools are async now. If a
            # legacy sync path ever receives an un-awaited coroutine, surface it as a
            # clear error instead of returning the coroutine object to the LLM.
            import asyncio as _asyncio
            if isinstance(result, _asyncio.Coroutine):
                result.close()  # prevent 'never awaited' warning; we cannot await here
                return f"ERROR: Tool '{tool_name}' is async and requires an async execution path."
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} execution error: {e}")
            import traceback
            return f"ERROR: Tool execution failed: {str(e)}"
    
    def get_tools_summary(self, api_key: str = None) -> Dict:
        """Get summary of ALL tools. CLIENT does NOT filter."""
        all_tool_names = sorted(self.tools.keys())
        return {
            "role": "client (SERVER enforces restrictions)",
            "total_tools_available": len(all_tool_names),
            "allowed_tools": all_tool_names,
            "has_unlimited_access": True,  # CLIENT side - SERVER restricts
        }
