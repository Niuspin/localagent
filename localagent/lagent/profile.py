"""模型档位：按模型规模与结构自动选择 Agent / 引擎参数，让同一套代码既照顾小模型，也吃满千亿级 MoE。

为什么需要：
- 小模型（≤十几 B）上下文紧、工具选择容易出错：工具要分级按需加载、少召回、早收敛、工具输出要短。
- 千亿级 MoE 能力强、上下文大、**重载一次要几分钟**：工具一次给全（省掉 load_tools 往返）、多召回、
  多步数、保留更多工具输出，上下文不够时先压缩再考虑扩容重启；批处理参数更大以加快专家层的提示处理。
- 中间档（standard）等于旧版默认值，行为不变。

分档只看 GGUF 元数据与文件大小，不加载模型：参数量 ≈ 文件字节 × 8 ÷ 量化位宽；MoE 的“能力规模”取 √(总参数 × 激活参数)。
这是启发式估算，误差主要来自量化位宽与专家占比；不合适时可在 设置→生成→Agent 里把“模型档位”手动指定。

用户自定义优先：配置里的数值与默认值相同时视为“没改过”，由档位接管；一旦改成别的数，永远按用户的来。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

# ---- 量化位宽（含元数据与缩放的有效值，近似）
_BPW = {
    "F32": 32.0, "F16": 16.0, "BF16": 16.0, "Q8_0": 8.5, "Q6_K": 6.6, "Q5_K_M": 5.7, "Q5_K_S": 5.5, "Q5_0": 5.5, "Q5_1": 6.0,
    "Q4_K_M": 4.85, "Q4_K_S": 4.6, "Q4_0": 4.5, "Q4_1": 5.0, "Q3_K_L": 4.3, "Q3_K_M": 3.9, "Q3_K_S": 3.5, "Q2_K": 2.9,
    "IQ4_XS": 4.3, "IQ4_NL": 4.5, "IQ3_M": 3.7, "IQ3_S": 3.5, "IQ3_XS": 3.3, "IQ3_XXS": 3.1, "IQ2_M": 2.7, "IQ2_S": 2.5,
    "IQ2_XS": 2.3, "IQ2_XXS": 2.1, "IQ1_M": 1.8, "IQ1_S": 1.6, "MXFP4": 4.25,
}
_FAMILY = (("IQ4", 4.4), ("IQ3", 3.3), ("IQ2", 2.4), ("IQ1", 1.7), ("Q8", 8.5), ("Q6", 6.6), ("Q5", 5.6), ("Q4", 4.8),
           ("Q3", 3.9), ("Q2", 2.9), ("BF16", 16.0), ("F16", 16.0), ("F32", 32.0))
_DEFAULT_BPW = 4.8          # 找不到量化信息时按最常见的 Q4_K_M 估算
_DENSE_SHARE = 0.06         # MoE 里注意力/嵌入等非专家权重占总参数的比例（经验值）

LABELS = {"compact": "紧凑", "standard": "标准", "large": "大型"}


@dataclass(frozen=True)
class Profile:
    tier: str
    params_b: float = 0.0          # 估算总参数量（十亿）
    active_b: float = 0.0          # 估算每 token 激活参数量（密集模型等于总量）
    preload_tools: bool = False    # 一次性加载全部静态工具组（大模型不怕工具多，省掉 load_tools 往返）
    recall_k: int = 6              # 记忆召回条数
    max_steps: int = 40            # 单次任务最大工具步数
    keep_recent_tools: int = 4     # 上下文精简时保留最近多少条完整工具输出
    tool_cap: int = 30000          # 单次工具结果交给模型的字符上限
    grow_first: bool = True        # 上下文不够时先扩容（重载）还是先压缩；大模型重载太贵，先压缩
    ctx_start: int = 32768         # 引擎起始上下文
    batch: int = 2048
    ubatch: int = 512
    cache_ram_mb: int = 4096       # llama.cpp 提示缓存占用的内存上限

    @property
    def label(self) -> str:
        return LABELS.get(self.tier, self.tier)

    def brief(self) -> dict:
        return {"tier": self.tier, "label": self.label, "params_b": round(self.params_b, 1), "active_b": round(self.active_b, 1)}

    def tuned(self, cfg: Any) -> "Profile":
        """叠加用户配置：数值与默认值相同 → 用档位的；用户改过 → 用用户的。"""
        from .config import DEFAULTS  # 延迟导入，避免循环
        g = cfg.get
        pairs = (("recall_k", ("agent", "memory_recall_k"), DEFAULTS["agent"]["memory_recall_k"]),
                 ("max_steps", ("agent", "max_steps"), DEFAULTS["agent"]["max_steps"]),
                 ("ctx_start", ("engine", "ctx_start"), DEFAULTS["engine"]["ctx_start"]),
                 ("batch", ("engine", "batch"), DEFAULTS["engine"]["batch"]),
                 ("ubatch", ("engine", "ubatch"), DEFAULTS["engine"]["ubatch"]),
                 ("cache_ram_mb", ("engine", "cache_ram_mb"), DEFAULTS["engine"]["cache_ram_mb"]))
        over = {}
        for attr, path, default in pairs:
            v = g(*path, default=default)
            if v != default:
                over[attr] = int(v)
        return replace(self, **over) if over else self


# ---- 各档的取值（standard = 旧版默认值）
_TIERS: dict[str, dict] = {
    "compact": dict(preload_tools=False, recall_k=4, max_steps=30, keep_recent_tools=3, tool_cap=12000, grow_first=True,
                    ctx_start=32768, batch=2048, ubatch=512, cache_ram_mb=4096),
    "standard": dict(preload_tools=False, recall_k=6, max_steps=40, keep_recent_tools=4, tool_cap=30000, grow_first=True,
                     ctx_start=32768, batch=2048, ubatch=512, cache_ram_mb=4096),
    "large": dict(preload_tools=True, recall_k=10, max_steps=80, keep_recent_tools=8, tool_cap=60000, grow_first=False,
                  ctx_start=65536, batch=2048, ubatch=1024, cache_ram_mb=2048),
}


def bits_per_weight(quant: str | None) -> float:
    q = (quant or "").upper().replace("UD-", "")
    if q in _BPW:
        return _BPW[q]
    for prefix, bpw in _FAMILY:
        if q.startswith(prefix):
            return bpw
    return _DEFAULT_BPW


def estimate(info: dict) -> tuple[float, float]:
    """(总参数量 B, 激活参数量 B)。info 是 gguf.model_info 的结果。"""
    size = float(info.get("size_bytes") or 0)
    total = size * 8 / bits_per_weight(info.get("quant")) / 1e9
    if not info.get("moe"):
        return total, total
    ne = int(info.get("experts") or 0)
    nu = int(info.get("experts_used") or 0)
    frac = (nu / ne) if ne and nu else 0.125
    return total, total * (_DENSE_SHARE + (1 - _DENSE_SHARE) * min(1.0, frac))


def classify(info: dict | None) -> tuple[str, float, float]:
    if not info:
        return "standard", 0.0, 0.0
    total, active = estimate(info)
    eff = math.sqrt(total * active) if info.get("moe") else total
    if total >= 100 or eff >= 45:
        tier = "large"
    elif eff < 12:
        tier = "compact"
    else:
        tier = "standard"
    return tier, total, active


def for_model(cfg: Any, info: dict | None) -> Profile:
    """该模型在当前配置下的档位（尚未叠加用户数值覆盖，见 Profile.tuned）。"""
    mode = cfg.get("agent", "profile", default="auto")
    tier, total, active = classify(info)
    if mode == "off":
        tier = "standard"
    elif mode in _TIERS:
        tier = mode
    return Profile(tier=tier, params_b=total, active_b=active, **_TIERS[tier])


def default(cfg: Any) -> Profile:
    """没有加载模型时的缺省档位（标准）。"""
    mode = cfg.get("agent", "profile", default="auto")
    return Profile(tier=mode if mode in _TIERS else "standard", **_TIERS[mode if mode in _TIERS else "standard"])
