#!/usr/bin/env bash
# LocalAgent 启动脚本（Linux / macOS）：首次运行自动创建虚拟环境并安装依赖。
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "未找到 python3，请先安装 Python 3.10+"; exit 1; }
if [ ! -d .venv ]; then
  echo "[LocalAgent] 创建虚拟环境并安装依赖…"
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
mkdir -p models bin skills
exec .venv/bin/python run.py "$@"
