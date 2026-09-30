"""Get time awareness instruction function - returns time awareness instructions for agent"""

def get_time_awareness_instruction() -> str:
    """Get time awareness instructions for agent"""
    return """
Use get_current_time for any time/date queries:
<tool>get_current_time</tool>
<tool_input>{}</tool_input>

Then use that information in your response.
"""