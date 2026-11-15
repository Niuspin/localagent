# Changelog

All notable changes to LocalAgent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
