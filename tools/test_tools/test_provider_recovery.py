import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

# SERVER-side test: it imports the SERVER `app` tree (<repo>/app), so the repo root must be
# importable. CLIENT-side tests in this folder swap that name to CLIENT/app inside their own
# fixture and restore it afterwards (see _client_imports), so order never matters.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.providers.llama import streaming
from app.providers.openai.responses import _parse_responses_output
from app.providers.stream_completion import StreamCompletion


URL = "http://mock-backend"
PARSE_ERROR = {"error": {"type": "invalid_request_error", "message":
                        "Unterminated string starting at: line 1 column 9 (char 8)"}}
TOOLS = {"tool_calls": [{"index": 0, "id": "demo", "function":
                         {"name": "example", "arguments": '{"value": 1}'}}]}


def sse(delta, finish_reason=None, *, done=True):
    chunk = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n"
                          + ("data: [DONE]\n\n" if done else ""))


class ProviderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, *responses, stop_event=None):
        self.requests = []
        pending = iter(responses)

        def handle(request):
            if request.url.path == "/health":
                return httpx.Response(200)
            self.requests.append(request)
            return next(pending)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(streaming, "get_async_client", return_value=client):
                return await streaming.chat_stream(
                    URL, 10, "test-model", [{"role": "user", "content": "test"}], None,
                    stop_event=stop_event, headers={"Authorization": "Bearer test"},
                )

    async def test_first_request_parse_rejection_is_replayed(self):
        text, stats = await self.generate(httpx.Response(400, json=PARSE_ERROR),
                                          sse({"content": "ok"}, "stop"))
        self.assertEqual(text, "ok")
        self.assertFalse(stats.truncated)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].content, self.requests[1].content)
        self.assertEqual(self.requests[1].headers["Authorization"], "Bearer test")

    async def test_a_persistent_parse_rejection_stops_after_one_retry(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            await self.generate(*(httpx.Response(400, json=PARSE_ERROR) for _ in range(2)))
        self.assertEqual(len(self.requests), 2)

    async def test_other_400_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            await self.generate(httpx.Response(400, json={"error": "invalid model"}))
        self.assertEqual(len(self.requests), 1)

    async def test_cancelled_request_is_not_retried(self):
        stop = asyncio.Event()
        stop.set()
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            await self.generate(httpx.Response(400, json=PARSE_ERROR), stop_event=stop)
        self.assertEqual(len(self.requests), 1)

    async def test_length_discards_even_parseable_tool_arguments(self):
        text, stats = await self.generate(sse(TOOLS, "length"))
        self.assertEqual(text, "")
        self.assertTrue(stats.truncated)
        self.assertTrue(stats.to_dict()["discarded_tool_calls"])
        self.assertEqual(len(self.requests), 1)

    async def test_clean_end_does_not_dispatch_a_tool_without_finish_reason(self):
        _, stats = await self.generate(sse(TOOLS))
        self.assertTrue(stats.truncated)
        self.assertTrue(stats.discarded_tool_calls)

    async def test_incomplete_text_is_preserved_on_early_eof(self):
        text, stats = await self.generate(sse({"content": "partial"}, done=False))
        self.assertEqual(text, "partial")
        self.assertTrue(stats.truncated)
        self.assertFalse(stats.discarded_tool_calls)

    async def test_complete_tool_batch_is_preserved(self):
        text, stats = await self.generate(sse(TOOLS, "tool_calls"))
        self.assertEqual(json.loads(text)["tool_calls"][0]["function"]["name"], "example")
        self.assertFalse(stats.truncated)
        self.assertFalse(stats.discarded_tool_calls)

    async def test_oversize_stream_exits_as_incomplete(self):
        with patch("app.providers.stream_completion.MAX_STREAM_JSON_BYTES", 100):
            text, stats = await self.generate(sse({"content": "a" * 101}, "stop"))
        self.assertEqual(text, "")
        self.assertTrue(stats.truncated)

    def test_damaged_stream_never_releases_tool_calls(self):
        completion = StreamCompletion(done=True, damaged=True)
        self.assertEqual(completion.classify("tool_calls", True, False), (True, True))

    def test_responses_incomplete_tool_batch_is_not_serialized(self):
        text, tools = _parse_responses_output({"output": [
            {"type": "function_call", "name": "example", "arguments": "{}"},
        ]}, incomplete=True)
        self.assertEqual(text, "")
        self.assertEqual(len(tools), 1)
