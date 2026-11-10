"""The background-job seam for SERVER-delivered tools (2026-12).

A delivered tool runs inside the loader sandbox on a worker thread and cannot import
CLIENT modules, so it never sees JobRegistry / JobHooks / OutputRing. It gets ONE
callable through the python_exec call context:

    job_starter(kind, label, owner, spawn_fn, output_limit_bytes=0) -> job_id

`spawn_fn(handle, cancel_event)` is a blocking sync function the TOOL owns:
  * it does the work (spawn a process, run a loop, poll a queue),
  * it calls handle.append(text, channel) as output arrives  (channel: 'stdout' |
    'stderr' | 'log' — 'log' is narration for the UI, never the model's read cursor),
  * it watches cancel_event and returns early when it is set,
  * it returns (status, detail, result) with status in completed | killed | failed.

The registry owns everything else: the id, the output ring with absolute offsets that
never move, the model's consuming cursor, the lifecycle, the terminal notice, and the
durable ledger row. A throwing producer becomes a FAILED job — it never crashes the
turn that launched it.

Kept in its own module so python_exec (SERVER side) and the CLIENT registry stay
decoupled: the CLIENT hands the callable in, this module only documents the contract.
"""
