# Jack — Private AI Agent.

<p align="center">
  <img src="./utils/ScreenShot.png" width="99%" alt="Jack in action"/>
</p>

## **Jack** is a local-first autonomous AI agent built on the **COOLEMS framework**. 
>In short what Jack does:

-  **Works on an entire repository with all its files — till the desired outcome.** Point it at a folder and give it a task
-  **Gives those models hands** — file operations, Python execution, browser automation, OCR and image generation
-  **Per-workspace working folder** — each workspace is set with its own directory
-  **Many people at the same time** — every workspace runs as an independent background session in parallel
-  **Scales across your LAN** — the SERVER manages one or more `llama-server` instances behind a fair request queue
-  **Private by default** — no data leaves your local network


---

## Features

- 🤖 **Persistent agentic loop** — Jack plans (plan files), acts with tools, reads results, self-corrects and only stops when the task is verified done
- 💬 **Parallel chats** — every conversation is an independent background session: multiple chats generate at once, switching between them never interrupts another chat; per-chat status dots plus a "N gen · M queued" pill show what is happening in every chat at a glance
- 🖥️ **Multi-server scaling** — one JSON file lists the `llama-server` instances (local or on other LAN machines); each gets its own worker, so N GPUs serve up to N chats simultaneously and the rest wait fairly in FIFO order
- 🔐 **Role-based access control** — per-profile models, tools, rate limits and connection caps, enforced server-side (`config/profiles.json`)
- 📁 **File tools** — read / write / move / rename / delete inside a locked working root
- 🌐 **Browser automation** — drives a real Chrome via CDP: navigation, screenshots, precise mouse targeting, OCR of what's on screen
- 🐍 **Python execution** — sandboxed code runs for calculation and data work
- 🔎 **Web search & fetch** — live internet access with SSRF protection
- 🖼️ **Image generation & vision** — GPU-tiered Qwen-Image / Z-Image-Turbo / SANA-Sprint pipelines + OCR transcription
- ⏰ **Time tools** — local/UTC/city time, timezone differences
- 🔁 **Model auto-switching with drain policy** — running requests finish on their backend; new ones are held back until every instance has reloaded
- 🩹 **LLM self-heal** — a 90 s stream stall kills and restarts the llama-server automatically
- ⚡ **MTP speculative decoding** — auto-detected (filename or GGUF header scan) for >2x speedup on MTP builds
- 🔒 **Security by default** — TLS relay, API-key auth, loopback-only client bind (refused without keys), gitignored secrets

## Parallel Chats & Multi-Server Scaling

Two independent mechanisms make Jack usable as a shared team service:

**1. Every chat is its own background session.** The CLIENT's `ChatBus` keeps one channel per conversation — each with its own generation task, stop event and frame buffer. The browser holds one WebSocket per open chat (LRU-capped), so switching to another chat never closes the first one: it keeps generating in the background. While you view chat B, frames arriving for chat A are not rendered but update A's sidebar status dot (`idle` / `queued` / `generating`). The header pill aggregates everything ("2 gen · 1 queued"), and re-attaching to a chat replays its buffered tail so nothing is lost when you switch back.

**2. One queue per model, one worker per slot.** Every request lands in the bounded FIFO queue of the model it names (`QUEUE_MAX_PENDING = 32` per model group by default). Each `llama-server` exposes N inference slots (`--parallel N`, discovered from `/props`) and the `WorkerPool` runs exactly one worker per slot. Whichever slot frees first takes the next request **of its own model group** — Qwen traffic never delays Gemma and vice versa. A full group gets a 5-second grace to free a slot; if none frees, the client gets a retryable *"Resend request in a min as server is full"* (no penalty — the rate-limit token is refunded). Per-user caps (`max_parallel_slots` in `profiles.json`) count **across the whole fleet**.

**`config/llama_servers.json` is the single source of truth for the fleet.** Local entries (`127.0.0.1`) are spawned by the SERVER with an explicit `"concurrency": N`; remote LAN entries are attach-only — the registry probes `/health` + `/props` every 30 s, learns each node's **model, slot count and per-slot context**, and routes by model. Adding a GPU box is one `{name, host, port}` entry; its slot count is discovered, never duplicated. Models are identified by GGUF filename, so keep filenames unique across the fleet.

## Architecture

```
CLIENT #1 ─┐
CLIENT #2 ─┼── WebSocket (TLS, API-key auth) ──▶ SERVER ── per-model FIFO queues (bounded)
CLIENT #n ─┘                                     │        ├── slot 1 ──▶ llama-server "local"  (spawned by SERVER)
                                                 ▼        ├── slot 2 ──▶ llama-server "h200-a" (LAN, attach-only, Qwen)
                                   auth + RBAC            ├── slot 3 ──▶ llama-server "h200-b" (LAN, attach-only, Qwen)
                            (profiles.json: models,        └── slot 4 ──▶ llama-server "h200-c" (LAN, attach-only, Gemma)
                             tools, rate, slots)

Each CLIENT is self-contained: web UI, agentic loop and all tool execution live on the client.
The SERVER never runs tools — it authenticates, applies profiles and relays LLM traffic only.
```

- **SERVER** (`code.py`) — headless relay + control plane. Accepts authenticated WebSocket clients, enforces role profiles (which models/tools each user may use), manages the backend registry, request queue and worker pool, and streams model responses back to the right chat. Two provider modes:
  - `coolems_server` → headless relay only (no UI)
  - `llama` → just relay + launches the CLIENT as a subprocess for local UI use
- **CLIENT** (`CLIENT/code_client.py`) — where all intelligence lives. Can be relocated on another machine and configured to connect to server. It hosts the web UI, the ChatBus background sessions, the agentic loop and every tool execution (files, browser via CDP/Playwright, Python exec, OCR, image generation). All LLM requests are routed through the SERVER; direct LLM access from the client is blocked by design.
- **llama_server** — llama.cpp runtime with pre-downloaded model weights (see `llama_server/download llama_server files.md` for where to get the binaries and how to lay them out). Local instances run on the SERVER machine; remote ones run wherever you put a GPU.

This split keeps secrets and heavy state on one side, makes the relay trivially auditable, lets any laptop on your LAN drive a machine that owns the GPUs, and scales horizontally by editing one JSON file.

## Security Model

| Layer | What it does |
|---|---|
| TLS always on | Relay port 8080 is TLS-only; a self-signed certificate with your LAN IPs as SANs is auto-generated in `config/` on first start if none exists |
| Optional cert pinning | Clients can pin the SERVER's exact SHA-256 certificate fingerprint — trust by fingerprint, no CA infrastructure needed |
| API-key auth | Every WebSocket connection presents its key in the first frame; unknown/inactive keys are rejected with a log entry on the SERVER |
| Role-based access control | `config/profiles.json` decides per user: allowed model folders, allowed tools, Python import restrictions, messages-per-second rate limit, max simultaneous connections — enforced server-side, hot-reloaded |
| Protocol versioning | Every auth frame carries a protocol version; mismatched CLIENT/SERVER builds are detected instead of silently breaking |
| Loopback-only client bind | The CLIENT UI binds to `127.0.0.1` by default; binding `0.0.0.0` is refused at startup unless API keys are configured first |
| Working-root lock | All file tools resolve paths inside one locked folder per machine (per-chat overridable); nothing escapes it |
| Gitignored secrets | Real keys, profiles and server lists live in gitignored files bootstrapped from `.example` templates — a fresh checkout always has the shapes to edit, never real values |
| CORS lockdown | Explicit localhost-only origin list on the CLIENT UI; no wildcards, credentials off |
| SSRF protection | Web search/fetch tools refuse private/internal addresses |
| Tool-source integrity | Tool code delivered from the SERVER is SHA-256 hashed and verified before loading |
| Rate limiting & connection caps | Per-profile messages-per-second limit and per-key max connections, enforced at the relay |

By default, no data leaves your local network: the relay only talks to the llama servers you list in `config/llama_servers.json`, and those are yours — on this machine or elsewhere on your LAN. (`web_server_relay/index.php` is an optional single-file pure-PHP web relay for internet clients — see its header docs; it is not part of the standard LAN deployment.)

## Who Is This For?

- **Companies that can't send prompts to cloud APIs** — legal, medical, financial and R&D teams whose data must stay on their own infrastructure
- **Teams with existing GPU hardware** — put idle or under-used GPU machines to work as a shared internal AI service; each box you add increases how many people can generate at once
- **Dev teams that want code-writing agents on their own stack** — the agent loop, file tools and Python execution run against your models, not a third-party API
- **Organizations needing per-employee access control** — every connection is authenticated, every user gets exactly the models and tools their role allows, with rate limits and connection caps enforced server-side

The reference hardware for Jack is the **base line: 64 GB RAM with an NVIDIA GeForce RTX 5090 (32 GB VRAM)** GPU. The installer's default model recommendations and context-window sizing are tuned to this machine; weaker GPUs still work — `ZZZ_initial_init.bat` detects your card and picks a Qwen3.8-27B quant that fits its actual VRAM, and any laptop can run the CLIENT (even without GPU). Choosing to not load vision module will give enough space to work with up to 200K context window on 32GB VRAM.

## Repository layout

```
├── code.py                 # SERVER entry point (auth + queue + relay)
├── ZZZ_SERVER.bat          # Windows launcher: auto-creates venv, installs deps, starts SERVER
├── requirements.txt        # All Python dependencies (server + client)
├── config/                 # SERVER configuration (+ .example templates, bootstrapped at startup)
│   ├── .api_keys.json      #    ← your API keys  (gitignored; seeded from .api_keys.example.json)
│   ├── profiles.json       #    ← role profiles  (gitignored; seeded from profiles.example.json)
│   └── llama_servers.json  #    ← LLM backends: local + LAN instances (gitignored; seeded from .example)
├── app/                    # Shared server-side modules
│   ├── providers/          #     llama backend registry, queue integration, auth gateway plumbing
│   ├── server_queue/       #     per-model queues + WorkerPool (one worker per slot)
│   └── auth_gateway/       #     key/profile validation and enforcement
├── CLIENT/
│   ├── code_client.py      # CLIENT entry point (UI + agent + tools)
│   ├── index.html          # Web UI
│   ├── app/chat_bus/       # Per-chat background sessions (parallel generation)
│   └── config/             # Per-machine client settings (gitignored; seeded from .example files)
│       ├── .api_client_keys.json  # ← bookkeeping ONLY (email + date) - the real key is never stored here
│       └── settings.json          # ← which SERVER address(es) to connect to
├── utils/                  # Helper scripts (zzz_init.py, gen_ui_certs.py) + ScreenShot.png
    ├── llama_server/           # Local LLM runtime binaries + models (binaries are NOT in git)
├── web_server_relay/       # Optional single-file pure-PHP internet relay (index.php; not part of the standard LAN deployment)
└── tests/                  # Test suite
```

---

## Installation & Setup Guide

Follow the steps **in order**. After step 6 you have a fully working UI ↔ CLIENT ↔ SERVER ↔ local LLM chain.

> ### TL;DR — quick start (Windows, ~5 minutes)
>
> The whole guide below is detail. For a fresh clone on Windows you only need:
>
> 1. **Download the repo** and double-click **`ZZZ_initial_init.bat`** at the root — it sets everything up in one go (checks Python, picks & downloads a model that fits your GPU, installs llama.cpp, generates the API key into both configs). At the end it prints your **EMAIL + API KEY** right inside its "Next steps" list — keep this window open until you have pasted them into the UI (step 5 below).
> 2. Double-click **`ZZZ_SERVER.bat`** at the root — **leave it running**.
> 3. In the `CLIENT/` folder, double-click **`ZZZ_CLIENT.bat`** — leave it running too.
> 4. Open a browser and go to **https://127.0.0.1:8000/** (the "Not secure" warning is expected — self-signed local certificate).
> 5. In the UI: **gear icon → Settings → Authentication** → paste the EMAIL + API KEY printed at the end of the `ZZZ_initial_init.bat` output and click **Connect**. The page refreshes itself — when the model list appears, start chatting.
> 6. *(Optional)* Point Jack at any repo you like: in the header there is a **Working Folder** — click it and type/paste any directory path to change the working root for all file operations. It starts out as the folder where the code was launched; you can change it at any time from the UI. All tools are restricred to operate only inside working root.

### Fastest path: `ZZZ_initial_init.bat` (recommended for a fresh clone)

On Windows, just **double-click `ZZZ_initial_init.bat`** at the repo root. It checks that you have
Python 3.10+ (and tells you where to get it if not), then runs `utils/zzz_init.py`, which in one go:

- detects your NVIDIA GPU and recommends a Qwen3.8-27B quant that fits its VRAM (you can override);
- downloads the model from HuggingFace (resumable, size-verified) into `llama_server/models/<quant>/`;
- the model files ship with the **MTP** head baked in, so MTP speculative decoding (>2x speedup)
  auto-enables at server start (detected from the GGUF header - no extra file or rename needed);
- optionally downloads the GLM-OCR model (enables `transcribe_image`);
- installs the pinned llama.cpp build **b10441** (or waits while you drop a newer one in by hand);
- generates an API key and writes it into **both** the SERVER and CLIENT config files, rewires all
  profile model/OCR paths to this machine, pre-seeds the startup model, and sets the context window
  to a value your GPU can hold;
- seeds `CLIENT/config/settings.json` with `localhost:8080` plus **this machine's LAN IP** — so the
  whole `CLIENT/` folder is portable: copy it to any other PC on the network, run
  `ZZZ_CLIENT.bat` there and it finds this SERVER automatically (no address editing).

Then run `ZZZ_SERVER.bat` (and `CLIENT\ZZZ_CLIENT.bat`) - first runs self-create their venvs. The
manual steps below describe what that script does, for reference / non-Windows machines.


### Step 1 — Install prerequisites & get the model binaries

1. Clone the repo and install Python 3.10+.
2. Get the `llama_server` binaries — follow **`llama_server/download llama_server files.md`** (~1.3 GB of llama.cpp Windows CUDA release; not shipped in git).
3. Put your model weights into **one folder per model** under `llama_server/models/`, e.g.:

   ```
   llama_server/models/qwen38-27b-q6_k/Dirk-Qwen3.8-27B-UD-Q6_K.gguf
   ```

   The *folder* is the model identity — profiles in step 3 reference these folders by absolute path. (Optional: a separate OCR model folder, e.g. `llama_server/models/glm_ocr/`, enables the `transcribe_image` tool.)

   **Any GGUF works.** Drop any `.gguf` file under `llama_server/models/` (flat or one subfolder deep), add its filename to a profile's `allowed_models`, and start a node with it (the brain's local node or a `ZZZ_LLAMA_NODE.bat` machine). **No model switching**: the model is chosen when the node starts, and requests are routed to the node that serves it.

### Step 2 — Create your API keys (SERVER side)

Open **`config/.api_keys.json`** in the repo root. On first start it is created automatically from the shipped `.api_keys.example.json` template, so a fresh checkout always has the file to edit. It contains one placeholder entry:

```json
[
  {
    "key": "PASTE_YOUR_API_KEY_HERE",
    "email": "your-email@example.com",
    "role": "admin",
    "date_acquired": null,
    "is_active": true,
    "max_connections": 10,
    "last_used": null
  }
]
```

Replace the placeholder with your real entry:

| Field | What to put |
|---|---|
| `key` | Any unique string. Generate one with: `python -c "import secrets; print(secrets.token_hex(32))"` (64 hex chars) |
| `email` | The e-mail you'll use in the HTML UI (step 6) — it is recorded with your key and returned by the SERVER at handshake, so keep both identical |
| `role` | The profile name this user gets — must exist in `config/profiles.json` (step 3). No matching profile = connection rejected |
| `is_active` | `true` to allow login; set `false` to disable a key without deleting it |

Add more entries to the array for more users. The file is **gitignored** — never commit real keys.

### Step 3 — Make profiles (SERVER side)

Profiles live in **`config/profiles.json`**: one JSON object per role name. The `role` field of every key in `.api_keys.json` picks its profile; the SERVER enforces everything from it (models, tools, rate limits). A fresh checkout is seeded with two examples — `admin` and `user`:

```json
{
  "coder": {
    "description": "Coding specialist - code tools and file operations only",
    "allowed_models": [
      "Dirk-Qwen3.8-27B-UD-Q6_K.gguf",
      "gemma-3-27b-Q6_K.gguf"
    ],
    "max_parallel_slots": 2,
    "ocr_model": "C:\\models\\glm_ocr",
    "allowed_tools": [
      "read_file", "write_file", "list_files", "delete_file", "rename_file",
      "move_file", "python_exec", "git_clone", "web_search", "fetch_url"
    ],
    "python_exec_blocked_libs": "blocked_libs_set01.json",
    "max_rate_limit": 90,
    "max_connections": 2
  }
}
```


| Field | Meaning |
|---|---|
| `description` | Free text — shown to humans only |
| `allowed_models` | List of **GGUF filenames** this role may use, or `null` = all models. Names must match what llama reports (`/props.model_path` basename) and what a node actually serves. There are no folders any more — one list, exact case-insensitive filename matching |
| `max_parallel_slots` | **REQUIRED** int ≥ 1. How many generations this user may run **at once across the whole fleet**. Missing/invalid = authentication for that role is denied (no fallback); hot reload picks up a fix |
| `ocr_model` | Absolute path to an OCR model folder (enables `transcribe_image`) or `null` |
| `allowed_tools` | `null` = **all tools allowed**, or an explicit list of tool names. Anything not listed is invisible to that user |
| `python_exec_blocked_libs` | Name of a blocked-libs set file inside `config/`: `blocked_libs_admin.json` (empty = no restrictions) or `blocked_libs_set01.json` (restricted imports). The SERVER ships the right set to each CLIENT automatically |
| `max_rate_limit` | Messages per second for this profile |
| `max_connections` | Simultaneous WebSocket connections allowed per key of this role |

**To create a new profile:** add a new object with its own name (e.g. `"coder"` above), then give people access by adding `.api_keys.json` entries with `"role": "coder"`.

> ⏱️ **No restart needed.** The SERVER re-reads both `config/.api_keys.json` and `config/profiles.json` automatically — changes apply within ~60 seconds (or immediately after the file's modification time changes). Only a *new* model folder may need a fresh UI page load to appear in the model selector.

### Step 4 — Start the SERVER (relay only)

The SERVER is headless — no UI, no tools. It authenticates clients and relays them to the LLM backends listed in `config/llama_servers.json`.

**Windows:** double-click **`ZZZ_SERVER.bat`** from the repo root (or run it from a terminal):

```bat
ZZZ_SERVER.bat                         # headless relay → listens for clients on port 8080 (TLS)
```

The launcher auto-creates `venv/`, installs `requirements.txt` and starts `code.py`. The SERVER then:

1. Bootstraps any missing config files from their `.example` templates (step 2/3 files, if you deleted them).
2. Starts or attaches every llama backend in `config/llama_servers.json` — local instances (`127.0.0.1`) are spawned with their configured `--parallel` slots on port **5000** by default (dedicated OCR instance on **5010** when used); remote LAN entries are attach-only and probed via `/health` + `/props` (model, slots, per-slot context). A **fleet table** logs every node's model, slots, context, build and state.
3. Listens for clients on the WebSocket relay port — default **8080**, TLS always on; a self-signed certificate is auto-generated in `config/` on first start if none exists.

| Port | What | Notes |
|---|---|---|
| 8080 | WebSocket relay, TLS on | the address clients connect to (`IP:8080`) |
| 5000 / 5010 | llama-server / OCR instance (local) | internal, auto-managed by the SERVER; remote backends use whatever ports you configured for them |

**Manual (any OS):**

```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python code.py                                         # headless relay (default provider=coolems_server)
```

> The SERVER keeps running until you stop it (Ctrl+C or close the console). Leave that window open — step 5 runs in its own terminal.

### Step 5 — Start the CLIENT and open the browser UI

The CLIENT is where all intelligence lives: web UI, ChatBus background sessions, agentic loop and tools. It serves an HTML page on **https://127.0.0.1:8000/** (TLS, loopback-only — plain HTTP only if no certificates are present at all) and connects to the SERVER from step 4 over WebSocket (UI → CLIENT → SERVER → LLM).

**Windows:** open a *second* console at the repo root and run **`CLIENT\ZZZ_CLIENT.bat`** (or just double-click that file in Explorer):

```bat
cd CLIENT
ZZZ_CLIENT.bat                         # auto-creates CLIENT/venv, installs deps + Chromium, frees port 8000, starts code_client.py
```

**Manual (any OS):**

```bash
cd CLIENT
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium                            # for browser tools
python code_client.py --port 8000                      # serves the HTML UI on https://127.0.0.1:8000/ when certs exist, else HTTP (loopback-only)
```

Then **open a browser** and go to **https://127.0.0.1:8000/** — this connects your browser to the CLIENT's HTML server on port 8000; from that page every request flows through the SERVER relay you started in step 4.

> 🔒 **Browser says "Not secure"? That is expected and safe.** The UI speaks HTTPS with a self-signed certificate, so browsers flag it as untrusted — nothing to worry about: the address is `127.0.0.1` only (loopback) and no data ever leaves your machine. To get the proper padlock instead of the warning, install the local CA once: run `python utils/gen_ui_certs.py CLIENT/certs` (creates `ca.crt`, `server.crt`, `server.key`) and import `CLIENT/certs/ca.crt` into your browser or OS trust store — step-by-step instructions in [`certs/_readme_how_to_install_cerificates.txt`](./certs/_readme_how_to_install_cerificates.txt). (`ZZZ_initial_init.bat` does all of this automatically on a fresh clone.)

| Port | What | Notes |
|---|---|---|
| 8000 | CLIENT web UI (HTTP/HTTPS) | loopback-only unless `COOLEMS_CLIENT_HOST=0.0.0.0` **and** keys are configured — otherwise startup refuses |

> 💡 **One-command shortcut:** on the same machine, `ZZZ_SERVER.bat provider=llama` (or `python code.py provider=llama`) starts the relay *and* auto-launches this CLIENT subprocess for you — handy for quick local use. The separate flow above is what you do when running a client on another machine or managing it yourself.

### Step 6 — Set the e-mail and API key in the HTML UI

This is what makes the UI actually work with your CLIENT process and SERVER. Do it **once per browser**.

1. Open **https://127.0.0.1:8000/** (plain `http://` only in the certificate-less standalone mode — see the note in step 5).
2. Click the **gear icon** (top right) → **Settings** → **Authentication** tab.
3. On a fresh machine you're in **SETUP MODE**: everything is hidden except the *API Key* field and the **Set API Key** button — by design, nothing else works until a key exists. The CLIENT starts its UI **immediately** (no waiting for the SERVER) and Settings shows live boot status: *Waiting for your API key...* while you're still in setup mode, then "Connecting to SERVER... (attempt N, ~Xs budget left)" once the key is set — no restart needed.
4. Paste the API key you created in step 2 into **API Key** and click **Set API Key**. This:
   - stores the key in your browser (localStorage) — it is NEVER written to disk on this machine, and
   - records `{email, date_acquired}` in `CLIENT/config/.api_client_keys.json` via `POST /api/auth/set-key` (bookkeeping only).

   The real key exists in three places: your browser UI (localStorage), the CLIENT config file `CLIENT/config/.api_client_keys.json` (plaintext single row — written by this dialog and by the init script; no registry writes, works on Windows/macOS/Linux) and the SERVER's `config/.api_keys.json`. Headless CLIENT startup (before any browser is open) authenticates with the key from that config file. An optional `COOLEMS_CLIENT_API_KEY` environment variable acts as a fallback for headless/CI use — the config file takes precedence over it.

   The rest of the Settings UI unlocks immediately — no restart needed.
5. In **Email Address**, enter the e-mail from step 2 — use the same one you recorded next to your key in `config/.api_keys.json`, since that's how the SERVER identifies you (it returns the registered e-mail on a successful handshake). The e-mail is mandatory for every request; without it nothing works. (What actually gates access is the API key — but keep both consistent so logs and status match.)
6. Click **Connect**. A green **"Connected successfully."** means the full chain validated: UI → CLIENT → SERVER WebSocket auth → profile applied. The connection status dot in the header switches from *Disconnected* to connected, and the model selector now lists exactly the models your profile allows.

> 🔁 Where things are stored: the real key lives in your browser localStorage (`coolems_api_key`), in `CLIENT/config/.api_client_keys.json` (exactly **ONE** row `{email, date_acquired, key}` at a time — every re-set replaces all previous rows, single-key contract; plaintext by design since 2026-10-08, no registry writes) and on the SERVER (`config/.api_keys.json`). The optional `COOLEMS_CLIENT_API_KEY` env var is only a fallback (the config file wins). A second machine or a second browser repeats step 6 with its own copy of the key. **Clear Key** in Settings removes the local key and drops you back into setup mode.

### Step 7 — Connect a remote CLIENT (optional): server on a powerful workstation, client on any laptop

**The entire `CLIENT/` folder is self-contained — no installation step.** You can copy it to any other machine on your LAN and run it there as-is. The typical setup: SERVER + llama_server on a **powerful GPU workstation**, CLIENT on **any laptop**. The laptop needs no GPU, no VRAM and no model files — every LLM request flows through the relay to the workstation's local model; the laptop only runs the UI, the agent loop and the tools.

1. On the **workstation** (SERVER side): start headless with `ZZZ_SERVER.bat` and allow **TCP port 8080** in its firewall.
2. On the **laptop**: copy the entire `CLIENT/` folder from the repo (USB stick, network share, or just clone the whole repo — either works). Before copying you may delete `venv/`, `logs/` and optionally `chat_history.db` — the launcher recreates a fresh local venv on first run and re-creates everything else it needs. Then run **`CLIENT\ZZZ_CLIENT.bat`**: it auto-creates `CLIENT/venv`, installs deps + Chromium, frees port 8000 and starts `code_client.py`.
3. **No address setup needed.** `ZZZ_initial_init.bat` already seeded `CLIENT/config/settings.json` with `localhost:8080` plus the workstation's LAN IP — on this PC the client connects to localhost, on any other LAN PC it fails over to that IP automatically (both PCs just need to be on the same network). If you ever change servers manually, edit `server_addresses` in `CLIENT/config/settings.json` (ordered failover list, index 0 tried first) or use **Settings → Server Address** in the UI — a changed address needs a CLIENT restart.
4. Point the working root at a local folder: every workspace (chat) has its OWN working folder, stored per-workspace in `CLIENT/chat_history.db` (`conversations.working_root`) — set it via the **folder chip in the UI header** while that workspace is open. A workspace without its own folder yet falls back to the project root (all file tools are locked into the active workspace's folder).
5. Set API key + e-mail as in step 6 → **Connect**. The laptop now drives Jack on the workstation: models listed are exactly what your profile allows, and all heavy lifting happens where the GPU is.

### Step 8 — Scale out: add GPU nodes (optional)

The SERVER routes each request to a node that serves the requested model. A node is any machine running `llama-server`; the SERVER never starts/stops remote nodes.

1. **On the GPU machine:** clone/copy the repo, bring the `llama_server/` binaries (gitignored, ~1.3 GB) and the model `.gguf` — put it under `llama_server/models/` (the scanner also looks one subfolder deep) or set an absolute path in the node config.
2. Double-click **`ZZZ_LLAMA_NODE.bat`** → **2) Configure this machine**. The wizard asks for node name, port, **concurrency (`--parallel` slots)**, context per slot and model, then writes `config/llama_node.json`:
   - `default` — 1 slot, full context (same as the brain's local node)
   - `h200` — 2 slots, full context per slot
   - `5090` — 1 slot, 64k context
3. **1) Start** (or run `ZZZ_LLAMA_NODE.bat start`). The launcher spawns `llama-server` detached, waits for the model, verifies `/props`, prints the exact entry to add, and exits. llama keeps running. `start` is idempotent, so schedule it every 5 minutes at logon as a free watchdog (`ZZZ_LLAMA_NODE.bat start`).
4. **On the SERVER**, add the printed entry to **`config/llama_servers.json`**:

   ```json
   [
     { "name": "local",   "host": "127.0.0.1",    "port": 5000, "concurrency": 1 },
     { "name": "h200-a",  "host": "192.168.1.60", "port": 5000 },
     { "name": "h200-b",  "host": "192.168.1.61", "port": 5000 }
   ]
   ```

   Remote entries need no `concurrency` — the SERVER reads each node's slot count, model and per-slot context from `/props` every 30 s. Restart the SERVER.
5. **Firewall:** allow the node's port (5000) inbound from the SERVER.

Notes:
- **Models are routed by GGUF filename.** Three Qwen nodes and two Gemma nodes simply appear as two groups; requests go to the group they name. Keep filenames unique across the fleet.
- Nodes must run a llama.cpp build that exposes **`/props`** (the repo ships b10441; `/props` is how the brain learns the model, slot count and per-slot context). A node without `/props` is never routed to.
- A node's concurrency is a **launch flag** (`--parallel`). Change it in the node wizard and restart that node (model reload); the SERVER picks the new count up within 30 s. Context per slot is also per node.
- **No model switching.** Nodes keep the model they started with; the CLIENT's model picker routes to a serving node or reports *"No llama node is serving X"*. The SERVER never reloads a node during serving.
- A full model group waits up to 5 s for a slot; then the client gets a retryable *"Resend request in a min as server is full"* (the rate-limit token is refunded).
- **Firewall:** restrict the node's port to the SERVER's IP. `llama-server` itself has **no authentication** — the SERVER's API keys protect clients, not the raw node port.
- If the brain's own model lives outside `llama_server/models`, the scanner also includes the folder recorded in `.last_model_loaded.json`; otherwise add the folder to `LLAMA_MODEL_DIRS` in `config/config.py`.
- Invalid entries in `llama_servers.json` (missing host, bad port, duplicate `host:port`) are skipped/deduped with a logged warning.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Banner *"No API key set yet - open Settings and set your API key"* | You're in setup mode — the UI starts immediately anyway; Settings shows live boot status. Do step 6 first; nothing else is reachable until then (by design) |
| SERVER log shows *sent empty API key -- rejecting* on every client retry | The CLIENT has no key yet (fresh machine, no key in `CLIENT/config/.api_client_keys.json`): open the CLIENT UI → Settings and set the key. The next bootstrap attempt authenticates automatically; the SERVER rejection is correct behavior |
| Connect says *Your data can't be validated* / 401 | The key isn't found or inactive on the SERVER: check `config/.api_keys.json` for an exact match and `"is_active": true`; check `logs/coolems.log` on the SERVER for the rejection reason |
| *"Authentication failed or no valid profile"* | The key's `role` has no matching entry in `config/profiles.json` — add the profile (step 3) or fix the role name; both files hot-reload, just retry Connect |
| Model selector empty / model missing | Non-admin profiles must list the model **filename** in `allowed_models` (and the SERVER must see the file under `llama_server/models/`, `LLAMA_MODEL_DIRS`, or the persisted model folder). A model served only by a remote node appears once that node is healthy. |
| CLIENT can't reach SERVER at all | Wrong address or firewall: verify the Server Address field (shows *Active* vs *Saved*), confirm port 8080 is open, and remember a changed address needs a CLIENT restart. `CLIENT/config/settings.json` is authoritative |
| A remote backend shows DOWN in the fleet table | The llama-server on that machine isn't running or its firewall blocks the probe: run `ZZZ_LLAMA_NODE.bat status` there. The SERVER only attaches, never starts remote nodes |
| Chat sits at *queued* for a long time | All slots of **that model group** are busy. It starts when one frees (FIFO); add a node for that model or raise its `concurrency` (step 8) |
| *"Resend request in a min as server is full"* | That model group's backlog (32) is full. Wait a minute and resend — nothing is penalized |
| *"No llama node is serving X"* | No configured node runs that model (or all its nodes are offline). Check the fleet table / `profiles.json` `allowed_models`, start the node, or pick another model |
| *"Refusing to bind 0.0.0.0 with no API keys configured"* | You set `COOLEMS_CLIENT_HOST=0.0.0.0` before any key exists — do step 6 first, or unset the variable (loopback default) |
| First start is slow / UI says *Loading models...* | The SERVER waits for llama-server to load the model into VRAM; the CLIENT keeps retrying for up to 30 min by default (`BOOTSTRAP_MAX_WAIT_SEC`). Watch `logs/coolems.log` |

---

## Configuration (summary)

- **SERVER** config lives in `config/` — real values (API keys, profiles, backend list, per-machine state) are **gitignored**; only `.example` templates are tracked and used to bootstrap first runs. See steps 2–3 above for the exact file shapes.
  - **`llama_servers.json`** — the fleet: one entry per llama-server instance: `name`, `host`, `port`, plus `concurrency` for LOCAL (`127.0.0.1`) entries that the SERVER spawns. Remote hosts are attach-only: probed every 30 s via `/health` + `/props` (model, slots, context), never started/stopped. Remote slot counts are discovered, not configured.
  - **`llama_node.json`** — per GPU machine, written by `ZZZ_LLAMA_NODE.bat`: `name`, `model`, `bind`, `port`, `concurrency` (`--parallel`), `ctx_per_slot`, `threads`, `gpu_layers`, `extra_args`. The node launcher uses it; the SERVER never reads it.
  - **`profiles.json`** — per role: `allowed_models` (GGUF **filenames**, `null` = all), **required** `max_parallel_slots` (parallel generations across the whole fleet; missing/invalid = authentication denied), `max_connections`, `max_rate_limit`, tools.
  - **`config.py`** — queue limits: `QUEUE_MAX_PENDING` (32 **per model group**), `QUEUE_FULL_GRACE_SEC` (5 s), `QUEUE_FULL_RETRY_AFTER_SEC` (60 s), `QUEUE_MODEL_WAIT_SEC` (120 s, model configured but all nodes down), `LLAMA_MAX_CONCURRENCY` (8).
- **CLIENT** config lives in `CLIENT/config/` — same pattern: gitignored data files bootstrapped from `.example` templates (`.api_client_keys.json`, `settings.json`). Working roots are per-workspace values in the CLIENT database (`conversations.working_root`) — each workspace is locked into its own folder, with the project root as fallback; change one live via the folder chip in the UI header.
- Bind the CLIENT UI beyond loopback only with `COOLEMS_CLIENT_HOST=0.0.0.0` **and** API keys configured — otherwise startup refuses.

## License

This project is **source-available, not open source**.

| Use | Terms |
|---|---|
| Individual / personal use (download, run, modify, private personal projects) | **Free**, no fee, see [LICENSE](./LICENSE) |
| Company / commercial use (any legal entity, products, services for third parties) | **Requires a written license from Coolems KG** |

Full text: [`LICENSE`](./LICENSE). For company licenses, contact **Coolems KG**.
