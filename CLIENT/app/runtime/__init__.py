"""app.runtime — the runtime layer behind subagents + background jobs (client side).

Modules, one job each:
    output_ring.py    bounded byte stream with offsets that never move
    jobs.py           JobRegistry (identity, lifecycle, ring, pump, first-wins settlement)
    children.py       ChildRegistry (delegation, lineage, steering, cascade stop)
    child_run.py      executes ONE child turn (spawn seed / fork seed)
    runtime_store.py  durable ledger rows (SQLite) for children + jobs
    notice_hub.py     settlement -> runtime notice on the owner's chat channel

Wired ONCE at app startup by wire_runtime(bus, ...): the registries never import the bus
directly (a bus built without the runtime still stops cleanly — see ChatBus._cascade_stop),
and the notice hub never imports the registries. That keeps the dependency graph a cycle
free: loop -> channel -> notice lane -> hub -> registries.
"""

from .output_ring import OutputRing
from .jobs import (
    JobHooks, JobOutcome, JobRegistry, JobSpec,
    create_job_registry, get_job_registry, set_job_registry,
)
from .children import (
    CHILD_FORBIDDEN_TOOLS, ChildRegistry, ChildRecord, ChildSink,
    get_child_registry, set_child_registry,
)
from .child_run import child_context, run_child
from .notice_hub import wire_runtime

__all__ = [
    "OutputRing",
    "JobHooks", "JobOutcome", "JobRegistry", "JobSpec",
    "create_job_registry", "get_job_registry", "set_job_registry",
    "CHILD_FORBIDDEN_TOOLS", "ChildRegistry", "ChildRecord", "ChildSink",
    "get_child_registry", "set_child_registry",
    "child_context", "run_child",
    "wire_runtime",
]
