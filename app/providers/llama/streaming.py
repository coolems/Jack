"""Streaming chat logic for the llama.cpp provider.

SSE parsing, content/thinking buffering, tool-call accumulation and WebSocket sends with
flush thresholds. chat_stream() reads real token counts from the final SSE chunk's
'usage' field (prompt_tokens, completion_tokens, eval_duration) and returns them in a
TokenStats, one per request. HTTP goes through the shared httpx.AsyncClient in
http_client.py.

Reasoning: delta chunks may carry 'reasoning_content'; what we SEND comes from
ThinkingPolicy (app/providers/llama/thinking.py), which reads the model's controls from
/props and maps the UI's canonical level to that model's vocabulary. A level also carries a
reasoning TOKEN budget (config.LLAMA_THINKING_BUDGET), sent as thinking_budget when the
template declares it. Reasoning is buffered and streamed as {type:"thinking"} frames. When
the controls cannot be discovered, or the endpoint rejects the keys, no thinking keys are
sent - see _resolve_thinking / _demote_thinking.
"""
import json
import re
import time
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple

import httpx

from config import (
    CONTEXT_WINDOW_TOKENS,
    LLAMA_MIN_SAFE_MAX_TOKENS,
    STREAM_BUFFER_SIZE_CHARS,
    STREAM_BUFFER_SIZE_MSGS,
    STREAM_BUFFER_TIMEOUT_SEC,
    LLAMA_SERVER_MAX_NEW_TOKENS,
    LLAMA_STREAM_STALL_TIMEOUT_SEC,
    LLAMA_AUTO_RESTART_ON_STALL,
    LLAMA_MODEL_STATE_TTL_SEC,
)

from ..token_stats import TokenStats
from .converter import convert_messages_for_vision
from .payload import build_llama_payload
from .thinking import ThinkingPolicy, normalize_mode
from .model_utils import _is_mmproj_file, normalize_model_name
from .vision_support import VisionNotAvailableError, make_vision_error, _looks_like_mmproj_error
from .http_client import get_async_client
from .errors import BackendUnavailableError, ContextWindowExceededError
from . import model_state

logger = logging.getLogger("COOLEMS.Provider.Llama.Streaming")


#: Body/status markers that mean "the engine is DOWN or RESTARTING and will come back",
#: as opposed to a permanent request failure (bad model, bad prompt, auth).
_BACKEND_UNAVAILABLE_MARKERS = (
    "engine exited before it was ready",
    "engine stopped",
    "engine did not start",
    "engine had stopped",
    "stopped making progress",
    "no progress",
    "mib free",
    "loading the experts",
    "starting the engine",
)


def _is_backend_unavailable(status_code: int, body: str) -> bool:
    """True when an HTTP failure means 'engine down/restarting', not a bad request.

    Strata's engine answers 503 with a JSON body naming the cause (OOM reserve, no-progress
    watchdog, expert reload). These are recoverable: the client should wait/resume, not fail
    the whole turn. 502/503/504 are treated as down regardless of body.
    """
    if status_code in (502, 503, 504):
        return True
    low = (body or "").lower()
    return any(m in low for m in _BACKEND_UNAVAILABLE_MARKERS)


#: Body markers that mean "the prompt alone exceeds the engine's context window", as opposed
#: to a malformed request (bad field, unknown model, thinking-key rejection). The engine
#: rejects these BEFORE generation; the fix is to send less context, never to wait.
_CONTEXT_WINDOW_MARKERS = (
    "exceeds the context window",
    "requests are never truncated",
    "prompt is too long",
    "prompt too long",
)

#: "request exceeds the context window: prompt (305082/321877 tokens) > 262144; ..."
_CONTEXT_WINDOW_400_RE = re.compile(
    r"prompt\s*\(\s*(\d+)\s*/\s*(\d+)\s*tokens?\s*\)\s*>\s*(\d+)", re.IGNORECASE)


def _is_context_window_exceeded(status_code: int, body: str) -> bool:
    """True when the engine rejected the request because the prompt exceeds its window."""
    if status_code not in (400, 413, 422):
        return False
    low = (body or "").lower()
    return any(m in low for m in _CONTEXT_WINDOW_MARKERS)


def _parse_context_window_400(body: str):
    """(prompt_tokens, context_window) from a context-window rejection body, or (None, None)."""
    m = _CONTEXT_WINDOW_400_RE.search(body or "")
    if m:
        return int(m.group(1)), int(m.group(3))
    return None, None


def _thinking_policy_for(api_url: str, model: str) -> ThinkingPolicy:
    """The thinking policy of the backend that will answer this request.

    Registry-first: the node's /props already says what its baked template accepts. With no
    registry (direct single-node use, tests) the answer is ThinkingPolicy.none() - a guessed
    kwarg on a strict template is an HTTP 400 on every request. Imported lazily to keep the
    import graph acyclic.
    """
    try:
        from .backend_registry import get_registry
        reg = get_registry()
    except Exception:  # pragma: no cover - defensive
        return ThinkingPolicy.none()
    if reg is None:
        return ThinkingPolicy.none()
    backend = reg.get_for_api_url(api_url)
    if backend is not None:
        return backend.thinking
    return reg.thinking_for_model(model or "")


def _resolve_thinking(api_url: str, model: str, thinking_mode, enable_thinking):
    """(mode, chat_template_kwargs, display) for one request - two separate decisions.

    What the MODEL is told: only from a canonical level, translated by the serving node's
    ThinkingPolicy; no level means no keys, so the model keeps its default and a strict
    template never sees an undeclared variable. Whether reasoning is SHOWN: a level other
    than 'none' shows it, otherwise the boolean decides. enable_thinking may be a bool or a
    level string, so the existing single field carries both.
    """
    mode = normalize_mode(thinking_mode) if thinking_mode not in (None, "") else ""
    if not mode and isinstance(enable_thinking, str):
        mode = normalize_mode(enable_thinking)
    policy = _thinking_policy_for(api_url, model)
    kwargs = policy.kwargs(mode) if mode else {}
    if mode:
        display = mode != "none"
    else:
        display = bool(enable_thinking)
    if mode and not kwargs:
        logger.debug(
            "[THINKING] mode=%s requested but %s/%s exposes no thinking knob "
            "(policy %s: %s) - sending nothing",
            mode, api_url, model or "?", policy.source, policy.hint or "no controls",
        )
    elif kwargs:
        logger.debug("[THINKING] %s/%s -> chat_template_kwargs %s", api_url, model or "?", kwargs)
    if mode and kwargs.get("thinking_budget"):
        logger.info(
            "[THINKING] %s/%s level=%s reasoning capped at %d tokens (thinking_budget)",
            api_url, model or "?", mode, kwargs["thinking_budget"],
        )
    return mode, kwargs, bool(display)


def _demote_thinking(api_url: str, model: str, reason: str, sent_kwargs: dict) -> None:
    """Stop sending thinking keys to a node whose advertised knob just failed.

    A template can declare a knob its build does not honor, and that only surfaces as an
    HTTP 4xx on a real request. On the first rejection the node's policy becomes
    ThinkingPolicy.demote(): later requests carry no thinking keys, the turn succeeds, and
    the catalog/UI shows "Think: n/a" with the reason. Only demoted when we actually sent
    keys, so an unrelated 400 cannot disable thinking for the whole node.
    """
    if not sent_kwargs:
        return
    try:
        from .backend_registry import get_registry
        reg = get_registry()
    except Exception:  # pragma: no cover - defensive
        return
    if reg is None:
        return
    backend = reg.get_for_api_url(api_url)
    if backend is None:
        return
    if backend.thinking.source == "runtime":
        return                      # already demoted - do not rewrite the reason
    if not backend.thinking.supported:
        return                      # nothing was ever advertised - nothing to demote
    backend.thinking = ThinkingPolicy.demote(backend.thinking, reason)
    logger.warning(
        "[THINKING] %s/%s rejected thinking keys (%s) - demoted to 'not available'; "
        "requests now send none. UI shows 'Think: n/a'. Pin it in the server config's "
        "'thinking' block if the model really supports these controls.",
        backend.label, model or "?", reason,
    )



async def _iter_sse_with_stall(aiter, timeout_sec: int, conv_id: str, api_url: str):
    """Wrap the SSE line iterator with a per-chunk silence watchdog.

    A server that accepts the request and then sends nothing would otherwise hang with no
    log and no result: this raises after timeout_sec of silence so the caller fails fast and
    can auto-restart. Yields SSE lines exactly as aiter_lines() would.
    """
    chunks = 0
    while True:
        t0 = time.time()
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=timeout_sec)
        except StopAsyncIteration:
            return
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(
                f"[STREAM-STALL] llama-server sent NO SSE chunk for {timeout_sec}s "
                f"(chunks so far: {chunks}, conv={conv_id}). GPU idle / server frozen - aborting request."
            )
            restarted = False
            if LLAMA_AUTO_RESTART_ON_STALL:
                from .backend_registry import get_registry

                _reg = get_registry()
                _backend = _reg.get_for_api_url(api_url) if _reg is not None else None
                if _backend is not None and not _backend.local:
                    # NEVER kill/spawn a remote node from the brain (the old code used
                    # LLAMA_SERVER_PORT here, i.e. it killed the brain's own llama and
                    # spawned a rogue local one on a remote stall).
                    _backend.mark_unhealthy("stream stalled; remote node is not auto-restarted")
                    logger.error(
                        "[STREAM-STALL] %s is REMOTE - not killing/restarting it from the brain. "
                        "Check that node's machine (llama-node log).", _backend.label,
                    )
                elif _backend is not None and not _backend.spawned_by_us:
                    # (2026-10-25) ATTACHED local node we did NOT spawn (admin launcher /
                    # manual): same rule as remote - the brain never kills it. Mark unhealthy;
                    # an admin restarts it via utils/start_llama_node/ZZZ_LLAMA_NODE.bat.
                    _backend.mark_unhealthy("stream stalled; attached node is not auto-restarted")
                    logger.error(
                        "[STREAM-STALL] %s is an ATTACHED local node (not spawned by the SERVER) "
                        "- not killing/restarting it from the brain.", _backend.label,
                    )
                else:
                    from .server import (
                        _kill_orphaned_process,
                        _kill_llama_server_by_port,
                        wait_for_port_free_async,
                        start_server as _restart,
                    )
                    from config import LLAMA_SERVER_PORT

                    _port = _backend.instance.port if _backend is not None else LLAMA_SERVER_PORT
                    _host = _backend.instance.host if _backend is not None else None
                    _conc = ((_backend.configured_capacity or _backend.capacity)
                             if _backend is not None else 1)
                    _key = _backend.key if _backend is not None else None
                    logger.error("[STREAM-STALL] Auto-restart enabled - restarting local node on "
                                 "port %d...", _port)
                    try:
                        # (2026-09 threadless fix) await on THIS loop; only OS leaves
                        # (terminate/wait, netstat/taskkill) run via asyncio.to_thread.
                        _lock = _backend.restart_lock if _backend is not None else None
                        if _lock is not None:
                            await _lock.acquire()
                        try:
                            await asyncio.to_thread(_kill_orphaned_process, _key)
                            await asyncio.to_thread(_kill_llama_server_by_port, _port)
                            if await wait_for_port_free_async(_port):
                                restarted = await _restart(
                                    api_url, host=_host, port=_port, concurrency=_conc,
                                )
                        finally:
                            if _lock is not None:
                                _lock.release()
                    except Exception as restart_err:
                        logger.error(f"[STREAM-STALL] Auto-restart failed: {restart_err}")
            raise RuntimeError(
                f"llama.cpp stream stalled: no SSE chunk for {timeout_sec}s after {chunks} "
                f"chunk(s). Server auto-restarted={restarted}"
            )

        elapsed = time.time() - t0
        if chunks == 0:
            logger.info(f"[STREAM] First SSE chunk received after {elapsed:.2f}s (conv={conv_id})")
        elif elapsed > 15:
            logger.warning(f"[STREAM] Slow gap between chunks: {elapsed:.1f}s (chunk #{chunks}, conv={conv_id})")
        chunks += 1

        try:
            yield item
        except GeneratorExit:
            # Consumer stopped reading (stop_event / break) - close quietly
            return


async def chat_stream(
    api_url: str,
    timeout: int,
    model: str,
    messages: List[Dict],
    websocket: Any,
    temperature: float = 0.7,
    enable_thinking: Any = False,
    conv_id: Optional[str] = None,
    tools: Optional[list] = None,
    stop_event: Optional[asyncio.Event] = None,
    context_window: Optional[int] = None,
    headers: Optional[dict] = None,
    provider_name: str = "llama",
    thinking_mode: Optional[str] = None,
) -> Tuple[str, TokenStats]:
    """Stream chat completions from llama.cpp via SSE, returning (response_str, TokenStats)
    with the real counts from the final response chunk (one TokenStats per call).

    thinking_mode: canonical level for this request - "none"/"low"/"medium"/"high",
    translated by the serving model's ThinkingPolicy into chat_template_kwargs. None/"" sends
    no thinking keys and leaves the display decision to enable_thinking.
    enable_thinking: bool (show reasoning) or the same level string; see _resolve_thinking.
    """

    window = int(context_window or CONTEXT_WINDOW_TOKENS)

    # ---- THINKING: what the model is told vs what the UI shows ----
    # The policy comes from the backend that will answer, so the kwargs use the model's own
    # vocabulary; `think_display` decides whether reasoning_content reaches the UI.
    _thinking_mode, thinking_kwargs, think_display = _resolve_thinking(
        api_url, model, thinking_mode, enable_thinking)

    # Convert messages to OpenAI-compatible multimodal format
    messages = convert_messages_for_vision(messages)

    logger.info(f"Sending {len(messages)} message(s) to llama.cpp")

    # Log if images are present
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            image_count = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "image_url")
            if image_count:
                logger.info(f"Message contains {image_count} image(s) for vision processing")

    full_response_parts = []
    content_buffer = ""
    think_buffer = ""
    message_counter = 0
    last_send_time = asyncio.get_running_loop().time()

    # Accumulator for tool_calls deltas (llama.cpp streams tool calls as deltas)
    accumulated_tool_calls = []

    # Variables to capture from the final SSE chunk
    prompt_tokens: int = 0
    completion_tokens: int = 0
    eval_duration_seconds: float = 0.0
    finish_reason: Optional[str] = None   # "stop" | "length" - set by llama.cpp on the last chunk

    # Track if generation was stopped by user
    was_stopped = False

    async def flush_content():
        """Flush the content buffer to the WebSocket client."""
        nonlocal content_buffer, message_counter, last_send_time
        if content_buffer and websocket:
            try:
                await websocket.send_text(json.dumps({
                    "type": "content",
                    "content": content_buffer
                }))
                content_buffer = ""
                message_counter = 0
                last_send_time = asyncio.get_running_loop().time()
            except Exception as e:
                logger.warning(f"Failed to send content buffer: {e}")
                raise

    async def flush_thinking():
        """Flush the thinking buffer to the WebSocket client."""
        nonlocal think_buffer
        if think_buffer and websocket:
            try:
                await websocket.send_text(json.dumps({
                    "type": "thinking",
                    "content": think_buffer
                }))
                think_buffer = ""
            except Exception as e:
                logger.warning(f"Failed to send thinking buffer: {e}")
                raise

    # ---- SAFE MAX-TOKENS (context-overflow guard) ------------------------
    # Requesting max_tokens == full context window is what caused the b10441
    # "Failed to parse tool call arguments as JSON" 500 errors: when the prompt
    # nearly fills the window, llama.cpp truncates generation mid-way through a
    # native tool-call JSON and then fails to parse its own output.
    # FIXED (2026-08): count tool_calls JSON in the prompt estimate. The old
    # content-only sum counted every assistant tool-call message (content="") as
    # 0 tokens, so this CONTEXT-GUARD clamp was blind to the fastest-growing part
    # of agentic history and could leave llama.cpp with no room for output.
    def _msg_chars(m: dict) -> int:
        # TRUNCATION-LOOP FIX (2026-09-10): the old version did len(str(content)),
        # which on a vision message counted RAW BASE64 IMAGE BLOBS as text (~330K
        # chars per 500KB image). That inflated est_prompt_tokens into the millions,
        # drove the clamp below to its floor (exactly 1024 tokens of output room),
        # cut every answer mid-sentence and made the ReAct loop re-ask forever.
        # Now: text parts count at chars/4 like before; each image is charged a
        # flat VISION estimate (llama.cpp's CLIP encoder turns a ~1600px JPEG into
        # roughly 1-2K tokens, independent of the base64 size).
        content = m.get("content", "")
        if isinstance(content, list):
            chars = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    chars += len(str(part.get("text", "")))
                elif ptype == "image_url":
                    chars += _VISION_TOKENS_PER_IMAGE * 4   # flat per-image estimate
            # Ollama-style 'images' key (pre-conversion safety net) - same flat charge.
            imgs = m.get("images")
            if isinstance(imgs, list):
                chars += len(imgs) * _VISION_TOKENS_PER_IMAGE * 4
        else:
            chars = len(str(content))
        tcs = m.get("tool_calls")
        if tcs:
            try:
                chars += len(json.dumps(tcs))
            except (TypeError, ValueError):
                chars += len(str(tcs))
        return chars

    _VISION_TOKENS_PER_IMAGE = 1500   # flat estimate per attached image (see _msg_chars)
    est_prompt_tokens = sum(_msg_chars(m) for m in messages if isinstance(m, dict)) // 4
    if tools:
        try:
            est_prompt_tokens += len(json.dumps(tools)) // 4
        except Exception:
            pass
    # Reasoning tokens are billed against max_tokens too, and they are consumed first: a
    # small budget can be spent entirely on thinking, leaving an empty answer.
    _THINK_RESERVE_TOKENS = {"low": 512, "medium": 1536, "high": 4096}
    # When the model accepts a thinking_budget, the cap IS the reserve: the same number we
    # send is what the context guard plans for. Otherwise the estimate above stands.
    _policy = _thinking_policy_for(api_url, model)
    _budget = _policy.budget_for(_thinking_mode) if _thinking_mode else 0
    if _budget and _policy.budget:
        _THINK_RESERVE_TOKENS = dict(_THINK_RESERVE_TOKENS)
        _THINK_RESERVE_TOKENS[_thinking_mode] = _budget
    _reserve = _THINK_RESERVE_TOKENS.get(_thinking_mode, 0) if think_display else 0
    _room = max(0, window - est_prompt_tokens - 512)
    # max_tokens is a ceiling, so it is never raised above the room the window actually has.
    # The reserve only says how much of that room reasoning is likely to take.
    safe_max_tokens = min(
        LLAMA_SERVER_MAX_NEW_TOKENS,
        # Floor is now configurable (was hardcoded 1024 - too small for a full
        # multi-image answer + the TASK DONE marker; see plan_20260910_1835.md).
        max(LLAMA_MIN_SAFE_MAX_TOKENS, _room),
    )
    if _reserve and _room < LLAMA_MIN_SAFE_MAX_TOKENS + _reserve:
        logger.warning(
            f"[CONTEXT-GUARD] Thinking is on ({_thinking_mode}, ~{_reserve} reasoning tokens) "
            f"but only {_room} tokens of output room remain - reasoning may consume most of "
            f"the answer. Prompt ~{est_prompt_tokens}/{window}."
        )
    if safe_max_tokens < LLAMA_SERVER_MAX_NEW_TOKENS:
        logger.warning(
            f"[CONTEXT-GUARD] Prompt ~{est_prompt_tokens} tokens leaves only "
            f"{max(0, window - est_prompt_tokens)} free of {window}. "
            f"Clamping max_tokens to {safe_max_tokens} (default {LLAMA_SERVER_MAX_NEW_TOKENS})."
        )
    if safe_max_tokens <= LLAMA_MIN_SAFE_MAX_TOKENS:
        # The prompt alone nearly fills the window - answers WILL be short. Say so
        # loudly: this is exactly the condition that caused the 2026-09-10 truncation loop.
        logger.warning(
            f"[CONTEXT-GUARD] *** OUTPUT ROOM AT FLOOR ({safe_max_tokens} tokens) *** "
            f"prompt ~{est_prompt_tokens}/{window}. Long answers or multi-part "
            f"responses will be truncated - consider trimming history / downscaling images."
        )

    llama_payload = build_llama_payload(
        model=model,
        messages=messages,
        stream=True,
        temperature=temperature,
        tools=tools,
        max_tokens=safe_max_tokens,
        thinking_kwargs=thinking_kwargs,
    )

    if tools:
        logger.info(f"Added {len(tools)} native tools to llama.cpp request")

    # ============================================================
    # PRE-FLIGHT HEALTH CHECK + AUTO-RESTART
    # (moved BEFORE auto-switch: the switch check below queries /v1/models,
    #  so the server must be known-up first — otherwise a downed server would
    #  silently skip the switch and request a model that is not loaded)
    # Uses shared HTTP client pool instead of creating new client per request
    # ============================================================
    # A rejected thinking_budget is retried once with the effort words only, so this
    # whole block runs at most twice per request.
    for _attempt in range(2):
        try:
            http_client = await get_async_client()
            health_resp = await http_client.get(f"{api_url}/health", timeout=3.0, headers=headers)
            if health_resp.status_code != 200:
                raise ConnectionError("Health check failed")
        except Exception:
            logger.warning(f"[LLAMA] Server not responding - checking whether it can be restarted...")
            try:
                from .server import start_server as _start_server
                from .backend_registry import get_registry

                # (2026-10 fleet) Only a LOCAL node may be spawned by the brain. A remote
                # node is owned by its launcher: spawning "api_url" here would silently start
                # a rogue local llama on port 5000. Resolve the backend and pass its real
                # host/port/concurrency instead of the legacy defaults.
                _reg = get_registry()
                _backend = _reg.get_for_api_url(api_url) if _reg is not None else None
                if _backend is not None and not _backend.spawnable:
                    # (2026-11 strata) Attach-only backends (remote llama nodes AND every Strata
                    # endpoint) are never spawned by the brain: mark them unhealthy and leave the
                    # fix to the health poller / the operator who owns the process.
                    _backend.mark_unhealthy(
                        "backend not responding (attach-only: the brain does not spawn %s backends)"
                        % _backend.kind
                    )
                    logger.error(
                        "[LLAMA] Backend %s is %s - the brain never spawns it. %s "
                        "Leaving it to the health poller.",
                        _backend.label,
                        "STRATA" if _backend.kind == "strata" else "REMOTE",
                        "Start/restart Strata and check its API key (config/strata_servers.json)."
                        if _backend.kind == "strata" else "Check the node machine (llama-node log).",
                    )
                elif _backend is not None:
                    # Serialize restarts between slots of the same local node.
                    async with _backend.restart_lock:
                        restarted = await _start_server(
                            api_url,
                            host=_backend.instance.host,
                            port=_backend.instance.port,
                            concurrency=_backend.configured_capacity or _backend.capacity,
                        )
                    if restarted:
                        logger.info(f"[LLAMA] Auto-restart successful, proceeding with request")
                    else:
                        _backend.mark_unhealthy("local backend restart failed")
                        logger.error(f"[LLAMA] Auto-restart failed, will attempt request anyway (may fail)")
                else:  # legacy single-instance path (no registry yet)
                    await _start_server(api_url)
            except Exception as restart_err:
                logger.error(f"[LLAMA] Auto-restart error: {restart_err}")

        # ============================================================
        # MODEL GUARD (2026-10 fleet): NO auto-switch.
        #
        # Every llama node is started with a FIXED model by its node launcher, and the
        # server queue routes each request to the model group it names. A mismatch here
        # therefore means the node was restarted with a different model than the registry
        # (and the client) believe. We must NEVER silently answer with the wrong model -
        # refuse with a clear, retryable error instead of reloading anything.
        # ============================================================
        if not _is_mmproj_file(model):
            try:
                req_norm = normalize_model_name(model)
                _ms_key = model_state.key_for_api_url(api_url)
                cached_loaded, cache_fresh = model_state.get_cached_model(
                    LLAMA_MODEL_STATE_TTL_SEC, key=_ms_key
                )
                if cache_fresh and cached_loaded and normalize_model_name(cached_loaded) != req_norm:
                    msg = (f"Backend {api_url} is serving '{cached_loaded}', not '{model}'. "
                           f"Model routing mismatch - start/restart that node with '{model}'.")
                    logger.error("[MODEL-GUARD] %s", msg)
                    raise RuntimeError(msg)
            except RuntimeError:
                raise
            except Exception as guard_err:
                logger.debug("[MODEL-GUARD] check skipped: %s", guard_err)
        # ============================================================

        try:
            logger.info(f"Calling llama.cpp model with STREAMING: {model}")
            start_time = time.time()

            # Use shared HTTP client pool — no new client created per request
            http_client = await get_async_client()

            async with http_client.stream(
                "POST",
                f"{api_url}/v1/chat/completions",
                json=llama_payload,
                # (2026-11 strata) Bearer auth headers when the backend needs them (Strata);
                # None on the llama path, which stays byte-for-byte the old request.
                headers=headers,
                # 2026-10 fleet: `timeout` is the long GENERATION budget (9300 s). Using it as
                # the connect timeout too means a blackholed remote node holds a slot for
                # hours. Keep generation unbounded-ish but fail an unreachable node in 10 s.
                timeout=httpx.Timeout(timeout, connect=10.0, read=timeout,
                                      write=timeout, pool=10.0),
            ) as response:
                if response.status_code == 401:
                    # (2026-11 strata) Only Strata authenticates: a 401 means the key is missing
                    # or wrong. Actionable message, no retry storm (the worker treats it as fatal).
                    raise RuntimeError(
                        f"Backend {api_url} rejected our credentials (HTTP 401). "
                        "Set the Strata Bearer key in config/strata_servers.json ('api_key') "
                        "or config.STRATA_API_KEY (env STRATA_API_KEY also works)."
                    )
                if response.status_code == 404:
                    raise FileNotFoundError(f"Model '{model}' not found in llama.cpp")
                elif response.status_code == 500:
                    error_detail = await response.aread()
                    err_text = error_detail.decode("utf-8", errors="replace")
                    # (2026-11 strata resilience) The engine can surface an OOM/restart as a
                    # 500 too (not just 503). Detect it FIRST so a recoverable outage is never
                    # mistaken for a vision/tool-JSON/thinking problem.
                    if _is_backend_unavailable(response.status_code, err_text):
                        low_err = err_text.lower()
                        logger.warning(
                            f"[BACKEND-DOWN] HTTP 500 from {api_url} looks like an engine "
                            f"restart/OOM: {err_text[:200]}"
                        )
                        raise BackendUnavailableError(
                            f"Backend {api_url} is unavailable (engine restarting or out of "
                            f"memory): {err_text.strip()[:300]}",
                            model=model,
                            oom=("mib free" in low_err or "out of memory" in low_err),
                            reason=err_text,
                        )
                    # (2026-08-23) VISION CHECK FIRST: llama-server started without a multimodal
                    # projector (mmproj) rejects ANY image input with HTTP 500 + the body
                    # "image input is not supported - hint: ... provide the mmproj". Detect it and
                    # raise a typed, human-friendly error so logs/UI never see raw JSON. The CLIENT
                    # recognizes this exception type (via the WS error_type tag) and shows a clean
                    # "Vision Not Available" card instead of retrying 60 times for nothing.
                    if _looks_like_mmproj_error(err_text):
                        logger.error(
                            f"[VISION] llama.cpp rejected image input: no multimodal projector (mmproj) "
                            f"is loaded for model '{model}'. Raw body: {err_text[:300]}"
                        )
                        raise make_vision_error(model) from None
                    if "parse tool call arguments" in err_text.lower():
                        # The model's own tool-call JSON was cut off before it could be parsed.
                        # Usual causes (in order of likelihood): max_tokens hit mid-JSON,
                        # or a prompt so large that output room ran out. est_prompt_tokens
                        # now includes tool_calls args, so [CONTEXT-GUARD] logs are trustworthy.
                        logger.error(
                            f"[TOOL-JSON-PARSE] llama.cpp could not parse its own (truncated) tool-call JSON. "
                            f"Prompt ~{est_prompt_tokens} est tokens of the {window}-token window; "
                            f"check [CONTEXT-GUARD]/[SAFE_TRIM] logs on the client side."
                        )
                        logger.error(f"[TOOL-JSON-PARSE] Raw llama.cpp error body: {err_text[:2000]}")
                        # Propagate a SHORT actionable message only. The raw body can be ~10KB of
                        # truncated JSON; sending it to the client would poison logs and history.
                        raise RuntimeError(
                            "llama.cpp truncated its own tool-call JSON (context exhausted mid-output). "
                            f"Prompt ~{est_prompt_tokens} tokens left only {max(0, window - est_prompt_tokens)} of {window}. "
                            "Re-issue with smaller payloads (split large code into multiple calls)."
                        )
                    logger.error(f"llama.cpp HTTP 500 body: {err_text[:2000]}")
                    # A jinja raise_exception in the chat template surfaces as HTTP 500. Demote
                    # only when the body names a thinking/effort term - a 500 from vision or
                    # tool-JSON problems must not disable thinking.
                    low_err = err_text.lower()
                    if any(t in low_err for t in ("reasoning_effort", "enable_thinking",
                                                  "thinking_budget", "reasoning effort",
                                                  "chat_template_kwargs")):
                        # A budget-only rejection must not drop the effort words: retry once
                        # without the cap so the level still influences thinking.
                        if "thinking_budget" in low_err and "thinking_budget" in thinking_kwargs:
                            thinking_kwargs = {k: v for k, v in thinking_kwargs.items()
                                               if k != "thinking_budget"}
                            llama_payload["chat_template_kwargs"] = dict(thinking_kwargs)
                            logger.info(
                                "[THINKING] %s/%s rejected thinking_budget - retrying with "
                                "effort words only (no token cap).", api_url, model or "?")
                            continue
                        _demote_thinking(api_url, model,
                                         f"HTTP 500: {err_text.strip()[:160]}", thinking_kwargs)
                    raise RuntimeError(f"llama.cpp server error: {err_text.strip()[:300]}")
                elif response.status_code == 400:
                        # Read the actual error body from llama.cpp for debugging
                        error_body = (await response.aread()).decode("utf-8", errors="replace")
                        logger.error(f"llama.cpp HTTP 400 Bad Request - body: {error_body[:500]}")
                        # (2026-11 context window) A prompt that exceeds the engine's window
                        # is rejected UP FRONT as HTTP 400. It is a distinct condition from a
                        # malformed request or a thinking-key rejection, and the ONLY correct
                        # response is to send less context: raise the typed error so the CLIENT
                        # can trim history and re-send instead of surfacing a bare error.
                        if _is_context_window_exceeded(response.status_code, error_body):
                            prompt_tokens, window = _parse_context_window_400(error_body)
                            raise ContextWindowExceededError(
                                f"request exceeds the context window: prompt "
                                f"({prompt_tokens or '?'} tokens) > {window or '?'}; "
                                f"requests are never truncated",
                                model=model, prompt_tokens=prompt_tokens,
                                context_window=window, reason=error_body,
                            )
                        # If we sent thinking keys and the endpoint rejected the request, its
                        # advertised knob is not usable: demote so the next request sends
                        # nothing instead of failing the same way every turn.
                        _demote_thinking(api_url, model,
                                         f"HTTP 400: {error_body.strip()[:160]}", thinking_kwargs)
                        raise RuntimeError(f"llama.cpp returned HTTP 400: {error_body.strip()[:300]}")
                elif response.status_code != 200:
                    error_text = await response.aread()
                    # Same contract as the 500/400 branches above: the body carries the
                    # actual llama.cpp reason, so a bare status code would hide it. Keep
                    # the message short - the raw body can be large.
                    body = error_text.decode("utf-8", errors="replace").strip()
                    # (2026-11 strata resilience) 502/503/504 (and OOM/restart markers) are a
                    # DOWN engine, not a bad request: raise the typed, recoverable error so the
                    # CLIENT preserves the call and resumes / offers a model switch.
                    if _is_backend_unavailable(response.status_code, body):
                        low_body = body.lower()
                        logger.warning(
                            f"[BACKEND-DOWN] HTTP {response.status_code} from {api_url}: "
                            f"engine down/restarting - {body[:200]}"
                        )
                        raise BackendUnavailableError(
                            f"Backend {api_url} is unavailable (HTTP {response.status_code}): {body[:300]}",
                            model=model,
                            oom=("mib free" in low_body or "out of memory" in low_body),
                            reason=body,
                        )
                    # A strict server may reject unknown body fields with a 4xx instead of
                    # ignoring them. 400/422 are the "bad request" shapes proving this node
                    # cannot take our keys; 429/413 are load/size problems and must not
                    # disable thinking.
                    if _is_context_window_exceeded(response.status_code, body):
                        prompt_tokens, window = _parse_context_window_400(body)
                        raise ContextWindowExceededError(
                            f"request exceeds the context window: prompt "
                            f"({prompt_tokens or '?'} tokens) > {window or '?'}; "
                            f"requests are never truncated",
                            model=model, prompt_tokens=prompt_tokens,
                            context_window=window, reason=body,
                        )
                    if response.status_code in (400, 422):
                        _demote_thinking(api_url, model,
                                         f"HTTP {response.status_code}: {body[:160]}",
                                         thinking_kwargs)
                    raise RuntimeError(
                        f"llama.cpp returned HTTP {response.status_code}: {body[:300]}"
                    )

                # Effective stall timeout scales with prompt size so a legitimate
                # long prefill of a near-full context is not mistaken for a freeze.
                effective_stall = max(
                    LLAMA_STREAM_STALL_TIMEOUT_SEC,
                    min(600, est_prompt_tokens // 400),
                )
                logger.info(f"[STREAM] Stall watchdog armed: {effective_stall}s (est_prompt={est_prompt_tokens} tokens)")

                async for line in _iter_sse_with_stall(response.aiter_lines(), effective_stall, conv_id or "", api_url):
                    # ---- STOP CHECK: on every SSE chunk ----
                    if stop_event and stop_event.is_set():
                        was_stopped = True
                        logger.info(f"Stop requested for conversation {conv_id}, ending generation")
                        await flush_content()
                        await flush_thinking()
                        break

                    if not line.strip():
                        continue

                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)

                        # ============================================================
                        # CRITICAL FIX: Capture token stats from the 'usage' field
                        # Present in final chunks of llama.cpp responses
                        # ============================================================
                        usage = data.get("usage", {})
                        if usage:
                            if "prompt_tokens" in usage:
                                prompt_tokens = usage.get("prompt_tokens", 0)
                            if "completion_tokens" in usage:
                                completion_tokens = usage.get("completion_tokens", 0)
                            # eval_duration in nanoseconds - convert to seconds
                            eval_dur_ns = usage.get("eval_duration", 0)
                            if eval_dur_ns:
                                eval_duration_seconds = eval_dur_ns / 1_000_000_000

                            logger.info(
                                f"[Llama.cpp] Token stats - prompt: {prompt_tokens}, "
                                f"completion: {completion_tokens}, "
                                f"duration: {eval_duration_seconds:.2f}s"
                            )

                        choices = data.get("choices", [])
                        if choices:
                            # TRUNCATION FIX (2026-09-10): llama.cpp reports finish_reason on the final chunk
                            # ("stop" = natural end, "length" = hit max_tokens). The old code dropped it
                            # entirely, so a cut-off answer looked like a complete one and the ReAct loop
                            # re-asked forever. Capture every value seen; the last non-null wins.
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = fr
                            delta = choices[0].get("delta", {})

                            # Tool calls - accumulate deltas
                            if "tool_calls" in delta and delta["tool_calls"]:
                                for tc_delta in delta["tool_calls"]:
                                    tc_index = tc_delta.get("index", 0)
                                    while len(accumulated_tool_calls) <= tc_index:
                                        accumulated_tool_calls.append({
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""}
                                        })
                                    acc_tc = accumulated_tool_calls[tc_index]
                                    if tc_delta.get("id"):
                                        acc_tc["id"] = tc_delta["id"]
                                    if tc_delta.get("type"):
                                        acc_tc["type"] = tc_delta["type"]
                                    func = tc_delta.get("function", {})
                                    if func.get("name"):
                                        acc_tc["function"]["name"] = func["name"]
                                    if func.get("arguments"):
                                        acc_tc["function"]["arguments"] += func["arguments"]

                            # ---- Thinking / Reasoning content extraction ----
                            # think_display gates it, not enable_thinking: a chosen thinking
                            # level shows reasoning even when the flag is off.
                            think_chunk = None
                            if think_display:
                                think_chunk = delta.get("reasoning_content", "")

                            if think_chunk:
                                think_buffer += think_chunk
                                await flush_thinking()

                            # Content chunk
                            chunk = delta.get("content", "")
                            if chunk:
                                full_response_parts.append(chunk)
                                content_buffer += chunk
                                message_counter += 1

                                current_time = asyncio.get_running_loop().time()
                                should_flush = (
                                    len(content_buffer) >= STREAM_BUFFER_SIZE_CHARS or
                                    message_counter >= STREAM_BUFFER_SIZE_MSGS or
                                    (current_time - last_send_time) >= STREAM_BUFFER_TIMEOUT_SEC
                                )

                                if should_flush:
                                    await flush_content()

                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse SSE line: {line[:100]}")
                        continue

                # Final flush (skip if stopped - already flushed on break)
                if not was_stopped:
                    await flush_content()
                    await flush_thinking()

                # Return accumulated tool calls if any
                if accumulated_tool_calls:
                    logger.info(f"Received {len(accumulated_tool_calls)} native tool call(s)")

                full_response = ''.join(full_response_parts)
                total_duration = time.time() - start_time

                # ============================================================
                # Calculate generation speed from REAL llama.cpp data
                # ============================================================
                gen_speed = None
                if eval_duration_seconds > 0 and completion_tokens > 0:
                    gen_speed = round(completion_tokens / eval_duration_seconds, 1)
                    logger.info(
                        f"Generation speed: {gen_speed} tok/s "
                        f"({completion_tokens} tokens in {eval_duration_seconds:.2f}s)"
                    )
                elif total_duration > 0 and completion_tokens > 0:
                    # Fallback using total request time
                    gen_speed = round(completion_tokens / total_duration, 1)
                    logger.info(f"Fallback speed estimate: {gen_speed} tok/s (using total duration)")

                # If we never saw token stats, log a warning
                if prompt_tokens == 0 and completion_tokens == 0:
                    logger.warning(
                        f"No token statistics received from llama.cpp! "
                        f"Response length: {len(full_response)} chars"
                    )
                    # Try to estimate tokens roughly (fallback)
                    prompt_tokens = max(1, sum(len(str(m.get("content", ""))) // 4 for m in messages))
                    completion_tokens = max(1, len(full_response) // 4)

                # TRUNCATION FIX (2026-09-10): expose finish_reason to the client. "length" with no
                # tool call means the answer was CUT OFF at max_tokens - the ReAct loop must treat it
                # differently from a normal thinking step (resume/accept) instead of re-asking forever.
                was_truncated = bool(finish_reason == "length" and not accumulated_tool_calls)
                if finish_reason:
                    logger.info(
                        f"[Llama.cpp] finish_reason={finish_reason!r} (truncated={was_truncated}, "
                        f"completion_tokens={completion_tokens})"
                    )
                if was_truncated:
                    logger.warning(
                        f"[TRUNCATED] Generation hit the output limit ({safe_max_tokens} tokens) before finishing. "
                        f"The client will resume or accept this partial answer instead of looping."
                    )

                # Build the TokenStats with REAL data from llama.cpp
                stats = TokenStats(
                    prompt_tokens=prompt_tokens,
                    generated_tokens=completion_tokens,
                    truncated=was_truncated,
                    generation_speed=gen_speed,
                    total_duration=round(total_duration, 2),
                    provider_name=provider_name,
                    model=model,
                    context_window=window,
                )

                logger.info(
                    f"[Llama.cpp] Final TokenStats: prompt={stats.prompt_tokens}, "
                    f"gen={stats.generated_tokens}, speed={stats.generation_speed}, "
                    f"remaining={stats.tokens_remaining}"
                )

                if accumulated_tool_calls:
                    return json.dumps({
                        "type": "tool_calls",
                        "tool_calls": accumulated_tool_calls
                    }), stats

                return full_response, stats

        except httpx.ConnectError as e:
            logger.error(f"Cannot connect to llama.cpp: {e}")
            raise ConnectionError("llama.cpp service is not running. Please start llama-server.")
        except httpx.TimeoutException as e:
            logger.error(f"llama.cpp timeout after {timeout}s: {e}")
            raise TimeoutError(f"llama.cpp request timed out after {timeout} seconds.")
        except httpx.HTTPError as e:
            # Expected user-facing condition: llama-server DIED MID-STREAM (crash, OOM kill,
            # manual restart) after the response had already started. ConnectError and
            # TimeoutException are subclasses of HTTPError so they keep their own handlers
            # above; what lands here is ReadError / RemoteProtocolError & co. Log ONE clean
            # line (no traceback) and re-raise as ConnectionError with an actionable message,
            # so chat_relay's expected-condition guard handles it the same way as a downed server.
            logger.warning(
                f"[LLAMA] llama.cpp connection dropped mid-stream for model '{model}' "
                f"({type(e).__name__}: {e}). The backend likely crashed or was restarted - retrying will re-route to a healthy node."
            )
            raise ConnectionError(
                "llama.cpp connection dropped mid-response (backend died or was restarted). Please try again."
            ) from None
        except VisionNotAvailableError as e:
            # Expected user-facing condition (image sent to a model without mmproj), NOT a
            # server fault — log one clean line, no traceback. The typed error still propagates
            # so chat_relay can tag the WS frame and show the "Vision Not Available" card.
            logger.warning(f"[VISION] Image input rejected for model '{model}': {e}")
            raise
        except BackendUnavailableError:
            # Expected recoverable condition (engine down/restarting) - already logged where
            # it was raised; propagate untouched so chat_relay tags the frame.
            raise
        except ContextWindowExceededError:
            # Expected condition (prompt exceeds the window) - already logged where it was
            # raised; propagate untouched so chat_relay tags the frame as a context-window
            # rejection rather than a generic "server error".
            raise
        except Exception as e:
            logger.error(f"llama.cpp stream failed: {e}")
            raise
