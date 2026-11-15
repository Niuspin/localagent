# LocalAgent

**Local-first AI workspace agent powered by local `gguf` + `llama.cpp`, packaged with a standalone desktop window, Claude Code-grade workflow capabilities, frontier MoE inference optimization, and hardened security boundaries.**

All data, sessions, SQLite memory, audit logs, and inference stay strictly on `127.0.0.1` unless you explicitly opt in to web search, web fetch, or external MCP servers.

[![CI](https://github.com/Niuspin/localagent/actions/workflows/ci.yml/badge.svg)](https://github.com/Niuspin/localagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Key Features

### Standalone Desktop UI Window
- **Native Desktop Shell (`lagent/appwindow.py`)**: Launches in a dedicated native desktop window via `pywebview` (Edge WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux), with automatic fallback to Edge/Chrome `--app=` isolated window mode or the system browser (`--browser`).
- **Single-Instance & Clean Lifecycle (`lagent/launcher.py`)**: Closing the desktop window stops the local Starlette/uvicorn server and terminates child `llama-server` processes cleanly.
- **100% Offline Frontend & Strict CSP**: Warm light/dark theme UI (`web/`) with zero external CDNs, fonts, or inline scripts. Includes a welcome canvas, streaming thinking blocks, diff approval cards, session search/grouping, file tree & diff drawer, and `@`/`/` popups.

### Claude Code-Standard Agent Capabilities
- **Slash Command Engine (`lagent/commands.py`)**:
  - Instant & session commands: `/help`, `/status`, `/model`, `/cost`, `/context`, `/compact`, `/clear`, `/undo`, `/doctor`, `/permissions`, `/hooks`, `/plan [task]`, `/mode <ask|auto_edit|plan|auto>`.
  - Prompt workflows: `/init` (generate/update `AGENTS.md`), `/review [files]` (code/security review).
  - Custom project commands loaded from `.localagent/commands/*.md` and `.claude/commands/*.md` with `$ARGUMENTS` expansion.
- **`@file` / `@dir` Context Mentions**: Autocomplete in the composer injects workspace files safely within sandbox boundaries.
- **Granular Permission Rules (`lagent/permissions.py`)**: Configurable `allow` / `ask` / `deny` pattern rules over tools, paths, and commands, layered strictly beneath immutable security boundaries (`PathGuard`, hard-deny shell patterns, sensitive file blocks).
- **Lifecycle Hooks (`lagent/hooks.py`)**: Workspace & user hooks (`PreToolUse`, `PostToolUse`, `Stop`, `UserPromptSubmit`) with safe environment sanitization.
- **Read-Only Subagent (`delegate_task`)**: Isolated read-only exploration subagent for focused codebase analysis without polluting the main conversation.
- **Turn Checkpoints & Rewind (`/undo`)**: Atomic journal snapshots allow rewinding workspace edits made in the last turn via `/undo` or `POST /api/sessions/{sid}/undo`.

### Frontier MoE & Speculative Inference Optimization
- **Hybrid CPU+GPU MoE Planning (`lagent/inference.py`, `lagent/engine.py`)**:
  - Automatic VRAM & KV budget analysis (`kv_budget_analysis`), `--fit on`, or explicit `--n-cpu-moe` / `--cpu-moe` / `-ot` expert offloading with `--moe-cache-mib` and `--moe-cache-cpu-share`.
  - Automatic `--load-mode none` for hybrid CPU-MoE models when system RAM headroom permits, eliminating mmap page-fault overhead during CPU expert routing.
- **Speculative Decoding (`spec_mode: auto | draft | ngram-mod | off`)**:
  - Automatic matching of compatible EAGLE3 / draft `.gguf` models (`-md` + `--spec-type draft-eagle3` / `draft-simple`) when VRAM fits without spilling active layers, plus zero-VRAM `ngram-mod` fallback (`--spec-draft-n-max`, `--spec-draft-n-min`).
- **Batching, Prefix Cache & Telemetry**:
  - Asymmetric batch threading (`--threads-batch`), KV prefix reuse (`--cache-reuse`), MoE-aware `ubatch` scaling, and real-time `TelemetryTracker` (`prompt_tps`, `eval_tps`, `spec_accept_rate`, `cache_hit_rate`).
- **Flag Guarding**: Every flag is checked against `llama-server --help` output before emission so older builds never fail to boot.

### Self-Validating Startup (`start.bat` & `start.sh` + `lagent/bootstrap.py`)
- Verifies Python `>= 3.10`, creates/reuses `.venv`, and syncs `requirements.txt` via `.venv/.req.sha256` hash stamping.
- Runs `python -m lagent.bootstrap --ensure` to detect CPU, RAM, and GPU backends (NVIDIA CUDA, AMD ROCm, Vulkan, Apple Silicon), then downloads, SHA-256 verifies, and safely extracts matching `llama.cpp` release binaries into `bin/`.

---

## Quick Start

### One-Command Launch (Recommended)
- **Windows**: Double-click `start.bat` or run:
  ```bat
  start.bat
  ```
- **Linux / macOS**:
  ```bash
  ./start.sh
  ```

Both scripts automatically validate Python, set up `.venv`, verify/install dependencies, download and verify `llama.cpp` into `bin/` if missing, and open the standalone desktop UI window.

### Manual Launch & CLI Flags
```bash
python run.py                     # Open standalone native desktop window (fallback: Edge/Chrome app -> browser)
python run.py --port 8765         # Bind specific loopback port
python run.py --workspace ./proj  # Pre-select workspace directory
python run.py --load              # Auto-load last configured model on startup
python run.py --browser           # Open in system default browser instead of native window
python run.py --no-browser        # Headless API/UI server mode (prints authenticated loopback URL)
python run.py --version           # Print LocalAgent version
```

Place `.gguf` models (and optional `mmproj` / `eagle3` draft `.gguf` files) inside `models/`.

---

## Repository Layout

```text
localagent/
├── run.py                  # Unified entry point
├── start.bat / start.sh    # Self-validating bootstrap & launch scripts
├── requirements*.txt       # Runtime, dev/test, and PyInstaller build dependencies
├── lagent/                 # Backend core package
│   ├── appwindow.py        # Native pywebview & Edge/Chrome app-mode window shell
│   ├── bootstrap.py        # Dependency check, hardware probe & verified llama.cpp installer
│   ├── commands.py         # Claude Code slash commands & @file/@dir context mentions
│   ├── config.py           # Atomic config persistence, ranges & defaults
│   ├── context.py          # Context budgeting & auto-compaction
│   ├── engine.py           # llama-server lifecycle, planning, flag guarding & streaming
│   ├── gguf.py             # GGUF header parser, MoE detection & draft model pairing
│   ├── hooks.py            # PreToolUse / PostToolUse / Stop / UserPromptSubmit hooks
│   ├── hw.py               # CPU / RAM / GPU / VRAM detection
│   ├── inference.py        # MoE/EAGLE3 advisor, KV budget analysis & TelemetryTracker
│   ├── launcher.py         # Single-instance lock, stdio setup & server orchestration
│   ├── loop.py             # Multi-step agent loop, approvals & checkpoint tracking
│   ├── memory.py           # Persistent SQLite FTS5 long-term memory
│   ├── permissions.py      # Granular allow/ask/deny permission rules
│   ├── security.py         # PathGuard, sensitive file blocks, command policy, SSRF guard
│   ├── server.py           # Starlette/ASGI HTTP & SSE API + strict CSP static serving
│   ├── mcp/                # MCP stdio/HTTP client & tool bridge
│   └── tools/              # File, edit, grep, search, shell, office, web & delegate_task tools
├── web/                    # Offline static UI (index.html, app.css, js/*.js)
├── tests/                  # Unit, integration, inference, bootstrap, Playwright UI & eval tests
├── packaging/              # PyInstaller spec & Windows build helper
├── docs/                   # HTTP/SSE API & real-environment testing docs
└── .github/                # CI workflows, issue templates & PR template
```

---

## Security Model

- **Local-only binding & auth**: Server binds to `127.0.0.1` with a per-launch random token exchanged for an `HttpOnly` cookie, `Host` header check, and strict CSP (`default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'`).
- **Workspace confinement (`PathGuard`)**: Every path is normalized via `os.path.realpath` to reject traversal and symlink escapes. Sensitive files (`.env`, `.git/config`, `~/.ssh`, AWS/GCP/Azure credentials, `config.json`) are denied unconditionally.
- **Command safety & taint tracking**: Destructive shell patterns are hard-denied; untrusted web/document content marks the session tainted so subsequent sensitive operations always require explicit approval even in `auto` mode.
- **SSRF defense**: Rejects non-HTTP(S) schemes and redirects/resolutions into loopback, link-local, RFC1918, or cloud metadata (`169.254.169.254`) endpoints.
- **Verified downloads**: `lagent.bootstrap` verifies SHA-256 checksums and blocks Zip Slip / symlink escapes during archive extraction.

---

## Development & Verification

```bash
# Install dev/test dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run full pytest suite (unit, inference, Claude Code features, bootstrap, MCP, E2E)
python -m pytest tests -q -p no:cacheprovider

# Check frontend JavaScript syntax (requires Node.js)
node --check web/js/*.js

# Run Chromium Playwright UI smoke test (offline & CSP check included)
python tests/ui_smoke.py

# Run real-model regression evaluation (auto-skips if no llama-server or GGUF model)
python tests/eval/run_eval.py
```

---

## License

Released under the [MIT License](LICENSE).
