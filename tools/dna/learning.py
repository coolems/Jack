"""Learning module for AgentDNA - tool-centric lesson storage and retrieval"""

import json
import os
import logging
from typing import List, Dict, Optional

from config import MODEL_NAME

logger = logging.getLogger("COOLEMS.AgentDNA.Learning")

# Available tool categories
TOOL_CATEGORIES = [
    "browser",
    "web_search", 
    "file",
    "pdf",
    "ocr",
    "calc",
    "time",
    "general"
]

# Prompt for extracting lessons from conversation
LEARNING_PROMPT = """You are analyzing a conversation where the TOOL "{tool_name}" was used.

## Current knowledge for this tool:
{current_knowledge}

## The conversation:
{conversation_text}

## Your task:
1. For EACH situation in current knowledge, compare with what happened. If user guided a BETTER approach, UPDATE it.
2. If a NEW situation happened (not in current knowledge), CREATE a new entry with:
   - situation: short description (max 15 words)
   - approach: how AI + user managed it successfully

## Return ONLY JSON:
{
  "tool": "{tool_name}",
  "situations": [
    {"situation": "...", "approach": "..."}
  ]
}

If no changes needed: {"tool": "{tool_name}", "unchanged": true}

Do not add any text outside JSON."""

class LessonManager:
    """Manages tool-centric lessons for AgentDNA"""
    
    def __init__(self, dna_dir: str):
        self.lessons_path = os.path.join(dna_dir, "lessons.json")
        self.lessons = self._load()
    
    def _load(self) -> Dict:
        """Load lessons from JSON file"""
        if os.path.exists(self.lessons_path):
            try:
                with open(self.lessons_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("Failed to parse lessons.json, starting empty")
                return {}
        return {}
    
    def _save(self):
        """Save lessons to JSON file"""
        with open(self.lessons_path, 'w', encoding='utf-8') as f:
            json.dump(self.lessons, f, indent=2, ensure_ascii=False)
    
    def get_tool_lessons(self, tool_name: str) -> List[Dict]:
        """Get all lessons for a specific tool"""
        return self.lessons.get(tool_name, [])
    
    def set_tool_lessons(self, tool_name: str, situations: List[Dict]):
        """Set lessons for a specific tool"""
        self.lessons[tool_name] = situations
        self._save()
    
    def ensure_tool_exists(self, tool_name: str):
        """Initialize tool category if it doesn't exist"""
        if tool_name not in self.lessons:
            self.lessons[tool_name] = []
            self._save()
    
    def get_all_tools_with_lessons(self) -> List[str]:
        """Get list of all tools that have lessons"""
        return [t for t, lessons in self.lessons.items() if lessons]

    def lessons_for_tool(self, tool_name: str, max_lessons: int = 5) -> str:
        """Get all lessons for a specific tool as formatted string"""
        lessons = self.lessons.get(tool_name, [])
        if not lessons:
            return ""
    
        lines = ["\n## LESSONS I HAVE LEARNED"]
        for lesson in lessons[:max_lessons]:
            lines.append(f"- When {lesson['situation']}, do: {lesson['approach']}")
    
        return "\n".join(lines)
    
    def format_for_prompt(self, user_message: str, max_lessons: int = 5) -> str:
        """Get relevant lessons based on user message keywords"""
        if not self.lessons:
            return ""
        
        user_lower = user_message.lower()
        relevant = []
        
        for tool, lessons in self.lessons.items():
            for lesson in lessons:
                situation = lesson.get("situation", "").lower()
                # Simple keyword matching
                if any(word in user_lower for word in situation.split()[:5]):
                    relevant.append(lesson)
        
        if not relevant:
            return ""
        
        lines = ["\n## LESSONS I HAVE LEARNED"]
        for lesson in relevant[:max_lessons]:
            lines.append(f"- When {lesson['situation']}, do: {lesson['approach']}")
        
        return "\n".join(lines)
    
    def detect_tools_used(self, conversation: List[Dict]) -> List[str]:
        """Detect which tools were used in conversation"""
        tools_found = set()
        conv_text = " ".join([m.get("content", "") for m in conversation]).lower()
        
        # Simple keyword detection
        tool_keywords = {
            "browser": ["click", "goto", "navigate", "browser", "chrome", "screenshot", "scroll", "fill"],
            "web_search": ["search", "google", "web search", "find online", "look up"],
            "file": ["read file", "write file", "save file", "delete file", "list files"],
            "pdf": ["pdf", "create pdf", "make pdf"],
            "ocr": ["transcribe", "ocr", "extract text from image", "read text from image"],
            "calc": ["calculate", "calculator", "math", "python exec"],
            "time": ["time", "date", "what time", "what date"]
        }
        
        for tool, keywords in tool_keywords.items():
            for keyword in keywords:
                if keyword in conv_text:
                    tools_found.add(tool)
                    break
        
        # Always include general for basic lessons
        if tools_found:
            tools_found.add("general")
        
        return list(tools_found)
    
    async def update_from_conversation(self, conversation: List[Dict], ollama_url: str, model: str = MODEL_NAME) -> List[str]:
        """Update lessons for all tools detected in conversation"""
        import aiohttp
        
        tools_used = self.detect_tools_used(conversation)
        if not tools_used:
            logger.info("No tools detected in conversation, skipping learning")
            return []
        
        # Format conversation for prompt
        conv_text = []
        for msg in conversation:
            role = "USER" if msg["role"] == "user" else "AI"
            content = msg.get("content", "")[:800]
            conv_text.append(f"{role}: {content}")
        full_conversation = "\n\n".join(conv_text)
        
        updated_tools = []
        
        for tool in tools_used:
            logger.info(f"Updating lessons for tool: {tool}")
            
            current = self.get_tool_lessons(tool)
            current_text = json.dumps(current, indent=2) if current else "No existing lessons for this tool."
            
            prompt = LEARNING_PROMPT.format(
                tool_name=tool,
                current_knowledge=current_text,
                conversation_text=full_conversation
            )
            
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.3}
            }
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(f"{ollama_url}/api/chat", json=payload, timeout=90) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            result_text = data.get("message", {}).get("content", "{}")
                            
                            import json as jsonlib
                            try:
                                result = jsonlib.loads(result_text)
                                if not result.get("unchanged", False):
                                    new_situations = result.get("situations", [])
                                    if new_situations:
                                        self.set_tool_lessons(tool, new_situations)
                                        updated_tools.append(tool)
                                        logger.info(f"Updated {len(new_situations)} lessons for {tool}")
                            except jsonlib.JSONDecodeError:
                                logger.warning(f"Failed to parse learning response for {tool}")
                        else:
                            logger.warning(f"Ollama returned {resp.status} for tool {tool}")
            except Exception as e:
                logger.error(f"Failed to update lessons for {tool}: {e}")
        
        return updated_tools
