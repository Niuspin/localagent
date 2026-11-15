# Security Policy

LocalAgent prioritizes user privacy and system security. Because LocalAgent can execute local system commands, edit files, and run local AI models, its security boundaries are strictly enforced.

---

## 1. Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

---

## 2. Reporting a Vulnerability

If you discover a security vulnerability in LocalAgent, please do **NOT** open a public issue. Instead, report it responsibly via one of the following channels:

- **Email**: Send details to `security@localagent.dev` (or open a confidential security advisory on GitHub).
- Include detailed reproduction steps, the operating system and environment, and potential impact.
- We will acknowledge receipt of your report within 48 hours and provide a timeline for triage and patch release.

---

## 3. Threat Model & Security Architecture

LocalAgent enforces defense-in-depth across multiple system boundaries:

### 1. Workspace Fencing (`PathGuard`)
- All file reading, writing, and deletion operations requested by the AI model are confined to the designated workspace directory.
- Attempts to escape via directory traversal (`../`), absolute paths, or symlinks pointing outside the workspace are blocked with a `SecurityError`.
- System directories (`/`, `/etc`, `C:\Windows`) and user roots cannot be selected as workspaces.
- Hidden paths (`.git/`, `data/`) and protected skill definitions cannot be overwritten.

### 2. Sensitive File Masking
- Files containing credentials (e.g., `.env`, `id_rsa`, `k.pem`, `.aws/credentials`, `credentials.json`) are blocked from automatic reading or require explicit user confirmation.

### 3. Command Execution & Taint Tracking
- System commands matching destructive patterns (e.g., `rm -rf /`, `mkfs`, raw fork bombs, pipe-to-shell, reverse shells) are permanently denied by `hard_deny_reason`.
- Any command execution that modifies system state requires explicit user confirmation.
- Output from untrusted external sources (such as web fetches) taints the conversation context, automatically escalating read/write/exec operations to require user confirmation.

### 4. Server-Side Request Forgery (SSRF) Defense
- All outbound HTTP requests made by tools validate target hostnames and resolved IP addresses against private, link-local, multicast, and loopback ranges (including `127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`, `169.254.169.254`, `::1`).
- Direct access to cloud metadata services (AWS/GCP/Azure) is strictly blocked.

### 5. Local Authentication & Network Isolation
- LocalAgent binds exclusively to `127.0.0.1` and does not accept remote network connections.
- Each startup session generates a high-entropy ephemeral token exchanged via an HttpOnly cookie to protect against Cross-Site Request Forgery (CSRF) and unauthorized local access.

### 6. Archive Extraction Security
- Installation packages and skill archives are protected against Zip Slip / Tar Slip vulnerabilities and malicious symlink injection before extraction into the file system.
