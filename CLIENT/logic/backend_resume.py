"""Resume an agentic turn after a recoverable backend outage.

When the SERVER tags a chat error as 'backend_unavailable' (Strata OOM / 503 / engine
restart / no-progress watchdog), the in-flight ReAct loop must NOT unwind: the in-loop
``messages`` list (tools, results, partial thinking) is preserved and the SAME list is
re-sent once the backend is healthy again. While waiting the user sees live "reloading"
status and - after a configurable grace - a question card offering keep-waiting /
switch-model / cancel. The card rides the same 'question' consent kind as ask_user_question,
so it lands in the normal turn history (cycle_question row) like every other dialog.

Liveness is modelled as a TRI-STATE probe (not a single "is the model served" check):

  SERVER_DOWN  - no SERVER answers (health probe failed AND the catalog is unreadable).
  SERVER_UP    - the SERVER answers but the model is not served yet (reloading / all-down).
  MODEL_SERVED - the SERVER answers and the model reports served.

The tri-state fixes two failure modes of the single-probe design: a SERVER outage was
indistinguishable from "model still loading" (so recovery was never detected and every poll
blocked up to 30 s), and the dialog had no way to tell the UI "the SERVER itself is down"
(so its keep-waiting button stayed disabled forever). Each probe is bounded to
BACKEND_PROBE_TIMEOUT_SEC so a hung SERVER never blocks past the poll interval.
"""

import asyncio
import json
import logging
import os
import time
from collections import namedtuple

from client_config import (
    BACKEND_RESUME_POLL_SEC,
    BACKEND_RESUME_MAX_WAIT_SEC,
    BACKEND_OFFER_SWITCH_AFTER_SEC,
    BACKEND_STATUS_POLL_SEC,
    BACKEND_PROBE_TIMEOUT_SEC,
)

from app.providers.coolems_client.errors import BackendUnavailableError

logger = logging.getLogger("COOLEMS.Logic.BackendResume")


class UserCancelled(Exception):
    """Raised when the user stops generation during the outage wait (clean cancel)."""
    pass


# --- probe outcome states ---------------------------------------------------

SERVER_DOWN = "server_down"    # no SERVER answers
SERVER_UP = "server_up"        # SERVER answers, model not served yet
MODEL_SERVED = "model_served"  # SERVER answers and the model reports served

#: One probe result: state, model-served flag, online slots, live catalog, reachability.
Probe = namedtuple("Probe", ["state", "served", "slots", "catalog", "reachable"])


# --- frame helpers ----------------------------------------------------------

async def _system(config, text: str) -> None:
    ws = getattr(config, "websocket", None)
    if ws is not None:
        try:
            await ws.send_text(json.dumps({"type": "system", "content": text}))
        except Exception:
            pass


async def _feedback(config, text: str) -> None:
    """Durable loop-history row (best-effort; identical text is recorded once)."""
    cycle = getattr(config, "cycle", None)
    if cycle is not None:
        try:
            await cycle.feedback(text)
        except Exception:
            pass


async def _recovery(config, text: str, status: str = "warning") -> None:
    """Orange recovery block + a durable loop-history row."""
    ws = getattr(config, "websocket", None)
    if ws is not None:
        try:
            await ws.send_text(json.dumps({
                "type": "recovery_warning", "message": text, "status": status,
            }))
        except Exception:
            pass
    await _feedback(config, text)


# --- model status -----------------------------------------------------------

def _strip_gguf(value: str) -> str:
    value = os.path.basename(value or "").lower()
    return value[:-5] if value.endswith(".gguf") else value


def _entry_matches(entry: dict, model: str) -> bool:
    name = str(entry.get("name") or "")
    if name == model:
        return True
    try:
        if _strip_gguf(name) == _strip_gguf(model):
            return True
    except Exception:
        pass
    if model in (entry.get("aliases") or []):
        return True
    return False


async def _catalog(config):
    """Model catalog from the SERVER, bounded so a hung SERVER never blocks the poll loop.

    Returns [] when the SERVER is unreachable, the catalog is empty, or the provider has no
    ``get_models_list`` (tests / mocks). A non-empty list is itself proof the SERVER answered.
    """
    provider = getattr(config, "provider", None)
    if provider is None:
        return []
    getter = getattr(provider, "get_models_list", None)
    if not callable(getter):
        return []
    try:
        models = await asyncio.wait_for(getter(), timeout=BACKEND_PROBE_TIMEOUT_SEC)
    except Exception as e:
        logger.debug(f"[BACKEND-RESUME] model list probe failed: {e}")
        return []
    return [m for m in (models or []) if isinstance(m, dict)]


async def _health_ok(config) -> bool:
    """True when the SERVER answers its health probe (reachability, not model-served).

    The SERVER's ``health_ok`` means "reachable and SOME node serves traffic", so this is
    the reachability signal the tri-state probe needs - a distinct thing from whether THIS
    model is served. A provider without ``health_check`` (tests / mocks) reports False.
    """
    provider = getattr(config, "provider", None)
    if provider is None:
        return False
    check = getattr(provider, "health_check", None)
    if not callable(check):
        return False
    try:
        ok, _msg, _models = await asyncio.wait_for(check(), timeout=BACKEND_PROBE_TIMEOUT_SEC)
    except Exception as e:
        logger.debug(f"[BACKEND-RESUME] health probe failed: {e}")
        return False
    return bool(ok)


async def _probe(config, model: str) -> Probe:
    """One bounded reachability + status probe, run concurrently.

    Reachability is ``health_ok OR non-empty catalog``: a readable catalog is itself proof
    the SERVER answered, so a fleet that is entirely down (health reports 'error' but the
    catalog still lists the configured models) is SERVER_UP, not SERVER_DOWN - the only
    thing missing then is a served model, which is exactly what the wait is for.
    """
    catalog, health_ok = await asyncio.gather(_catalog(config), _health_ok(config))
    reachable = health_ok or bool(catalog)

    served = False
    slots = 0
    for m in catalog:
        if _entry_matches(m, model):
            served = bool(m.get("served"))
            slots = int(m.get("online_slots") or 0)
            break

    if served:
        state = MODEL_SERVED
    elif reachable:
        state = SERVER_UP
    else:
        state = SERVER_DOWN
    return Probe(state, served, slots, catalog, reachable)


def _served_alternatives(catalog, model: str):
    """Other served model names the user could switch to."""
    alt = []
    for m in catalog:
        name = str(m.get("name") or "")
        if name and m.get("served") and not _entry_matches(m, model):
            alt.append(name)
    return alt


async def _push_fleet_status(config, catalog=None, server_down: bool = False) -> None:
    """Push a live 'fleet_status' frame so the outage card updates in place.

    ``server_down`` tells the UI the SERVER itself is unreachable (no catalog), so it shows
    the keep-waiting state instead of a dead "no model status available" panel and keeps the
    keep-waiting button clickable.
    """
    ws = getattr(config, "websocket", None)
    if ws is None:
        return
    if catalog is None:
        catalog = await _catalog(config)
    try:
        await ws.send_text(json.dumps({
            "type": "fleet_status", "models": catalog, "server_down": bool(server_down),
        }))
    except Exception:
        pass


# --- retry the preserved call ------------------------------------------------

async def _retry_same_messages(config, messages, model: str):
    """Re-send the SAME in-flight messages against *model* (bypasses call_provider_with_retry
    so a second outage is handled by the resume loop above, not by a fresh run).

    ``tools=`` comes from getattr(..., "ollama_tools", None): the agentic config carries
    the tool contract; the normal-mode config does not (plain chat has no tools)."""
    return await config.provider.chat_stream(
        model,
        messages,
        config.websocket,
        temperature=config.temperature,
        enable_thinking=config.enable_thinking,
        conv_id=config.conversation_id,
        tools=getattr(config, "ollama_tools", None),
        stop_event=config.stop_event,
    )


# --- switch-model question card ----------------------------------------------

def _parse_switch_answer(answer: str, model: str, alternatives) -> str:
    """Map a card answer to one of: 'wait' | 'switch:<model>' | 'cancel'."""
    a = str(answer or "").strip()
    if a.startswith("USER ANSWER:"):
        a = a[len("USER ANSWER:"):].strip()
    if "keep waiting" in a.lower() or "resume" in a.lower() or a.lower() in ("wait",):
        return "wait"
    if "cancel" in a.lower():
        return "cancel"
    for alt in alternatives:
        if alt in a:
            return f"switch:{alt}"
    return "wait"


async def _ask_model_switch(config, model: str, alternatives, reachable: bool,
                            catalog=None) -> str:
    """Show the switch-model question card (same 'question' kind as ask_user_question).

    ``reachable`` is the CURRENT state (SERVER answering or not) at card-show time: when the
    SERVER is down the card offers only keep-waiting / cancel and carries ``server_down`` so
    the UI keeps its keep-waiting button enabled; when the SERVER is up the card offers a
    model switch from the live catalog. A concurrent watcher keeps the live status fresh and
    auto-closes the card the moment the backend returns. Returns 'wait' | 'switch:<model>' |
    'cancel' (defaults 'wait' when no UI can ask).
    """
    from app.chat_bus import get_chat_bus
    from app.chat_bus.consent_dialog import get_approval_state

    conv_id = getattr(config, "conversation_id", None) or ""
    websocket = getattr(config, "websocket", None)
    channel = getattr(config, "steer_source", None)
    session_timer = getattr(config, "session_timer", None)

    if not conv_id or websocket is None:
        return "wait"

    state = get_approval_state(get_chat_bus(), conv_id)
    if state is None:
        return "wait"

    server_down = not reachable

    if server_down:
        opts = [{"label": "Keep waiting (auto-resume when it returns)", "recommended": True,
                 "resume": True}]
        for alt in alternatives:
            opts.append({"label": f"Switch to '{alt}'"})
        opts.append({"label": "Cancel this task"})
        question = "The AI SERVER went down. What should I do?"
        context = ("The SERVER (not just the model) stopped responding. Your in-progress "
                   "work is preserved and will resume automatically when the SERVER returns.")
    else:
        opts = [{"label": f"Resume with '{model}'", "recommended": True, "resume": True}]
        for alt in alternatives:
            opts.append({"label": f"Switch to '{alt}'"})
        opts.append({"label": "Cancel this task"})
        question = f"The AI backend for '{model}' went down and is restarting. What should I do?"
        context = (f"Backend '{model}' stopped responding (restarting or out of memory). "
                   "Your in-progress work is preserved and will resume automatically when it returns.")

    if catalog is None:
        catalog = await _catalog(config)
    payload = json.dumps({
        "question": question, "options": opts, "context": context,
        "requirement": "handle the backend outage",
        "monitor": True, "failed_model": model, "models": catalog,
        "server_down": bool(server_down),
    }, ensure_ascii=False)
    request_id = state.new_request_id()
    if channel is not None:
        channel.add_pending_question(request_id, {
            "question": question, "options": opts, "context": context,
            "mode": "blocking", "requirement": "handle the backend outage",
        })
    try:
        await websocket.send_text(json.dumps({
            "type": "exec_approval_request", "conv_id": conv_id,
            "request_id": request_id, "kind": "question",
            "code": payload, "description": context,
        }))
        if channel is not None:
            await channel.record_runtime("question", request_id, question=question,
                                         options=[o["label"] for o in opts], pending=True)
    except Exception:
        if channel is not None:
            channel.drop_pending_question(request_id)
        return "wait"

    watcher = asyncio.create_task(
        _watch_while_card(config, request_id, state, model, alternatives,
                          server_down=server_down),
        name="backend-resume-watch",
    )
    if session_timer is not None:
        session_timer.pause()
    try:
        answer = await state.wait_for_decision(request_id, kind="question")
    finally:
        if session_timer is not None:
            session_timer.resume()
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass

    if channel is not None:
        channel.drop_pending_question(request_id)
        try:
            await channel.record_runtime("question", request_id, question=question,
                                         options=[o["label"] for o in opts], decision=answer)
        except Exception:
            pass
    return _parse_switch_answer(answer, model, alternatives)


async def _watch_while_card(config, request_id, state, model: str, alternatives,
                            server_down: bool = False) -> None:
    """Live status while the switch card is open; auto-resolve when the backend returns."""
    last_reachable = None
    try:
        while True:
            await asyncio.sleep(BACKEND_STATUS_POLL_SEC)
            probe = await _probe(config, model)
            if probe.served:
                await _recovery(
                    config,
                    (f"\u2705 The SERVER is back online - resuming now." if server_down
                     else f"\u2705 Backend '{model}' is back online - resuming now."),
                    "success",
                )
                try:
                    state.resolve(request_id, "keep waiting")
                except Exception:
                    pass
                return
            # SERVER-down card: the first time the SERVER becomes reachable again, say so
            # explicitly (it is distinct from "the model is served" - a reload follows).
            if server_down and last_reachable is False and probe.reachable:
                await _recovery(
                    config,
                    f"\u2705 The SERVER is back online - '{model}' is reloading; resuming when served.",
                    "success",
                )
            last_reachable = probe.reachable
            await _push_fleet_status(config, catalog=probe.catalog,
                                     server_down=not probe.reachable)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


# --- main resume loop --------------------------------------------------------

async def resume_after_backend_outage(config, messages, error: BackendUnavailableError):
    """Preserve *messages* and resume the call when the backend (or the SERVER) returns.

    Returns (result, stats) after a successful re-send. Raises UserCancelled on stop, or a
    non-retryable RuntimeError when the budget is exhausted / the user cancels.

    Two flavours, both recoverable:
      - backend down   (error.server_down False): the SERVER is alive and tagged the outage;
        the dialog offers a model switch from the live catalog.
      - SERVER down    (error.server_down True):  no catalog is reachable, so the dialog only
        offers keep-waiting / cancel, and the wait resumes when the SERVER answers again.
    """
    model = config.model
    server_down = bool(getattr(error, "server_down", False))
    started = time.monotonic()
    deadline = started + BACKEND_RESUME_MAX_WAIT_SEC
    offer_shown = False
    last_status = 0.0
    last_state = None

    if server_down:
        await _recovery(
            config,
            "\u26a0\ufe0f The AI SERVER went down. Your in-progress work is preserved - "
            "waiting for it to come back online.",
            "warning",
        )
    else:
        await _recovery(
            config,
            f"\u26a0\ufe0f Backend '{model}' went down (restarting or out of memory). "
            "Your in-progress work is preserved - waiting for it to come back.",
            "warning",
        )

    while True:
        if getattr(config, "stop_event", None) is not None and config.stop_event.is_set():
            raise UserCancelled("User cancelled during backend outage wait.")

        now = time.monotonic()
        probe = await _probe(config, model)

        # The SERVER just came back (was down, now reachable but the model still loads):
        # announce the transition - it is a distinct milestone from "model served".
        if last_state == SERVER_DOWN and probe.state == SERVER_UP:
            await _recovery(
                config,
                f"\u2705 The SERVER is back online - '{model}' is reloading; resuming when served.",
                "success",
            )
        last_state = probe.state

        if probe.served:
            await _recovery(
                config,
                (f"\u2705 The SERVER is back online ({probe.slots} slot(s) for '{model}') - resuming."
                 if server_down
                 else f"\u2705 Backend '{model}' is back online ({probe.slots} slot(s)) - resuming."),
                "success",
            )
            try:
                return await _retry_same_messages(config, messages, model)
            except BackendUnavailableError as e2:
                error = e2
                server_down = bool(getattr(e2, "server_down", False)) or server_down
                await _system(config, f"{'SERVER' if server_down else 'Backend'} flapped again - continuing to wait.")
                last_state = None
                continue

        if now >= deadline:
            if not offer_shown:
                offer_shown = True
                alternatives = _served_alternatives(probe.catalog, model)
                decision = await _ask_model_switch(config, model, alternatives,
                                                   probe.reachable, probe.catalog)
                if decision == "cancel":
                    raise RuntimeError(
                        (f"The SERVER did not recover and you cancelled the wait. " if server_down
                         else f"Backend '{model}' did not recover and you cancelled the wait. ")
                        + "Your in-progress work could not be resumed automatically.")
                if decision.startswith("switch:"):
                    return await _switch_and_retry(config, messages, decision[len("switch:"):].strip(), error)
            raise RuntimeError(
                (f"The SERVER did not come back online within {BACKEND_RESUME_MAX_WAIT_SEC:.0f}s. " if server_down
                 else f"Backend '{model}' did not recover within {BACKEND_RESUME_MAX_WAIT_SEC:.0f}s. ")
                + "Your in-progress work could not be resumed automatically - send your message again.")

        if now - last_status >= BACKEND_RESUME_POLL_SEC:
            last_status = now
            await _push_fleet_status(config, catalog=probe.catalog,
                                     server_down=not probe.reachable)

        if not offer_shown and (now - started) >= BACKEND_OFFER_SWITCH_AFTER_SEC:
            offer_shown = True
            alternatives = _served_alternatives(probe.catalog, model)
            decision = await _ask_model_switch(config, model, alternatives,
                                               probe.reachable, probe.catalog)
            if decision == "cancel":
                raise RuntimeError(
                    (f"The SERVER did not recover and you cancelled the wait. " if server_down
                     else f"Backend '{model}' did not recover and you cancelled the wait. ")
                    + "Your in-progress work could not be resumed automatically.")
            if decision.startswith("switch:"):
                return await _switch_and_retry(config, messages, decision[len("switch:"):].strip(), error)

        await asyncio.sleep(BACKEND_RESUME_POLL_SEC)


async def _switch_and_retry(config, messages, new_model: str, error):
    """Re-target the preserved call at *new_model* and resume.

    In the 2026-10 fleet there is no server-side reload: ``provider.switch_model(name)`` is a
    ROUTING CHECK that reports whether the swarm actually serves *new_model* right now. Verify
    it BEFORE re-sending so a flapped/not-served alternative surfaces a clean error instead of
    re-entering the same outage path.
    """
    await _recovery(config, f"\U0001F504 Switching to '{new_model}' (backend for the previous "
                            f"model was down).", "warning")
    config.model = new_model

    switch = getattr(config.provider, "switch_model", None)
    if callable(switch):
        try:
            ok, msg = await switch(new_model)
        except Exception as e:
            ok, msg = False, f"switch check failed: {e}"
        if not ok:
            raise RuntimeError(
                f"Switched to '{new_model}' but it is not served right now: {msg}. "
                "Your in-progress work could not be resumed automatically."
            )

    try:
        return await _retry_same_messages(config, messages, new_model)
    except BackendUnavailableError as e2:
        # The alternative is also down - surface that but do not loop forever.
        raise RuntimeError(
            f"Switched to '{new_model}' but it is also unavailable ({e2}). "
            "Your in-progress work could not be resumed automatically."
        ) from e2
