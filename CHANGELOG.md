# Changelog

All notable changes to LocalAgent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-16 — "Swarm & Mission"

### Added
- **Multi-Agent Swarm Orchestration (`lagent/swarm/`)**:
  - Added DAG task scheduling (`TaskGraph`, 4-level priority `SwarmScheduler`), 7 monotonically constrained role specs (`RoleSpec`), per-swarm hierarchical budget ledger, JSONL `Mailbox`, `FileLeaseManager`, serialized 3-way `MergeQueue` with conflict cards, `SwarmStore` crash recovery, adaptive `engine.slots="auto"` planning, Swarm inspector drawer (`web/js/swarm.js`), and `/api/swarms*` HTTP/SSE APIs.
- **Autonomous Long-Horizon Missions (`lagent/mission/`)**:
  - Added segmented episode runner (`Agent.run_mission_episode`), bidirectional `.localagent/missions/<id>/MISSION.md` sync, deterministic `gates.py` verification, Git branch `mission/<id>` milestone rollback, 4-tier `StallWatchdog`, `MissionPolicy` approval queues, 4D budget tracking, sleep prevention (`power.py`), `--resume-missions` crash recovery, `/mission` commands, and `/api/missions*` HTTP/SSE APIs.
- **Sandboxed Multimodal I/O & Artifacts (`lagent/media/`, `lagent/artifacts.py`)**:
  - Added magic-byte sniffing, `/api/capabilities` negotiation, Win32 Job Object / POSIX `rlimit` isolated ingest workers (EXIF stripping, zip/pixel bomb protection, PDF render & table extraction, Office sanitization, Parquet/CSV/JSONL sampling), SQLite `ArtifactStore`, zero-dependency SVG `make_chart`, sandboxed HTML/PDF/PPTX/table/Zip previews, `bundle_artifacts`, and `md/html/pdf` session export.
- **Compliant Research Engine & Net Ledger (`lagent/research/`)**:
  - Added default-off `ResearchPolicy` + `--offline` socket enforcement, RFC 9309 `RobotsCache`, `DomainLimiter` with `429/503` backoff, conditional `ETag` `WebCache`, `extract.py` injection stripping, `EvidenceStore`, `cite_check` claim verification, `NetLedger`, `PageWatchManager`, `research_*` tools, `/research` & `/net` commands, `/api/research*` & `/api/net/ledger`, and UI panels.
- **UI Design System v2 (`web/css/`, `web/js/`)**:
  - Refactored stylesheets into modular tokens/base/layout/components/themes (zero `transition: all`), added 6 WCAG 2.2 AA themes verified by `tests/ui_contrast.py`, local SVG sprite `web/icons/sprite.svg`, resizable splitters, Focus Mode, View Transitions, and `zh-CN`/`en` i18n.
- **Skill Library v2 (`lagent/skills.py`, `lagent/builtin_skills/`)**:
  - Upgraded to Spec v2 metadata, 38 built-in skills across 7 packs (12 default-enabled core skills, prompt index cap `<= 14`), hybrid keyword/glob/MIME/`local_embed` skill router, `SKILL.compact.md` profiles for small models, monotonic `allowed_tools` narrowing, SHA-256 `skills.lock`, enhanced `scan_warnings`, `python -m lagent.skills lint` CI gate, and pack-filter Skills modal.
- **Local Runtime Observability & Release Hardening (`lagent/metrics.py`)**:
  - Added local-only `GET /api/metrics` aggregator (`steps_per_hour`, `tokens`, `verify_pass_rate`, `stall_count`, `denied_requests`, `merge_conflict_rate`, engine telemetry) and Settings → 运行指标 panel; bumped version to `0.2.0` and updated PyInstaller spec (`packaging/localagent.spec`) and `THIRD_PARTY_LICENSES.md`.
- **Previous Pre-0.2.0 Engine & Workbench Enhancements**:
- **Inference Scheduling, Multi-Role Routing & KV Cache Protection**:
  - Added multi-slot scheduling (`engine.slots`, `_SlotScheduler` foreground priority queue) and `ModelRouter` (`lagent/router.py`) supporting OpenAI-compatible and Anthropic `/v1/messages` compaction endpoints plus external/local embeddings.
  - Added append-only KV prefix cache projection (`project_api_messages` & `advance_prune_watermark` in `lagent/context.py`), `llama-server` `/slots` save/restore checkpoints, keep-alive connection pooling (`lagent/net.py`), multi-GPU `--tensor-split` / `--split-mode`, `--reasoning-format` / `--reasoning-budget`, and `schema_to_gbnf()` fallback.
  - Enhanced `TelemetryTracker` with TTFT (`ttft_ms`), `cache_hit_rate`, `spec_accept_rate`, and automatic low-benefit speculation downgrade.
- **Agent Runtime, Code Intelligence & Hybrid Memory**:
  - Added `git worktree`-isolated `worker` subagent execution with safe unified diff application via `Agent._apply_worktree()` and live `subagent_step` trace streaming.
  - Added `repo_map` (Python AST, multi-language skeleton extractor, and optional Tree-sitter integration), line-hash anchored reads/edits (`anchors=True`, `line_hash`, `start_line`, atomic `edits`), `code_diagnostics` + post-edit checks, and stateful workspace-scoped terminal tracking (`shell_cwd` / filtered `shell_env`).
  - Added `mem_vectors` hybrid retrieval (`local_embed` + external embeddings + FTS5 RRF fusion), background episode distillation, lazy `sessions_index.json` loading, per-session permission mode overrides, and turn-grouped `Journal.checkpoints()`.
- **Cross-Platform Hardening, Full MCP Stack & Model Hub**:
  - Tightened macOS `sandbox-exec` seatbelt read policy to explicit allowlists and added Win32 `Job Object` process-tree & UI hardening (`harden_windows_process`, `release_windows_job`) on Windows.
  - Updated `HookManager` to safely run audited `read_only=True` hooks under `tainted=True` contexts while keeping non-readonly hooks skipped.
  - Added `HttpMcpSession` (Streamable HTTP / SSE), MCP `Resources` / `Prompts` / `Roots` (`roots/list`), `list_changed` SSE broadcasts, and `$defs`/`$ref`/`anyOf`/`oneOf` JSON Schema handling.
  - Added `lagent/hub.py` hardware-aware GGUF recommendations, `hf-mirror` URL rewriting, HTTP `Range` resumable downloads with SHA-256 verification, Linux ROCm binary matching in `lagent/bootstrap.py`, `--offline` mode, and native OS folder picker (`/api/fs/pick`).
- **Offline UI Workbench Upgrades & Expanded Tests**:
  - Updated `web/` with block-level incremental Markdown DOM reconciliation, long-chat windowing, KV cache & TTFT pills, global approval queue bar, post-deny rewrite hints, session permission override menu, HTTP MCP & Model Hub UI panels, native folder picker button, and per-turn checkpoints drawer.
  - Added `tests/test_phase2_3.py` and expanded `tests/eval/run_eval.py` (`multirefactor`, `selfheal`, `redteam_taint`), alongside synchronized `docs/API.md` and `ARCHITECTURE_PROGRESS.md`.

### Fixed
- Resolved `/model`, `/plan`, `/mode` slash command crashes (`Config.set()`, `eng.profile`), recursive `@` file completion, custom Slash commands/rules/hooks UI wiring, `verify` subagent command permissions, attachment preservation on edit/regenerate, multi-turn `truncate` rollback, and `/api/state` `tainted` reporting.

## [0.1.0] - 2026-11-15

### Added
- **True Standalone Desktop UI Window**:
  - Integrated `pywebview` native window support using Edge WebView2 on Windows, WebKit on macOS, and WebKitGTK on Linux.
  - Graceful fallback hierarchy: Native OS window -> Isolated browser app mode (`--app=`) -> System default browser.
  - Single-process lifetime management: closing the native window cleanly shuts down Starlette/uvicorn and terminates active `llama-server` inference processes.
- **Self-Validating & Auto-Installing Bootstrap (`lagent.bootstrap`)**:
  - Automated environment validation: Python runtime (>= 3.10) and 15 core and optional Python dependencies.
  - Dependency integrity caching via `.venv/.req.sha256` stamp comparison.
  - Hardware probing: CPU architecture, threads, RAM, NVIDIA GPU with CUDA driver version parsing, AMD ROCm, Vulkan, and Apple Silicon.
  - Automated `llama.cpp` release asset discovery and installation from GitHub Releases matching system hardware.
  - Security hardening during installation: SHA-256 digest verification, Zip Slip path traversal defense, and symlink escape rejection.
  - Post-install `llama-server --version` verification.
- **Robust Startup Scripts (`start.bat` & `start.sh`)**:
  - Windows `start.bat`: UTF-8 support, automatic Python detection (`py -3` / `python`), `.venv` creation, dependency sync, and preflight bootstrap.
  - Linux/macOS `start.sh`: Shell environment validation, venv dependency hash tracking, and bootstrap execution.
- **Core Security Architecture**:
  - PathGuard workspace boundary fencing against path traversal and symlink escapes.
  - Sensitive file protection preventing access to `.env`, SSH keys, and cloud credentials.
  - Strict command execution policies with hard-deny patterns and taint tracking.
  - SSRF defense blocking loopback, link-local, private IP ranges, and cloud metadata services.
  - Cryptographically chained audit logging for operation auditing and rollback.
