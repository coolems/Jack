"""A delivered mid-run question must not veto an otherwise valid final reply."""

import asyncio
import json
from types import SimpleNamespace

from tools.test_tools._client_imports import client_app_scope


def test_midrun_question_does_not_force_an_extra_provider_call(monkeypatch):
    from logic import react_loop

    pending = [{"id": "question", "text": "Is the audit ready?"}]
    calls = []
    frames = []

    def drain():
        items = pending[:]
        pending.clear()
        return items

    async def send_text(raw):
        frames.append(json.loads(raw))

    answer = "Yes; the audit is finished.\n\nAgentic AI: TASK DONE"

    async def provider(config, messages):
        calls.append([message.copy() for message in messages])
        return answer, None

    monkeypatch.setattr(react_loop, "max_history_tokens", lambda: 100_000)
    monkeypatch.setattr(react_loop, "get_context_window", lambda: 120_000)
    monkeypatch.setattr(react_loop, "call_provider_with_retry", provider)
    config = SimpleNamespace(stop_event=None, steer_source=SimpleNamespace(drain_steer=drain),
                             websocket=SimpleNamespace(send_text=send_text))
    result = asyncio.run(react_loop.run_react_loop(config, [
        {"role": "system", "content": "Review files."},
        {"role": "user", "content": "Audit the Files panel.", "_protect": True},
    ], None, []))

    assert result == answer
    assert len(calls) == 1
    assert calls[0][-1]["content"].endswith("Is the audit ready?")
    assert "CONTINUE the original task" in calls[0][-1]["content"]
    assert [frame["type"] for frame in frames] == ["steer_applied"]
