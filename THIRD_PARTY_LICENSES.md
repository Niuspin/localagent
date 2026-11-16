# Third-Party Licenses and Attributions

LocalAgent adheres to a strict local-first, offline-bundle policy: all runtime assets and dependencies must carry permissive or compatible open-source licenses and must be inspectable offline.

> Updated: `2026-11-15` (Baseline established in M0 of v0.2.0 iteration)

---

## 1. Python Runtime Dependencies (`requirements.txt`)

### Core HTTP / Server Stack

| Package | Version Pin | License | Purpose |
|---|---|---|---|
| `starlette` | `>=0.40,<2` | BSD-3-Clause | Local HTTP / SSE application framework |
| `uvicorn` | `>=0.30,<1` | BSD-3-Clause | ASGI server (loopback-only) |
| `python-multipart` | `>=0.0.9,<1` | Apache-2.0 | Multipart file upload parsing |
| `beautifulsoup4` | `>=4.12,<5` | MIT | HTML parsing and extraction |

### Recommended / Graceful-Degradation Stack

| Package | Version Pin | License | Purpose |
|---|---|---|---|
| `lxml` | `>=5.0,<7` | BSD-3-Clause | Fast HTML/XML backend for BeautifulSoup |
| `pillow` | `>=10.0,<13` | MIT-CMU | Image inspection and thumbnail generation |
| `psutil` | `>=5.9,<8` | BSD-3-Clause | Hardware memory/CPU probing and process tree cleanup |
| `pyyaml` | `>=6.0,<7` | MIT | YAML frontmatter parsing for skills/metadata |
| `pywebview` | `>=5.0,<7` | BSD-3-Clause | Native desktop application window |

### Document & Office Processing (Optional / On-Demand)

| Package | Version Pin | License | Purpose |
|---|---|---|---|
| `openpyxl` | `>=3.1,<4` | MIT | `.xlsx` / `.xlsm` workbook reading & writing |
| `python-docx` | `>=1.1,<2` | MIT | `.docx` document reading & writing |
| `python-pptx` | `>=1.0,<2` | MIT | `.pptx` presentation reading & writing |
| `pypdf` | `>=4.0,<7` | BSD-3-Clause | Fallback `.pdf` text extraction |
| `pdfplumber` | `>=0.11,<1` | MIT | Layout- and table-aware `.pdf` extraction |
| `reportlab` | `>=4.0,<6` | BSD-3-Clause | `.pdf` generation |

---

## 2. Development & Build Tooling (`requirements-dev.txt`, `requirements-build.txt`)

| Package | Version Pin | License | Notes |
|---|---|---|---|
| `pytest` | `>=8.0,<10` | MIT | Automated test suite |
| `playwright` | `>=1.45,<2` | Apache-2.0 | Optional headless Chromium UI smoke checks |
| `pyinstaller` | `>=6.6,<7` | GPL-2.0-or-later with Bootloader Exception | Build-time bundler only (special exception permits bundling non-GPL applications) |

---

## 3. Native Inference Engine Runtime (`bin/`, User-Provided or Official Release)

- **llama.cpp (`llama-server`)**: MIT License — Local LLM inference server binary located in `bin/`. Not embedded in source control; users obtain or build binaries independently.

---

## 4. Bundled & Reserved Offline Web Assets (M2 / M6)

No external CDN requests are permitted by LocalAgent CSP (`default-src 'none'; font-src 'self'; script-src 'self'`). Current status under `web/`:

| Asset | Location | License | Status |
|---|---|---|---|
| **SVG Icon Sprite** (`la-ic-*`, bespoke 24×24 strokes) | `web/icons/sprite.svg` | Same as LocalAgent repository | Bundled (M6) |
| **System CJK + Mono Typography Stack** | `web/css/tokens.css`, `web/fonts/README.md` | OS-provided system fonts | Active (M6, zero extra bundle weight) |
| **Inter Variable** (optional offline drop-in subset) | `web/fonts/Inter-Variable.woff2` | SIL Open Font License 1.1 (OFL-1.1) | Optional slot (M6 drop-in documented in `web/fonts/README.md`) |
| **JetBrains Mono** (optional offline drop-in subset) | `web/fonts/JetBrainsMono.woff2` | SIL Open Font License 1.1 (OFL-1.1) | Optional slot (M6 drop-in documented in `web/fonts/README.md`) |

*(Whenever any new font, icon set, WASM runtime, or third-party JS library is added in M1–M8, its exact version, source URL, copyright notice, and full license text or SPDX identifier must be recorded in this file before merge.)*
