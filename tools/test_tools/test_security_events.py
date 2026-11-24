"""Security event reporting: sanitising, never-raising, and what an admin sees."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope


@pytest.fixture
def events(tmp_path, monkeypatch):
    """Fresh module state + a throwaway JSONL file (no writes into the real CLIENT/logs)."""
    from app import security_events

    security_events.clear()
    monkeypatch.setattr(security_events, "_LOGS_DIR", str(tmp_path))
    yield security_events
    security_events.clear()


def test_event_is_single_line_json(tmp_path, events):
    event = events.record("workspace_foreign", client_ip="10.0.0.9", workspace="ownwsaa")
    lines = events.event_file()
    written = Path(lines).read_text(encoding="utf-8").splitlines()
    assert len(written) == 1
    assert json.loads(written[0]) == event
    assert event["kind"] == "workspace_foreign" and event["ts"]


def test_caller_controlled_values_cannot_forge_a_log_line(events):
    """A newline in a conversation id used to be able to fake a whole log entry."""
    events.record("workspace_unknown", workspace="abc\r\n2026-01-01 INFO all good", client_ip="10.0.0.9")
    payload = Path(events.event_file()).read_text(encoding="utf-8")
    assert len(payload.splitlines()) == 1
    stored = events.recent(1)[0]
    assert "\n" not in stored["workspace"] and "\r" not in stored["workspace"]
    assert stored["workspace"].startswith("abc") and "2026-01-01 INFO all good" in stored["workspace"]


def test_long_values_are_capped(events):
    events.record("workspace_id_invalid", claimed="x" * 5000)
    assert len(events.recent(1)[0]["claimed"]) == events.MAX_FIELD_CHARS


def test_recording_never_raises_when_the_file_is_unwritable(events, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr("builtins.open", boom)
    event = events.record("path_escape", requested="../etc/passwd")
    assert event["kind"] == "path_escape"
    assert events.recent(1)[0]["kind"] == "path_escape"


def test_summary_counts_by_kind_and_recent_is_newest_first(events):
    events.record("workspace_unknown", workspace="aaaabbb")
    events.record("workspace_foreign", workspace="ccccddd")
    events.record("workspace_unknown", workspace="eeeefff")

    summary = events.summary()
    assert summary["total"] == 3
    assert summary["by_kind"] == {"workspace_unknown": 2, "workspace_foreign": 1}
    assert [e["workspace"] for e in events.recent(2)] == ["eeeefff", "ccccddd"]


def test_ring_is_bounded(events):
    for index in range(events.KEEP_EVENTS + 25):
        events.record("workspace_unknown", workspace=f"w{index}")
    assert len(events.recent(events.KEEP_EVENTS)) == events.KEEP_EVENTS
    assert events.summary()["total"] == events.KEEP_EVENTS + 25


def test_a_burst_of_identical_refusals_is_written_once_but_counted(events):
    """A polling UI must not bury a real event under copies of the same refusal."""
    for _ in range(5):
        events.record("workspace_unknown", client_ip="10.0.0.9", workspace="aaaabbb", path="/api/tree")

    assert len(events.recent(10)) == 1
    assert events.summary()["by_kind"]["workspace_unknown"] == 5
    assert len(Path(events.event_file()).read_text(encoding="utf-8").splitlines()) == 1


def test_different_targets_or_clients_are_not_coalesced(events):
    events.record("workspace_unknown", client_ip="10.0.0.9", workspace="aaaabbb")
    events.record("workspace_unknown", client_ip="10.0.0.9", workspace="ccccddd")
    events.record("workspace_unknown", client_ip="10.0.0.8", workspace="aaaabbb")
    assert len(events.recent(10)) == 3


def test_concurrent_records_never_lose_or_tear_a_line(events, monkeypatch):
    """Evidence must survive a burst from several threads: one line per event, all parseable.

    An append's file offset is not atomic between handles, so unserialised writers to the
    same JSONL file overwrite and interleave each other. ``open`` is slowed here to widen
    that window, which turns the race into a guaranteed overlap instead of a lucky one.
    """
    real_open = open

    def slow_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        time.sleep(0.001)  # lets a competing writer open the same file mid-append
        return handle

    monkeypatch.setattr("builtins.open", slow_open)
    monkeypatch.setattr(events, "MAX_FILE_BYTES", 10 ** 9)  # rotation must not muddy the count
    threads, per_thread = 8, 12
    barrier = threading.Barrier(threads)  # every round starts with all writers in the window

    def worker(index):
        for i in range(per_thread):
            barrier.wait()
            events.record("path_escape", workspace=f"t{index}-{i}", requested="../etc/passwd")

    workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    lines = Path(events.event_file()).read_text(encoding="utf-8").splitlines()
    assert len(lines) == threads * per_thread
    assert {json.loads(line)["workspace"] for line in lines} == {
        f"t{n}-{i}" for n in range(threads) for i in range(per_thread)
    }
    assert events.summary()["total"] == threads * per_thread
