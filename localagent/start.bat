@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo 未找到 python，请先安装 Python 3.10+ 并勾选 "Add to PATH" & pause & exit /b 1)
if not exist .venv (
  echo [LocalAgent] 创建虚拟环境并安装依赖...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\pip install -q -r requirements.txt || (pause & exit /b 1)
)
if not exist models mkdir models
if not exist bin mkdir bin
if not exist skills mkdir skills
.venv\Scripts\python run.py %*
pause
