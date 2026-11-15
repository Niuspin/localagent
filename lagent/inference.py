"""大模型与 MoE 前沿推理优化：硬件自适应 MoE 混合显存规划、投机解码（Draft / EAGLE3 / ngram-mod）、
KV 缓存预算分解与吞吐量遥测。

设计原则：
1. 启发式规划零依赖：输入硬件探测信息与 GGUF 元数据，纯计算输出最优部署参数。
2. 标志位严格守卫（has_flag_fn）：所有新增参数必须经过 llama-server --help 探针验证，未支持的参数绝不传递。
3. 稳健降级：从 EAGLE3/Draft 逐级降级到 ngram-mod、再到无投机；旧版 llama.cpp 平滑兼容。
"""
from __future__ import annotations

import re
from typing import Any, Callable

GiB = 2 ** 30
MiB = 2 ** 20

# KV cache 每元素字节倍数（相对于 f16 = 2 字节）
_KV_SCALE = {
    "f32": 2.0, "f16": 1.0, "bf16": 1.0, "q8_0": 0.53, "q5_1": 0.38, "q5_0": 0.35,
    "q4_1": 0.30, "q4_0": 0.28, "iq4_nl": 0.28, "f8_e4m3": 0.50,
}


def _stem_key(name: str) -> str:
    s = re.sub(r"\.gguf$", "", name, flags=re.I)
    s = re.sub(r"-\d{5}-of-\d{5}$", "", s)
    s = re.sub(r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ\d(?:_[A-Z0-9]+)*|Q\d(?:_K)?(?:_[A-Z0-9]+)?|BF16|F16|F32|MXFP4))(?![A-Za-z0-9])", "", s, flags=re.I)
    s = re.sub(r"[-_.]*(gguf|mmproj|eagle\d*|draft)[-_.]*", "-", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def kv_budget_analysis(
    hw_info: dict,
    model_info: dict,
    ctx: int,
    kv_type: str = "f16",
    ubatch: int = 512,
    draft_model: dict | None = None,
    vram_reserve_mb: float = 1024.0,
    n_cpu_moe: int | None = None,
    mmproj_size_gb: float = 0.0,
    mmproj_offload: bool = True,
) -> dict:
    """计算显存与内存详细预算分解（稠密权重、专家 GPU/CPU 分布、KV 缓存、工作区、Draft 显存及 MoE 缓存推荐）。"""
    gpus = hw_info.get("gpus", [])
    has_gpu = bool(gpus)
    total_vram_gb = round(sum(g.get("vram_gb", 0.0) for g in gpus), 2)
    free_vram_gb = round(sum(g.get("free_gb", 0.0) for g in gpus) * (0.95 if len(gpus) > 1 else 1.0), 2)
    reserve_gb = round(vram_reserve_mb / 1024.0, 2)
    budget_vram_gb = round(max(0.0, free_vram_gb - reserve_gb) if has_gpu else 0.0, 2)

    size_bytes = float(model_info.get("size_bytes") or 0)
    S = size_bytes / GiB
    n_layers = int(model_info.get("n_layers") or 32)
    is_moe = bool(model_info.get("moe"))

    if is_moe:
        dense_gb = round(S * 0.08, 3)
        total_exp_gb = round(S * 0.92, 3)
        avg_exp_gb = total_exp_gb / max(1, n_layers)
        eff_n_cpu_moe = n_cpu_moe if n_cpu_moe is not None else 0
        expert_cpu_gb = round(min(n_layers, eff_n_cpu_moe) * avg_exp_gb, 3)
        expert_gpu_gb = round(max(0.0, total_exp_gb - expert_cpu_gb), 3)
    else:
        dense_gb = round(S, 3)
        total_exp_gb = 0.0
        expert_gpu_gb = 0.0
        expert_cpu_gb = 0.0
        avg_exp_gb = 0.0

    kv_per_tok = int(model_info.get("kv_per_tok") or 0)
    scale = _KV_SCALE.get(kv_type.lower(), 1.0)
    kv_cache_gb = round((kv_per_tok * scale * ctx) / GiB, 3) if kv_per_tok else round(0.5 * (ctx / 4096), 3)

    compute_workspace_gb = round(0.55 + (0.35 if mmproj_size_gb else 0.0) + (ubatch / 512) * 0.1, 3)
    mmproj_vram_gb = round((mmproj_size_gb + 0.4) if (mmproj_size_gb and mmproj_offload and has_gpu) else 0.0, 3)
    draft_vram_gb = round((float(draft_model.get("size_bytes") or 0) / GiB) if (draft_model and has_gpu) else 0.0, 3)

    used_vram_gb = round(
        ((dense_gb if has_gpu else 0.0) + expert_gpu_gb + kv_cache_gb + compute_workspace_gb + mmproj_vram_gb + draft_vram_gb)
        if has_gpu else 0.0, 2
    )
    vram_headroom_gb = round(max(0.0, budget_vram_gb - used_vram_gb) if has_gpu else 0.0, 2)
    ram_used_gb = round(expert_cpu_gb + (dense_gb if not has_gpu else 0.0) + 1.2, 2)

    # 当 MoE 专家部分上 CPU 且 GPU 仍有余量时，推荐配置 MoE 专家缓存（GPU Cache for CPU experts）
    if is_moe and expert_cpu_gb > 0 and has_gpu and vram_headroom_gb >= 0.25:
        rec_mib = int(min(4096, max(256, (vram_headroom_gb * 1024 * 0.45))))
        moe_cache_mib_recommended = (rec_mib // 64) * 64
    else:
        moe_cache_mib_recommended = 0

    # 预估推理特性（显存带宽瓶颈 vs 算力瓶颈）
    bound_type = "memory_bandwidth_bound" if (is_moe and expert_cpu_gb > 0) or not has_gpu else "compute_bound"
    active_b = float(model_info.get("experts_used") or 1) / max(1, int(model_info.get("experts") or 1)) if is_moe else 1.0

    # 启发式评估 tokens/sec 范围
    if has_gpu:
        base_tps = max(10.0, 180.0 / max(1.0, (dense_gb + expert_gpu_gb + expert_cpu_gb * 4.0)))
        est_decode_tps = round(base_tps * (1.3 if draft_model else 1.0), 1)
        est_prefill_tps = round(base_tps * 12.0 * (ubatch / 512), 1)
    else:
        est_decode_tps = round(max(1.5, 30.0 / max(1.0, S)), 1)
        est_prefill_tps = round(est_decode_tps * 5.0, 1)

    profile_estimate = {
        "bound_type": bound_type,
        "est_prefill_tps": est_prefill_tps,
        "est_decode_tps": est_decode_tps,
        "expert_active_ratio": round(active_b, 4),
    }

    return {
        "total_vram_gb": total_vram_gb,
        "free_vram_gb": free_vram_gb,
        "budget_vram_gb": budget_vram_gb,
        "dense_weights_gb": dense_gb,
        "expert_gpu_weights_gb": expert_gpu_gb,
        "expert_cpu_weights_gb": expert_cpu_gb,
        "kv_cache_gb": kv_cache_gb,
        "compute_workspace_gb": compute_workspace_gb,
        "mmproj_vram_gb": mmproj_vram_gb,
        "draft_vram_gb": draft_vram_gb,
        "used_vram_gb": used_vram_gb,
        "vram_headroom_gb": vram_headroom_gb,
        "ram_used_gb": ram_used_gb,
        "moe_cache_mib_recommended": moe_cache_mib_recommended,
        "profile_estimate": profile_estimate,
    }


class InferenceAdvisor:
    """前沿推理优化顾问：针对大模型与 MoE 提供自动投机解码策略、MoE 专家层缓存与执行参数。"""

    def __init__(
        self,
        hw_info: dict,
        model_info: dict,
        draft_models: list[dict] | None = None,
        engine_cfg: dict | None = None,
        has_flag_fn: Callable[[str], bool] | None = None,
    ):
        self.hw = hw_info
        self.model = model_info
        self.drafts = draft_models or []
        self.cfg = engine_cfg or {}
        self.has_flag = has_flag_fn or (lambda flag: True)

    def find_compatible_draft(self) -> dict | None:
        """从已探测模型中寻找最匹配的 Draft / EAGLE 模型。"""
        want = self.cfg.get("draft_model")
        if want and want != "auto":
            for d in self.drafts:
                if d.get("file") == want or d.get("path") == want:
                    return d
            return None

        # 优先读取 model_info 中自动配对的 draft
        paired = self.model.get("draft_model")
        if paired:
            for d in self.drafts:
                if d.get("file") == paired:
                    return d

        target_k = _stem_key(self.model.get("name", ""))
        scored: list[tuple[int, dict]] = []
        for d in self.drafts:
            dk = _stem_key(d.get("name", ""))
            score = 0
            if target_k and dk:
                if target_k == dk:
                    score = 10
                elif target_k in dk or dk in target_k:
                    score = 6
                elif target_k[:6] and dk[:6] and target_k[:6] == dk[:6]:
                    score = 3
            if d.get("size_bytes", 0) < self.model.get("size_bytes", float("inf")):
                score += 1
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[0][1] if scored else None

    def advise_speculative(self, vram_headroom_gb: float = 0.0, n_cpu_moe: int = 0) -> dict:
        """计算投机解码策略与相关命令行参数（仅发射 has_flag 验证过的参数）。"""
        mode = str(self.cfg.get("spec_mode", "auto")).lower()
        if mode == "off" or mode == "none":
            return {"mode": "off", "flags": [], "draft_model": None, "draft_n_max": 0, "draft_n_min": 0}

        selected_draft = None
        has_draft_support = (
            self.has_flag("-md") or self.has_flag("--model-draft") or self.has_flag("--spec-draft-model")
        )
        has_spec_type = self.has_flag("--spec-type")

        # 尝试选择 Draft 模型
        if mode in ("auto", "draft"):
            cand = self.find_compatible_draft()
            if cand:
                draft_gb = (cand.get("size_bytes", 0) / GiB)
                # 在 auto 模式下，若已有 CPU 专家溢出，不再挤占宝贵显存装载草稿模型（用户可显式设 draft 强制启用）
                can_fit = (n_cpu_moe == 0 and draft_gb <= vram_headroom_gb + 0.2) or not bool(self.hw.get("gpus"))
                if can_fit or mode == "draft":
                    selected_draft = cand

        flags: list[str] = []
        eff_mode = "none"
        draft_n_max = int(self.cfg.get("spec_draft_max", 0))
        draft_n_min = int(self.cfg.get("spec_draft_min", 0))

        if selected_draft and has_draft_support:
            eff_mode = "draft"
            is_eagle = "eagle" in selected_draft.get("file", "").lower() or "eagle" in selected_draft.get("name", "").lower()
            dpath = selected_draft.get("path") or selected_draft.get("file")

            if self.has_flag("-md"):
                flags += ["-md", dpath]
            elif self.has_flag("--model-draft"):
                flags += ["--model-draft", dpath]
            elif self.has_flag("--spec-draft-model"):
                flags += ["--spec-draft-model", dpath]

            if has_spec_type:
                spec_type = "draft-eagle3" if is_eagle else "draft-simple"
                flags += ["--spec-type", spec_type]

            eff_max = draft_n_max or (4 if is_eagle else 3)
            if self.has_flag("--spec-draft-n-max"):
                flags += ["--spec-draft-n-max", str(eff_max)]

            if draft_n_min > 0 and self.has_flag("--spec-draft-n-min"):
                flags += ["--spec-draft-n-min", str(draft_n_min)]

            if self.cfg.get("spec_draft_n_ubatch") and self.has_flag("--spec-draft-n-ubatch"):
                flags += ["--spec-draft-n-ubatch", str(int(self.cfg["spec_draft_n_ubatch"]))]

            return {
                "mode": eff_mode,
                "flags": flags,
                "draft_model": selected_draft.get("file"),
                "draft_path": dpath,
                "is_eagle": is_eagle,
                "draft_n_max": eff_max,
                "draft_n_min": draft_n_min,
            }

        # 若无兼容 draft 模型且 spec_mode 为 auto / ngram-mod，尝试使用 ngram-mod 投机解码
        if mode in ("auto", "ngram-mod", "ngram-simple") and has_spec_type:
            eff_mode = "ngram-simple" if mode == "ngram-simple" else "ngram-mod"
            flags += ["--spec-type", eff_mode]

            eff_max = draft_n_max or 64
            eff_min = draft_n_min or 16

            if self.has_flag("--spec-draft-n-max"):
                flags += ["--spec-draft-n-max", str(eff_max)]
            elif self.has_flag("--spec-ngram-mod-n-max") and eff_mode == "ngram-mod":
                flags += ["--spec-ngram-mod-n-max", str(eff_max)]

            if eff_min > 0:
                if self.has_flag("--spec-draft-n-min"):
                    flags += ["--spec-draft-n-min", str(eff_min)]
                elif self.has_flag("--spec-ngram-mod-n-min") and eff_mode == "ngram-mod":
                    flags += ["--spec-ngram-mod-n-min", str(eff_min)]

            return {
                "mode": eff_mode,
                "flags": flags,
                "draft_model": None,
                "draft_n_max": eff_max,
                "draft_n_min": eff_min,
            }

        return {"mode": "none", "flags": [], "draft_model": None, "draft_n_max": 0, "draft_n_min": 0}

    def advise_moe(self, n_cpu_moe: int, total_layers: int, rec_cache_mib: int = 0) -> dict:
        """为 MoE 模型规划 GPU/CPU 专家切分及 --moe-cache-mib 参数。"""
        flags: list[str] = []
        is_moe = bool(self.model.get("moe"))
        cfg_cache = self.cfg.get("moe_cache_mib")
        eff_cache = int(cfg_cache) if cfg_cache not in (None, "auto", "") else rec_cache_mib

        if is_moe and n_cpu_moe > 0:
            if n_cpu_moe >= total_layers and self.has_flag("--cpu-moe"):
                flags += ["--cpu-moe"]
            elif self.has_flag("--n-cpu-moe"):
                flags += ["--n-cpu-moe", str(n_cpu_moe)]
            elif self.has_flag("-ncmoe"):
                flags += ["-ncmoe", str(n_cpu_moe)]
            elif self.has_flag("--override-tensor"):
                layers = "|".join(str(i) for i in range(n_cpu_moe))
                flags += ["-ot", rf"blk\.({layers})\.ffn_.*_exps\.=CPU"]

            if eff_cache > 0 and self.has_flag("--moe-cache-mib"):
                flags += ["--moe-cache-mib", str(eff_cache)]

            share = self.cfg.get("moe_cache_cpu_share")
            if share is not None and float(share) > 0.0 and self.has_flag("--moe-cache-cpu-share"):
                flags += ["--moe-cache-cpu-share", str(float(share))]

        return {
            "is_moe": is_moe,
            "n_cpu_moe": n_cpu_moe,
            "moe_cache_mib": eff_cache if (is_moe and n_cpu_moe > 0) else 0,
            "flags": flags,
        }

    def advise_threads(self, gen_threads: int) -> dict:
        """计算物理核心（生成）与全逻辑线程（批处理与预填）参数。"""
        flags: list[str] = []
        logical = int(self.hw.get("cpu_threads") or 1)
        cfg_tb = self.cfg.get("threads_batch")
        tb = int(cfg_tb) if cfg_tb is not None and int(cfg_tb) > 0 else logical

        if tb > gen_threads:
            if self.has_flag("--threads-batch"):
                flags += ["--threads-batch", str(tb)]
            elif self.has_flag("-tb"):
                flags += ["-tb", str(tb)]

        return {"threads": gen_threads, "threads_batch": tb, "flags": flags}

    def advise_cache_reuse(self) -> dict:
        """前缀 KV 缓存复用阈值规划。"""
        flags: list[str] = []
        cfg_val = self.cfg.get("cache_reuse")
        threshold = int(cfg_val) if cfg_val is not None and int(cfg_val) > 0 else 0
        if threshold > 0 and self.has_flag("--cache-reuse"):
            flags += ["--cache-reuse", str(threshold)]
        return {"threshold": threshold, "flags": flags}

    def advise_memory_lock(self, n_cpu_moe: int = 0) -> dict:
        """内存锁定（mlock）与载入模式：混合 CPU-MoE 且内存充足时默认 --load-mode none 以消除 mmap 页故障开销。"""
        flags: list[str] = []
        mlock = bool(self.cfg.get("mlock", False))
        mode = "default"
        if mlock:
            mode = "mmap+mlock"
            if self.has_flag("--load-mode"):
                flags += ["--load-mode", "mmap+mlock"]
            elif self.has_flag("--mlock"):
                flags += ["--mlock"]
        elif n_cpu_moe > 0 and self.has_flag("--load-mode"):
            ram_free = float(self.hw.get("ram_free_gb") or self.hw.get("ram_gb") or 0.0)
            model_gb = float(self.model.get("size_gb") or (self.model.get("size_bytes") or 0) / GiB)
            if ram_free >= model_gb + 2.0:
                mode = "none"
                flags += ["--load-mode", "none"]
        return {"enabled": mlock, "mode": mode, "flags": flags}

    def advise(
        self,
        ctx: int,
        kv_type: str = "f16",
        ubatch: int = 512,
        n_cpu_moe: int = 0,
        gen_threads: int = 4,
        mmproj_size_gb: float = 0.0,
        mmproj_offload: bool = True,
    ) -> dict:
        """生成全面推理建议与参数清单。"""
        budget = kv_budget_analysis(
            hw_info=self.hw,
            model_info=self.model,
            ctx=ctx,
            kv_type=kv_type,
            ubatch=ubatch,
            n_cpu_moe=n_cpu_moe,
            mmproj_size_gb=mmproj_size_gb,
            mmproj_offload=mmproj_offload,
        )

        spec = self.advise_speculative(vram_headroom_gb=budget["vram_headroom_gb"], n_cpu_moe=n_cpu_moe)
        # 若投机解码选择了 draft 模型，重新刷新含 draft 显存占用的预算
        if spec.get("draft_model"):
            cand = self.find_compatible_draft()
            budget = kv_budget_analysis(
                hw_info=self.hw,
                model_info=self.model,
                ctx=ctx,
                kv_type=kv_type,
                ubatch=ubatch,
                draft_model=cand,
                n_cpu_moe=n_cpu_moe,
                mmproj_size_gb=mmproj_size_gb,
                mmproj_offload=mmproj_offload,
            )

        n_layers = int(self.model.get("n_layers") or 32)
        moe = self.advise_moe(n_cpu_moe=n_cpu_moe, total_layers=n_layers, rec_cache_mib=budget["moe_cache_mib_recommended"])
        threads = self.advise_threads(gen_threads=gen_threads)
        cache_reuse = self.advise_cache_reuse()
        mem_lock = self.advise_memory_lock(n_cpu_moe=n_cpu_moe)

        return {
            "spec": spec,
            "moe": moe,
            "threads": threads,
            "cache_reuse": cache_reuse,
            "memory_lock": mem_lock,
            "kv_budget": budget,
        }


def advise(
    hw_info: dict,
    model_info: dict,
    draft_models: list[dict] | None = None,
    engine_cfg: dict | None = None,
    has_flag_fn: Callable[[str], bool] | None = None,
    ctx: int = 32768,
    kv_type: str = "f16",
    ubatch: int = 512,
    n_cpu_moe: int = 0,
    gen_threads: int = 4,
) -> dict:
    """快捷入口函数。"""
    advisor = InferenceAdvisor(hw_info, model_info, draft_models, engine_cfg, has_flag_fn)
    return advisor.advise(ctx=ctx, kv_type=kv_type, ubatch=ubatch, n_cpu_moe=n_cpu_moe, gen_threads=gen_threads)


class TelemetryTracker:
    """推理遥测跟踪器：维护 prompt_tps、eval_tps、投机解码采纳率与缓存命中率的滑动指数平均。"""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self.prompt_tps = 0.0
        self.eval_tps = 0.0
        self.spec_accept_rate: float | None = None
        self.cache_hit_rate = 0.0
        self.total_prompt_tokens = 0
        self.total_eval_tokens = 0
        self.requests = 0

    def record_tps(self, tps: float) -> None:
        """记录单一生成 TPS。"""
        if tps > 0:
            val = float(tps)
            self.eval_tps = val if self.eval_tps == 0.0 else self.alpha * val + (1.0 - self.alpha) * self.eval_tps

    def record_timings(self, timings: dict) -> None:
        """解析并记录 llama.cpp /v1/chat/completions 返回的完整 timings 数据。"""
        if not isinstance(timings, dict):
            return
        self.requests += 1

        p_tps = timings.get("prompt_per_second") or timings.get("prompt_tps")
        if p_tps and float(p_tps) > 0:
            val = float(p_tps)
            self.prompt_tps = val if self.prompt_tps == 0.0 else self.alpha * val + (1.0 - self.alpha) * self.prompt_tps

        e_tps = timings.get("predicted_per_second") or timings.get("eval_tps") or timings.get("tps")
        if e_tps and float(e_tps) > 0:
            val = float(e_tps)
            self.eval_tps = val if self.eval_tps == 0.0 else self.alpha * val + (1.0 - self.alpha) * self.eval_tps

        # 投机解码采纳率
        rate = timings.get("spec_accept_rate")
        if rate is None:
            draft_n = next((timings[k] for k in ("draft_n", "draft_total") if timings.get(k) is not None), None)
            draft_acc = next((timings[k] for k in ("draft_n_accepted", "draft_accepted", "draft_hits") if timings.get(k) is not None), None)
            if draft_n and int(draft_n) > 0 and draft_acc is not None:
                rate = float(draft_acc) / float(draft_n)
        if rate is not None:
            val = float(rate)
            self.spec_accept_rate = val if self.spec_accept_rate is None else self.alpha * val + (1.0 - self.alpha) * self.spec_accept_rate

        # 缓存命中率
        hit = next((timings[k] for k in ("n_cache_hit", "cache_n") if timings.get(k) is not None), None)
        p_n = timings.get("prompt_n") or timings.get("prompt_tokens")
        if hit is not None and p_n is not None and (int(hit) + int(p_n)) > 0:
            denom = float(int(hit) + int(p_n)) if "cache_n" in timings else float(int(p_n))
            hr = float(hit) / denom
            self.cache_hit_rate = hr if self.cache_hit_rate == 0.0 else self.alpha * hr + (1.0 - self.alpha) * self.cache_hit_rate

        # Token 累加
        if p_n:
            self.total_prompt_tokens += int(p_n)
        comp_n = timings.get("predicted_n") or timings.get("completion_tokens")
        if comp_n:
            self.total_eval_tokens += int(comp_n)

    def benchmark_summary(self) -> dict:
        """返回性能指标字典。"""
        return {
            "prompt_tps": round(self.prompt_tps, 1),
            "eval_tps": round(self.eval_tps, 1),
            "tps": round(self.eval_tps, 1),
            "spec_accept_rate": round(self.spec_accept_rate, 3) if self.spec_accept_rate is not None else None,
            "cache_hit_rate": round(self.cache_hit_rate, 3),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_eval_tokens": self.total_eval_tokens,
            "requests": self.requests,
        }


def benchmark_summary(tracker: TelemetryTracker | Any) -> dict:
    """获取 TelemetryTracker 或 Engine 实例的性能概览。"""
    if hasattr(tracker, "benchmark_summary"):
        return tracker.benchmark_summary()
    if hasattr(tracker, "telemetry"):
        return tracker.telemetry.benchmark_summary()
    return {"prompt_tps": 0.0, "eval_tps": 0.0, "tps": 0.0, "spec_accept_rate": None, "cache_hit_rate": 0.0}
