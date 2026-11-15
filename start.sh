#!/usr/bin/env bash
# LocalAgent 启动脚本（Linux / macOS）：环境自检、依赖同步与启动。
set -e
cd "$(dirname "$0")"

# ---- 1. 检测 Python 3.10+ ----
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[错误] 未找到 $PY，请先安装 Python 3.10+"
  exit 1
fi

"$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null || {
  echo "[错误] $PY 版本过低，需要 Python 3.10 或更高版本"
  exit 1
}

# ---- 2. 创建虚拟环境 ----
if [ ! -f .venv/bin/python ]; then
  echo "[LocalAgent] 创建 Python 虚拟环境..."
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi

# ---- 3. 依赖哈希检查与自动安装 ----
NEED_INSTALL=0
if [ ! -f .venv/.req.sha256 ]; then
  NEED_INSTALL=1
else
  .venv/bin/python -c "import hashlib, pathlib, sys; p=pathlib.Path('.venv/.req.sha256'); r=pathlib.Path('requirements.txt'); sys.exit(0 if p.exists() and r.exists() and p.read_text('utf-8').strip() == hashlib.sha256(r.read_bytes()).hexdigest() else 1)" 2>/dev/null || NEED_INSTALL=1
fi

if [ "$NEED_INSTALL" = "1" ]; then
  echo "[LocalAgent] 安装/同步依赖包（requirements.txt）..."
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python -c "import hashlib, pathlib; pathlib.Path('.venv/.req.sha256').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(), encoding='utf-8')"
fi

# ---- 4. 环境自检与 llama.cpp 安装 ----
BOOT_ARGS="--ensure"
for arg in "$@"; do
  if [ "$arg" = "--offline" ]; then
    BOOT_ARGS="$BOOT_ARGS --offline"
  fi
done

if [ -z "$LAGENT_SKIP_BOOTSTRAP" ]; then
  .venv/bin/python -m lagent.bootstrap $BOOT_ARGS || {
    echo "[警告] 环境自检发现异常，尝试继续启动..."
  }
fi

# ---- 5. 确保目录并启动 ----
mkdir -p models bin skills
exec .venv/bin/python run.py "$@"
