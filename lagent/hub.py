"""模型与运行时 Hub：依据本机硬件探测推荐适配 GGUF / mmproj / Draft 模型，并提供断点续传下载与 SHA-256 校验。"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import hw, net

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{3,160}\.gguf$", re.I)

# 内置轻量精选模型目录（无外网也可离线展示推荐档位与量化规格）
CATALOG: list[dict[str, Any]] = [
    {
        "id": "qwen2.5-coder-7b-q4km",
        "name": "Qwen2.5-Coder-7B-Instruct (Q4_K_M)",
        "file": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        "size_gb": 4.68,
        "vram_min_gb": 5.5,
        "ram_min_gb": 8.0,
        "params_b": 7.6,
        "moe": False,
        "vision": False,
        "tags": ["代码", "均衡", "单卡/8GB"],
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "url": "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf",
    },
    {
        "id": "qwen3-8b-q4km",
        "name": "Qwen3-8B (Q4_K_M)",
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "size_gb": 5.03,
        "vram_min_gb": 6.0,
        "ram_min_gb": 8.0,
        "params_b": 8.2,
        "moe": False,
        "vision": False,
        "tags": ["思考/推理", "中文强", "8GB+"],
        "repo": "Qwen/Qwen3-8B-GGUF",
        "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
    },
    {
        "id": "qwen3-14b-q4km",
        "name": "Qwen3-14B (Q4_K_M)",
        "file": "Qwen3-14B-Q4_K_M.gguf",
        "size_gb": 9.0,
        "vram_min_gb": 10.5,
        "ram_min_gb": 16.0,
        "params_b": 14.8,
        "moe": False,
        "vision": False,
        "tags": ["高精度", "ToolCall", "12GB+"],
        "repo": "Qwen/Qwen3-14B-GGUF",
        "url": "https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf",
    },
    {
        "id": "qwen3-30b-a3b-q4km",
        "name": "Qwen3-30B-A3B MoE (Q4_K_M)",
        "file": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "size_gb": 18.6,
        "vram_min_gb": 12.0,
        "ram_min_gb": 24.0,
        "params_b": 30.5,
        "moe": True,
        "vision": False,
        "tags": ["MoE 混合加速", "30B 级质效", "CPU+GPU"],
        "repo": "Qwen/Qwen3-30B-A3B-GGUF",
        "url": "https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf",
    },
    {
        "id": "gemma-3-4b-it-q4km",
        "name": "Gemma-3-4B-IT (Q4_K_M)",
        "file": "gemma-3-4b-it-Q4_K_M.gguf",
        "size_gb": 2.49,
        "vram_min_gb": 3.2,
        "ram_min_gb": 6.0,
        "params_b": 4.0,
        "moe": False,
        "vision": True,
        "tags": ["轻量", "视觉可选", "低配/CPU"],
        "repo": "bartowski/google_gemma-3-4b-it-GGUF",
        "url": "https://huggingface.co/bartowski/google_gemma-3-4b-it-GGUF/resolve/main/google_gemma-3-4b-it-Q4_K_M.gguf",
    },
]


def rewrite_mirror(url: str, mirror: str = "official") -> str:
    if mirror == "hf-mirror" and url.startswith("https://huggingface.co/"):
        return "https://hf-mirror.com/" + url[len("https://huggingface.co/"):]
    return url


def recommend_models(hw_info: dict | None = None, models_dir: Path | None = None) -> dict[str, Any]:
    """根据当前硬件显存/内存推荐匹配度最高的模型清单。"""
    h = hw_info or hw.detect()
    gpus = h.get("gpus") or []
    vram_gb = round(sum(float(g.get("vram_gb") or 0) for g in gpus), 2)
    ram_gb = float(h.get("ram_gb") or 8.0)
    installed = {p.name.lower() for p in models_dir.glob("*.gguf")} if (models_dir and models_dir.is_dir()) else set()

    items: list[dict[str, Any]] = []
    for entry in CATALOG:
        e = dict(entry)
        vmin = float(e["vram_min_gb"])
        rmin = float(e["ram_min_gb"])
        if vram_gb >= vmin + 1.5:
            fit, score = "best", 3
        elif vram_gb >= vmin - 0.5 or (e["moe"] and vram_gb >= 6.0 and ram_gb >= rmin):
            fit, score = "good", 2
        elif ram_gb >= rmin:
            fit, score = "cpu_slow", 1
        else:
            fit, score = "too_large", 0
        e["fit"] = fit
        e["score"] = score
        e["installed"] = e["file"].lower() in installed
        items.append(e)

    items.sort(key=lambda x: (-x["score"], -x["params_b"]))
    return {"hardware": {"vram_gb": vram_gb, "ram_gb": ram_gb, "gpu_count": len(gpus)}, "models": items}


class DownloadManager:
    """后台支持 HTTP Range 断点续传与 SHA-256 校验的 GGUF 下载器。"""

    def __init__(self, models_dir: Path, *, on_event: Any = None):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._on_event = on_event
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()

    def _emit(self, info: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event("hub", dict(info))
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def cancel(self, tid: str) -> bool:
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return False
            self._cancelled.add(tid)
            if t.get("status") == "downloading":
                t["status"] = "cancelled"
                t["error"] = "已暂停/取消（保留 .part 断点文件，可随时续传）"
                self._emit(t)
            return True

    def cancel_all(self) -> None:
        with self._lock:
            for tid in list(self._tasks):
                self._cancelled.add(tid)

    def start_download(self, url: str, filename: str, *, mirror: str = "official", expected_sha256: str = "", proxy: str = "") -> dict[str, Any]:
        if os.environ.get("LOCALAGENT_OFFLINE") == "1":
            raise PermissionError("离线模式（--offline）已禁止网络下载")
        fn = Path(filename).name
        if not _SAFE_NAME.match(fn):
            raise ValueError("文件名必须是合法的 .gguf 名称")
        target_url = rewrite_mirror(url.strip(), mirror)
        sp = urlsplit(target_url)
        if sp.scheme not in ("http", "https"):
            raise ValueError("只支持 http/https 下载链接")
        net.check_hostname_literal(sp.hostname or "")

        dest = self.models_dir / fn
        if dest.exists():
            raise FileExistsError(f"模型已存在: {fn}")
        part = self.models_dir / (fn + ".part")
        tid = hashlib.blake2b(fn.encode(), digest_size=6).hexdigest()

        with self._lock:
            self._cancelled.discard(tid)
            existing = self._tasks.get(tid)
            if existing and existing.get("status") == "downloading":
                return dict(existing)
            info = {
                "id": tid,
                "file": fn,
                "url": target_url,
                "status": "downloading",
                "downloaded": part.stat().st_size if part.is_file() else 0,
                "total": 0,
                "pct": 0.0,
                "error": None,
                "started": time.time(),
            }
            self._tasks[tid] = info
        self._emit(info)

        threading.Thread(target=self._worker, args=(tid, target_url, part, dest, expected_sha256, proxy), daemon=True).start()
        return dict(info)

    def _worker(self, tid: str, url: str, part: Path, dest: Path, expected_sha256: str, proxy: str) -> None:
        last_push = 0.0
        try:
            sp = urlsplit(url)
            if not proxy:
                net.resolve_public(sp.hostname or "", sp.port or (443 if sp.scheme == "https" else 80))
            offset = part.stat().st_size if part.is_file() else 0
            hdrs = {"User-Agent": net.UA}
            if offset > 0:
                hdrs["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(url, headers=hdrs)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=30) as resp:
                status = getattr(resp, "status", 200)
                if offset > 0 and status == 200:
                    offset = 0  # 服务端不支持 Range，重头写
                cl = int(resp.headers.get("Content-Length") or 0)
                total = (offset + cl) if (status == 206 and cl) else (cl or 0)
                mode = "ab" if (offset > 0 and status == 206) else "wb"
                with self._lock:
                    self._tasks[tid]["total"] = total
                done = offset
                with open(part, mode) as out:
                    while True:
                        if tid in self._cancelled:
                            return
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        with self._lock:
                            self._tasks[tid]["downloaded"] = done
                            self._tasks[tid]["pct"] = round(100.0 * done / total, 1) if total > 0 else 0.0
                            snap = dict(self._tasks[tid])
                        now = time.monotonic()
                        if now - last_push >= 0.4:
                            last_push = now
                            self._emit(snap)
            if tid in self._cancelled:
                return
            if expected_sha256:
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for c in iter(lambda: f.read(1 << 20), b""):
                        h.update(c)
                if h.hexdigest().lower() != expected_sha256.strip().lower():
                    part.unlink(missing_ok=True)
                    raise ValueError("SHA-256 校验失败，已删除损坏的临时文件")
            os.replace(part, dest)
            with self._lock:
                self._tasks[tid]["status"] = "done"
                self._tasks[tid]["pct"] = 100.0
                snap = dict(self._tasks[tid])
            self._emit(snap)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                if tid in self._cancelled:
                    return
                self._tasks[tid]["status"] = "error"
                self._tasks[tid]["error"] = str(e)
                snap = dict(self._tasks[tid])
            self._emit(snap)
