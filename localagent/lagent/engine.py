"""llama.cpp 引擎管理：发现 llama-server、探测参数能力、按硬件规划（MoE 专家层 CPU/GPU 分配、KV、mmproj）、
启动/停止/重启、健康检查、崩溃与 OOM 诊断。

设计要点
- 参数能力来自 `llama-server --help` 实时探测，而不是写死版本号：旧版缺失的参数自动回退（如 --n-cpu-moe → -ot 正则）。
- 引擎只监听 127.0.0.1 随机端口，且每次启动生成随机 --api-key，关闭自带 WebUI（缩小攻击面）。
- 用户的 extra_args 经过黑名单过滤，不能改监听地址/端口/密钥/静态目录/远程下载。
- 子进程随主进程退出（Linux PDEATHSIG / Windows Job Object / atexit）。
"""
from __future__ import annotations

import asyncio
import collections
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import gguf, hw, net
from .config import Config

_BLOCKED_FLAGS = {"--host", "--port", "--api-key", "--api-key-file", "--webui", "--ui", "--path", "--api-prefix", "--slot-save-path",
                  "--media-path", "--models-dir", "--models-preset", "--props", "-hf", "--hf-repo", "-hff", "--hf-file", "-mu",
                  "--model-url", "-hft", "--hf-token", "--ssl-key-file", "--ssl-cert-file", "--tools", "--webui-config", "--webui-config-file",
                  "-m", "--model", "-mm", "--mmproj", "-c", "--ctx-size", "-np", "--parallel"}
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
GiB = 2 ** 30

OOM_HINT = ("显存/内存不足。可尝试：① 设置里把上下文调小；② 选择更低量化的模型；③ 保持“MoE 专家层上 CPU”为自动或手动调大；"
            "④ 关闭 mmproj 的 GPU 卸载；⑤ KV 缓存类型改为 q8_0。")


class EngineError(Exception):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_tree(proc: Any) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _pdeathsig() -> None:  # 在子进程中执行（仅 Linux）
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass
    os.setsid()


_job_handle: Any = None


def _win_attach_job(pid: int) -> None:
    global _job_handle
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]

        class BASIC(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("c", ctypes.c_size_t),
                        ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IOC), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t)]

        if _job_handle is None:
            _job_handle = k.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k.SetInformationJobObject(_job_handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        h = k.OpenProcess(0x1F0FFF, False, pid)
        k.AssignProcessToJobObject(_job_handle, h)
    except Exception:  # noqa: BLE001
        pass


class Engine:
    def __init__(self, cfg: Config, models_dir: Path, bin_dir: Path, on_event: Callable[[str, dict], None] | None = None):
        self.cfg, self.models_dir, self.bin_dir = cfg, Path(models_dir), Path(bin_dir)
        self.emit = on_event or (lambda e, d: None)
        self.status = "stopped"
        self.error: str | None = None
        self.proc: Any = None
        self.port = 0
        self.api_key = ""
        self.model: dict | None = None
        self.mmproj_file: str | None = None
        self.ctx = 0
        self.ctx_cap = 0
        self.vision = False
        self.plan_info: dict = {}
        self.log: collections.deque[str] = collections.deque(maxlen=600)
        self.tps = 0.0
        self.binary: Path | None = None
        self.binary_version: str | None = None
        self._help: dict[str, str] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._last: dict = {}
        self._ready_evt = asyncio.Event()
        self.auto_restarts = 0

    # ------------------------------------------------------------ 发现与能力
    def find_binary(self) -> Path | None:
        exe = "llama-server.exe" if os.name == "nt" else "llama-server"
        want = (self.cfg.get("engine", "binary", default="") or "").strip()
        cands: list[Path] = []
        if want:
            p = Path(want).expanduser()
            cands.append(p / exe if p.is_dir() else p)
        cands.append(self.bin_dir / exe)
        if self.bin_dir.is_dir():
            cands += sorted(self.bin_dir.glob(f"*/{exe}")) + sorted(self.bin_dir.glob(f"*/*/{exe}"))
        w = shutil.which("llama-server")
        if w:
            cands.append(Path(w))
        for c in cands:
            try:
                if c.is_file() and (os.access(c, os.X_OK) or os.name == "nt"):
                    return c.resolve()
            except OSError:
                continue
        return None

    def _run_probe(self, args: list[str]) -> str:
        assert self.binary
        env = self._child_env()
        try:
            kw: dict[str, Any] = {"creationflags": 0x08000000} if os.name == "nt" else {}
            r = subprocess.run([str(self.binary), *args], capture_output=True, text=True, timeout=30, env=env, **kw)
            return (r.stdout or "") + "\n" + (r.stderr or "")
        except Exception as e:  # noqa: BLE001
            return f"__error__ {e}"

    def probe(self) -> None:
        self.binary = self.find_binary()
        self.binary_version = None
        if not self.binary:
            return
        key = f"{self.binary}:{self.binary.stat().st_mtime_ns}"
        if key not in self._help:
            self._help = {key: self._run_probe(["--help"])}
        v = self._run_probe(["--version"])
        m = re.search(r"version:\s*(\S+)(?:\s*\(([0-9a-f]+)\))?", v)
        self.binary_version = (f"b{m.group(1)}" if m and m.group(1).isdigit() else (m.group(1) if m else None))
        self._help_text = next(iter(self._help.values()))

    def has(self, flag: str) -> bool:
        return bool(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", getattr(self, "_help_text", "")))

    def _flash_takes_value(self) -> bool:
        return bool(re.search(r"(--flash-attn|-fa)[^\n]{0,40}\[on\|off\|auto\]", getattr(self, "_help_text", "")))

    # ------------------------------------------------------------ 模型选择
    def scan(self) -> tuple[list[dict], list[dict]]:
        return gguf.scan(self.models_dir)

    def _pick(self, file: str | None) -> tuple[dict, list[dict]]:
        models, mmprojs = self.scan()
        if not models:
            raise EngineError("models/ 目录里没有 .gguf 模型文件")
        want = file or self.cfg.get("engine", "model", default="")
        m = next((x for x in models if x["file"] == want), None) if want else None
        if want and not m:
            raise EngineError(f"找不到模型 {want}（只能选择 models/ 目录内的文件）")
        return m or models[0], mmprojs

    # ------------------------------------------------------------ 规划
    def plan(self, model_file: str | None = None, mmproj: str | None = None, ctx: Any = None) -> dict:
        e = self.cfg.get("engine")
        info, mmprojs = self._pick(model_file)
        mm_opt = mmproj if mmproj is not None else e["mmproj"]
        mm_path: Path | None = None
        mm_size = 0.0
        if mm_opt == "none":
            pass
        elif mm_opt == "auto":
            if info.get("mmproj"):
                mm_path = self.models_dir / info["mmproj"]
        else:
            q = next((x for x in mmprojs if x["file"] == mm_opt), None)
            if not q:
                raise EngineError(f"找不到 mmproj 文件 {mm_opt}")
            mm_path = Path(q["path"])
        if mm_path:
            mm_size = mm_path.stat().st_size / GiB
        h = hw.detect()
        gpus = h["gpus"]
        has_gpu = bool(gpus)
        gpu_free = sum(g["free_gb"] for g in gpus) * (0.95 if len(gpus) > 1 else 1.0)
        reserve = e["vram_reserve_mb"] / 1024
        budget = max(0.0, gpu_free - reserve) if has_gpu else 0.0
        S = info["size_bytes"] / GiB
        n_layers = int(info.get("n_layers") or 32)
        notes: list[str] = []

        # ---- KV 缓存类型
        kvt = e["kv_cache_type"]
        kvtok = int(info.get("kv_per_tok") or 0)
        scale = {"f16": 1.0, "q8_0": 0.53, "q4_0": 0.28}

        # ---- 权重分布（MoE 精确到每层专家大小）
        lay = gguf.tensor_layout(info["path"]) if info["moe"] else None
        if info["moe"]:
            if lay and lay["expert_per_layer"]:
                exp_layers = lay["expert_per_layer"]
                dense = lay["dense"] / GiB
                total_exp = sum(exp_layers.values()) / GiB
            else:
                dense, total_exp = S * 0.08, S * 0.92
                exp_layers = {i: total_exp * GiB / n_layers for i in range(n_layers)}
                notes.append("无法精确读取张量表（分片或解析失败），专家层大小按经验比例估算。")
            avg_exp = total_exp / max(1, len(exp_layers))
        else:
            dense, total_exp, avg_exp, exp_layers = S, 0.0, 0.0, {}

        compute = 0.55 + (0.35 if mm_path else 0.0) + e["ubatch"] / 512 * 0.1
        mm_gpu = mm_size + 0.4 if (mm_path and has_gpu and e["mmproj_offload"] is not False) else 0.0

        def kv_gib(tokens: int, t: str) -> float:
            return kvtok * scale[t] * tokens / GiB

        # ---- 上下文上限（按内存能承受的 KV）
        train = int(info.get("ctx_train") or 32768)
        if has_gpu:
            kv_room = budget - compute - mm_gpu - (dense if info["moe"] else min(S, budget * 0.5))
            if not info["moe"]:
                kv_room = max(kv_room, budget * 0.25)
        else:
            kv_room = h["ram_free_gb"] - S - 2.0
        kv_room = max(kv_room, 0.5)
        auto_kv = "f16"
        if kvt == "auto":
            if kvtok and kv_gib(e["ctx_start"], "f16") > 0.3 * max(budget if has_gpu else h["ram_free_gb"], 1):
                auto_kv = "q8_0"
            kvt_eff = auto_kv
        else:
            kvt_eff = kvt
        per_tok = kvtok * scale[kvt_eff] if kvtok else 0
        mem_cap = int(kv_room * GiB / per_tok) if per_tok else train
        cap = max(4096, min(train, e["ctx_max"], mem_cap) // 1024 * 1024)
        c = ctx if ctx is not None else e["ctx"]
        # 配置是“自动”且允许增长时，即使本次是显式数值（自动扩容重启）也保留继续增长的空间
        grow_ok = e["ctx"] == "auto" and bool(e["ctx_auto_grow"])
        if c == "auto":
            ctx_eff = max(4096, min(e["ctx_start"], cap))
        else:
            ctx_eff = int(c)
            if ctx_eff > train:
                notes.append(f"所设上下文 {ctx_eff} 超过模型训练长度 {train}，长文本质量可能下降。")
            cap = max(cap, ctx_eff)
        if not grow_ok:
            cap = ctx_eff

        # ---- GPU/CPU 放置
        kv_need = kv_gib(ctx_eff, kvt_eff)
        fixed = compute + mm_gpu + kv_need
        ngl: Any = "all"
        n_cpu_moe = 0
        gpu_w = 0.0
        if not has_gpu:
            ngl, gpu_w = 0, 0.0
            if S > h["ram_gb"] * 0.9:
                notes.append("模型大于物理内存，将依赖 mmap 从磁盘分页，速度会很慢。")
        elif info["moe"]:
            if fixed + dense + total_exp <= budget:
                n_cpu_moe, gpu_w = 0, dense + total_exp
            else:
                avail = budget - fixed - dense
                if avail <= 0:
                    n_cpu_moe, gpu_w = len(exp_layers), max(0.0, dense)
                    notes.append("显存连非专家部分(注意力/嵌入)加 KV 都放不下：建议减小上下文或换更低量化。")
                else:
                    on_gpu = int(avail // avg_exp) if avg_exp else 0
                    n_cpu_moe = max(0, len(exp_layers) - on_gpu)
                    gpu_w = dense + min(on_gpu, len(exp_layers)) * avg_exp
        else:
            if S + fixed <= budget:
                gpu_w = S
            else:
                per_layer = S / (n_layers + 1)
                n = int(max(0.0, budget - fixed) / per_layer) if per_layer else 0
                ngl = max(0, min(n_layers, n))
                gpu_w = per_layer * ngl
                if ngl == 0:
                    notes.append("显存不足以放下任何层，将纯 CPU 推理。")
        cpu_w = max(0.0, S - gpu_w)
        if has_gpu and cpu_w > h["ram_free_gb"] * 0.9 + 0.5:
            notes.append(f"CPU 侧需要约 {cpu_w:.1f} GB 内存，而可用约 {h['ram_free_gb']:.1f} GB，可能触发换页导致变慢。")
        if info["moe"] and has_gpu and n_cpu_moe > 0:
            notes.append(f"MoE 优化：注意力/共享层与 {len(exp_layers) - n_cpu_moe} 层专家放 GPU，前 {n_cpu_moe} 层专家放 CPU（--n-cpu-moe）。")
        if mm_path:
            notes.append(f"已启用视觉：{mm_path.name}（{mm_size:.2f} GB，{'GPU' if mm_gpu else 'CPU'} 运行）。")
        elif info.get("vision") is False and "vl" in info["file"].lower():
            notes.append("文件名像是多模态模型，但 models/ 里没找到匹配的 mmproj-*.gguf，将只能处理文字。")
        fa_mode = e["flash_attn"]
        fa_eff = "on" if (fa_mode == "auto" and kvt_eff != "f16" and has_gpu) else fa_mode
        threads = e["threads"] or max(1, (_physical_cores() or h["cpu_threads"]))
        summary = (f"{info['name']} · {info['quant']} · {S:.1f} GB" + (" · MoE %s专家/激活%s" % (info["experts"], info["experts_used"]) if info["moe"] else "")
                   + f" → ctx {ctx_eff}" + (f"(可增至 {cap})" if cap > ctx_eff else "") + f" · KV≈{kv_need:.1f} GB({kvt_eff})"
                   + (f" · GPU≈{gpu_w + fixed:.1f}/{budget:.1f} GB" if has_gpu else " · 纯 CPU") + f" · CPU 权重≈{cpu_w:.1f} GB")
        return {"summary": summary, "model": info["file"], "mmproj": mm_path.relative_to(self.models_dir).as_posix() if mm_path and self.models_dir in mm_path.parents else (mm_path.name if mm_path else None),
                "mmproj_path": str(mm_path) if mm_path else None, "ctx": ctx_eff, "ctx_cap": cap, "ctx_train": train,
                "ngl": ngl, "n_cpu_moe": n_cpu_moe, "moe": info["moe"], "kv_type": kvt_eff, "flash_attn": fa_eff, "threads": threads,
                "kv_mb": int(kv_need * 1024), "gpu_gb": round(gpu_w + fixed, 2), "cpu_gb": round(cpu_w, 2), "budget_gb": round(budget, 2),
                "mmproj_offload": bool(mm_gpu) if mm_path else False, "notes": notes, "model_path": info["path"], "n_exp_layers": len(exp_layers)}

    # ------------------------------------------------------------ 参数构建
    def build_args(self, plan: dict) -> list[str]:
        assert self.binary
        e = self.cfg.get("engine")
        a = [str(self.binary), "-m", plan["model_path"], "--host", "127.0.0.1", "--port", str(self.port), "--api-key", self.api_key,
             "-c", str(plan["ctx"]), "-np", "1"]
        if self.has("--no-webui"):
            a.append("--no-webui")
        if self.has("--jinja"):
            a.append("--jinja")
        if self.has("--reasoning-format"):
            a += ["--reasoning-format", "deepseek"]
        if self.has("--no-context-shift"):
            a.append("--no-context-shift")
        if plan.get("mmproj_path"):
            a += ["--mmproj", plan["mmproj_path"]]
            if not plan["mmproj_offload"] and self.has("--no-mmproj-offload"):
                a.append("--no-mmproj-offload")
            if e["image_max_tokens"] and self.has("--image-max-tokens"):
                a += ["--image-max-tokens", str(e["image_max_tokens"])]
        elif self.has("--no-mmproj"):
            a.append("--no-mmproj")
        # GPU 放置
        manual = e["gpu_layers"] != "auto" or e["n_cpu_moe"] != "auto"
        use_fit = self.has("--fit") and not manual
        if use_fit:
            a += ["--fit", "on"]
            if self.has("--fit-target"):
                a += ["--fit-target", str(int(e["vram_reserve_mb"]))]
        else:
            if self.has("--fit") and manual:
                a += ["--fit", "off"]
            ngl = plan["ngl"] if e["gpu_layers"] == "auto" else e["gpu_layers"]
            if ngl == "all":
                ngl = 999
            a += ["-ngl", str(ngl)]
            ncm = plan["n_cpu_moe"] if e["n_cpu_moe"] == "auto" else int(e["n_cpu_moe"])
            if plan["moe"] and ncm > 0:
                if self.has("--n-cpu-moe"):
                    a += ["--n-cpu-moe", str(ncm)]
                elif self.has("--override-tensor"):
                    layers = "|".join(str(i) for i in range(ncm))
                    a += ["-ot", rf"blk\.({layers})\.ffn_.*_exps\.=CPU"]
        fa = plan["flash_attn"]
        if fa in ("on", "off") and self.has("--flash-attn"):
            a += (["-fa", fa] if self._flash_takes_value() else (["-fa"] if fa == "on" else []))
        if plan["kv_type"] != "f16":
            a += ["-ctk", plan["kv_type"], "-ctv", plan["kv_type"]]
        a += ["-t", str(plan["threads"]), "-b", str(e["batch"]), "-ub", str(e["ubatch"])]
        if e["cache_ram_mb"] != 0 and self.has("--cache-ram"):
            a += ["--cache-ram", str(e["cache_ram_mb"])]
        a += self._safe_extra(e["extra_args"])
        return a

    @staticmethod
    def _safe_extra(extra: list[str]) -> list[str]:
        out: list[str] = []
        toks: list[str] = []
        for x in extra:
            try:
                toks += shlex.split(x)
            except ValueError:
                toks.append(x)
        skip_val = False
        for t in toks:
            if skip_val:
                skip_val = False
                continue
            name = t.split("=", 1)[0]
            if name in _BLOCKED_FLAGS:
                skip_val = "=" not in t
                continue
            out.append(t)
        return out

    @staticmethod
    def display_command(args: list[str]) -> str:
        shown = list(args)
        for i, x in enumerate(shown[:-1]):
            if x == "--api-key":
                shown[i + 1] = "****"
        return " ".join(shlex.quote(x) for x in shown)

    def _child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k) and not k.upper().endswith("_PROXY")}
        if self.binary:
            d = str(self.binary.parent)
            for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
                env[var] = d + os.pathsep + env.get(var, "")
        return env

    # ------------------------------------------------------------ 状态
    def state(self) -> dict:
        ls = list(self.log)[-40:]
        return {"status": self.status, "model": self.model["file"] if self.model else None, "mmproj": self.mmproj_file,
                "ctx": self.ctx, "ctx_max": self.ctx_cap, "vision": self.vision, "error": self.error,
                "binary": str(self.binary) if self.binary else None, "binary_version": self.binary_version,
                "load_log_tail": ls, "plan": self.plan_info, "tps": round(self.tps, 1)}

    def _set(self, status: str, error: str | None = None) -> None:
        self.status, self.error = status, error
        if status == "ready":
            self._ready_evt.set()
        else:
            self._ready_evt.clear()
        self.emit("engine", self.state())

    # ------------------------------------------------------------ 生命周期
    async def start(self, model: str | None = None, mmproj: str | None = None, ctx: Any = None) -> None:
        async with self._lock:
            await self._stop_locked()
            self.error = None
            self.probe()
            if not self.binary:
                self._set("error", "没有找到 llama-server。请把 llama.cpp 发行包解压到 bin/ 目录，或在设置里指定路径。")
                raise EngineError(self.error or "")
            try:
                plan = self.plan(model, mmproj, ctx)
            except EngineError as e:
                self._set("error", str(e))
                raise
            self._last = {"model": plan["model"], "mmproj": mmproj, "ctx": ctx}   # 记录“请求值”，None 表示按配置自动
            self.port, self.api_key = _free_port(), secrets.token_urlsafe(24)
            self.model = next(m for m in self.scan()[0] if m["file"] == plan["model"])
            self.mmproj_file, self.ctx, self.ctx_cap, self.plan_info = plan["mmproj"], plan["ctx"], plan["ctx_cap"], plan
            self.vision = False
            self.log.clear()
            args = self.build_args(plan)
            self.plan_info["command"] = self.display_command(args)
            self.log.append("$ " + self.plan_info["command"])
            self._set("starting")
            kw: dict[str, Any] = {}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000 | 0x00000200
            else:
                kw["preexec_fn"] = _pdeathsig if sys.platform.startswith("linux") else os.setsid
            try:
                self.proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.binary.parent), env=self._child_env(),
                                                                 stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                                 stderr=asyncio.subprocess.STDOUT, **kw)
            except OSError as e:
                self._set("error", f"无法启动 llama-server: {e}")
                raise EngineError(self.error or "")
            if os.name == "nt":
                _win_attach_job(self.proc.pid)
            self._tasks = [asyncio.create_task(self._read_log(self.proc)), asyncio.create_task(self._await_ready(self.proc))]

    async def _read_log(self, proc: Any) -> None:
        try:
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").rstrip()
                if s:
                    self.log.append(s[:400])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        code = await proc.wait()
        if proc is self.proc and self.status in ("starting", "ready"):
            tail = "\n".join(list(self.log)[-30:])
            oom = re.search(r"out of memory|failed to allocate|cudaMalloc failed|unable to allocate|ggml_backend_.*alloc", tail, re.I)
            why = f"引擎进程意外退出（退出码 {code}）。" + (OOM_HINT if oom else "详见下方日志。")
            if re.search(r"unknown model architecture|unsupported", tail, re.I):
                why = "当前 llama.cpp 版本不支持该模型架构，请升级 bin/ 中的 llama-server。"
            self._set("error", why)

    async def _await_ready(self, proc: Any) -> None:
        t0 = time.time()
        while proc is self.proc and proc.returncode is None and time.time() - t0 < 1800:
            try:
                st, _ = await net.local_json("127.0.0.1", self.port, "GET", "/health", timeout=3)
                if st == 200:
                    break
            except net.NetError:
                pass
            await asyncio.sleep(0.6)
        else:
            if proc is self.proc and self.status == "starting" and proc.returncode is None:
                self._set("error", "加载超时（>30 分钟）")
            return
        if proc is not self.proc:
            return
        try:
            st, props = await net.local_json("127.0.0.1", self.port, "GET", "/props", headers=self._auth(), timeout=10)
            if st == 200 and isinstance(props, dict):
                n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
                if isinstance(n_ctx, int) and n_ctx > 0:
                    self.ctx = n_ctx
                    self.ctx_cap = max(self.ctx_cap, n_ctx) if self.cfg.get("engine", "ctx_auto_grow") else n_ctx
                mod = props.get("modalities") or {}
                self.vision = bool(mod.get("vision")) or (bool(self.plan_info.get("mmproj_path")) and "vision" not in mod)
        except net.NetError:
            pass
        self.log.append(f"[LocalAgent] 引擎就绪：ctx={self.ctx} vision={self.vision}")
        self._set("ready")

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _stop_locked(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        p, self.proc = self.proc, None
        if p is not None and p.returncode is None:
            self._set("stopping")
            try:
                if os.name == "nt":
                    _kill_tree(p)
                else:
                    os.killpg(p.pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(p.wait(), 6)
            except asyncio.TimeoutError:
                _kill_tree(p)
                try:
                    await asyncio.wait_for(p.wait(), 4)
                except asyncio.TimeoutError:
                    pass
        if self.status != "error":
            self._set("stopped")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def restart(self, reset_ctx: bool = False, **over: Any) -> None:
        """reset_ctx=True（用户主动重启）：除非显式给出 ctx，否则按当前配置重新规划；
        默认（自动扩容/崩溃自愈）沿用上次请求的 ctx，避免丢掉已扩容的长度。"""
        given = {k: v for k, v in over.items() if v is not None}
        o = {**self._last, **given}
        if reset_ctx and "ctx" not in given:
            o["ctx"] = None
        await self.start(o.get("model"), o.get("mmproj"), o.get("ctx"))

    def shutdown_sync(self) -> None:
        p = self.proc
        if p is not None and p.returncode is None:
            _kill_tree(p)

    async def wait_ready(self, timeout: float = 600) -> bool:
        if self.status == "ready":
            return True
        if self.status not in ("starting",):
            return False
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.status == "ready"

    async def ensure_ready(self) -> None:
        """循环每次请求前调用：必要时等待加载；若引擎崩溃，最多自动重启 2 次。"""
        if self.status == "ready" and self.proc is not None and self.proc.returncode is None:
            return
        if self.status == "starting":
            if await self.wait_ready():
                return
        if self.status in ("error", "stopped") and self._last and self.auto_restarts < 2:
            self.auto_restarts += 1
            self.emit("notice", {"level": "warn", "text": "引擎已停止，正在自动重启…"})
            await self.restart()
            if await self.wait_ready():
                return
        raise EngineError(self.error or "引擎未就绪：请先在 设置→模型与引擎 中加载模型。")

    # ------------------------------------------------------------ 推理接口
    def chat_stream(self, body: dict):
        return net.sse_post("127.0.0.1", self.port, "/v1/chat/completions", body, headers=self._auth())

    async def chat(self, body: dict, timeout: float = 600) -> dict:
        st, data = await net.local_json("127.0.0.1", self.port, "POST", "/v1/chat/completions", {**body, "stream": False},
                                        headers=self._auth(), timeout=timeout)
        if st != 200:
            raise net.HTTPStatusError(st, data if isinstance(data, str) else str(data))
        return data

    def record_tps(self, v: float) -> None:
        if v > 0:
            self.tps = v


def _physical_cores() -> int:
    try:
        import psutil  # type: ignore
        return psutil.cpu_count(logical=False) or 0
    except Exception:  # noqa: BLE001
        return 0
