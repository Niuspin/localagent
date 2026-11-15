"""自检与自安装环境引导模块 (Bootstrap)

功能：
1. 检查 Python 运行版本（>=3.10）及关键依赖包完整性与 SHA-256 戳记；
2. 探测硬件能力（CPU 架构、NVIDIA GPU + CUDA 驱动版本、AMD ROCm、Vulkan、Apple Silicon）；
3. 检查 bin/ 下 llama-server，如缺失则自动匹配下载 ggml-org/llama.cpp 适合当前硬件的最新可执行二进制包；
4. 安全校验（SHA-256 摘要、Zip-Slip 路径穿越防御、拒绝恶意符号链接）并验证运行能力；
5. 检查 models/ 模型目录并提供使用建议。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .paths import ROOT

# 必须或推荐的 Python 依赖模块列表：(导入模块名, 分发包名, 是否核心必装)
PYTHON_PACKAGES: list[tuple[str, str, bool]] = [
    ("starlette", "starlette", True),
    ("uvicorn", "uvicorn", True),
    ("bs4", "beautifulsoup4", True),
    ("multipart", "python-multipart", True),
    ("lxml", "lxml", False),
    ("PIL", "pillow", False),
    ("psutil", "psutil", False),
    ("yaml", "pyyaml", False),
    ("openpyxl", "openpyxl", False),
    ("docx", "python-docx", False),
    ("pptx", "python-pptx", False),
    ("pypdf", "pypdf", False),
    ("pdfplumber", "pdfplumber", False),
    ("reportlab", "reportlab", False),
    ("webview", "pywebview", False),
]


# ------------------------------------------------------------------ Python 与依赖检查
def check_python_version() -> tuple[bool, str]:
    """检查当前 Python 是否 >= 3.10。"""
    v = sys.version_info
    ver_str = f"{v[0]}.{v[1]}.{v[2]}"
    return (v[0], v[1]) >= (3, 10), ver_str


def check_python_packages(
    req_file: Path | None = None, stamp_file: Path | None = None
) -> dict[str, Any]:
    """检查依赖模块完整性及 requirements.txt 的 SHA-256 戳记。"""
    missing_required: list[str] = []
    missing_optional: list[str] = []
    installed: list[str] = []

    for mod_name, pkg_name, is_core in PYTHON_PACKAGES:
        try:
            importlib.import_module(mod_name)
            installed.append(pkg_name)
        except Exception:
            if is_core:
                missing_required.append(pkg_name)
            else:
                missing_optional.append(pkg_name)

    req_path = req_file or (ROOT / "requirements.txt")
    stamp_path = stamp_file or (ROOT / ".venv" / ".req.sha256")
    stamp_ok = False
    req_sha = ""

    if req_path.is_file():
        req_sha = hashlib.sha256(req_path.read_bytes()).hexdigest()
        if stamp_path.is_file():
            try:
                stamp_val = stamp_path.read_text("utf-8").strip()
                stamp_ok = (stamp_val == req_sha)
            except OSError:
                stamp_ok = False

    return {
        "ok": len(missing_required) == 0,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "installed": installed,
        "stamp_ok": stamp_ok,
        "requirements_sha": req_sha,
    }


def update_requirements_stamp(
    req_file: Path | None = None, stamp_file: Path | None = None
) -> bool:
    """把当前 requirements.txt 的 SHA-256 写入 .req.sha256 戳记文件。"""
    req_path = req_file or (ROOT / "requirements.txt")
    stamp_path = stamp_file or (ROOT / ".venv" / ".req.sha256")
    if not req_path.is_file():
        return False
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(req_path.read_bytes()).hexdigest()
        stamp_path.write_text(sha, encoding="utf-8")
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 硬件探测
def detect_hardware() -> dict[str, Any]:
    """探测系统架构、CPU、内存与 GPU（NVIDIA/CUDA、AMD ROCm、Apple Silicon 等）。"""
    from . import hw

    hw_info = hw.detect()
    raw_arch = platform.machine()
    arch_lower = raw_arch.lower()
    if "arm64" in arch_lower or "aarch64" in arch_lower:
        arch = "arm64"
    else:
        arch = "x64"

    os_name = hw_info.get("os", "windows")

    # 细化 NVIDIA CUDA 驱动支持版本探测
    has_nvidia = False
    cuda_ver: float | None = None
    nvidia_gpus: list[dict] = []

    for gpu in hw_info.get("gpus", []):
        if gpu.get("vendor") == "nvidia":
            has_nvidia = True
            nvidia_gpus.append(gpu)

    if has_nvidia or shutil.which("nvidia-smi"):
        smi = shutil.which("nvidia-smi")
        if smi:
            try:
                kw: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}
                res = subprocess.run([smi], capture_output=True, text=True, timeout=5, **kw)
                out = res.stdout
                m = re.search(r"CUDA\s+(?:UMD\s+)?Version:\s*([0-9.]+)", out)
                if m:
                    cuda_ver = float(m.group(1))
                    has_nvidia = True
            except Exception:
                pass

    return {
        "os": os_name,
        "arch": arch,
        "raw_arch": raw_arch,
        "cpu_threads": hw_info.get("cpu_threads", os.cpu_count() or 1),
        "ram_gb": hw_info.get("ram_gb", 0.0),
        "ram_free_gb": hw_info.get("ram_free_gb", 0.0),
        "has_nvidia": has_nvidia,
        "cuda_version": cuda_ver,
        "nvidia_gpus": nvidia_gpus,
        "has_amd": any(g.get("vendor") == "amd" for g in hw_info.get("gpus", [])),
        "has_apple": hw_info.get("unified", False) or any(g.get("vendor") == "apple" for g in hw_info.get("gpus", [])),
    }


# ------------------------------------------------------------------ 本地目录与文件检查
def find_llama_server(root: Path = ROOT) -> Path | None:
    """在 bin/ 目录、其子目录及系统 PATH 中查找 llama-server。"""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    bin_dir = root / "bin"

    # 1. 优先 bin/ 根目录
    p = bin_dir / exe
    if p.is_file():
        return p

    # 2. 检查一级与二级子目录（解压常见目录层级）
    for pattern in (f"*/{exe}", f"*/*/{exe}"):
        for found in bin_dir.glob(pattern):
            if found.is_file():
                return found

    # 3. 系统 PATH
    w = shutil.which(exe) or shutil.which("llama-server")
    if w:
        return Path(w)

    return None


def check_models(root: Path = ROOT) -> list[str]:
    """检查 models/ 目录下的 .gguf 模型文件。"""
    m_dir = root / "models"
    if not m_dir.is_dir():
        return []
    try:
        return [f.name for f in m_dir.glob("*.gguf") if f.is_file()]
    except OSError:
        return []


# ------------------------------------------------------------------ GitHub 发行版与资产匹配
def fetch_github_releases(
    repo: str = "ggml-org/llama.cpp", per_page: int = 10, timeout: float = 15.0
) -> list[dict]:
    """从 GitHub Releases API 获取最近的发行版列表。"""
    url = f"https://api.github.com/repos/{repo}/releases?per_page={per_page}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LocalAgent-Bootstrap",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_asset(assets: list[dict], pattern: str) -> dict | None:
    rx = re.compile(pattern, re.IGNORECASE)
    for a in assets:
        if rx.search(a.get("name", "")):
            return a
    return None


def select_release_assets(
    releases: list[dict], hw_info: dict[str, Any]
) -> tuple[dict, list[dict]] | None:
    """根据硬件架构与系统，在发行版列表中挑选最新且带有对应二进制资产的版本。"""
    os_name = hw_info.get("os", "windows")
    arch = hw_info.get("arch", "x64")
    has_nvidia = hw_info.get("has_nvidia", False)
    cuda_ver = hw_info.get("cuda_version")

    for rel in releases:
        assets = rel.get("assets", [])
        if not assets:
            continue

        selected: list[dict] = []

        if os_name == "windows":
            if arch == "x64":
                if has_nvidia:
                    # 1. 如果驱动支持 CUDA 13+，尝试匹配 13.x
                    if cuda_ver and cuda_ver >= 13.0:
                        main_cuda13 = _find_asset(assets, r"^llama-.*-bin-win-cuda-13.*-x64\.zip$")
                        cudart13 = _find_asset(assets, r"^cudart-llama-.*bin-win-cuda-13.*-x64\.zip$")
                        if main_cuda13:
                            selected.append(main_cuda13)
                            if cudart13:
                                selected.append(cudart13)
                            return rel, selected

                    # 2. 尝试 CUDA 12.4
                    main_cuda12 = _find_asset(assets, r"^llama-.*-bin-win-cuda-12\.4-x64\.zip$") or _find_asset(assets, r"^llama-.*-bin-win-cuda-12.*-x64\.zip$")
                    cudart12 = _find_asset(assets, r"^cudart-llama-.*bin-win-cuda-12\.4-x64\.zip$") or _find_asset(assets, r"^cudart-llama-.*bin-win-cuda-12.*-x64\.zip$")
                    if main_cuda12:
                        selected.append(main_cuda12)
                        if cudart12:
                            selected.append(cudart12)
                        return rel, selected

                # 3. Vulkan 或 CPU
                vulkan = _find_asset(assets, r"^llama-.*-bin-win-vulkan-x64\.zip$")
                if vulkan:
                    return rel, [vulkan]
                cpu = _find_asset(assets, r"^llama-.*-bin-win-cpu-x64\.zip$")
                if cpu:
                    return rel, [cpu]

            elif arch == "arm64":
                vulkan = _find_asset(assets, r"^llama-.*-bin-win-vulkan-arm64\.zip$")
                if vulkan:
                    return rel, [vulkan]
                cpu = _find_asset(assets, r"^llama-.*-bin-win-cpu-arm64\.zip$")
                if cpu:
                    return rel, [cpu]

        elif os_name == "macos":
            if arch == "arm64" or hw_info.get("has_apple"):
                mac_arm = _find_asset(assets, r"^llama-.*-bin-macos-arm64\.(tar\.gz|zip)$")
                if mac_arm:
                    return rel, [mac_arm]
            mac_x64 = _find_asset(assets, r"^llama-.*-bin-macos-x64\.(tar\.gz|zip)$")
            if mac_x64:
                return rel, [mac_x64]

        elif os_name == "linux":
            if arch == "x64":
                if has_nvidia:
                    if cuda_ver and cuda_ver >= 13.0:
                        main_c13 = _find_asset(assets, r"^llama-.*-bin-ubuntu-cuda-13.*-x64\.tar\.gz$")
                        cudart13 = _find_asset(assets, r"^cudart-llama-.*bin-ubuntu-cuda-13.*-x64\.tar\.gz$")
                        if main_c13:
                            selected.append(main_c13)
                            if cudart13:
                                selected.append(cudart13)
                            return rel, selected

                    main_c12 = _find_asset(assets, r"^llama-.*-bin-ubuntu-cuda-12.*-x64\.tar\.gz$")
                    cudart12 = _find_asset(assets, r"^cudart-llama-.*bin-ubuntu-cuda-12.*-x64\.tar\.gz$")
                    if main_c12:
                        selected.append(main_c12)
                        if cudart12:
                            selected.append(cudart12)
                        return rel, selected

                vulkan = _find_asset(assets, r"^llama-.*-bin-ubuntu-vulkan-x64\.tar\.gz$")
                if vulkan:
                    return rel, [vulkan]
                cpu = _find_asset(assets, r"^llama-.*-bin-ubuntu-x64\.tar\.gz$")
                if cpu:
                    return rel, [cpu]

            elif arch == "arm64":
                vulkan = _find_asset(assets, r"^llama-.*-bin-ubuntu-vulkan-arm64\.tar\.gz$")
                if vulkan:
                    return rel, [vulkan]
                cpu = _find_asset(assets, r"^llama-.*-bin-ubuntu-arm64\.tar\.gz$")
                if cpu:
                    return rel, [cpu]

    return None


# ------------------------------------------------------------------ 安全下载与解压
def download_asset(
    asset: dict, dest_path: Path, timeout: float = 120.0, quiet: bool = False
) -> Path:
    """下载资产并校验 GitHub 提供的 SHA-256 摘要（digest）。"""
    url = asset.get("browser_download_url")
    if not url:
        raise ValueError("资产缺少 browser_download_url")

    name = asset.get("name", "archive")
    if not quiet:
        print(f"[下载] {name} ...")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()

    req = urllib.request.Request(url, headers={"User-Agent": "LocalAgent-Bootstrap"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        while True:
            chunk = resp.read(128 * 1024)
            if not chunk:
                break
            out.write(chunk)
            hasher.update(chunk)

    computed_sha = hasher.hexdigest().lower()
    digest = asset.get("digest") or ""
    if digest.lower().startswith("sha256:"):
        expected_sha = digest.split("sha256:")[-1].strip().lower()
        if computed_sha != expected_sha:
            try:
                dest_path.unlink()
            except OSError:
                pass
            raise ValueError(f"SHA-256 摘要校验失败: 预期 {expected_sha}, 实际 {computed_sha}")

    return dest_path


def safe_extract(archive_path: Path, dest_dir: Path) -> list[Path]:
    """安全解压 zip 或 tar.gz，严格防御路径穿越（Zip Slip）及恶意符号链接。"""
    dest_resolved = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as z:
            for member in z.infolist():
                fn = member.filename.replace("\\", "/")
                # 检查绝对路径或跨盘符
                if fn.startswith("/") or (len(fn) > 1 and fn[1] == ":") or ".." in fn.split("/"):
                    raise ValueError(f"检测到非法压缩包路径穿越: {member.filename}")
                target = (dest_dir / fn).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise ValueError(f"检测到非法 Zip Slip 路径越界: {member.filename}")
                # 过滤符号链接（Unix 属性 0o120000）
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    continue
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d)
                extracted.append(target)

    elif name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar"):
        with tarfile.open(archive_path, "r:*") as t:
            for member in t.getmembers():
                fn = member.name.replace("\\", "/")
                if fn.startswith("/") or (len(fn) > 1 and fn[1] == ":") or ".." in fn.split("/"):
                    raise ValueError(f"检测到非法压缩包路径穿越: {member.name}")
                target = (dest_dir / fn).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise ValueError(f"检测到非法 Tar Slip 路径越界: {member.name}")
                if member.issym() or member.islnk():
                    link_dest = (target.parent / member.linkname).resolve()
                    if not link_dest.is_relative_to(dest_resolved):
                        raise ValueError(f"符号链接跨越目录限制: {member.linkname}")
                if hasattr(tarfile, "data_filter"):
                    t.extract(member, path=dest_dir, filter="data")
                else:
                    t.extract(member, path=dest_dir)
                extracted.append(target)
    else:
        raise ValueError(f"不支持的压缩包格式: {archive_path.name}")

    return extracted


def verify_llama_server(exe_path: Path) -> tuple[bool, str]:
    """运行 llama-server --version 验证可执行程序及动态链接库是否完整可用。"""
    if not exe_path.is_file():
        return False, f"文件不存在: {exe_path}"

    if os.name != "nt":
        try:
            exe_path.chmod(exe_path.stat().st_mode | 0o755)
        except OSError:
            pass

    try:
        kw: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}
        res = subprocess.run([str(exe_path), "--version"], capture_output=True, text=True, timeout=10, **kw)
        if res.returncode == 0:
            lines = (res.stdout or res.stderr).strip().splitlines()
            ver = lines[0] if lines else "ok"
            return True, ver
        return False, f"退出码 {res.returncode}: {(res.stderr or res.stdout).strip()}"
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------------ 一键安装与环境修复
def install_llama_cpp(
    root: Path = ROOT,
    hw_info: dict[str, Any] | None = None,
    quiet: bool = False,
) -> Path:
    """自动获取最佳匹配的 llama.cpp 发行版并解压安装到 bin/ 目录。"""
    hw = hw_info or detect_hardware()
    if not quiet:
        print("[Bootstrap] 查询 GitHub ggml-org/llama.cpp 最新发行版本...")

    releases = fetch_github_releases(per_page=10)
    match = select_release_assets(releases, hw)
    if not match:
        raise RuntimeError("未在最近的 GitHub Releases 中找到适合当前系统与硬件的 llama.cpp 资产")

    rel, assets = match
    tag = rel.get("tag_name", "unknown")
    if not quiet:
        print(f"[Bootstrap] 匹配版本: {tag}，包含 {len(assets)} 个相关文件")

    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lagent_llama_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for a in assets:
            archive_file = tmp_path / a.get("name", "archive")
            download_asset(a, archive_file, quiet=quiet)
            if not quiet:
                print(f"[解压] {archive_file.name} -> {bin_dir}")
            safe_extract(archive_file, bin_dir)

    server_exe = find_llama_server(root)
    if not server_exe:
        raise RuntimeError("解压完成但未在 bin/ 目录下检测到 llama-server 可执行文件")

    ok, ver = verify_llama_server(server_exe)
    if not ok:
        raise RuntimeError(f"llama-server 验证失败: {ver}")

    if not quiet:
        print(f"[成功] llama-server 安装并验证通过: {ver}")

    return server_exe


# ------------------------------------------------------------------ 主入口与 CLI
def run_bootstrap(
    check_only: bool = False,
    install_llama: bool = False,
    ensure: bool = False,
    offline: bool = False,
    quiet: bool = False,
) -> int:
    """运行自检与环境配置流程。"""
    py_ok, py_ver = check_python_version()
    pkg_status = check_python_packages()
    hw_info = detect_hardware()
    server_path = find_llama_server(ROOT)
    models = check_models(ROOT)

    if not quiet:
        print("\n" + "=" * 62)
        print(" LocalAgent 环境引导与健康自检 (Bootstrap)")
        print("=" * 62)
        print(f" Python 版本:      {py_ver} ({'正常' if py_ok else '过低，需要 3.10+'})")
        missing_req = pkg_status["missing_required"]
        missing_opt = pkg_status["missing_optional"]
        if not missing_req:
            print(f" 核心依赖包:      全部就绪 ({len(pkg_status['installed'])} 个模块)")
        else:
            print(f" 缺少核心依赖:    {', '.join(missing_req)}")
        if missing_opt:
            print(f" 推荐/按需缺失:   {', '.join(missing_opt)} (对应功能将降级)")

        gpu_desc = "无独立显卡 / CPU 模式"
        if hw_info.get("nvidia_gpus"):
            g = hw_info["nvidia_gpus"][0]
            name = g["name"] if g["name"].startswith("NVIDIA") else f"NVIDIA {g['name']}"
            cuda_str = f", CUDA {hw_info['cuda_version']}" if hw_info.get("cuda_version") else ""
            gpu_desc = f"{name} ({g['vram_gb']} GB 显存{cuda_str})"
        elif hw_info.get("has_apple"):
            gpu_desc = "Apple Silicon (Metal 统一内存)"
        elif hw_info.get("has_amd"):
            gpu_desc = "AMD 显卡 (ROCm/Vulkan)"

        print(f" 硬件环境:        {hw_info['os'].capitalize()} {hw_info['arch']} | {hw_info['cpu_threads']} 核 CPU | 内存 {hw_info['ram_gb']} GB")
        print(f" GPU 加速:        {gpu_desc}")

        if server_path:
            s_ok, s_ver = verify_llama_server(server_path)
            print(f" llama-server:    {server_path} ({'可用: ' + s_ver if s_ok else '异常: ' + s_ver})")
        else:
            print(" llama-server:    未安装 (bin/ 目录为空)")

        if models:
            print(f" 模型文件:        已发现 {len(models)} 个模型 ({', '.join(models[:3])}{' 等' if len(models) > 3 else ''})")
        else:
            print(" 模型文件:        models/ 目录下暂无 .gguf 模型")
            print("                  提示: 请将 GGUF 模型文件放入 models/ 目录，例如 Qwen2.5-7B-Instruct-Q4_K_M.gguf")
        print("=" * 62 + "\n")

    # 如果仅检查
    if check_only:
        ready = py_ok and pkg_status["ok"] and (server_path is not None)
        return 0 if ready else 1

    # 尝试自动安装 llama-server
    need_llama = install_llama or (ensure and server_path is None)
    if need_llama:
        if offline or os.environ.get("LAGENT_SKIP_BOOTSTRAP") == "1":
            if not quiet:
                print("[Bootstrap] 当前处于离线模式或已设置跳过，不尝试自动下载 llama.cpp。")
        else:
            try:
                server_path = install_llama_cpp(ROOT, hw_info=hw_info, quiet=quiet)
            except Exception as e:
                print(f"[错误] 自动下载/安装 llama.cpp 失败: {e}", file=sys.stderr)
                if not ensure:
                    return 1

    # 如果核心包齐全，刷新 requirements 戳记
    if pkg_status["ok"]:
        update_requirements_stamp()

    return 0 if py_ok and pkg_status["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LocalAgent 环境自检与依赖引导工具")
    parser.add_argument("--check-only", action="store_true", help="仅执行环境检查，不下载或安装任何组件")
    parser.add_argument("--install-llama", action="store_true", help="强制自动下载并安装适合当前硬件的 llama.cpp")
    parser.add_argument("--ensure", action="store_true", help="确保依赖与 llama.cpp 就绪，缺失时自动下载安装")
    parser.add_argument("--offline", action="store_true", help="离线模式，跳过所有网络请求")
    parser.add_argument("--yes", "-y", action="store_true", help="非交互自动确认")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    args = parser.parse_args(argv)

    return run_bootstrap(
        check_only=args.check_only,
        install_llama=args.install_llama,
        ensure=args.ensure,
        offline=args.offline,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    sys.exit(main())
