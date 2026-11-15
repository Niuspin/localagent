# Contributing to LocalAgent

Thank you for your interest in contributing to LocalAgent! This document outlines our development process, architecture principles, and contribution guidelines.

---

## 1. Core Principles

LocalAgent is designed as a secure, private, offline-first local agent powered by `llama.cpp` and GGUF models. All contributions must uphold these non-negotiable principles:

1. **Offline-First & Privacy**:
   - The user interface must never reference external CDNs, fonts, telemetry, or remote scripts. All assets in `web/` must remain self-contained.
   - Inference runs locally on the user's hardware.
2. **Security-First Architecture**:
   - Any file system operation must be validated by `lagent.security.PathGuard`.
   - Dangerous commands must be rejected by `hard_deny_reason` and tracked for permission tainting.
   - Network tools must validate targets using `lagent.net.check_hostname_literal` to prevent SSRF against internal IP spaces and metadata services.
3. **Single-Process Desktop Lifetime**:
   - When the user closes the desktop application window, all background worker threads and the underlying `llama-server` process must shut down cleanly.

---

## 2. Development Setup

### Prerequisites
- Python 3.10 or higher.
- Git.
- Optional: NVIDIA GPU with CUDA drivers, or Vulkan/Metal compatible hardware.

### Quick Start
Clone the repository and run the setup script:

```bash
# Windows
start.bat

# Linux / macOS
./start.sh
```

Or manually initialize a virtual environment:

```bash
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# Linux / macOS
.venv/bin/pip install -r requirements.txt
```

Verify your environment using the bootstrap module:

```bash
python -m lagent.bootstrap --check-only
```

---

## 3. Running Tests

We use `pytest` for all unit and integration testing. Always run tests before opening a pull request:

```bash
# Windows
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider

# Linux / macOS
.venv/bin/python -m pytest -q -p no:cacheprovider
```

To run targeted tests:

```bash
.venv\Scripts\python.exe -m pytest tests/test_unit.py tests/test_bootstrap.py -q -p no:cacheprovider
```

---

## 4. Code Style & Conventions

- **Code Formatting**: Follow standard PEP 8 conventions. Keep functions modular and concise.
- **Type Annotations**: Use Python 3.10+ type hints (`from __future__ import annotations`, `str | None`, etc.).
- **Comments**: Maintain the existing dual Chinese and English comment style where domain logic is documented.
- **Dependencies**: Keep runtime dependencies minimal. If introducing a new dependency, provide a graceful fallback when it is absent.

---

## 5. Submitting Changes

1. Fork the repository and create a branch for your feature or bug fix:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. Commit your changes with clear, descriptive commit messages.
3. Ensure all tests pass.
4. Open a Pull Request referencing any relevant issues, following the provided PR template.
