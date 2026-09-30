"""
COOLEMS SERVER configuration — SINGLE SOURCE OF TRUTH for all server-side values.

Conventions:
  - All values are set directly here (no env-var overrides in this file).
  - WEB_RELAY_* connection constants are ALSO mirrored in
    web_server_relay/index.php (single-file pure-PHP relay) because it is a standalone
    deploy that cannot import from this package at runtime. Keep them identical.
  - Values consumed by CLIENT-side tools (shipped over WebSocket) MUST be listed
    in app/providers/coolems/tool_scanner.extract_config_constants().NEEDED_CONSTANTS
    so the SERVER value is delivered and becomes authoritative on the client.
"""

import json as _json
import logging as _logging
import os
from dataclasses import dataclass as _dataclass
from typing import Optional


# --- Context / Token Limits ---
CONTEXT_WINDOW_TOKENS: int = 184800             # Total token budget for model context def:131072
# MUST equal the --ctx-size the deployed llama-server actually runs with (it is started from this
# value by command_builder.py). Raising it here without a matching restart + VRAM check silently
# desyncs client trim math / auth_ok context_window from reality - that drift caused the 2026-08-16
# truncated tool-call crash (see plan_20260817_trace_fix.md).

# --- Provider / Model ---
PROVIDER: str = "coolems_server"                        # Relay mode for server
SERVER_BACKEND: str = "llama"                           # Local LLM engine (llama.cpp)

# --- Coolems Distributed Provider Configuration ---
COOLEMS_SERVER_HOST: str = "0.0.0.0"                    # Host for coolems_server listener (0.0.0.0 = all interfaces including localhost)

# --- Web Relay / Direct WS Connection Constants (shared with CLIENT + relay) ---
# NOTE: the WEB_RELAY_* / WS_HANDSHAKE_TIMEOUT values below must stay in sync with
# web_server_relay/index.php (single-file pure-PHP relay — standalone deploy).
# PROTOCOL VERSION (2026-08-20): version of the COOLEMS WebSocket JSON protocol.
# Carried in every auth frame ("protocol_version") and echoed back in auth_ok.
# Both ends MUST agree; a mismatch is rejected with an actionable error instead of
# silent misbehavior. Bump when adding/removing message types or changing field semantics.
# (2026-09-08) v3: additive optional frame types queue_status / queue_start (SERVER -> CLIENT,
# multi-chat request queue). Old clients simply ignore unknown frame types; both ends must
# still agree on the version number itself (parity enforced by tests/test_protocol_sync_20260820.py).
# (2026-09-23) v4: relays forward each client's API key + email in 'client_connected' so the BRAIN
# authenticates every relayed client against its OWN config/.api_keys.json (same logic as direct
# mode - a client never sends a role). auth_ok frames echo protocol_version on all paths.
PROTOCOL_VERSION: int = 4

USE_WEB_SERVER: Optional[str] = None        # Web relay address "host:port" (None = direct WS mode on LAN)
WEB_RELAY_MAX_MESSAGE_SIZE: int = 1048576 * 10          # 10 MB max WebSocket message size
WEB_RELAY_HEARTBEAT_INTERVAL: int = 30        # Ping interval (seconds) for keep-alive
WEB_RELAY_HEARTBEAT_TIMEOUT: int = 10         # Timeout (s) before a dead peer is dropped
WS_HANDSHAKE_TIMEOUT: int = 10               # Timeout (s) for the WS auth handshake (brain + client sides)
WEB_RELAY_DEFAULT_PORT: int = 8443           # Default relay port when USE_WEB_SERVER has no explicit port

# (2026-09-23) The brain->relay link is internet-facing: in relay mode the transport layer
# FAILS CLOSED unless either full CA verification (True - Let's Encrypt etc.) or a usable
# cert pin below is configured. No silent CERT_NONE fallback for relay connections anymore.
WEB_RELAY_VERIFY_SSL_CERTS: bool = False      # Full CA verification of the relay TLS cert (Let's Encrypt). Keep True in production; leave False only when WEB_RELAY_CERT_PIN_FINGERPRINT is set - otherwise relay connections are refused (fail-closed)

# (2026-09-01 S3) Certificate PINNING for the brain->relay link. Stronger than full CA
# verification for a self-signed relay cert: TLS still encrypts, but trust is an EXACT
# SHA-256 fingerprint of the relay's certificate -- any other cert (even CA-signed) fails
# the handshake before a byte of auth data flows. Leave empty to keep the legacy
# WEB_RELAY_VERIFY_SSL_CERTS behavior. Accepted formats: "sha256/<base64-of-DER>" or a
# path to the relay's .pem/.crt (fingerprinted at startup). To obtain it once from a
# trusted network:  openssl s_client -connect <relay>:<port> </dev/null | openssl x509 -outform DER | sha256sum
WEB_RELAY_CERT_PIN_FINGERPRINT: str = ""
COOLEMS_RECONNECT_MIN_DELAY: float = 1.0      # Min delay between reconnect attempts (seconds)
COOLEMS_RECONNECT_MAX_DELAY: float = 30.0     # Max delay between reconnect attempts (seconds)

# --- Direct WebSocket Mode (PC-to-PC encrypted, no relay server needed) ---
COOLEMS_DIRECT_WS_PORT: int = 8080               # Port for direct WebSocket server (TLS always ON)
COOLEMS_DIRECT_WS_CERT_PATH: str = ""            # Path to SSL cert file (leave empty for auto-generated self-signed)
COOLEMS_DIRECT_WS_KEY_PATH: str = ""             # Path to SSL key file (leave empty for auto-generated self-signed)
LLAMA_URL: str = "http://localhost:5000"        # llama.cpp server base URL
# The loaded model is NO LONGER hardcoded here (the old value was a dead Ollama-style
# reference that matched nothing on disk). Source of truth for the active model:
# config/.last_model_loaded.json - written by app/providers/llama/model_persistence.py
# after every verified launch/switch; read back on startup (see server.start_server()).
MODEL_NAME: str = ""  # empty = "load whatever .last_model_loaded.json says"

# --- Chrome CDP (Browser Debugging) ---
CHROME_CDP_PORT: int = 9222                           # Chrome DevTools Protocol debugging port

# --- Provider Defaults ---
PROVIDER_DEFAULT_TIMEOUT: int = 9300                    # HTTP timeout (s) for provider API calls (single source of truth)
PROFILE_DEFAULT_RATE_LIMIT: int = 30                    # Fallback msg/sec when a profile in profiles.json has no max_rate_limit

# --- Streaming Buffer ---
STREAM_BUFFER_SIZE_CHARS: int = 350                     # Max chars before flushing response buffer
STREAM_BUFFER_SIZE_MSGS: int = 25                       # Max chunks before flushing buffer
STREAM_BUFFER_TIMEOUT_SEC: float = 0.15                 # Max hold time (s) before flushing buffer

# --- Provider Health ---
HEALTH_CHECK_TIMEOUT: int = 5                           # HTTP timeout (s) for health checks
HEALTH_CHECK_INTERVAL_SEC: int = 30                     # Seconds between health re-evaluations
PROVIDER_STARTUP_TIMEOUT: int = 260                      # Max health-check attempts on startup

# --- Llama Server Configuration ---
LLAMA_SERVER_THREADS: int = 8                           # CPU threads for auto-started llama-server
LLAMA_SERVER_GPU_LAYERS: int = 99                       # GPU layers to offload (99 = all)
# (2026-09-08 multi-chat): the LIVE list of llama servers we create is the hand-editable
# config/llama_servers.json (see get_llama_server_instances() below). LLAMA_SERVER_PORT now
# only serves as the FALLBACK default when that file is missing/corrupt, so a fresh checkout
# behaves exactly like before. Edit llama_servers.json to add more instances (other ports on
# this machine or remote LAN machines); changes take effect on the next SERVER boot.
LLAMA_SERVER_PORT: int = 5000                           # Fallback TCP port for auto-started llama-server (legacy single instance)

# --- Chat Request Queue (2026-09-08 multi-chat) ---
# Every chat request from every client lands in one FIFO queue; free worker slots pick them up.
QUEUE_MAX_PENDING: int = 32                             # Max requests waiting in the queue before new ones are rejected with a clear error
# (2026-09-10 log-noise fix) Seconds between queue-stats CHECKS. An INFO line is logged only when the
# snapshot CHANGES vs the previous check (first tick = baseline); unchanged idle ticks go to DEBUG and stay
# silent by default. 0 = disabled entirely.
QUEUE_STATS_LOG_INTERVAL_SEC: int = 15                  # Queue-stats heartbeat interval, see comment above
# (2026-08-26) Dedicated OCR instance: the main chat server may run a non-vision model,
# so transcribe_image can spin up its own GLM-OCR + mmproj on this side port and keep it warm.
LLAMA_OCR_SERVER_PORT: int = 5010                      # TCP port for the dedicated OCR llama-server
LLAMA_OCR_CTX_SIZE: int = 32768                       # Context tokens for the OCR instance (images + 4k output fit easily; keeps VRAM low)
# (2026-08-27) After a SUCCESSFUL transcription on the dedicated OCR instance, terminate it again to
# free its VRAM. True = unload right after every successful transcribe_image call (next use pays the
# cold-start cost of a few seconds); False = keep the process warm so back-to-back OCR calls are instant.
LLAMA_OCR_UNLOAD_AFTER_USE: bool = False              # Unload dedicated OCR llama-server after each successful tool use
LLAMA_SERVER_STARTUP_WAIT_SEC: int = 2                  # Sleep (s) between llama health checks
# (2026-09-09 switch-timeout fix) How long (s) to wait for the model to ACTUALLY LOAD after /health
# comes up, in BOTH start_server() and reload_with_model(). Sized for a COLD load of a ~23 GB 27B-class
# GGUF on consumer hardware: warm loads finish in ~20-90 s, but cold page cache + MTP draft-context init
# can need several minutes. The old hardcoded 120 s (60 x 2s) killed the new server mid-load during a
# model switch and left the instance with no model at all. NOTE: CLIENT MODEL_SWITCH_MAX_WAIT_SEC
# (CLIENT/config/config.py, 300 s) must stay ABOVE this value + port-free wait margin.
LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC: int = 240          # Model-load wait budget after /health is up (s)
    
# --- Llama Model State Cache (2026-08-18 AUTO-SWITCH optimization) ---
# How long (s) the in-memory "which model is loaded" state may be trusted WITHOUT an HTTP
# /v1/models round-trip. server.py marks it after every verified launch, so within this TTL
# chat requests pay ZERO extra latency for the auto-switch check.
LLAMA_MODEL_STATE_TTL_SEC: int = 60

# --- Llama Streaming Stall Detection (b10441 hang fix) ---
# If NO SSE chunk arrives from llama-server for this long, the request is
# considered stalled (server frozen / pipe deadlock). 90s covers even a very
# slow cold prefill of a ~50k-token prompt on consumer GPUs.
LLAMA_STREAM_STALL_TIMEOUT_SEC: int = 90                 # Max silence (s) between SSE chunks before aborting the request
LLAMA_AUTO_RESTART_ON_STALL: bool = True                 # Kill + restart llama-server after a stall (self-heal, like old code's auto-restart)

# --- Llama Server Optimization (Qwen3.6 / MoE) ---
# Based on research: aminrj.com, llama.cpp best practices
# These flags dramatically improve VRAM usage and throughput for Qwen3.6
LLAMA_SERVER_CACHE_TYPE_K: str = "q8_0"                 # KV key cache quantization q8_0 f16
LLAMA_SERVER_CACHE_TYPE_V: str = "q8_0"                 # KV value cache quantization
LLAMA_SERVER_FLASH_ATTN: str = "on"                     # Flash attention (~30% VRAM savings + speed boost)
LLAMA_SERVER_PARALLEL: int = 1                          # Single inference slot for stability
LLAMA_SERVER_CACHE_RAM: int = 4096                      # Max RAM (MB) for KV cache (prevents OOM on tool calls)

# --- Llama Server MTP (Multi-Token Prediction) Support ---
# For MTP models (e.g. Qwen3.6-27B-MTP), these params enable speculative decoding
# for >2x speedup. Auto-detected by server.py when 'MTP' is in model filename.
LLAMA_SERVER_MTP_ENABLED: bool = True                          # Auto-enable MTP for MTP models
LLAMA_SERVER_SPEC_TYPE: str = "draft-mtp"                      # Speculative decoding type
LLAMA_SERVER_SPEC_DRAFT_N_MAX: int = 4                         # Draft tokens for MTP (2-3 recommended)
LLAMA_SERVER_SPEC_DRAFT_N_MIN: int = 0                         # Min draft tokens (default 0)
LLAMA_SERVER_SPEC_DRAFT_P_SPLIT: float = 0.10                  # Speculative split probability
LLAMA_SERVER_SPEC_DRAFT_P_MIN: float = 0.3                     # Min speculative prob (greedy, default=0.0)

# --- Llama Server File Loading Flags ---
LLAMA_SERVER_NO_MMAP: bool = False           # Disable memory-mapped file loading
LLAMA_SERVER_NO_WARMUP: bool = False         # Disable server warmup
LLAMA_SERVER_JINJA: bool = False             # Disable Jinja template processing
LLAMA_SERVER_FIT: str = "on"                 # Fit mode: 'on' adjusts unset args to fit device memory, 'off' disables

# --- Server-Level Generation Defaults ---
LLAMA_SERVER_TEMP: float = 0.6               # Generation temperature (single source of truth for default temp)
LLAMA_SERVER_REPEAT_PENALTY: float = 1.1     # Repeat penalty for generation
LLAMA_SERVER_MAX_NEW_TOKENS: int = 32768     # Max new tokens per request (CONTEXT-GUARD clamps it down when free context room is smaller)
LLAMA_MIN_SAFE_MAX_TOKENS: int = 2048        # CONTEXT-GUARD floor: never clamp output below this. Was a hardcoded 1024, which was too small for a full multi-image answer + TASK DONE marker (2026-09-10 truncation-loop fix).

# --- Reasoning / Thinking Mode ---
LLAMA_SERVER_REASONING: bool = False         # Enable reasoning/thinking mode
LLAMA_SERVER_REASONING_BUDGET: int = 256     # Token budget for reasoning
LLAMA_SERVER_PRESERVE_THINKING: bool = False # Preserve thinking in output

# --- Timeouts ---
OCR_TIMEOUT: int = 120                                  # Timeout (s) for OCR API

# --- OCR ---
OCR_MAX_TOKENS: int = 4096                              # Max tokens for OCR model

# --- Web Search ---
WEB_SEARCH_DEFAULT_MAX_RESULTS: int = 20                # Max search results returned
WEB_SEARCH_DDGS_DEFAULT_RESULTS: int = 5                # Default DDGS results fetched
WEB_SEARCH_WIKIPEDIA_TIMEOUT: int = 15                  # Timeout (s) for Wikipedia API
WEB_SEARCH_DDGS_TIMEOUT: int = 20                       # Timeout (s) for DuckDuckGo HTML
WEB_SEARCH_BING_TIMEOUT: int = 15                       # Timeout (s) for Bing HTML
WEB_SEARCH_SEARXNG_TIMEOUT: int = 15                    # Timeout (s) for SearXNG HTML
WEB_SEARCH_RESULT_SNIPPET_CHARS: int = 200              # Max chars per result snippet
WEB_SEARCH_SEARXNG_INSTANCES: list = [
    "https://searx.be",
    "https://search.bus-hit.me",
    "https://search.rowie.at",
    "https://searx.fmac.xyz",
    "https://searx.privacydev.net",
    "https://searx.tiegeek.fr",
]

# --- File Handling (shipped to CLIENT tools via config_constants) ---
FILE_EXEC_OUTPUT_TRUNCATE_CHARS: int = 10000            # Max python_exec output chars
FILE_EXEC_ERROR_TRUNCATE_CHARS: int = 2000              # Max python_exec error chars
FILE_EXEC_TIMEOUT: int = 300                            # Timeout (s) for python_exec (5 minutes)

# --- Image Generation (shipped to CLIENT tools via config_constants) ---
IMAGE_SUBPROCESS_TIMEOUT: int = 300                     # Max wait (s) for image gen subprocess
IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD: int = 900             # Max wait (s) for GPU-offloaded tiers (FP8 + cpu offload on <16 GB cards): slower warm-up + first-run ~14 GB download safety net
# (2026-09-20 user-defined tier override) Pin the image-gen model line INSTEAD of letting
# generate_image auto-classify by probed VRAM:
#   'auto' = classify by GPU VRAM (high / mid / low - see tools/generate_tool TIER_MODELS)
#   'high' = Z-Image-Turbo bf16 resident (~23 GB peak; needs a >= 24 GB card)
#   'mid'  = SANA-Sprint 1.6B resident, EXACTLY 2 steps (~11 GB peak; needs >= 12 GB)
#   'low'  = SANA-Sprint + cpu offload (peak ~5.3 GB, resolution-clamped) - smallest model,
#            fastest generation and the smallest download even on a big card like a 5090.
# SAFETY: if the probed GPU has less VRAM than the pinned tier's minimum, generate_image
# falls back to the auto-classified tier instead of OOMing (logged loudly).
IMAGE_GEN_TIER_OVERRIDE: str = "low"
TOOLS_BOOTSTRAP_TIMEOUT_SEC: int = 2400                 # Max wait (s) for a self-unpacking tool's first-run setup (venv create + pip install of torch/diffusers-class deps)


# Path to the hand-editable llama server instance list (single source of truth for which
# llama servers we create - see get_llama_server_instances()). The .example.json template is
# shipped; bootstrap.py seeds the real file from it at first boot. Gitignored: per-machine.
LLAMA_SERVERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama_servers.json")

# --- Derived Helpers ---


def get_api_url() -> str:
    """Return the API URL for the currently selected provider."""
    if PROVIDER in ("llama", "coolems_server"):
        return LLAMA_URL
    elif PROVIDER == "coolems_client":
        return ""  # Client connects via encrypted WebSocket, no local API URL needed
    raise ValueError(f"Unknown provider: {PROVIDER}")


def get_provider_name() -> str:
    """Return the currently selected provider name."""
    return PROVIDER


# ---------------------------------------------------------------------------
# Llama server instances - config/llama_servers.json (2026-09-08 multi-chat)
# ---------------------------------------------------------------------------

_logger = _logging.getLogger("COOLEMS.Config")


@_dataclass(frozen=True)
class LlamaServerInstance:
    """One llama-server instance to create/attach at boot.

    Mirrors the shape of CLIENT's settings.json server_addresses pattern, but for
    SERVER-side LLM instances: a plain hand-editable JSON list in config/.
      name - human label only (logs/startup table); optional, defaults to host:port.
      host - "127.0.0.1"/"localhost" for same-machine instances, or a LAN IP of a
             remote machine running its own llama-server.
      port - integer TCP port of that instance's HTTP endpoint.

    Every instance runs the SAME model + launch args from this config module
    (LLAMA_SERVER_*) - one knob stays in one place.
    """
    name: str
    host: str
    port: int

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _write_default_local_file(path: str) -> None:
    """(2026-09-09 default-local fix) Re-create a missing llama_servers.json with the
    default local entry. Best-effort - a write failure only costs one boot cycle.
    """
    try:
        payload = _json.dumps([{"name": "local", "host": "127.0.0.1", "port": LLAMA_SERVER_PORT}], indent=2) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        _logger.error("[CONFIG] Could not re-create %s: %s", path, e)


def get_llama_server_instances() -> list["LlamaServerInstance"]:
    """Fresh read + validation of config/llama_servers.json.

    Same contract as CLIENT's get_server_address_list(): the file is the single
    source of truth for WHICH llama servers we create, and it is re-read on every
    call (boot reads it once; tests may point LLAMA_SERVERS_FILE elsewhere).

    Validation rules (hand-edited file - be forgiving but loud):
      * entry not an object / missing host / non-integer port -> skipped with a warning.
      * duplicate host:port -> deduped, first occurrence wins, warning logged.
      * empty list after validation, OR file corrupt/non-list -> FALL BACK to the default
        local instance (127.0.0.1:LLAMA_SERVER_PORT). A MISSING file is re-seeded on disk
        with that same default local entry (2026-09-09), so there is always a local server
        in the list that we can spawn and start with the last-loaded model.

    Returns a non-empty list of LlamaServerInstance.
    """
    path = LLAMA_SERVERS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
    except FileNotFoundError:
        # (2026-09-09 default-local fix) always keep a local entry in the list and
        # re-seed the missing file so it exists for hand-editing afterwards.
        _logger.warning(
            "[CONFIG] %s not found - recreating it with the default local instance 127.0.0.1:%d",
            path, LLAMA_SERVER_PORT,
        )
        _write_default_local_file(path)
        return [LlamaServerInstance("local", "127.0.0.1", LLAMA_SERVER_PORT)]
    except (OSError, ValueError) as e:
        _logger.warning(
            "[CONFIG] %s unreadable/invalid JSON (%s) - keeping the default local instance 127.0.0.1:%d "
            "(the corrupt file is left in place for inspection; fix it to configure more instances)",
            path, e, LLAMA_SERVER_PORT,
        )
        return [LlamaServerInstance("local", "127.0.0.1", LLAMA_SERVER_PORT)]

    if not isinstance(raw, list):
        _logger.warning(
            "[CONFIG] %s must be a JSON LIST of {name, host, port} entries - using the default local instance 127.0.0.1:%d",
            path, LLAMA_SERVER_PORT,
        )
        return [LlamaServerInstance("local", "127.0.0.1", LLAMA_SERVER_PORT)]

    instances: list[LlamaServerInstance] = []
    seen: set[tuple[str, int]] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _logger.warning("[CONFIG] llama_servers.json entry #%d is not an object - skipped", i)
            continue
        host = str(entry.get("host") or "").strip()
        port_raw = entry.get("port")
        name = str(entry.get("name") or "").strip()
        if not host:
            _logger.warning("[CONFIG] llama_servers.json entry #%d has no 'host' - skipped", i)
            continue
        # bool is a subclass of int in Python - reject it explicitly.
        if isinstance(port_raw, bool) or not isinstance(port_raw, int) or not (0 < port_raw < 65536):
            _logger.warning(
                "[CONFIG] llama_servers.json entry #%d has an invalid 'port' (%r) - skipped", i, port_raw
            )
            continue
        key = (host.lower(), port_raw)
        if key in seen:
            _logger.warning("[CONFIG] llama_servers.json duplicate host:port %s:%d - deduped", host, port_raw)
            continue
        seen.add(key)
        instances.append(LlamaServerInstance(name or f"{host}:{port_raw}", host, port_raw))

    if not instances:
        _logger.warning(
            "[CONFIG] llama_servers.json has no valid entries - using the default local instance 127.0.0.1:%d "
            "(the file is kept; add a {name, host, port} entry to configure more instances)",
            LLAMA_SERVER_PORT,
        )
        return [LlamaServerInstance("local", "127.0.0.1", LLAMA_SERVER_PORT)]

    return instances
