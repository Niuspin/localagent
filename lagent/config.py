"""配置：config.json（可分享的设置）+ data/secrets.json（密钥，权限 0600）。

所有写入都会经过类型/范围校验，非法值抛 ValueError（HTTP 层转成 400）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .paths import CODE_DIR, ROOT          # 代码/资源目录与数据根目录（源码运行、打包运行、测试重定向都在 paths 里处理）

MODELS_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
SKILLS_DIR = ROOT / "skills"
BIN_DIR = ROOT / "bin"
WEB_DIR = CODE_DIR / "web"
CONFIG_PATH = ROOT / "config.json"
SECRETS_PATH = DATA_DIR / "secrets.json"

SECRET_NAMES = (
    "brave", "google_cse", "google_cx", "tavily", "serper",
    "compact_api_key", "embed_api_key", "swarm_worker_api_key",
)
SEARCH_PROVIDERS = ("duckduckgo", "bing", "baidu", "yahoo", "brave", "google_cse", "searxng", "tavily", "serper")
_ROLE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

DEFAULTS: dict[str, Any] = {
    "workspace": "",
    "engine": {
        "binary": "", "model": "", "mmproj": "auto", "draft_model": "auto",
        "ctx": "auto", "ctx_start": 32768, "ctx_max": 131072, "ctx_auto_grow": True,
        "gpu_layers": "auto", "n_cpu_moe": "auto", "moe_cache_mib": "auto", "moe_cache_cpu_share": 0.0,
        "tensor_split": "auto", "split_mode": "layer",
        "mmproj_offload": "auto", "flash_attn": "auto", "kv_cache_type": "auto",
        "spec_mode": "auto", "spec_draft_max": 0, "spec_draft_min": 0,
        "threads": 0, "threads_batch": 0, "batch": 2048, "ubatch": 512,
        "cache_reuse": 256, "mlock": False,
        "slots": 1, "reasoning_format": "auto", "auto_disable_bad_spec": True,
        "image_max_tokens": 1024, "cache_ram_mb": 4096, "vram_reserve_mb": 1024,
        "extra_args": [],
    },
    "generation": {
        "thinking": "auto", "reasoning_budget": -1,
        "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
        "max_tokens": 8192, "presence_penalty": 0.0,
    },
    "agent": {
        "profile": "auto",                    # 模型档位：auto 按模型规模自动选；compact / standard / large 强制；off 关闭自适应
        "max_steps": 40, "permission_mode": "ask", "system_prompt_extra": "",
        "auto_title": True, "memory_auto_recall": True, "memory_recall_k": 6,
        "require_read_before_edit": True,     # 改/覆盖已有文本文件前，本会话必须读过它且之后没被别人改动
        "parallel_readonly": True,            # 同一步里连续的只读工具调用并发执行
        "verify_after_edit": True,            # 改过文件后收尾前提醒模型跑验证命令（AGENTS.md 的“验证命令:”或下面的 verify_command）
        "verify_command": "",                 # 手动指定验证命令（优先于 AGENTS.md；用户自己填的才算可信）
        "offload_outputs": True,              # 大输出落盘并用句柄取回，而不是永久截断/让模型重跑工具（见 outputs.py）
        "project_instructions": True,         # 自动加载工作区里的 AGENTS.md / CLAUDE.md
        "hooks_enabled": True,                # 项目生命周期钩子（.localagent/hooks.json）
        "granular_permissions": True,         # 细粒度权限规则（allow/ask/deny，不绕过硬安全防线）
        "subagent_max_steps": 8,              # delegate_task 子代理步数上限
        "compact_endpoint": "",               # 可选：用于摘要压缩的外部 OpenAI / Anthropic 兼容端点（留空走本地引擎）
        "compact_model": "",                  # 可选：摘要压缩专用轻量模型名
        "compact_provider": "auto",           # compact 协议：auto / openai / anthropic
        "embed_endpoint": "",                 # 可选：外部 Embedding 端点（/v1/embeddings，留空走本地引擎或离线哈希向量）
        "embed_model": "",                    # 可选：Embedding 模型名
    },
    "security": {
        "allow_network_commands": False, "command_sandbox": "auto", "command_timeout_s": 120,
        "max_file_mb": 20, "trash_retention_days": 14, "block_remote_images": True, "taint_tracking": True,
    },
    "search": {
        "providers": ["duckduckgo", "bing"], "mode": "merge", "max_results": 8,
        "proxy": "", "searxng_url": "", "region": "", "safe": "moderate",
        "html_scrapers_ack": False,
        "keys": {n: False for n in SECRET_NAMES},
    },
    "web": {"timeout_s": 20, "max_bytes": 3_000_000, "max_chars": 12000},
    "research": {
        "mode": "off",
        "user_agent": "LocalAgentResearch/0.2 (+local-desktop)",
        "respect_robots": True,
        "per_domain_rps": 0.5,
        "per_domain_conc": 2,
        "max_requests_per_task": 40,
        "allow_domains": [],
        "deny_domains": [],
        "deny_categories": ["adult", "malware", "paywall_bypass"],
        "cache_ttl_h": 24,
        "cache_max_mb": 256,
        "quote_max_chars": 400,
        "pii_redact": True,
        "reader_isolation": True,
    },
    "swarm": {
        "enabled": True,
        "max_agents": "auto",
        "max_depth": 2,
        "max_children": 4,
        "default_budget": {"steps": 120, "wall_s": 600, "tokens": 500_000},
        "merge_strategy": "queue",
        "worktree_gc_hours": 24,
        "role_limits": {"reviewer": 2, "researcher": 4, "coder": 4, "verifier": 2},
        "worker_endpoint": "",
        "worker_model": "",
    },
    "mission": {
        "enabled": True,
        "episode_max_steps": 25,
        "heartbeat_s": 10,
        "prevent_sleep": True,
        "unattended_default": "attended",
        "budget": {"wall_s": 14400, "tokens": 2_000_000, "steps": 400, "disk_mb": 2048},
    },
    "media": {
        "subprocess_timeout_s": 30,
        "max_pdf_pages": 50,
        "max_archive_entries": 500,
        "html_scripts_default": False,
        "recorder_enabled": False,
        "max_image_pixels": 40_000_000,
        "max_archive_mb": 64,
        "max_video_frames": 6,
        "artifact_max_mb": 512,
        "tts_enabled": False,
    },
    "skills": {
        "default_enabled_limit": 14,
        "auto_route": True,
    },
    "ui": {
        "theme": "auto", "font_size": 15,
        "motion": "auto", "density": "comfortable", "code_ligatures": False,
    },
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


def _role_limits(path: str, v: Any) -> dict[str, int]:
    if not isinstance(v, dict):
        raise _bad(path, v, "(需要角色并发限制字典)")
    out: dict[str, int] = {}
    for k, val in list(v.items())[:32]:
        role = str(k).strip().lower()
        if not _ROLE_RE.match(role):
            raise _bad(path, k, "(角色名仅限 lowercase a-z0-9_-)")
        try:
            n = int(val)
        except (TypeError, ValueError):
            raise _bad(path, val, f"(角色 {role} 上限需为整数)")
        out[role] = max(1, min(8, n))
    return out


def _tensor_split(path: str, v: Any) -> str:
    s = str(v or "auto").strip()
    if not s or s.lower() == "auto":
        return "auto"
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return "auto"
    cleaned: list[str] = []
    for p in parts:
        try:
            n = float(p)
        except ValueError:
            raise _bad(path, v, "(需为 auto 或逗号分隔正数，如 24,16)")
        if n < 0:
            raise _bad(path, v, "(权重不能为负)")
        cleaned.append(str(int(n)) if n.is_integer() else f"{n:.2f}")
    return ",".join(cleaned)


SPECIAL: dict[str, Callable[[str, Any], Any]] = {
    "engine.ctx": _auto_int(2048, 4_194_304),
    "engine.slots": _auto_int(1, 8),
    "engine.gpu_layers": _auto_int(0, 999, ("all",)),
    "engine.n_cpu_moe": _auto_int(0, 999),
    "engine.moe_cache_mib": _auto_int(0, 65536),
    "engine.tensor_split": _tensor_split,
    "engine.split_mode": _enum("auto", "layer", "row", "none"),
    "engine.mmproj_offload": _auto_bool,
    "engine.flash_attn": _enum("auto", "on", "off"),
    "engine.kv_cache_type": _enum("auto", "f16", "q8_0", "q4_0"),
    "engine.spec_mode": _enum("auto", "off", "draft", "ngram-mod", "ngram-simple"),
    "engine.reasoning_format": _enum("auto", "deepseek", "none"),
    "engine.extra_args": _str_list,
    "generation.thinking": _enum("auto", "on", "off"),
    "agent.permission_mode": _enum("ask", "auto_edit", "plan", "auto"),
    "agent.profile": _enum("auto", "compact", "standard", "large", "off"),
    "agent.compact_provider": _enum("auto", "openai", "anthropic"),
    "security.command_sandbox": _enum("auto", "off"),
    "search.mode": _enum("merge", "first"),
    "search.safe": _enum("off", "moderate", "strict"),
    "search.providers": _providers,
    "research.mode": _enum("off", "ask", "allowlist", "open"),
    "research.allow_domains": _str_list,
    "research.deny_domains": _str_list,
    "research.deny_categories": _str_list,
    "swarm.max_agents": _auto_int(1, 8),
    "swarm.merge_strategy": _enum("queue", "manual"),
    "swarm.role_limits": _role_limits,
    "mission.unattended_default": _enum("attended", "unattended"),
    "ui.theme": _enum("auto", "light", "dark", "paper", "midnight", "hc", "colorblind"),
    "ui.motion": _enum("auto", "full", "reduced", "off"),
    "ui.density": _enum("comfortable", "compact"),
}

RANGES: dict[str, tuple[float, float]] = {
    "engine.ctx_start": (2048, 4_194_304), "engine.ctx_max": (2048, 4_194_304),
    "engine.threads": (0, 512), "engine.threads_batch": (0, 512), "engine.batch": (32, 16384), "engine.ubatch": (32, 16384),
    "engine.spec_draft_max": (0, 256), "engine.spec_draft_min": (0, 256),
    "engine.moe_cache_cpu_share": (0.0, 1.0), "engine.cache_reuse": (0, 16384),
    "engine.image_max_tokens": (0, 16384), "engine.cache_ram_mb": (-1, 1_048_576),
    "engine.vram_reserve_mb": (0, 65536), "engine.slots": (1, 8),
    "generation.reasoning_budget": (-1, 32768),
    "generation.temperature": (0, 2), "generation.top_p": (0, 1), "generation.top_k": (0, 500),
    "generation.min_p": (0, 1), "generation.max_tokens": (64, 262144), "generation.presence_penalty": (-2, 2),
    "agent.max_steps": (1, 200), "agent.memory_recall_k": (0, 30), "agent.subagent_max_steps": (1, 30),
    "security.command_timeout_s": (1, 3600), "security.max_file_mb": (1, 2048),
    "security.trash_retention_days": (0, 3650),
    "web.timeout_s": (3, 120), "web.max_bytes": (10_000, 50_000_000), "web.max_chars": (1000, 200_000),
    "search.max_results": (1, 30),
    "research.per_domain_rps": (0.1, 5.0), "research.per_domain_conc": (1, 4),
    "research.max_requests_per_task": (1, 2000), "research.cache_ttl_h": (1, 720),
    "research.cache_max_mb": (10, 4096), "research.quote_max_chars": (100, 2000),
    "swarm.max_depth": (1, 3), "swarm.max_children": (1, 8), "swarm.worktree_gc_hours": (1, 720),
    "swarm.default_budget.steps": (5, 1000), "swarm.default_budget.wall_s": (30, 86400),
    "swarm.default_budget.tokens": (1000, 10_000_000),
    "mission.episode_max_steps": (5, 100), "mission.heartbeat_s": (2, 120),
    "mission.budget.wall_s": (60, 604_800), "mission.budget.tokens": (10_000, 50_000_000),
    "mission.budget.steps": (10, 5000), "mission.budget.disk_mb": (64, 65536),
    "media.subprocess_timeout_s": (5, 300), "media.max_pdf_pages": (1, 500), "media.max_archive_entries": (1, 5000),
    "media.max_image_pixels": (1_000_000, 120_000_000), "media.max_archive_mb": (1, 1024), "media.max_video_frames": (1, 24),
    "media.artifact_max_mb": (32, 16384),
    "skills.default_enabled_limit": (1, 50),
    "ui.font_size": (12, 22),
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
        if path in SPECIAL:
            try:
                base[k] = _coerce(path, base[k], v)
            except ValueError:
                if strict:
                    raise
        elif isinstance(base[k], dict):
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

    def set(self, *keys_and_value: Any) -> dict:
        """按嵌套键路径设置单个配置项并持久化，等价于 patch({k1: {k2: val}})。"""
        if len(keys_and_value) < 2:
            raise ValueError("set() 需要至少一个键名和目标值")
        *keys, val = keys_and_value
        partial: Any = val
        for k in reversed(keys):
            partial = {str(k): partial}
        return self.patch(partial)

    def patch(self, partial: dict) -> dict:
        """深合并。校验失败时抛 ValueError 且不产生任何修改。"""
        if not isinstance(partial, dict):
            raise ValueError("配置必须是对象")
        with self._lock:
            new = copy.deepcopy(self.data)
            clean_partial = copy.deepcopy(partial)
            new_secrets = dict(self.secrets)
            ag = clean_partial.get("agent")
            if isinstance(ag, dict):
                for sk in ("compact_api_key", "embed_api_key"):
                    if sk in ag:
                        val = ag.pop(sk)
                        if isinstance(val, str):
                            if val == "":
                                new_secrets.pop(sk, None)
                            else:
                                new_secrets[sk] = val.strip()
            sw = clean_partial.get("swarm")
            if isinstance(sw, dict) and "worker_api_key" in sw:
                val = sw.pop("worker_api_key")
                if isinstance(val, str):
                    if val == "":
                        new_secrets.pop("swarm_worker_api_key", None)
                    else:
                        new_secrets["swarm_worker_api_key"] = val.strip()
            _merge(new, clean_partial)
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
            d["search"]["keys"] = {n: bool(self.secrets.get(n)) for n in ("brave", "google_cse", "google_cx", "tavily", "serper")}
            d.setdefault("agent", {})
            d["agent"]["compact_api_key"] = bool(self.secrets.get("compact_api_key"))
            d["agent"]["embed_api_key"] = bool(self.secrets.get("embed_api_key"))
            d.setdefault("swarm", {})
            d["swarm"]["worker_api_key"] = bool(self.secrets.get("swarm_worker_api_key"))
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
