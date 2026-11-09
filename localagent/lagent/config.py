"""配置：config.json（可分享的设置）+ data/secrets.json（密钥，权限 0600）。

所有写入都会经过类型/范围校验，非法值抛 ValueError（HTTP 层转成 400）。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

CODE_DIR = Path(__file__).resolve().parent.parent          # 代码所在目录（web/ 在这里）
ROOT = Path(os.environ.get("LAGENT_HOME") or CODE_DIR).resolve()  # 数据根目录（测试可重定向）
MODELS_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
SKILLS_DIR = ROOT / "skills"
BIN_DIR = ROOT / "bin"
WEB_DIR = CODE_DIR / "web"
CONFIG_PATH = ROOT / "config.json"
SECRETS_PATH = DATA_DIR / "secrets.json"

SECRET_NAMES = ("brave", "google_cse", "google_cx", "tavily", "serper")
SEARCH_PROVIDERS = ("duckduckgo", "bing", "baidu", "yahoo", "brave", "google_cse", "searxng", "tavily", "serper")

DEFAULTS: dict[str, Any] = {
    "workspace": "",
    "engine": {
        "binary": "", "model": "", "mmproj": "auto",
        "ctx": "auto", "ctx_start": 32768, "ctx_max": 131072, "ctx_auto_grow": True,
        "gpu_layers": "auto", "n_cpu_moe": "auto", "mmproj_offload": "auto",
        "flash_attn": "auto", "kv_cache_type": "auto",
        "threads": 0, "batch": 2048, "ubatch": 512,
        "image_max_tokens": 1024, "cache_ram_mb": 4096, "vram_reserve_mb": 1024,
        "extra_args": [],
    },
    "generation": {
        "thinking": "auto", "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
        "max_tokens": 8192, "presence_penalty": 0.0,
    },
    "agent": {
        "max_steps": 40, "permission_mode": "ask", "system_prompt_extra": "",
        "auto_title": True, "memory_auto_recall": True, "memory_recall_k": 6,
        "require_read_before_edit": True,     # 改/覆盖已有文本文件前，本会话必须读过它且之后没被别人改动
        "parallel_readonly": True,            # 同一步里连续的只读工具调用并发执行
        "project_instructions": True,         # 自动加载工作区里的 AGENTS.md / CLAUDE.md
    },
    "security": {
        "allow_network_commands": False, "command_sandbox": "auto", "command_timeout_s": 120,
        "max_file_mb": 20, "trash_retention_days": 14, "block_remote_images": True, "taint_tracking": True,
    },
    "search": {
        "providers": ["duckduckgo", "bing"], "mode": "merge", "max_results": 8,
        "proxy": "", "searxng_url": "", "region": "", "safe": "moderate",
        "keys": {n: False for n in SECRET_NAMES},
    },
    "web": {"timeout_s": 20, "max_bytes": 3_000_000, "max_chars": 12000},
    "ui": {"theme": "auto", "font_size": 15},
}


def _bad(path: str, v: Any, why: str = "") -> ValueError:
    return ValueError(f"配置项 {path} 的值无效: {v!r} {why}".strip())


def _enum(*vals: str) -> Callable[[str, Any], Any]:
    def f(path: str, v: Any) -> Any:
        if v not in vals:
            raise _bad(path, v, f"(可选: {', '.join(vals)})")
        return v
    return f


def _auto_int(lo: int, hi: int, extra: tuple[str, ...] = ()) -> Callable[[str, Any], Any]:
    def f(path: str, v: Any) -> Any:
        if v == "auto" or v in extra:
            return v
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise _bad(path, v)
        return max(lo, min(hi, n))
    return f


def _auto_bool(path: str, v: Any) -> Any:
    if v == "auto":
        return "auto"
    if isinstance(v, bool):
        return v
    raise _bad(path, v)


def _providers(path: str, v: Any) -> list[str]:
    if not isinstance(v, list):
        raise _bad(path, v)
    out: list[str] = []
    for x in v:
        if x not in SEARCH_PROVIDERS:
            raise _bad(path, x, f"(未知搜索引擎，可选: {', '.join(SEARCH_PROVIDERS)})")
        if x not in out:
            out.append(x)
    return out


def _str_list(path: str, v: Any) -> list[str]:
    if not isinstance(v, list):
        raise _bad(path, v)
    return [str(x) for x in v][:200]


SPECIAL: dict[str, Callable[[str, Any], Any]] = {
    "engine.ctx": _auto_int(2048, 4_194_304),
    "engine.gpu_layers": _auto_int(0, 999, ("all",)),
    "engine.n_cpu_moe": _auto_int(0, 999),
    "engine.mmproj_offload": _auto_bool,
    "engine.flash_attn": _enum("auto", "on", "off"),
    "engine.kv_cache_type": _enum("auto", "f16", "q8_0", "q4_0"),
    "engine.extra_args": _str_list,
    "generation.thinking": _enum("auto", "on", "off"),
    "agent.permission_mode": _enum("ask", "auto_edit", "plan", "auto"),
    "security.command_sandbox": _enum("auto", "off"),
    "search.mode": _enum("merge", "first"),
    "search.safe": _enum("off", "moderate", "strict"),
    "search.providers": _providers,
    "ui.theme": _enum("auto", "light", "dark"),
}

RANGES: dict[str, tuple[float, float]] = {
    "engine.ctx_start": (2048, 4_194_304), "engine.ctx_max": (2048, 4_194_304),
    "engine.threads": (0, 512), "engine.batch": (32, 16384), "engine.ubatch": (32, 16384),
    "engine.image_max_tokens": (0, 16384), "engine.cache_ram_mb": (-1, 1_048_576),
    "engine.vram_reserve_mb": (0, 65536),
    "generation.temperature": (0, 2), "generation.top_p": (0, 1), "generation.top_k": (0, 500),
    "generation.min_p": (0, 1), "generation.max_tokens": (64, 262144), "generation.presence_penalty": (-2, 2),
    "agent.max_steps": (1, 200), "agent.memory_recall_k": (0, 30),
    "security.command_timeout_s": (1, 3600), "security.max_file_mb": (1, 2048),
    "security.trash_retention_days": (0, 3650),
    "web.timeout_s": (3, 120), "web.max_bytes": (10_000, 50_000_000), "web.max_chars": (1000, 200_000),
    "search.max_results": (1, 30), "ui.font_size": (12, 22),
}


def _coerce(path: str, default: Any, v: Any) -> Any:
    if path in SPECIAL:
        return SPECIAL[path](path, v)
    if isinstance(default, bool):
        if not isinstance(v, bool):
            raise _bad(path, v, "(需要布尔值)")
        return v
    if isinstance(default, (int, float)):
        try:
            n = float(v) if isinstance(default, float) else int(v)
        except (TypeError, ValueError):
            raise _bad(path, v, "(需要数字)")
        lo, hi = RANGES.get(path, (-1e18, 1e18))
        n = max(lo, min(hi, n))
        return float(n) if isinstance(default, float) else int(n)
    if isinstance(default, str):
        if not isinstance(v, str):
            raise _bad(path, v, "(需要字符串)")
        return v.strip() if path != "agent.system_prompt_extra" else v[:8000]
    return v


def _merge(base: dict, inc: dict, prefix: str = "", strict: bool = True) -> None:
    for k, v in inc.items():
        if k not in base:
            if strict:
                raise ValueError(f"未知配置项: {prefix}{k}")
            continue
        path = f"{prefix}{k}"
        if path == "search.keys":
            continue  # 密钥单独处理
        if isinstance(base[k], dict):
            if not isinstance(v, dict):
                if strict:
                    raise _bad(path, v)
                continue
            _merge(base[k], v, path + ".", strict)
        elif path == "workspace":
            base[k] = str(v or "")
        else:
            try:
                base[k] = _coerce(path, base[k], v)
            except ValueError:
                if strict:
                    raise


class Config:
    def __init__(self, path: Path = CONFIG_PATH, secrets_path: Path = SECRETS_PATH):
        self._lock = threading.RLock()
        self.path, self.secrets_path = Path(path), Path(secrets_path)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.secrets: dict[str, str] = {}
        self._load()

    # ---- 读写 ----
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if isinstance(raw, dict):
                _merge(self.data, raw, strict=False)
        except (OSError, ValueError):
            pass
        try:
            s = json.loads(self.secrets_path.read_text("utf-8"))
            if isinstance(s, dict):
                self.secrets = {k: str(v) for k, v in s.items() if k in SECRET_NAMES and v}
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        with self._lock:
            d = copy.deepcopy(self.data)
            d["search"]["keys"] = {n: False for n in SECRET_NAMES}  # 不把密钥写进可分享的 config.json
            _atomic_json(self.path, d, 0o644)
            _atomic_json(self.secrets_path, self.secrets, 0o600)

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.data
        with self._lock:
            for p in path:
                if not isinstance(cur, dict) or p not in cur:
                    return default
                cur = cur[p]
            return copy.deepcopy(cur)

    def set_workspace(self, path: str) -> None:
        with self._lock:
            self.data["workspace"] = path
            self.save()

    def secret(self, name: str) -> str:
        return self.secrets.get(name, "")

    def patch(self, partial: dict) -> dict:
        """深合并。校验失败时抛 ValueError 且不产生任何修改。"""
        if not isinstance(partial, dict):
            raise ValueError("配置必须是对象")
        with self._lock:
            new = copy.deepcopy(self.data)
            _merge(new, partial)
            new_secrets = dict(self.secrets)
            keys = (partial.get("search") or {}).get("keys") if isinstance(partial.get("search"), dict) else None
            if keys is not None:
                if not isinstance(keys, dict):
                    raise ValueError("search.keys 必须是对象")
                for name, val in keys.items():
                    if name not in SECRET_NAMES:
                        raise ValueError(f"未知密钥名: {name}")
                    if not isinstance(val, str):
                        continue  # 布尔等视为“不变”
                    if val == "":
                        new_secrets.pop(name, None)
                    else:
                        new_secrets[name] = val.strip()
            self.data, self.secrets = new, new_secrets
            self.save()
            return self.public()

    def public(self) -> dict:
        with self._lock:
            d = copy.deepcopy(self.data)
            d["search"]["keys"] = {n: bool(self.secrets.get(n)) for n in SECRET_NAMES}
            return d


def _atomic_json(path: Path, obj: Any, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
