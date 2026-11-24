"""Regression coverage for queue admission, worker ownership and offline metadata."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.providers.llama import backend_model_cache as cache
from app.server_queue.queue_manager import RequestQueue
from app.server_queue.request import ChatRequest
from app.server_queue.worker_pool import WorkerPool
from tools.test_tools._client_imports import client_app, load_module


def request(owner):
    return ChatRequest(owner, SimpleNamespace(is_closed=False), model_key="model",
                       stop_event=asyncio.Event())


@pytest.mark.parametrize("reason", ["cancelled", "closed"])
def test_queue_rechecks_liveness_when_capacity_opens(reason):
    async def scenario():
        queue = RequestQueue(max_pending=1, grace_sec=5)
        original, waiting = request("first"), request("second")
        assert (await queue.enqueue(original)).accepted
        blocked = asyncio.create_task(queue.enqueue(waiting))
        try:
            await asyncio.sleep(0)
            assert not blocked.done()
            if reason == "cancelled":
                waiting.stop_event.set()
            else:
                waiting.sink.is_closed = True
            await queue.cancel_owner(original.owner_key)
            result = await asyncio.wait_for(blocked, 1)
            assert not result.accepted
            assert not result.retryable
            assert queue.pending_count() == 0
            assert (await queue.enqueue(request("third"))).accepted
        finally:
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)

    asyncio.run(scenario())


def test_queue_rejects_closed_sink_even_with_space():
    async def scenario():
        queue = RequestQueue()
        req = request("closed")
        req.sink.is_closed = True
        assert not (await queue.enqueue(req)).accepted
        assert queue.pending_count() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("remove_first", [True, False])
def test_workers_retire_and_replacement_gets_own_workers(remove_first):
    async def scenario():
        def backend():
            return SimpleNamespace(key=("endpoint",), model_key="model", capacity=2,
                                   healthy=True, ctx_per_slot=1, label="endpoint",
                                   instance=SimpleNamespace(name="endpoint"))

        original, replacement = backend(), backend()
        registry = SimpleNamespace(backends={original.key: original})
        queue = RequestQueue()
        pool = WorkerPool(registry, queue)
        entered, finish, replacement_ran = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def execute(node, req):
            seen.append(node)
            if node is original:
                entered.set()
                await finish.wait()
            else:
                replacement_ran.set()

        pool._execute = execute
        pool.start()
        try:
            await queue.enqueue(request("first"))
            await asyncio.wait_for(entered.wait(), 1)
            old_states = list(pool._workers[original.key])
            busy = next(state for state in old_states if state["busy"])
            idle = next(state for state in old_states if not state["busy"])
            if remove_first:
                registry.backends.clear()
                pool.reconcile()
                assert original.key not in pool._workers
            registry.backends[replacement.key] = replacement
            pool.reconcile()
            await asyncio.wait_for(asyncio.gather(idle["task"], return_exceptions=True), 1)
            assert idle["task"].cancelled()
            assert not busy["task"].done()
            await queue.enqueue(request("second"))
            await asyncio.wait_for(replacement_ran.wait(), 1)
            finish.set()
            await asyncio.wait_for(busy["task"], 1)
            assert len(seen) == 2 and seen[1] is replacement
        finally:
            await pool.stop()

    asyncio.run(scenario())


def test_corrupt_model_cache_recovers_and_keeps_valid_entries(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    monkeypatch.setattr(cache, "_cache_path", lambda: str(path))
    path.write_bytes(b"\xff")
    assert cache.remembered_models() == []
    path.write_text(json.dumps({"backends": {
        "host|node": {"models": None},
        "broken-entry": [],
        "other": {"models": [{"model_key": []}, {"model_key": "model",
                             "aliases": [None, "ok", 1]}]},
    }}), encoding="utf-8")
    remembered = cache.remembered_models()
    assert [(item["model_key"], item["aliases"]) for item in remembered] == [("model", ["ok"])]

    path.write_text(json.dumps({"backends": {"llama|node:8000": []}}), encoding="utf-8")
    node = SimpleNamespace(parent=None, is_hosted=False, kind="llama", model_key="new",
                           model_label="New", model_aliases=set(),
                           instance=SimpleNamespace(host="node", port=8000, name="Node"))
    assert cache.remember(SimpleNamespace(backends={node.instance.name: node}))
    assert cache.remembered_models()[0]["model_key"] == "new"


@pytest.mark.parametrize("raw", [False, True])
def test_frame_is_buffered_only_if_no_subscriber_received_it(raw):
    with client_app():
        channel = load_module("CLIENT/app/chat_bus/channel.py", "review_channel").ChatChannel("chat")

        class Socket:
            def __init__(self, disconnected=False):
                self.disconnected = disconnected
                self.sent = []

            async def send_text(self, text):
                if self.disconnected:
                    raise RuntimeError("socket closed")
                self.sent.append(text)

        frame = {"type": "content", "content": "final chunk"}
        text = json.dumps(frame)

        async def send():
            if raw:
                await channel.emit_raw(text)
            else:
                await channel.emit_frame(frame)

        async def scenario():
            live = Socket()
            channel.attach(live)
            channel.attach(Socket(disconnected=True))
            await send()
            assert live.sent == [text]
            assert not channel.buffered_frames()
            live.disconnected = True
            await send()
            assert not channel.subscribers
            assert channel.buffered_frames() == [text]

        asyncio.run(scenario())


def test_worker_capacity_can_shrink_and_recover_while_busy():
    async def scenario():
        node = SimpleNamespace(key=("endpoint",), model_key="model", capacity=2,
                               healthy=True, ctx_per_slot=1, label="endpoint",
                               instance=SimpleNamespace(name="endpoint"))
        queue = RequestQueue()
        pool = WorkerPool(SimpleNamespace(backends={node.key: node}), queue)
        entered = asyncio.Queue()
        finish = asyncio.Event()

        async def execute(backend, req):
            entered.put_nowait(req)
            await finish.wait()

        pool._execute = execute
        pool.start()
        try:
            for owner in ("first", "second"):
                await queue.enqueue(request(owner))
                await asyncio.wait_for(entered.get(), 1)
            states = list(pool._workers[node.key])
            node.capacity = 1
            pool.reconcile()
            assert [state["retire"] for state in states] == [False, True]
            node.capacity = 2
            pool.reconcile()
            assert pool._workers[node.key] == states
            assert all(not state["retire"] for state in states)
            # Both slots must serve new work without waiting for another watchdog pass.
            finish.set()
            await asyncio.sleep(0)
            finish.clear()
            for owner in ("third", "fourth"):
                await queue.enqueue(request(owner))
                await asyncio.wait_for(entered.get(), 1)
        finally:
            await pool.stop()

    asyncio.run(scenario())
