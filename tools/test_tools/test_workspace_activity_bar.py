"""Regression coverage for the sidebar workspace activity/warning bar (2026-11-28).

The bar between the "New Workspace" header and the workspace list must tell the user, at a
glance, whether anything is still working - and let them jump between those workspaces:

  * light blue  while any workspace is generating/queued -> click cycles the running ones;
  * green       for finished-and-unopened workspaces     -> click cycles them (visit = seen);
  * orange      when a workspace waits on a decision card -> overrides the blue, click cycles
                the workspaces that need the user;
  * unchanged background when nothing is operating.

Covered here:
  * GET /api/conversations/live: only THIS client's live sessions, with needs_attention
    (the client cannot render a consent card from the DB after a reload);
  * the pure resolver + cycle order (run in node, no DOM);
  * the static wiring: markup between header and list, css module, refresh hooks.
"""
import asyncio
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from types import ModuleType

from tools.test_tools._client_imports import client_app_scope, load_module  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BAR_JS = _REPO_ROOT / "CLIENT/static/ui/workspace-activity-bar.js"

OWN_IP = "10.0.0.7"
OTHER_IP = "10.0.0.99"

CONV_SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address TEXT,
    working_root TEXT,
    color TEXT,
    parent_id TEXT,
    unread_finished INTEGER DEFAULT 0
)
"""


def _load_conversations(monkeypatch, db):
    """Load conversations.py with db/helpers/turn_ops stubbed; keep the real ownership guard."""
    import app.workspace_tree
    import app.workspace_access

    monkeypatch.setitem(sys.modules, "app.workspace_tree", app.workspace_tree)
    monkeypatch.setitem(sys.modules, "app.workspace_access", app.workspace_access)

    manager = ModuleType("app.db_manager")
    manager.get_db_connection = lambda: db
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, "app.db_manager", manager)

    helpers = ModuleType("app.helpers")
    helpers.get_client_ip = lambda request: OWN_IP
    monkeypatch.setitem(sys.modules, "app.helpers", helpers)

    ws_pkg = ModuleType("app.websocket")
    ws_pkg.__path__ = []
    turn_ops = ModuleType("app.websocket.turn_ops")
    turn_ops.delete_conversation_rows = lambda cursor, conv_id: {"turn_log": 0, "messages": 0, "conversations": 0}
    turn_ops.truncate_turn_logs = lambda *a, **k: 0
    turn_ops.load_turn_logs = lambda conv_id: []
    ws_pkg.turn_ops = turn_ops
    monkeypatch.setitem(sys.modules, "app.websocket", ws_pkg)
    monkeypatch.setitem(sys.modules, "app.websocket.turn_ops", turn_ops)

    conversations = load_module("CLIENT/app/routers/conversations.py", "_conversations_live_bar_test")
    agent = types.SimpleNamespace(get_name=lambda: "Jack")
    router = conversations.create_conversations_router(":memory:", "test-model", agent)
    return conversations, router


def _route(router, path_suffix, method=None):
    for r in router.routes:
        if not getattr(r, "path", "").endswith(path_suffix):
            continue
        if method is not None and method not in (getattr(r, "methods", None) or set()):
            continue
        return r
    raise AssertionError(f"route not found: {path_suffix} {method}")


class _FakeRequest:
    pass


class _Channel:
    """Minimal ChatChannel stand-in: status + optional pending consent card."""

    def __init__(self, status, queue_position=0, pending=()):
        self.status = types.SimpleNamespace(value=status)
        self.queue_position = queue_position
        if pending:
            self.exec_approval = types.SimpleNamespace(pending_requests=lambda: list(pending))


class _Bus:
    def __init__(self, channels):
        self.channels = channels


def _stub_bus(monkeypatch, bus):
    chat_bus = ModuleType("app.chat_bus")
    chat_bus.get_chat_bus = lambda: bus
    monkeypatch.setitem(sys.modules, "app.chat_bus", chat_bus)


def _db(*ids):
    db = sqlite3.connect(":memory:")
    db.execute(CONV_SCHEMA)
    for conv_id in ids:
        owner = OWN_IP if not conv_id.startswith("foreign") else OTHER_IP
        db.execute("INSERT INTO conversations (id, title, ip_address) VALUES (?, ?, ?)",
                   (conv_id, conv_id, owner))
    db.commit()
    return db


# ===== GET /api/conversations/live =====


def test_live_lists_only_own_live_sessions(monkeypatch):
    db = _db("ownaaa", "ownbbb", "ownccc", "foreign")
    _stub_bus(monkeypatch, _Bus({
        "ownaaa": _Channel("generating", queue_position=1),
        "ownbbb": _Channel("queued", queue_position=2, pending=[("req1", "python")]),
        "ownccc": _Channel("idle"),                       # finished: not live
        "foreign": _Channel("generating"),                # another client's workspace
    }))

    _, router = _load_conversations(monkeypatch, db)
    result = asyncio.run(_route(router, "/api/conversations/live", "GET").endpoint(_FakeRequest()))

    live = {item["id"]: item for item in result["live"]}
    assert set(live) == {"ownaaa", "ownbbb"}
    assert live["ownaaa"]["status"] == "generating"
    assert live["ownaaa"]["needs_attention"] is False
    assert live["ownbbb"]["status"] == "queued"
    assert live["ownbbb"]["queue_position"] == 2
    assert live["ownbbb"]["needs_attention"] is True      # parked on a consent card
    db.close()


def test_live_is_empty_without_a_bus(monkeypatch):
    db = _db("ownaaa")
    _stub_bus(monkeypatch, None)
    _, router = _load_conversations(monkeypatch, db)
    result = asyncio.run(_route(router, "/api/conversations/live", "GET").endpoint(_FakeRequest()))
    assert result == {"live": []}
    db.close()


def test_live_survives_a_broken_bus(monkeypatch):
    class _Broken:
        @property
        def channels(self):
            raise RuntimeError("bus exploded")

    db = _db("ownaaa")
    _stub_bus(monkeypatch, _Broken())
    _, router = _load_conversations(monkeypatch, db)
    result = asyncio.run(_route(router, "/api/conversations/live", "GET").endpoint(_FakeRequest()))
    assert result == {"live": []}       # the bar must never break the sidebar
    db.close()


# ===== pure resolver / cycle order (node) =====


def _run_bar_js(cases):
    """Evaluate the bar's pure functions in node and return their results."""
    script = f"""
const bar = require({json.dumps(str(_BAR_JS))});
const cases = {json.dumps(cases)};
const out = cases.map(c => ({{
    resolved: c.resolve ? bar.resolveWorkspaceActivity(c.resolve) : null,
    next: c.next ? bar.nextInCycle(c.next.list, c.next.current, c.next.last) : null,
}}));
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_resolver_and_cycle_order():
    results = _run_bar_js([
        # idle: nothing operating
        {"resolve": {"order": ["a", "b"], "statuses": {"a": "idle", "b": "idle"}}},
        # running: order follows the sidebar
        {"resolve": {"order": ["a", "b", "c"], "statuses": {"a": "idle", "b": "generating", "c": "queued"}}},
        # a pending dialog overrides running and still reports the running list
        {"resolve": {"order": ["a", "b", "c"], "statuses": {"a": "generating", "b": "generating"},
                     "attention": {"c": "dialog"}}},
        # finished: a live task is never "finished"; done markers and the DB flag agree
        {"resolve": {"order": ["a", "b", "c"], "statuses": {"a": "generating"},
                     "attention": {"a": "done", "b": "done"}, "finished": {"c": True}}},
        # server truth fills a workspace this browser has no socket for
        {"resolve": {"order": ["a"], "statuses": {}, "serverLive": {"a": "queued"}}},
        # local frames win over the last server poll
        {"resolve": {"order": ["a"], "statuses": {"a": "idle"}, "serverLive": {"a": "generating"}}},
        # unacknowledged server dialog -> attention
        {"resolve": {"order": ["a"], "statuses": {"a": "generating"}, "serverAttention": {"a": True}}},
        # cycle: current in the group walks forward and wraps
        {"next": {"list": ["a", "b", "c"], "current": "b", "last": None}},
        {"next": {"list": ["a", "b", "c"], "current": "c", "last": None}},
        # cycle: group that shrinks as it is visited continues after the last jump
        {"next": {"list": ["b", "c"], "current": "a", "last": "b"}},
        {"next": {"list": ["b", "c"], "current": "a", "last": None}},
        {"next": {"list": [], "current": "a", "last": None}},
    ])

    assert results[0]["resolved"]["primary"] == "idle"
    assert results[1]["resolved"]["primary"] == "running"
    assert results[1]["resolved"]["groups"]["running"] == ["b", "c"]
    assert results[2]["resolved"]["primary"] == "attention"
    assert results[2]["resolved"]["groups"]["attention"] == ["c"]
    assert results[2]["resolved"]["groups"]["running"] == ["a", "b"]
    assert results[3]["resolved"]["groups"]["finished"] == ["b", "c"]   # 'a' is running
    assert results[3]["resolved"]["primary"] == "running"
    assert results[4]["resolved"]["primary"] == "running"
    assert results[5]["resolved"]["primary"] == "idle"
    assert results[6]["resolved"]["primary"] == "attention"

    assert results[7]["next"] == "c"
    assert results[8]["next"] == "a"
    assert results[9]["next"] == "c"
    assert results[10]["next"] == "b"
    assert results[11]["next"] is None


def test_server_live_fallback_clears_when_quiet():
    """A socket-less workspace must not stay 'running' forever after /api/conversations/live
    stops reporting it - the fallback status the bar painted has to be cleared."""
    script = r"""
const { WorkspaceActivityBar: bar } = require(BAR_PATH);
const statuses = {};
const pool = { statuses, sockets: {}, updateChatStatus(id, st) { statuses[id] = st; } };
global.ChatSocketPool = pool;

bar.serverLive = { wsA: 'generating', wsB: 'queued' };
bar.serverClaimed = {};
bar.paintServerStatuses();                     // paint + claim both (no real socket owns them)
const afterClaim = { statuses: { ...pool.statuses }, claimed: { ...bar.serverClaimed } };

// next poll: wsA finished (no longer live), wsB still queued -> only wsA is cleared
bar.reconcileServerLive({ wsB: 'queued' });
const afterPartial = { statuses: { ...pool.statuses }, claimed: { ...bar.serverClaimed } };

// a real socket now owns wsB -> a later "gone quiet" must NOT clobber it
pool.sockets = { wsB: { ws: {}, reconnecting: false } };
bar.reconcileServerLive({});
const afterSocket = { statuses: { ...pool.statuses }, claimed: { ...bar.serverClaimed } };

console.log(JSON.stringify({ afterClaim, afterPartial, afterSocket }));
"""
    script = script.replace("BAR_PATH", json.dumps(str(_BAR_JS)))
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["afterClaim"]["statuses"] == {"wsA": "generating", "wsB": "queued"}
    assert out["afterClaim"]["claimed"] == {"wsA": True, "wsB": True}

    # wsA no longer live -> cleared to idle and unclaimed; wsB still live -> untouched
    assert out["afterPartial"]["statuses"] == {"wsA": "idle", "wsB": "queued"}
    assert out["afterPartial"]["claimed"] == {"wsB": True}

    # wsB's real socket owns it -> gone-quiet must not clobber it; it stays claimed (skipped)
    assert out["afterSocket"]["statuses"] == {"wsA": "idle", "wsB": "queued"}
    assert out["afterSocket"]["claimed"] == {"wsB": True}


# ===== static wiring =====



# ===== DOM smoke test: mount -> paint -> click cycling (node, stubbed DOM) =====

_DOM_HARNESS = r"""
function makeEl(tag) {
    const el = {
        tagName: tag, children: [], dataset: {}, hidden: false, textContent: '', title: '',
        className: '', type: '', handlers: {},
        append(...kids) { this.children.push(...kids); },
        replaceChildren(...kids) { this.children = kids; },
        addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
        setAttribute() {},
        fire(type, target, key) {
            for (const fn of this.handlers[type] || []) {
                fn({ target: target || this, key, stopPropagation() {}, preventDefault() {} });
            }
        },
    };
    return el;
}

const els = { workspaceActivityBar: makeEl('div') };
const document = {
    readyState: 'loading',
    hidden: false,
    handlers: {},
    getElementById(id) { return els[id] || null; },
    createElement(tag) { return makeEl(tag); },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
};

const calls = [];
const pool = { statuses: {}, attention: {}, updateChatStatus(id, st) { this.statuses[id] = st; } };
const manager = {
    byId: new Map([['wsA', {}], ['wsB', {}], ['wsC', {}]]),
    markSeen(id) { calls.push(['markSeen', id]); },
};

global.document = document;
global.window = { ConversationMgr: manager };
global.ChatSocketPool = pool;
global.COOLEMS = { currentConversation: 'wsB' };
global.loadConversation = async (id) => { calls.push(['load', id]); };
global.fetch = async () => ({ ok: true, json: async () => ({ live: [] }) });

const { WorkspaceActivityBar: bar } = require(BAR_PATH);
const mounted = bar.mount();

function snap() {
    return {
        mounted,
        state: bar.el.dataset.state,
        hasRunning: bar.el.dataset.hasRunning,
        label: bar.labelEl.textContent,
        chip: bar.chipEl.hidden ? null : { group: bar.chipEl.dataset.group || null },
        pos: bar.posEl.hidden ? null : bar.posEl.textContent,
    };
}

const out = [];
bar.refresh();
out.push({ step: 'idle', snap: snap(), calls: calls.slice() });

pool.statuses = { wsA: 'generating', wsC: 'queued' };
bar.refresh();
out.push({ step: 'running', snap: snap(), calls: calls.slice() });

pool.attention = { wsC: 'dialog' };
bar.refresh();
out.push({ step: 'attention', snap: snap(), calls: calls.slice() });

calls.length = 0;
bar.el.fire('click');                       // -> the workspace that needs the user
out.push({ step: 'click-attention', snap: snap(), calls: calls.slice() });

calls.length = 0;
pool.attention = {};
pool.statuses = {};
manager.byId = new Map([['wsA', { unread_finished: 1 }], ['wsB', {}], ['wsC', { unread_finished: 1 }]]);
bar.refresh();
out.push({ step: 'finished', snap: snap(), calls: calls.slice() });

bar.el.fire('click');                       // first finished workspace
out.push({ step: 'click-finished-1', snap: snap(), calls: calls.slice() });
bar.el.fire('click');                       // the NEXT one (list did not change in the stub)
out.push({ step: 'click-finished-2', snap: snap(), calls: calls.slice() });

// secondary chip: running while a dialog is pending -> the chip cycles ONLY running
calls.length = 0;
pool.statuses = { wsA: 'generating' };
pool.attention = { wsC: 'dialog' };
bar.refresh();
const chipGroup = bar.chipEl.dataset.group;
bar.el.fire('click', bar.chipEl);           // click on the chip must not run the bar's own cycle
bar.chipEl.fire('click');
out.push({ step: 'chip', snap: snap(), chipGroup, calls: calls.slice() });

// keyboard: Enter on the bar cycles the primary group
calls.length = 0;
bar.el.fire('keydown', null, 'Enter');
out.push({ step: 'keydown', calls: calls.slice([0]) });

bar.stopPolling();
console.log(JSON.stringify(out));
process.exit(0);
"""


def test_bar_paints_and_cycles_in_a_stubbed_dom():
    script = _DOM_HARNESS.replace("BAR_PATH", json.dumps(str(_BAR_JS)))
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    steps = {item["step"]: item for item in json.loads(proc.stdout)}

    idle = steps["idle"]["snap"]
    assert idle["mounted"] is True
    assert idle["state"] == "idle" and idle["label"] == "No workspace running"
    assert idle["chip"] is None and idle["pos"] is None

    running = steps["running"]["snap"]
    assert running["state"] == "running" and running["label"] == "2 running"
    assert running["pos"] == "0/2"          # neither running workspace is on screen

    attention = steps["attention"]["snap"]
    assert attention["state"] == "attention"
    assert attention["hasRunning"] == "1"   # blue underline: tasks still run behind the dialog
    assert attention["label"] == "1 needs you"
    assert attention["chip"]["group"] == "running"     # secondary group stays reachable

    clicked = steps["click-attention"]["calls"]
    assert clicked == [["markSeen", "wsC"], ["load", "wsC"]]

    finished = steps["finished"]["snap"]
    assert finished["state"] == "finished" and finished["label"] == "2 finished"
    assert finished["pos"] == "0/2"
    # visiting marks seen + opens the workspace, then the NEXT click moves on to the other one
    assert steps["click-finished-1"]["calls"] == [["markSeen", "wsA"], ["load", "wsA"]]
    assert steps["click-finished-2"]["calls"] == [["markSeen", "wsA"], ["load", "wsA"],
                                                  ["markSeen", "wsC"], ["load", "wsC"]]
    assert steps["click-finished-2"]["snap"]["pos"] == "0/2"

    chip = steps["chip"]
    assert chip["chipGroup"] == "running"              # the chip targets the OTHER group
    assert chip["calls"] == [["markSeen", "wsA"], ["load", "wsA"]]
    assert steps["keydown"]["calls"] == [["markSeen", "wsC"], ["load", "wsC"]]
def test_bar_sits_between_header_and_list():
    html = (_REPO_ROOT / "CLIENT/index.html").read_text(encoding="utf-8")
    bar_at = html.index('id="workspaceActivityBar"')
    assert html.index('class="new-chat-btn"') < bar_at < html.index('id="chatHistory"')
    assert "workspace-activity-bar.js" in html
    assert "?v=20261128b" in html          # stylesheet cache-bust bumped for the new CSS module


def test_css_module_and_states():
    styles = (_REPO_ROOT / "CLIENT/static/ui/styles.css").read_text(encoding="utf-8")
    assert "css/10-workspace-activity-bar.css" in styles
    css = (_REPO_ROOT / "CLIENT/static/ui/css/10-workspace-activity-bar.css").read_text(encoding="utf-8")
    for state in ("running", "finished", "attention"):
        assert f'[data-state="{state}"]' in css
    # orange overrides the blue but keeps the running information visible
    assert '[data-has-running="1"]' in css


def test_ui_hooks_keep_the_bar_in_sync():
    pool = (_REPO_ROOT / "CLIENT/static/ui/chat-socket-pool.js").read_text(encoding="utf-8")
    assert pool.count("refreshWorkspaceActivityBar") >= 4     # status + attention set/clear + closeAll
    manager = (_REPO_ROOT / "CLIENT/static/ui/conversation-manager.js").read_text(encoding="utf-8")
    assert "refreshWorkspaceActivityBar" in manager           # re-render -> green group refresh
    assert "WorkspaceActivityBar.acknowledge" in manager      # visit = seen, server dialog ack'd
    bar = _BAR_JS.read_text(encoding="utf-8")
    assert "/api/conversations/live" in bar
    assert "markSeen" in bar
    assert "loadConversation" in bar                          # same jump path as a row click
