@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM ---- 1. 检查系统 Python 3.10+ ----
set "PY_CMD="
where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if not "%PY_CMD%"=="" goto :py_found

:try_python
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
if not "%PY_CMD%"=="" goto :py_found

:no_python
echo ======================================================================
echo [错误] 未找到 Python 3.10 或更高版本！
echo 请先安装 Python 3.10+ 并勾选 "Add to PATH"：
echo   - 方式 1: 在终端运行 winget install Python.Python.3.11
echo   - 方式 2: 前往官网下载 https://www.python.org/downloads/
echo ======================================================================
pause
exit /b 1

:py_found
REM ---- 2. 检查并创建虚拟环境 .venv ----
if exist ".venv\Scripts\python.exe" goto :check_deps
echo [LocalAgent] 创建 Python 虚拟环境...
%PY_CMD% -m venv .venv
if errorlevel 1 (
  echo [错误] 虚拟环境创建失败。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -q --upgrade pip

:check_deps
REM ---- 3. 校验 requirements.txt 哈希与依赖同步 ----
set "NEED_INSTALL=0"
if not exist ".venv\.req.sha256" set "NEED_INSTALL=1"
if "%NEED_INSTALL%"=="1" goto :do_install
.venv\Scripts\python.exe -c "import hashlib, pathlib, sys; p=pathlib.Path('.venv/.req.sha256'); r=pathlib.Path('requirements.txt'); sys.exit(0 if p.exists() and r.exists() and p.read_text('utf-8').strip() == hashlib.sha256(r.read_bytes()).hexdigest() else 1)" >nul 2>nul
if errorlevel 1 set "NEED_INSTALL=1"
if not "%NEED_INSTALL%"=="1" goto :bootstrap

:do_install
echo [LocalAgent] 安装/同步依赖包（requirements.txt）...
.venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络或 pip 配置。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -c "import hashlib, pathlib; pathlib.Path('.venv/.req.sha256').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(), encoding='utf-8')" >nul 2>nul

:bootstrap
REM ---- 4. 自动环境自检与 llama.cpp 下载（可用 LAGENT_SKIP_BOOTSTRAP=1 跳过） ----
set "BOOT_ARGS=--ensure"
for %%A in (%*) do if "%%~A"=="--offline" set "BOOT_ARGS=--ensure --offline"
if "%LAGENT_SKIP_BOOTSTRAP%"=="1" goto :launch
.venv\Scripts\python.exe -m lagent.bootstrap %BOOT_ARGS%
if errorlevel 1 echo [警告] 环境自检发现异常，尝试继续启动...

:launch
REM ---- 5. 确保基础目录存在并启动应用 ----
if not exist models mkdir models
if not exist bin mkdir bin
if not exist skills mkdir skills

.venv\Scripts\python.exe run.py %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo [LocalAgent] 进程已退出（代码: %EXIT_CODE%）
  pause
)
exit /b %EXIT_CODE%
