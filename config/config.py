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
CONTEXT_WINDOW_TOKENS: int = 185000             # Total token budget for model context (fallback)
# MUST equal the --ctx-size the deployed llama-server actually runs with (it is started from this
# value by command_builder.py). Raising it here without a matching restart + VRAM check silently
# desyncs client trim math / auth_ok context_window from reality - that drift caused the 2026-08-16
# truncated tool-call crash (see plan_20260817_trace_fix.md).
# (2026-10 fleet / 2026-11 strata) Per-backend context wins over this number: the registry reads the
# real ctx/slot from llama_servers.json or from the backend's /props, and the worker sets
# req.context_window from it, so a smaller node (e.g. Strata below this window) is still trimmed
# correctly. This value is what a backend WITHOUT a reported ctx/slot, and the CLIENT's auth_ok
# budget, see. To actually run 185000 on Strata, start Strata itself with --max-context 185000 -
# Jack never resizes a backend it only attaches to.

# --- Provider / Model ---
PROVIDER: str = "coolems_server"                        # Relay mode for server
# Local LLM engine behind coolems_server (2026-11 strata backend):
#   "llama"  -> llama.cpp / llama-server on LLAMA_SERVER_PORT (config/llama_servers.json)
#   "strata" -> Strata's own OpenAI-compatible endpoint on STRATA_SERVER_PORT
#               (config/strata_servers.json). The brain NEVER spawns or kills Strata - it
#               attaches to whatever Strata already runs and authenticates with STRATA_API_KEY.
SERVER_BACKEND: str = "llama"                           # Local LLM engine ("llama" | "strata")

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
# still agree on the version number itself (parity enforced by the WS handshake - ws_auth.py closes
# the socket on a mismatch, CLIENT raises ConnectionError; tripwire:
# tools/test_tools/test_shared_copies_identical.py).
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

# (2026-11-07) The LICENSE endpoint (https://coolems.com) has NO cert-pin knob on purpose.
# It is a public CA-signed host (Let's Encrypt), so trust = the system CA bundle + hostname
# verification (app/license_server.py::_build_ssl_context). A leaf SHA-256 pin would break
# every license check at each ~90-day LE rotation, and issuer-level pinning is not
# enforceable in this CPython/OpenSSL build (ssl exposes only the leaf; verify_callback is
# unreliable — see app/providers/coolems/ssl_pinning.py header). Cert pinning stays for the
# SELF-SIGNED relay link (WEB_RELAY_CERT_PIN_FINGERPRINT), where it is the right tool.
COOLEMS_RECONNECT_MIN_DELAY: float = 1.0      # Min delay between reconnect attempts (seconds)
COOLEMS_RECONNECT_MAX_DELAY: float = 30.0     # Max delay between reconnect attempts (seconds)

# --- Direct WebSocket Mode (PC-to-PC encrypted, no relay server needed) ---
COOLEMS_DIRECT_WS_PORT: int = 8080               # Port for direct WebSocket server (TLS always ON)
COOLEMS_DIRECT_WS_CERT_PATH: str = ""            # Path to SSL cert file (leave empty for auto-generated self-signed)
COOLEMS_DIRECT_WS_KEY_PATH: str = ""             # Path to SSL key file (leave empty for auto-generated self-signed)
# --- Llama Server Port (single source of truth for the legacy single-instance port) ---
# (2026-10-02 port-config fix) The port lives in EXACTLY ONE place: LLAMA_SERVER_PORT below.
# LLAMA_URL is DERIVED from it so the two can never desync. Multi-instance setups
# use config/llama_servers.json instead (see get_llama_server_instances()).
LLAMA_SERVER_PORT: int = 5000                           # TCP port for auto-started llama-server (legacy single instance)

LLAMA_URL: str = f"http://localhost:{LLAMA_SERVER_PORT}"  # llama.cpp server base URL - DERIVED from LLAMA_SERVER_PORT (single knob)
# The loaded model is NO LONGER hardcoded here (the old value was a dead Ollama-style
# reference that matched nothing on disk). Source of truth for the active model:
# config/.last_model_loaded.json - written by app/providers/llama/model_persistence.py
# after every verified launch/switch; read back on startup (see server.start_server()).
MODEL_NAME: str = ""  # empty = "load whatever .last_model_loaded.json says"

# --- Strata Backend (2026-11): SERVER_BACKEND = "strata" ---
# Strata is a SEPARATE local app that serves an OpenAI-compatible (llama.cpp-shaped)
# HTTP endpoint. The SERVER only talks to it over HTTP: no spawn, no kill, no model
# loading - Strata owns the process and the model. Every request carries a Bearer token.
STRATA_SERVER_PORT: int = 18500                         # TCP port of Strata's inference endpoint
STRATA_URL: str = f"http://localhost:{STRATA_SERVER_PORT}"  # Strata base URL - DERIVED from STRATA_SERVER_PORT (single knob)
# Bearer token for /props, /v1/models and /v1/chat/completions (/health needs none).
# Precedence: strata_servers.json entry 'api_key' > this value > env STRATA_API_KEY.
# Leave empty here and put the real key in config/strata_servers.json (gitignored) or in
# the environment - never commit a real key.
STRATA_API_KEY: str = ""
# NOTE: model routing uses the id Strata reports in /props (its model_alias). The CLIENT
# picker is built from that id, so clients never send Strata's /v1/models aliases; an alias
# sent by hand would route to a model group with no workers.

# --- Chrome CDP (Browser Debugging) ---
CHROME_CDP_PORT: int = 9222                           # Chrome DevTools Protocol debugging port

# --- Local CLIENT Web UI Port ---
# HTTP port of the local CLIENT web UI. Defined in BOTH config packages on purpose:
# SERVER root code.py launches CLIENT with it; CLIENT/entry/cli_parser.py uses it as its
# --port default (CLIENT/config/config.py holds the client-side copy). Change ONE, change
# the other - tests/test_port_single_source_of_truth.py fails if a stray literal appears.
CLIENT_UI_PORT: int = 8000

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

# --- Chat Request Queue (2026-09-08 multi-chat; per-model groups 2026-10) ---
# Every chat request lands in the FIFO queue of its MODEL GROUP; free worker slots pick them up.
QUEUE_MAX_PENDING: int = 32                             # Max requests waiting PER MODEL GROUP (Qwen 32 + Gemma 32 ...)
# (2026-10 fleet) A full group does NOT reject instantly: we wait this long for one of its
# slots to free, then answer with a RETRYABLE "server is full" error. No penalty, no ban,
# no backoff lock - the client simply resends. This wait is also the natural retry spacing.
QUEUE_FULL_GRACE_SEC: float = 5.0                       # Max wait for queue space before answering "server is full"
QUEUE_FULL_RETRY_AFTER_SEC: int = 60                    # Hint sent to the client (seconds) on a full-queue rejection
QUEUE_MODEL_WAIT_SEC: int = 120                         # Model configured but ALL its nodes down -> retryable error after this
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
# comes up, in BOTH start_server() and the (removed) reload path. Sized for a COLD load of a ~23 GB 27B-class
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
LLAMA_SERVER_PARALLEL: int = 1                          # DEFAULT slots for a LOCAL instance without "concurrency"; also OCR
# (2026-10 fleet) Concurrency is now per instance: config/llama_servers.json "concurrency": N
# for LOCAL entries (we spawn them with --parallel N) and discovered from each REMOTE node's
# /props.total_slots (its node launcher owns the number). This cap is a sanity guard only.
LLAMA_MAX_CONCURRENCY: int = 8                          # Hard ceiling per llama instance (1..8)
LLAMA_SERVER_CACHE_RAM: int = 4096                      # Max RAM (MB) for KV cache (prevents OOM on tool calls)
# (2026-11 memory gate) Headroom that must stay FREE before a llama-server is spawned.
# app/providers/llama/memory.py refuses a launch when weights + KV + buffers would eat
# the free RAM/VRAM down to this margin: the brain itself, the OS and whatever else is
# loaded (Strata, another llama node) need room to keep running. Raise it on a machine
# that runs other GPU workloads; 0 disables the margin (the estimate alone is checked).
LLAMA_LAUNCH_RESERVE_MB: int = 1500                     # MB of RAM/VRAM that must stay free after a launch

# --- Llama Server MTP (Multi-Token Prediction) Support ---
# For MTP models (e.g. Qwen3.6-27B-MTP), these params enable speculative decoding
# for >2x speedup. Auto-detected by server.py via model_utils._is_mtp_model(): first the
# 'MTP' filename heuristic, then a header scan of the .gguf itself (baked-in MTP tensors /
# '<arch>.nextn_predict_layers'), so files without 'MTP' in their name are handled too.
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
# One shared deadline for a whole web_search call (2026-11-06 rewrite): every enabled
# provider runs concurrently, so the call is bounded by this instead of the sum of the
# per-engine timeouts (the old sequential chain could run minutes on dead engines).
WEB_SEARCH_OVERALL_DEADLINE_SEC: int = 12               # Overall web_search deadline (s)
# Report size sent to the model; the full report spills to .temp/spill/ with a locator.
WEB_SEARCH_OUTPUT_MAX_CHARS: int = 8000                 # Max chars of web_search output
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
IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD: int = 1800            # Max wait (s) for GPU-offloaded tiers (v21: the Qwen high tier streams a 20B MMDiT through CPU offload at every step; low tier likewise): slower warm-up + first-run multi-GB download safety net
# (2026-09-20 user-defined tier override) Pin the image-gen model line INSTEAD of letting
# generate_image auto-classify by probed VRAM:
#   'auto' = classify by GPU VRAM (high / mid / low - see tools/generate_tool TIER_MODELS)
#   'high' = Qwen/Qwen-Image fp8 + cpu offload (img2img via Qwen/Qwen-Image-Edit; needs a >= 24 GB card)
#   'mid'  = Z-Image-Turbo fp8, ~9 guidance-free steps (~17 GB peak resident, auto-offloaded below ~18 GB)
#   'low'  = SANA-Sprint + cpu offload (peak ~5.3 GB, resolution-clamped) - smallest model,
#            fastest generation and the smallest download even on a big card like a 5090.
# SAFETY: if the probed GPU has less VRAM than the pinned tier's minimum, generate_image
# falls back to the auto-classified tier instead of OOMing (logged loudly).
IMAGE_GEN_TIER_OVERRIDE: str = "low"
TOOLS_BOOTSTRAP_TIMEOUT_SEC: int = 10800                # Max wait (s) for a self-unpacking tool's first-run setup (venv create + pip install + large model download; 3h safety net so slow links can finish multi-GB downloads)


# Path to the hand-editable llama server instance list (single source of truth for which
# llama servers we create - see get_llama_server_instances()). The .example.json template is
# shipped; bootstrap.py seeds the real file from it at first boot. Gitignored: per-machine.
LLAMA_SERVERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama_servers.json")

# (2026-10 fleet) Extra local directories scanned for .gguf models. Profiles no longer
# carry model folders (they authorize by FILENAME), so the scanner owns the search path:
# llama_server/models is always scanned (flat + one subfolder deep), plus the folder of
# the model persisted in .last_model_loaded.json, plus these optional absolute paths.
LLAMA_MODEL_DIRS: list = []

# --- Derived Helpers ---


def get_api_url() -> str:
    """Return the API URL for the currently selected provider.

    coolems_server picks its local LLM engine from SERVER_BACKEND: "llama" -> LLAMA_URL,
    "strata" -> STRATA_URL (the brain attaches to Strata, it never spawns it)."""
    if PROVIDER in ("llama", "coolems_server"):
        return STRATA_URL if SERVER_BACKEND == "strata" else LLAMA_URL
    elif PROVIDER == "coolems_client":
        return ""  # Client connects via encrypted WebSocket, no local API URL needed
    raise ValueError(f"Unknown provider: {PROVIDER}")


def get_provider_name() -> str:
    """Return the currently selected provider name."""
    return PROVIDER


def set_server_backend(name: str) -> None:
    """Runtime override of the local LLM engine ("llama" | "strata").

    Used by code.py when the CLI names the engine explicitly (provider=llama / provider=strata),
    which is the more specific request than config.SERVER_BACKEND. Both copies are updated:
    get_api_url() reads this module's global, while app code reads config.SERVER_BACKEND from
    the package re-export - keeping them apart would make the URL and the provider disagree."""
    global SERVER_BACKEND
    SERVER_BACKEND = name
    try:
        import config as _pkg
        _pkg.SERVER_BACKEND = name
    except Exception:  # pragma: no cover - defensive
        pass


# ---------------------------------------------------------------------------
# LLM backend instances - config/llama_servers.json + config/strata_servers.json
# (2026-09-08 multi-chat; per-config entries 2026-11)
# ---------------------------------------------------------------------------

_logger = _logging.getLogger("COOLEMS.Config")


@_dataclass(frozen=True)
class LlamaServerInstance:
    """One LLM backend from config/llama_servers.json (2026-11 multi-config fleet).

    The file is a plain hand-editable JSON list; ONE ENTRY PER llama-server we run or
    attach to, and every entry carries its OWN model and launch parameters. That is what
    makes "three llama-services on three ports, three different models, on one machine"
    possible: each entry is a complete, self-contained config.

      name        - human label; used by utils/start_llama_node (list / start / stop /
                    configure) and by the fleet table. Optional -> defaults to host:port.
      host        - "127.0.0.1"/"localhost" for a node on THIS machine (we may spawn it),
                    or a LAN IP of another machine's llama-server (attach-only).
      port        - integer TCP port of that instance's HTTP endpoint.
      model       - .gguf FILENAME or absolute path this node must run. LOCAL entries only:
                    None = "whatever this machine has" (the disk scan / persisted model).
                    Ignored for remote entries - the node reports its model via /props.
      bind        - LOCAL spawn bind address (default 127.0.0.1; 0.0.0.0 = reachable on LAN).
      concurrency - requests this instance may run at once (llama.cpp --parallel N).
                    OPTIONAL everywhere: None = DISCOVERED from /props.total_slots, which is
                    then the routing capacity. LOCAL with an int = the exact spawn value.
                    REMOTE: an int only CAPS a node below its reported slots.
      ctx_per_slot- context tokens PER slot for a LOCAL spawn (total --ctx-size = N x this).
                    None = config CONTEXT_WINDOW_TOKENS. /props always wins for routing.
      threads     - CPU threads for a LOCAL spawn (None = LLAMA_SERVER_THREADS).
      gpu_layers  - --n-gpu-layers for a LOCAL spawn (None = LLAMA_SERVER_GPU_LAYERS).
      extra_args  - raw extra llama-server flags, appended verbatim (LOCAL spawn only).
      auto_start  - LOCAL: may the SERVER spawn this node at boot when nothing is on its
                    port? false = probe/attach only (an admin owns its lifecycle).
      api_key     - only meaningful for kind="strata" (Bearer token); None for llama.
      kind        - "llama" (llama-server) or "strata" (attach-only endpoint).
    """
    name: str
    host: str
    port: int
    concurrency: Optional[int] = None
    api_key: Optional[str] = None
    ctx_per_slot: Optional[int] = None
    model: Optional[str] = None
    bind: Optional[str] = None
    threads: Optional[int] = None
    gpu_layers: Optional[int] = None
    extra_args: tuple = ()
    auto_start: bool = True
    kind: str = "llama"

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        """Operator-facing identity: name + endpoint (+ kind when it is not llama)."""
        base = f"{self.name} ({self.host}:{self.port})"
        return base if self.kind == "llama" else f"{base} [{self.kind}]"

    @property
    def is_local(self) -> bool:
        """Local = the instance WE may spawn. Everything else is attach-only."""
        return self.host.strip().lower() in ("127.0.0.1", "localhost", "::1")

    @property
    def is_spawnable(self) -> bool:
        """We may start/stop this process: only our own local llama-server entries."""
        return self.is_local and self.kind == "llama"


def _parse_instance_concurrency(entry: dict, loopback: bool, index: int,
                                source: str = "llama_servers.json") -> Optional[int]:
    """Validate one entry's 'concurrency' (alias 'parallel') value.

    LOCAL and REMOTE alike: returns None on missing/bad input - None means "DISCOVER from
    /props.total_slots" (the node owns its slot count). A local instance with None is
    SPAWNED with --parallel LLAMA_SERVER_PARALLEL as a launch fallback, after which
    discovery takes over and the worker pool reconciles. An explicit int is an UPPER
    BOUND (and the exact spawn value for local instances).
    """
    raw = entry.get("concurrency", entry.get("parallel"))
    if raw is None:
        return None

    # bool is a subclass of int in Python - reject it explicitly.
    if isinstance(raw, bool) or not isinstance(raw, int):
        _logger.warning(
            "[CONFIG] %s entry #%d has an invalid 'concurrency' (%r) - "
            "%s", source, index, raw,
            f"spawning with {LLAMA_SERVER_PARALLEL} slot(s), then discovering from /props"
            if loopback else "will discover it from /props",
        )
        return None

    if raw < 1:
        _logger.warning(
            "[CONFIG] %s entry #%d has 'concurrency' < 1 (%r) - %s",
            source, index, raw,
            f"spawning with {LLAMA_SERVER_PARALLEL} slot(s), then discovering from /props"
            if loopback else "will discover it from /props",
        )
        return None

    if raw > LLAMA_MAX_CONCURRENCY:
        _logger.warning(
            "[CONFIG] %s entry #%d 'concurrency' %d exceeds LLAMA_MAX_CONCURRENCY=%d - clamped",
            source, index, raw, LLAMA_MAX_CONCURRENCY,
        )
        return LLAMA_MAX_CONCURRENCY

    return raw


def _parse_int(entry: dict, key: str, source: str, index: int, minimum: int = 1,
               maximum: Optional[int] = None, note: str = "") -> Optional[int]:
    """Validate one optional int field of a server entry (bool rejected explicitly).

    Returns None on missing/invalid input so the caller applies its own default. Bad
    input is said and ignored - a hand-edited file must never stop a boot.
    """
    raw = entry.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        _logger.warning("[CONFIG] %s entry #%d has an invalid '%s' (%r) - ignored%s",
                        source, index, key, raw, note)
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _logger.warning("[CONFIG] %s entry #%d has an invalid '%s' (%r) - ignored%s",
                        source, index, key, raw, note)
        return None
    if value < minimum:
        _logger.warning("[CONFIG] %s entry #%d has '%s' < %d (%r) - ignored%s",
                        source, index, key, minimum, raw, note)
        return None
    if maximum is not None and value > maximum:
        _logger.warning("[CONFIG] %s entry #%d '%s' %d exceeds %d - clamped",
                        source, index, key, value, maximum)
        return maximum
    return value


def _parse_instance_ctx_per_slot(entry: dict, source: str, index: int) -> Optional[int]:
    """Validate an optional ``ctx_per_slot`` override on a server instance entry.

    For a LOCAL llama spawn this is the per-slot --ctx-size we launch with (total
    --ctx-size = slots x this). For an endpoint we only attach to it is a fallback for a
    /props that reports no n_ctx - a reported per-slot context always wins
    (backend_registry.refresh_props overwrites it). Bad input is said and ignored.
    """
    return _parse_int(entry, "ctx_per_slot", source, index, minimum=256,
                      note=" (per-slot context comes from /props or config)")


def _default_local_instance() -> "LlamaServerInstance":
    """The always-present local instance (what a fresh machine gets).

    model=None: the node launcher picks whatever this machine has (persisted last-loaded
    model, else the first .gguf on disk). concurrency=None: slots are DISCOVERED from
    /props after the spawn (the spawn uses LLAMA_SERVER_PARALLEL as a launch fallback)."""
    return LlamaServerInstance("local", "127.0.0.1", LLAMA_SERVER_PORT)


def _write_default_local_file(path: str) -> None:
    """Re-create a missing llama_servers.json with the default local entry.

    Best-effort - a write failure only costs one boot cycle. Written in the FULL shape
    (model + slots + ctx + auto_start) so a fresh checkout shows what can be edited.
    """
    try:
        payload = _json.dumps([{
            "name": "local",
            "host": "127.0.0.1",
            "port": LLAMA_SERVER_PORT,
            "model": None,
            "concurrency": LLAMA_SERVER_PARALLEL,
            "ctx_per_slot": CONTEXT_WINDOW_TOKENS,
            "auto_start": True,
        }], indent=2) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        _logger.error("[CONFIG] Could not re-create %s: %s", path, e)


def _entry_to_instance(entry: dict, index: int, source: str, kind: str = "llama",
                       api_key: Optional[str] = None) -> Optional["LlamaServerInstance"]:
    """Validate ONE backend-list entry into a LlamaServerInstance (None = skip, loudly).

    Shared by llama_servers.json and strata_servers.json - same shape; only 'kind' and
    the api_key differ. Every field is optional except host+port.
    """
    if not isinstance(entry, dict):
        _logger.warning("[CONFIG] %s entry #%d is not an object - skipped", source, index)
        return None

    host = str(entry.get("host") or "").strip()
    port_raw = entry.get("port")
    if not host:
        _logger.warning("[CONFIG] %s entry #%d has no 'host' - skipped", source, index)
        return None
    # bool is a subclass of int in Python - reject it explicitly.
    if isinstance(port_raw, bool) or not isinstance(port_raw, int) or not (0 < port_raw < 65536):
        _logger.warning("[CONFIG] %s entry #%d has an invalid 'port' (%r) - skipped",
                        source, index, port_raw)
        return None

    loopback = host.lower() in ("127.0.0.1", "localhost", "::1")
    name = str(entry.get("name") or "").strip() or f"{host}:{port_raw}"

    model = entry.get("model")
    if model is not None and not isinstance(model, str):
        _logger.warning("[CONFIG] %s entry #%d has a non-string 'model' (%r) - ignored",
                        source, index, model)
        model = None
    model = (model or "").strip() or None
    if model and kind == "strata":
        # Strata picks its own model; a 'model' here would only lie about who serves what.
        _logger.warning("[CONFIG] %s entry #%d sets 'model' but Strata owns its model - ignored",
                        source, index)
        model = None

    bind_raw = entry.get("bind")
    bind = str(bind_raw).strip() if bind_raw else None
    if bind and kind == "strata":
        bind = None

    return LlamaServerInstance(
        name=name,
        host=host,
        port=port_raw,
        concurrency=_parse_instance_concurrency(entry, loopback=loopback, index=index,
                                               source=source),
        api_key=api_key,
        ctx_per_slot=_parse_instance_ctx_per_slot(entry, source, index),
        model=model,
        bind=bind,
        threads=_parse_int(entry, "threads", source, index, minimum=1),
        gpu_layers=_parse_int(entry, "gpu_layers", source, index, minimum=0),
        extra_args=_parse_extra_args(entry, source, index),
        # Strata starts its own process - the brain never spawns it, whatever the file says.
        auto_start=(entry.get("auto_start") if isinstance(entry.get("auto_start"), bool) else True)
                   if kind == "llama" else False,
        kind=kind,
    )


def _parse_extra_args(entry: dict, source: str, index: int) -> tuple:
    """Validate an entry's 'extra_args' (list of raw llama-server flags) as a tuple."""
    raw = entry.get("extra_args")
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(a, str) for a in raw):
        return tuple(a for a in raw if a.strip())
    _logger.warning("[CONFIG] %s entry #%d has an invalid 'extra_args' (%r) - ignored "
                    "(expected a list of strings)", source, index, raw)
    return ()


def _load_server_list(path: str, kind: str, default_factory, write_default,
                      api_key_fallback: Optional[str] = None,
                      env_key: Optional[str] = None,
                      dedupe: bool = True) -> list["LlamaServerInstance"]:
    """Read + validate one backend list file (llama_servers.json / strata_servers.json).

    Identical contract for both files:
      * missing file   -> re-seed it with the default entry, return that entry.
      * corrupt/not a list -> leave the file for inspection, return the default entry.
      * bad entries    -> skipped with a warning; duplicate host:port deduped, first wins
                          (dedupe=False keeps every valid entry - the admin menu view).
      * nothing valid  -> the default entry.
    """
    source = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
    except FileNotFoundError:
        _logger.warning("[CONFIG] %s not found - recreating it with the default entry", path)
        write_default(path)
        return [default_factory()]
    except (OSError, ValueError) as e:
        _logger.warning(
            "[CONFIG] %s unreadable/invalid JSON (%s) - keeping the default entry "
            "(the corrupt file is left in place for inspection; fix it to configure more backends)",
            path, e,
        )
        return [default_factory()]

    if not isinstance(raw, list):
        _logger.warning("[CONFIG] %s must be a JSON LIST of backend entries - using the default entry",
                        path)
        return [default_factory()]

    instances: list[LlamaServerInstance] = []
    seen: set[tuple[str, int]] = set()
    for i, entry in enumerate(raw):
        api_key = None
        if kind == "strata" and isinstance(entry, dict):
            api_key = (str(entry.get("api_key") or "").strip()
                       or (api_key_fallback or "") or (env_key or "") or None)
        inst = _entry_to_instance(entry, i, source, kind=kind, api_key=api_key)
        if inst is None:
            continue
        key = ("127.0.0.1" if inst.host.lower() == "localhost" else inst.host.lower(), inst.port)
        if dedupe and key in seen:
            _logger.warning("[CONFIG] %s duplicate host:port %s:%d - deduped",
                            path, inst.host, inst.port)
            continue
        if dedupe:
            seen.add(key)
        instances.append(inst)

    if not instances:
        _logger.warning(
            "[CONFIG] %s has no valid entries - using the default entry (the file is kept; "
            "add a {name, host, port, model, ...} entry to configure more backends)", path)
        return [default_factory()]

    return instances


def get_llama_server_instances() -> list["LlamaServerInstance"]:
    """Fresh read + validation of config/llama_servers.json.

    The file is the single source of truth for WHICH llama servers exist and WHAT each
    one runs (model, slots, ctx/slot, threads, gpu layers, bind, auto_start). Re-read on
    every call (boot reads it once; tests may point LLAMA_SERVERS_FILE elsewhere), so an
    edit made by utils/start_llama_node takes effect on the next boot.

    A MISSING file is re-seeded with the default local entry, so there is always at least
    one local server in the list. Returns a non-empty list of LlamaServerInstance.
    """
    return _load_server_list(
        LLAMA_SERVERS_FILE, "llama", _default_local_instance, _write_default_local_file,
    )


def get_llama_server_entries_raw() -> list["LlamaServerInstance"]:
    """EVERY valid entry in llama_servers.json, INCLUDING duplicate host:port pairs.

    The admin menu (list / configure / delete) must see the whole file: a shadowed
    duplicate is still editable and deletable, even though the runtime fleet view
    (get_llama_server_instances) keeps only the first entry per host:port.
    """
    return _load_server_list(
        LLAMA_SERVERS_FILE, "llama", _default_local_instance, _write_default_local_file,
        dedupe=False,
    )


def instance_to_entry(inst: "LlamaServerInstance") -> dict:
    """Serialize one instance back to the JSON entry shape (for saving a list)."""
    entry = {
        "name": inst.name,
        "host": inst.host,
        "port": inst.port,
        "model": inst.model,
        "concurrency": inst.concurrency,
        "ctx_per_slot": inst.ctx_per_slot,
        "threads": inst.threads,
        "gpu_layers": inst.gpu_layers,
        "bind": inst.bind,
        "extra_args": list(inst.extra_args),
        "auto_start": bool(inst.auto_start),
    }
    if inst.kind == "strata":
        entry["api_key"] = inst.api_key or ""
    return entry


def save_llama_server_instances(instances: list["LlamaServerInstance"],
                                path: Optional[str] = None) -> bool:
    """Write config/llama_servers.json atomically (tmp file + os.replace).

    utils/start_llama_node uses this when an admin saves a config; the file stays the
    single source of truth and a half-written file can never be read by a boot.
    """
    target = path or LLAMA_SERVERS_FILE
    payload = _json.dumps([instance_to_entry(i) for i in instances], indent=2) + "\n"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, target)
        return True
    except OSError as e:
        _logger.error("[CONFIG] Could not write %s: %s", target, e)
        return False


# ---------------------------------------------------------------------------
# Strata endpoints - config/strata_servers.json (2026-11 strata backend)
# ---------------------------------------------------------------------------

# Path to the hand-editable Strata endpoint list. Same contract and same entry shape as
# llama_servers.json, but every entry is ATTACH-ONLY: Strata starts its own inference
# process, so the brain only probes and talks HTTP to it. Gitignored (per-machine, carries
# the Bearer key); the .example.json template is tracked and bootstrap.py seeds it.
STRATA_SERVERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strata_servers.json")


def strata_auth_headers(api_key: Optional[str]) -> Optional[dict]:
    """Bearer auth headers for a Strata endpoint, or None when no key is configured."""
    key = (api_key or "").strip()
    if not key:
        return None
    return {"Authorization": f"Bearer {key}"}


def _default_strata_instance() -> "LlamaServerInstance":
    """The always-present local Strata endpoint (127.0.0.1:STRATA_SERVER_PORT)."""
    return LlamaServerInstance("strata", "127.0.0.1", STRATA_SERVER_PORT, kind="strata")


def _write_default_strata_file(path: str) -> None:
    """Re-create a missing strata_servers.json with the default local endpoint. Best-effort."""
    try:
        payload = _json.dumps([{
            "name": "strata",
            "host": "127.0.0.1",
            "port": STRATA_SERVER_PORT,
            "api_key": "",
            "concurrency": None,
            "ctx_per_slot": None,
        }], indent=2) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        _logger.error("[CONFIG] Could not re-create %s: %s", path, e)


def get_strata_server_instances() -> list["LlamaServerInstance"]:
    """Fresh read + validation of config/strata_servers.json (same loader as llama's).

    Every entry comes back with kind="strata" and auto_start=False - Strata owns its
    process, so the brain never spawns, relaunches or terminates it. 'api_key' falls back
    to config.STRATA_API_KEY, then env STRATA_API_KEY. A missing file is re-seeded with
    the default local endpoint. Returns a non-empty list.
    """
    return _load_server_list(
        STRATA_SERVERS_FILE, "strata", _default_strata_instance, _write_default_strata_file,
        api_key_fallback=(STRATA_API_KEY or "").strip(),
        env_key=(os.environ.get("STRATA_API_KEY") or "").strip(),
    )


def save_strata_server_instances(instances: list["LlamaServerInstance"],
                                 path: Optional[str] = None) -> bool:
    """Write config/strata_servers.json atomically. Carries the Bearer key - gitignored."""
    target = path or STRATA_SERVERS_FILE
    payload = _json.dumps([instance_to_entry(i) for i in instances], indent=2) + "\n"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, target)
        return True
    except OSError as e:
        _logger.error("[CONFIG] Could not write %s: %s", target, e)
        return False
