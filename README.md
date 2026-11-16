# LocalAgent (`v0.2.0` — *"Swarm & Mission"*)

**Local-first AI workspace agent powered by local `gguf` + `llama.cpp`, packaged with a standalone desktop window, multi-agent DAG Swarm orchestration, autonomous long-horizon Missions, sandboxed multimodal I/O & Artifact previews, compliant web Research Engine, UI Design System v2, and a 38-skill Spec v2 library.**

All sessions, SQLite memory, evidence databases, artifacts, audit logs, and inference stay strictly on `127.0.0.1` unless you explicitly enable web search, Research mode (`ask` / `allowlist` / `open`), or external MCP servers. Passing `--offline` guarantees zero outbound network connections at runtime.

[![CI](https://github.com/Niuspin/localagent/actions/workflows/ci.yml/badge.svg)](https://github.com/Niuspin/localagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Key Capabilities (`v0.2.0`)

### 1. Multi-Agent Swarm Orchestration (`lagent/swarm/`)
- **DAG Topology & Priority Scheduling**: `spawn_agent`, `swarm_plan`, `await_agents`, `agent_status`, `cancel_agent`, and `send_message` orchestrated via topological dependencies, cycle rejection, 4-level priority aging queues, and `/api/swarms*` HTTP/SSE streams.
- **Monotonic Role Isolation & MergeQueue**: 7 role presets (`explorer`, `planner`, `coder`, `verifier`, `reviewer`, `researcher`, `summarizer`) that only narrow permissions, isolated `git worktree` authoring branches, serialized 3-way `MergeQueue` with interactive conflict resolution cards, and per-swarm token/step budgets.

### 2. Autonomous Long-Horizon Missions (`lagent/mission/`)
- **Segmented Episodes & `MISSION.md` Sync**: Runs across sessions in bounded episodes with crash recovery (`--resume-missions`), bidirectional `.localagent/missions/<id>/MISSION.md` milestone checklists, deterministic acceptance gates (`pytest` / command checks), Git branch `mission/<id>` milestone rollback, and `/mission` commands.
- **Watchdog, Sleep Prevention & Unattended Policy**: 4-tier `StallWatchdog` (`nudge -> replan -> review -> pause`), 4-dimensional budget tracking (wall/tokens/steps/disk), quiet-hours sleep prevention, and strict allowlist queues blocking unsafe actions for human approval.

### 3. Sandboxed Multimodal I/O & Artifacts (`lagent/media/`, `lagent/artifacts.py`)
- **Subprocess-Isolated Ingestion**: Magic-byte sniffing (`sniff.py`), hardware/backend capability negotiation (`/api/capabilities`), Win32 Job Object / POSIX `rlimit` worker isolation against zip bombs/pixel bombs, EXIF stripping, PDF rendering/table extraction, Office (`docx`/`xlsx`/`pptx`) sanitization, CSV/JSONL/Parquet sampling, and optional offline Whisper transcription.
- **Multi-Format Artifacts & Previews**: SQLite-indexed `ArtifactStore` (`GET /api/artifacts`), zero-dependency SVG `make_chart`, sandboxed HTML previews (default scripts off, `connect-src 'none'`), paginated PDF/table/PPTX previews, `bundle_artifacts` Zip export with `MANIFEST.json`, and `md/html/pdf` session exports.

### 4. Compliant Research Engine & Net Ledger (`lagent/research/`)
- **Default-Off Compliance Gates**: `research.mode = "off"` by default (`ask` / `allowlist` / `open` require explicit opt-in), RFC 9309 `RobotsCache` with `Crawl-delay`, per-domain rate limiting and circuit breaking, conditional `ETag` `WebCache`, outbound credential/query guard, PII redaction, Dual-LLM untrusted reader isolation, `cite_check` claim verification, and full SQLite/CSV `NetLedger` auditability (`/api/net/ledger`).

### 5. UI Design System v2 & Local Observability (`web/`, `lagent/metrics.py`)
- **100% Offline Frontend & Strict CSP**: Modular token/component stylesheets (zero `transition: all`), 6 WCAG 2.2 AA themes (`light`, `dark`, `paper`, `midnight`, `hc`, `colorblind`), bespoke local SVG icon sprite (`web/icons/sprite.svg`), resizable side/inspector splitters, Focus Mode, View Transitions, i18n (`zh-CN` / `en`), and dedicated Swarm / Mission / Artifact / Research / Net Ledger inspector panels.
- **Local Runtime Metrics (`GET /api/metrics`)**: Tracks `steps_per_hour`, token counts, verification pass rate, stall counts, denied tool/network requests, Swarm merge conflict rate, and live `llama-server` telemetry (`ttft_ms`, KV cache hit rate, `eval_tps`) completely offline in Settings → 运行指标.

### 6. Skill Library v2 (`lagent/skills.py`, `lagent/builtin_skills/`)
- **38 Built-in Skills Across 7 Packs** (`core`, `dev`, `research`, `office`, `media`, `ops`, `meta`) with Spec v2 bilingual metadata, `default_enabled_limit` (12 core skills active by default, hard cap `<= 14` in system prompt), hybrid keyword/glob/MIME/`local_embed` auto-routing, `SKILL.compact.md` profiles for smaller models, `allowed_tools` monotonic tightening, SHA-256 `skills.lock` verification, and CI-enforced `python -m lagent.skills lint`.

---

## Quick Start

### One-Command Launch (Recommended)
- **Windows**: `start.bat` (or double-click `LocalAgent.exe` from PyInstaller build)
- **Linux / macOS**: `./start.sh`

Both launchers validate Python (`>= 3.10`), manage `.venv`, sync `requirements.txt`, probe hardware (`NVIDIA CUDA` / `AMD ROCm` / `Vulkan` / `Apple Silicon`), verify/install `llama.cpp` binaries into `bin/`, and launch the standalone desktop window.

### CLI Options
```bash
python run.py                     # Open standalone native desktop window (Edge/Chrome --app or browser fallback)
python run.py --port 8765         # Bind specific loopback port
python run.py --workspace ./proj  # Pre-select workspace directory
python run.py --load              # Auto-load last configured model on startup
python run.py --offline           # Enforce zero outbound network connections (socket-level block)
python run.py --resume-missions   # Resume interrupted autonomous Missions after restart
python run.py --no-browser        # Headless loopback server mode
python run.py --version           # Print LocalAgent version (0.2.0)
```

Place `.gguf` models (and optional `mmproj` / `eagle3` draft `.gguf` files) inside `models/`.

---

## Repository Layout

```text
localagent/
├── run.py                  # Unified entry point
├── start.bat / start.sh    # Self-validating bootstrap & launch scripts
├── packaging/              # PyInstaller spec & Windows executable builder
├── lagent/                 # Backend core package (v0.2.0)
│   ├── swarm/              # Multi-agent DAG orchestration, MergeQueue & budgets
│   ├── mission/            # Long-horizon Missions, Watchdog, Gates & Policy
│   ├── media/              # Sandboxed multimodal ingest, SVG charts, preview & export
│   ├── research/           # Compliant Research Engine, robots, limiter & NetLedger
│   ├── builtin_skills/     # 38 Spec v2 built-in skills across 7 packs + skills.lock
│   ├── mcp/                # MCP stdio/HTTP clients, Resources, Prompts & Roots
│   ├── tools/              # Built-in tools (files, edit, shell, web, swarm, research, etc.)
│   ├── metrics.py          # Local runtime observability aggregator
│   ├── security.py         # PathGuard, AuditLog hash chains, Win32 Job / Seatbelt / bwrap
│   └── server.py           # Starlette/ASGI HTTP & SSE API + strict CSP static serving
├── web/                    # Offline modular UI (css/, js/, icons/, i18n/)
├── tests/                  # Unit, integration, security, Playwright UI & eval suites
├── docs/                   # HTTP/SSE API specification & real-env guides
└── .github/                # CI workflows (lint, contrast, pytest, Playwright, build-exe)
```

---

## Development & Verification

```bash
# Install dev/test dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Validate Skill Library v2 Spec, compact budgets & skills.lock integrity
python -m lagent.skills lint

# Check WCAG 2.2 AA contrast across all 6 UI themes & CSS rules
python tests/ui_contrast.py

# Run full pytest regression suite (M0-M8, security, unit, E2E with mock llama-server)
python -m pytest tests -q -p no:cacheprovider

# Run headless Chromium Playwright UI smoke test (offline & CSP check included)
python tests/ui_smoke.py

# Check frontend JavaScript syntax (requires Node.js)
node --check web/js/*.js web/js/i18n/*.js

# Run real-model regression evaluation (auto-skips if no llama-server / GGUF present)
python tests/eval/run_eval.py
```

---

## License & Security
- Licensed under the [MIT License](LICENSE). See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for bundled and optional dependencies.
- Security boundaries and reporting guidelines are documented in [SECURITY.md](SECURITY.md).
