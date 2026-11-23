"""Completion checks shared by OpenAI-compatible chat streams."""

from dataclasses import dataclass

# Count SSE JSON, including reasoning and tool deltas. Leave room to re-encode
# the final response in the relay's 10 MiB WebSocket frame.
MAX_STREAM_JSON_BYTES = 3 * 1024 * 1024


@dataclass
class StreamCompletion:
    json_bytes: int = 0
    done: bool = False
    damaged: bool = False

    def accept_event(self, data: str) -> bool:
        self.json_bytes += len(data.encode("utf-8"))
        if self.json_bytes > MAX_STREAM_JSON_BYTES:
            self.damaged = True
            return False
        return True

    def classify(self, finish_reason, has_tools, stopped):
        """Return (incomplete response, discard native tool batch)."""
        if stopped:
            return False, bool(has_tools)
        incomplete = (self.damaged or finish_reason == "length"
                      or (not finish_reason and not self.done))
        # Some compatible backends finish a native call with 'stop'. A bare
        # [DONE] is enough for legacy text streams, but not for tool execution.
        discard_tools = bool(has_tools and (incomplete or finish_reason not in ("stop", "tool_calls")))
        return incomplete or discard_tools, discard_tools
