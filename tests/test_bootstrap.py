"""bootstrap、appwindow 原生窗口、启动器及安全解压的单元测试。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lagent import appwindow, bootstrap
from lagent.launcher import SingleInstance


# ------------------------------------------------------------------ Python 与包检查
def test_check_python_version(monkeypatch):
    ok, ver_str = bootstrap.check_python_version()
    assert ok is True
    assert ver_str.startswith("3.")

    monkeypatch.setattr(sys, "version_info", (3, 9, 7, "final", 0))
    bad_ok, bad_ver = bootstrap.check_python_version()
    assert bad_ok is False
    assert bad_ver == "3.9.7"


def test_check_python_packages_and_stamp(tmp_path):
    req_file = tmp_path / "requirements.txt"
    stamp_file = tmp_path / ".venv" / ".req.sha256"

    req_file.write_bytes(b"starlette>=0.40\n")

    # 未打戳记前
    res = bootstrap.check_python_packages(req_file=req_file, stamp_file=stamp_file)
    assert res["ok"] is True
    assert res["stamp_ok"] is False

    # 写入戳记后
    assert bootstrap.update_requirements_stamp(req_file=req_file, stamp_file=stamp_file) is True
    res2 = bootstrap.check_python_packages(req_file=req_file, stamp_file=stamp_file)
    assert res2["stamp_ok"] is True
    assert res2["requirements_sha"] == hashlib.sha256(b"starlette>=0.40\n").hexdigest()

    # requirements.txt 变更后戳记失效
    req_file.write_bytes(b"starlette>=0.40\nuvicorn>=0.30\n")
    res3 = bootstrap.check_python_packages(req_file=req_file, stamp_file=stamp_file)
    assert res3["stamp_ok"] is False


def test_check_python_packages_missing(monkeypatch):
    orig_import = bootstrap.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "starlette":
            raise ImportError("Mock missing starlette")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(bootstrap.importlib, "import_module", fake_import)
    res = bootstrap.check_python_packages()
    assert res["ok"] is False
    assert "starlette" in res["missing_required"]


# ------------------------------------------------------------------ 硬件探测
def test_detect_hardware(monkeypatch):
    from lagent import hw

    monkeypatch.setattr(
        hw,
        "detect",
        lambda: {
            "os": "windows",
            "arch": "AMD64",
            "cpu_threads": 16,
            "ram_gb": 32.0,
            "ram_free_gb": 16.0,
            "gpus": [{"name": "RTX 4060", "vram_gb": 8.0, "free_gb": 7.0, "vendor": "nvidia"}],
            "unified": False,
        },
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(
            stdout="NVIDIA-SMI 550.54   CUDA Version: 12.4\n", returncode=0
        ),
    )
    monkeypatch.setattr(bootstrap.shutil, "which", lambda x: "C:\\Windows\\nvidia-smi.exe" if "nvidia-smi" in x else None)

    info = bootstrap.detect_hardware()
    assert info["os"] == "windows"
    assert info["arch"] == "x64"
    assert info["has_nvidia"] is True
    assert info["cuda_version"] == 12.4
    assert len(info["nvidia_gpus"]) == 1


# ------------------------------------------------------------------ 本地 llama-server 与模型探测
def test_find_llama_server_and_verify(tmp_path, monkeypatch):
    assert bootstrap.find_llama_server(tmp_path) is None

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    fake_exe = bin_dir / "llama-b12400" / exe_name
    fake_exe.parent.mkdir()
    fake_exe.write_text("#!/bin/sh\necho ok")

    found = bootstrap.find_llama_server(tmp_path)
    assert found == fake_exe

    # 验证测试：mock 成功与失败
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(stdout="version: 1.0.0 (build 12400)\n", returncode=0),
    )
    ok, ver = bootstrap.verify_llama_server(found)
    assert ok is True
    assert "build 12400" in ver

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(stderr="DLL not found\n", returncode=1),
    )
    bad_ok, bad_msg = bootstrap.verify_llama_server(found)
    assert bad_ok is False
    assert "DLL not found" in bad_msg


def test_check_models(tmp_path):
    assert bootstrap.check_models(tmp_path) == []
    m_dir = tmp_path / "models"
    m_dir.mkdir()
    (m_dir / "qwen.gguf").write_bytes(b"GGUF")
    (m_dir / "llama.gguf").write_bytes(b"GGUF")
    (m_dir / "readme.txt").write_text("info")

    models = bootstrap.check_models(tmp_path)
    assert len(models) == 2
    assert "qwen.gguf" in models
    assert "llama.gguf" in models


# ------------------------------------------------------------------ 资产挑选规则
@pytest.fixture
def fake_releases():
    return [
        {
            "tag_name": "v1.0.0-empty",
            "assets": [],  # 没有二进制资产的发行版
        },
        {
            "tag_name": "b12403",
            "assets": [
                {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip", "browser_download_url": "http://x/cudart12.zip"},
                {"name": "cudart-llama-bin-win-cuda-13.5-x64.zip", "browser_download_url": "http://x/cudart13.zip"},
                {"name": "llama-b12403-bin-win-cuda-12.4-x64.zip", "browser_download_url": "http://x/cuda12.zip"},
                {"name": "llama-b12403-bin-win-cuda-13.5-x64.zip", "browser_download_url": "http://x/cuda13.zip"},
                {"name": "llama-b12403-bin-win-vulkan-x64.zip", "browser_download_url": "http://x/vulkan.zip"},
                {"name": "llama-b12403-bin-win-cpu-x64.zip", "browser_download_url": "http://x/cpu.zip"},
                {"name": "llama-b12403-bin-macos-arm64.tar.gz", "browser_download_url": "http://x/mac_arm.tar.gz"},
                {"name": "llama-b12403-bin-macos-x64.tar.gz", "browser_download_url": "http://x/mac_x64.tar.gz"},
                {"name": "llama-b12403-bin-ubuntu-cuda-12.8-x64.tar.gz", "browser_download_url": "http://x/u_cuda.tar.gz"},
                {"name": "llama-b12403-bin-ubuntu-vulkan-x64.tar.gz", "browser_download_url": "http://x/u_vulkan.tar.gz"},
                {"name": "llama-b12403-bin-ubuntu-x64.tar.gz", "browser_download_url": "http://x/u_cpu.tar.gz"},
            ],
        },
    ]


def test_select_release_assets_windows_cuda13(fake_releases):
    hw = {"os": "windows", "arch": "x64", "has_nvidia": True, "cuda_version": 13.4}
    res = bootstrap.select_release_assets(fake_releases, hw)
    assert res is not None
    rel, assets = res
    assert rel["tag_name"] == "b12403"
    names = [a["name"] for a in assets]
    assert "llama-b12403-bin-win-cuda-13.5-x64.zip" in names
    assert "cudart-llama-bin-win-cuda-13.5-x64.zip" in names


def test_select_release_assets_windows_cuda12(fake_releases):
    hw = {"os": "windows", "arch": "x64", "has_nvidia": True, "cuda_version": 12.2}
    res = bootstrap.select_release_assets(fake_releases, hw)
    assert res is not None
    names = [a["name"] for a in res[1]]
    assert "llama-b12403-bin-win-cuda-12.4-x64.zip" in names
    assert "cudart-llama-bin-win-cuda-12.4-x64.zip" in names


def test_select_release_assets_windows_no_nvidia(fake_releases):
    hw = {"os": "windows", "arch": "x64", "has_nvidia": False, "cuda_version": None}
    res = bootstrap.select_release_assets(fake_releases, hw)
    assert res is not None
    names = [a["name"] for a in res[1]]
    assert names == ["llama-b12403-bin-win-vulkan-x64.zip"]


def test_select_release_assets_macos_arm64(fake_releases):
    hw = {"os": "macos", "arch": "arm64", "has_apple": True}
    res = bootstrap.select_release_assets(fake_releases, hw)
    assert res is not None
    assert res[1][0]["name"] == "llama-b12403-bin-macos-arm64.tar.gz"


def test_select_release_assets_linux(fake_releases):
    hw_nv = {"os": "linux", "arch": "x64", "has_nvidia": True, "cuda_version": 12.4}
    res_nv = bootstrap.select_release_assets(fake_releases, hw_nv)
    assert res_nv is not None
    assert "llama-b12403-bin-ubuntu-cuda-12.8-x64.tar.gz" in [a["name"] for a in res_nv[1]]

    hw_cpu = {"os": "linux", "arch": "x64", "has_nvidia": False}
    res_cpu = bootstrap.select_release_assets(fake_releases, hw_cpu)
    assert res_cpu is not None
    assert res_cpu[1][0]["name"] == "llama-b12403-bin-ubuntu-vulkan-x64.tar.gz"


# ------------------------------------------------------------------ 下载与 SHA-256 校验
def test_download_asset_digest_verification(tmp_path, monkeypatch):
    content = b"fake binary package content"
    good_sha = hashlib.sha256(content).hexdigest()

    class FakeResp:
        def __init__(self, data):
            self.stream = io.BytesIO(data)

        def read(self, n=-1):
            return self.stream.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: FakeResp(content))

    # 1. SHA 匹配成功
    asset_good = {
        "name": "test.zip",
        "browser_download_url": "http://example.com/test.zip",
        "digest": f"sha256:{good_sha}",
    }
    dest1 = tmp_path / "test1.zip"
    bootstrap.download_asset(asset_good, dest1, quiet=True)
    assert dest1.is_file()
    assert dest1.read_bytes() == content

    # 2. SHA 伪造或损坏 -> 抛出异常并清理脏文件
    asset_bad = {
        "name": "bad.zip",
        "browser_download_url": "http://example.com/bad.zip",
        "digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    }
    dest2 = tmp_path / "test2.zip"
    with pytest.raises(ValueError, match="SHA-256 摘要校验失败"):
        bootstrap.download_asset(asset_bad, dest2, quiet=True)
    assert not dest2.exists()


# ------------------------------------------------------------------ 安全解压（Zip Slip 防御）
def test_safe_extract_zip_slip_and_symlinks(tmp_path):
    dest_dir = tmp_path / "bin"
    dest_dir.mkdir()

    # 1. 正常 Zip
    normal_zip = tmp_path / "normal.zip"
    with zipfile.ZipFile(normal_zip, "w") as z:
        z.writestr("sub/llama-server.txt", "llama executable")
    extracted = bootstrap.safe_extract(normal_zip, dest_dir)
    assert (dest_dir / "sub" / "llama-server.txt").is_file()
    assert len(extracted) == 1

    # 2. Zip Slip 恶意包（带 ../ 路径穿越）
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as z:
        z.writestr("../evil.txt", "pwned")

    with pytest.raises(ValueError, match="Zip Slip|非法压缩包"):
        bootstrap.safe_extract(evil_zip, dest_dir)
    assert not (tmp_path / "evil.txt").exists()

    # 3. 包含符号链接的包（应安全忽略）
    sym_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(sym_zip, "w") as z:
        z.writestr("normal.txt", "hello")
        zi = zipfile.ZipInfo("sym_target")
        zi.create_system = 3
        zi.external_attr = 0o120777 << 16  # S_IFLNK
        z.writestr(zi, "/etc/passwd")

    bootstrap.safe_extract(sym_zip, dest_dir)
    assert (dest_dir / "normal.txt").is_file()
    assert not (dest_dir / "sym_target").exists()


def test_safe_extract_tar_slip(tmp_path):
    dest_dir = tmp_path / "bin_tar"
    dest_dir.mkdir()

    # 1. 正常 tar.gz
    normal_tar = tmp_path / "normal.tar.gz"
    with tarfile.open(normal_tar, "w:gz") as t:
        ti = tarfile.TarInfo(name="llama-server")
        ti.size = 5
        t.addfile(ti, io.BytesIO(b"llama"))

    bootstrap.safe_extract(normal_tar, dest_dir)
    assert (dest_dir / "llama-server").is_file()

    # 2. 恶意 tar.gz
    evil_tar = tmp_path / "evil.tar.gz"
    with tarfile.open(evil_tar, "w:gz") as t:
        ti = tarfile.TarInfo(name="../escape.txt")
        ti.size = 4
        t.addfile(ti, io.BytesIO(b"evil"))

    with pytest.raises(ValueError, match="Tar Slip|非法压缩包"):
        bootstrap.safe_extract(evil_tar, dest_dir)
    assert not (tmp_path / "escape.txt").exists()


# ------------------------------------------------------------------ 原生窗口与降级测试
def test_appwindow_native_support(monkeypatch, tmp_path):
    # 模拟已安装 pywebview
    fake_webview = MagicMock()
    fake_window = MagicMock()
    fake_webview.create_window.return_value = fake_window

    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    assert appwindow.has_native_window() is True

    # 测试启动原生窗口
    called = []
    fake_webview.start.side_effect = lambda **kw: called.append(kw)

    profile_dir = tmp_path / "browser_data"
    res = appwindow.run_native_window(
        url="http://127.0.0.1:8765/?t=secret",
        profile_dir=profile_dir,
        title="LocalAgent",
        width=1320,
        height=880,
        min_size=(920, 640),
    )
    assert res is True
    fake_webview.create_window.assert_called_once_with(
        title="LocalAgent",
        url="http://127.0.0.1:8765/?t=secret",
        width=1320,
        height=880,
        min_size=(920, 640),
        confirm_close=False,
        text_select=True,
    )
    assert len(called) == 1
    assert called[0]["private_mode"] is False
    assert called[0]["storage_path"] == str(profile_dir)

    # 测试异常时返回 False 以便启动器平滑降级
    fake_webview.start.side_effect = RuntimeError("WebView2 runtime not installed")
    res_fail = appwindow.run_native_window(
        url="http://127.0.0.1:8765/?t=secret",
        profile_dir=profile_dir,
    )
    assert res_fail is False

    # 关闭窗口
    appwindow._active_window = fake_window
    appwindow.close_native_window()
    fake_window.destroy.assert_called_once()
    assert appwindow._active_window is None


def test_appwindow_missing_pywebview(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)
    assert appwindow.has_native_window() is False


# ------------------------------------------------------------------ 单实例锁与 CLI 测试
def test_single_instance_lock(tmp_path):
    lock_file = tmp_path / "test.lock"
    inst1 = SingleInstance(lock_file)
    inst2 = SingleInstance(lock_file)

    assert inst1.acquire() is True
    assert inst2.acquire() is False

    if inst1.fh:
        inst1.fh.close()
    assert SingleInstance(lock_file).acquire() is True


def test_bootstrap_main_check_only(monkeypatch):
    monkeypatch.setattr(bootstrap, "check_python_version", lambda: (True, "3.11.9"))
    monkeypatch.setattr(
        bootstrap,
        "check_python_packages",
        lambda: {"ok": True, "missing_required": [], "missing_optional": [], "installed": ["all"]},
    )
    monkeypatch.setattr(bootstrap, "find_llama_server", lambda *args: Path("bin/llama-server"))
    monkeypatch.setattr(bootstrap, "verify_llama_server", lambda *args: (True, "1.0.0"))

    assert bootstrap.main(["--check-only", "--quiet"]) == 0
