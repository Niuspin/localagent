"""前沿推理优化（MoE 专家切分、投机解码、KV 预算与吞吐量遥测）的单元测试。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_gguf  # noqa: E402
from lagent import gguf, hw, inference  # noqa: E402
from lagent import profile as P  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.engine import Engine  # noqa: E402
from lagent.inference import InferenceAdvisor, TelemetryTracker, kv_budget_analysis  # noqa: E402

GB = 1_000_000_000
GiB = 1024 ** 3


def fake_hw(vram_gb=16.0, free_gb=14.0, ram_gb=32.0, ram_free=24.0, cpu_threads=16, cpu_cores=8):
    gpus = [{"name": "RTX 4080", "vram_gb": vram_gb, "free_gb": free_gb, "vendor": "nvidia"}] if vram_gb else []
    return {
        "os": "windows", "arch": "AMD64", "cpu_threads": cpu_threads, "cpu_cores": cpu_cores,
        "ram_gb": ram_gb, "ram_free_gb": ram_free, "gpus": gpus, "unified": False,
    }


def fake_model(name="Mock-MoE", size_gb=20.0, moe=True, experts=64, used=8, n_layers=32, quant="Q4_K_M"):
    return {
        "name": name, "file": f"{name}.gguf", "path": f"/models/{name}.gguf",
        "size_bytes": int(size_gb * GiB), "moe": moe, "experts": experts,
        "experts_used": used, "n_layers": n_layers, "quant": quant,
        "kv_per_tok": 128 * 2 * n_layers, "ctx_train": 32768, "arch": "llama",
    }


# ------------------------------------------------------------------ 1. KV 缓存与显存预算分析
def test_kv_budget_dense_vs_moe():
    h = fake_hw(vram_gb=16.0, free_gb=14.0)
    dense_m = fake_model(name="Dense-14B", size_gb=9.0, moe=False)
    b_dense = kv_budget_analysis(h, dense_m, ctx=16384, kv_type="f16", ubatch=512)

    assert b_dense["dense_weights_gb"] == 9.0
    assert b_dense["expert_gpu_weights_gb"] == 0.0
    assert b_dense["expert_cpu_weights_gb"] == 0.0
    assert b_dense["kv_cache_gb"] > 0
    assert b_dense["moe_cache_mib_recommended"] == 0
    assert b_dense["profile_estimate"]["bound_type"] == "compute_bound"

    # MoE 模型：专家层部分上 CPU（当 22 层在 CPU，10 层在 GPU，留出充足 VRAM 余量用于专家缓存）
    moe_m = fake_model(name="MoE-60B", size_gb=32.0, moe=True, n_layers=32)
    b_moe = kv_budget_analysis(h, moe_m, ctx=8192, kv_type="q8_0", ubatch=512, n_cpu_moe=24)

    assert b_moe["dense_weights_gb"] < 3.0  # 约 8%
    assert b_moe["expert_cpu_weights_gb"] > 18.0  # 约 75% 的专家层在 CPU
    assert b_moe["expert_gpu_weights_gb"] > 0.0
    assert b_moe["vram_headroom_gb"] >= 0.25
    assert b_moe["moe_cache_mib_recommended"] >= 256  # 存在 GPU 余量时推荐专家缓存
    assert b_moe["profile_estimate"]["bound_type"] == "memory_bandwidth_bound"


# ------------------------------------------------------------------ 2. GGUF Draft 检测与自动配对
def test_gguf_draft_detection_and_pairing(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    # 创建主模型与 EAGLE3 Draft 模型
    main_p = models_dir / "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"
    draft_p = models_dir / "EAGLE3-Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"
    make_gguf.make(main_p, name="Qwen2.5-Coder-32B-Instruct", layers=4, experts=0)
    make_gguf.make(draft_p, name="EAGLE3-Qwen2.5-Coder-32B-Instruct", layers=1, experts=0)

    # 扫描并验证
    all_models, mm = gguf.scan(models_dir)
    assert len(all_models) == 2

    draft_info = next(m for m in all_models if "eagle" in m["file"].lower())
    main_info = next(m for m in all_models if "eagle" not in m["file"].lower())

    assert draft_info["is_draft"] is True
    assert draft_info["draft"] is True
    assert main_info["is_draft"] is False

    # 验证主模型自动配对上 Draft
    assert main_info["draft_model"] == draft_info["file"]
    assert draft_info["file"] in main_info["compatible_drafts"]

    # 验证 scan_all 3-tuple 签名
    mains, mms, drafts = gguf.scan_all(models_dir)
    assert len(mains) == 1 and mains[0]["file"] == main_info["file"]
    assert len(drafts) == 1 and drafts[0]["file"] == draft_info["file"]


# ------------------------------------------------------------------ 3. 投机解码自动模式选择
def test_speculative_decoding_auto_draft_selection():
    h = fake_hw(vram_gb=24.0, free_gb=20.0)
    main_m = fake_model(name="Qwen2.5-Coder-32B", size_gb=18.0)
    draft_m = {
        "name": "EAGLE3-Qwen2.5-Coder-32B", "file": "EAGLE3-Qwen2.5-Coder-32B.gguf",
        "path": "/models/EAGLE3-Qwen2.5-Coder-32B.gguf", "size_bytes": int(1.2 * GiB), "is_draft": True,
    }

    # 当支持 -md 与 --spec-type 时，auto 模式选用 draft 模型
    has_fn = lambda flag: flag in ("-md", "--spec-type", "--spec-draft-n-max", "--spec-draft-n-min")
    advisor = InferenceAdvisor(h, main_m, draft_models=[draft_m], engine_cfg={"spec_mode": "auto"}, has_flag_fn=has_fn)
    res = advisor.advise_speculative(vram_headroom_gb=5.0)

    assert res["mode"] == "draft"
    assert res["draft_model"] == draft_m["file"]
    assert res["is_eagle"] is True
    assert "-md" in res["flags"] and res["flags"][res["flags"].index("-md") + 1] == draft_m["path"]
    assert "--spec-type" in res["flags"] and res["flags"][res["flags"].index("--spec-type") + 1] == "draft-eagle3"
    assert "--spec-draft-n-max" in res["flags"]


def test_speculative_decoding_ngram_mod_fallback():
    h = fake_hw(vram_gb=16.0, free_gb=12.0)
    main_m = fake_model(name="Qwen2.5-Coder-32B", size_gb=18.0)

    # 没有 draft 模型，但支持 --spec-type
    has_fn = lambda flag: flag in ("--spec-type", "--spec-draft-n-max")
    advisor = InferenceAdvisor(h, main_m, draft_models=[], engine_cfg={"spec_mode": "auto"}, has_flag_fn=has_fn)
    res = advisor.advise_speculative(vram_headroom_gb=2.0)

    assert res["mode"] == "ngram-mod"
    assert res["draft_model"] is None
    assert "--spec-type" in res["flags"] and res["flags"][res["flags"].index("--spec-type") + 1] == "ngram-mod"
    assert "--spec-draft-n-max" in res["flags"]


def test_speculative_decoding_off():
    h = fake_hw()
    main_m = fake_model()
    advisor = InferenceAdvisor(h, main_m, draft_models=[], engine_cfg={"spec_mode": "off"}, has_flag_fn=lambda _: True)
    res = advisor.advise_speculative()
    assert res["mode"] == "off" and not res["flags"]


# ------------------------------------------------------------------ 4. 标志位严格守卫（未支持的绝不发射）
def test_flags_strict_guarding_when_server_lacks_features():
    h = fake_hw(vram_gb=16.0, free_gb=12.0)
    moe_m = fake_model(name="MoE-60B", size_gb=30.0, moe=True)

    # 模拟老版本 llama-server：不支持 spec-type、不支持 moe-cache-mib、不支持 threads-batch
    supported = {"-ngl", "-c", "-b", "-ub", "-t", "-fa", "--n-cpu-moe"}
    has_fn = lambda f: f in supported

    advisor = InferenceAdvisor(h, moe_m, draft_models=[], engine_cfg={"spec_mode": "auto", "moe_cache_mib": 1024, "cache_reuse": 256}, has_flag_fn=has_fn)
    plan = advisor.advise(ctx=8192, kv_type="f16", ubatch=512, n_cpu_moe=8, gen_threads=8)

    # spec flags 必须为空（因为 --spec-type 不支持）
    assert not plan["spec"]["flags"]
    # moe flags 只能包含 --n-cpu-moe，不能包含 --moe-cache-mib
    assert "--n-cpu-moe" in plan["moe"]["flags"]
    assert "--moe-cache-mib" not in plan["moe"]["flags"]
    # threads flags 必须为空（因为 -tb 不支持）
    assert not plan["threads"]["flags"]
    # cache reuse 必须为空
    assert not plan["cache_reuse"]["flags"]


def test_cpu_moe_and_ot_fallbacks():
    h = fake_hw()
    moe_m = fake_model(size_gb=40.0, moe=True, n_layers=32)

    # 情况 A：全部层在 CPU，且支持 --cpu-moe
    adv_a = InferenceAdvisor(h, moe_m, has_flag_fn=lambda f: f in ("--cpu-moe", "--n-cpu-moe"))
    res_a = adv_a.advise_moe(n_cpu_moe=32, total_layers=32)
    assert "--cpu-moe" in res_a["flags"]

    # 情况 B：不支持 --n-cpu-moe，回退到 -ot 正则
    adv_b = InferenceAdvisor(h, moe_m, has_flag_fn=lambda f: f == "--override-tensor")
    res_b = adv_b.advise_moe(n_cpu_moe=4, total_layers=32)
    assert "-ot" in res_b["flags"]
    assert r"blk\.(0|1|2|3)\.ffn_.*_exps\.=CPU" in res_b["flags"]


# ------------------------------------------------------------------ 5. Profile MoE ubatch 扩展
def test_profile_moe_ubatch_scaling(tmp_path):
    c = Config(tmp_path / "c.json", tmp_path / "s.json")

    # 超大 MoE 模型：ubatch 自动上调至 2048 加速 prefill
    big_moe = {"size_bytes": int(140 * GB), "quant": "Q4_K_M", "moe": True, "experts": 128, "experts_used": 8}
    p_big = P.for_model(c, big_moe).tuned(c)
    assert p_big.tier == "large" and p_big.ubatch == 2048

    # 小密集模型：保持 512
    p_small = P.for_model(c, {"size_bytes": int(3 * GB), "quant": "Q4_K_M", "moe": False}).tuned(c)
    assert p_small.tier == "compact" and p_small.ubatch == 512

    # 用户显式指定 ubatch 覆盖
    c.patch({"engine": {"ubatch": 256}})
    p_user = P.for_model(c, big_moe).tuned(c)
    assert p_user.ubatch == 256


# ------------------------------------------------------------------ 6. 批处理线程与缓存复用建议
def test_threads_batch_and_cache_reuse_advise():
    h = fake_hw(cpu_threads=32, cpu_cores=16)
    has_fn = lambda f: f in ("--threads-batch", "-tb", "--cache-reuse", "--load-mode")
    advisor = InferenceAdvisor(h, fake_model(), engine_cfg={"cache_reuse": 256, "mlock": True}, has_flag_fn=has_fn)

    t_res = advisor.advise_threads(gen_threads=16)
    assert "--threads-batch" in t_res["flags"]
    assert t_res["flags"][t_res["flags"].index("--threads-batch") + 1] == "32"

    cr_res = advisor.advise_cache_reuse()
    assert "--cache-reuse" in cr_res["flags"]
    assert cr_res["flags"][cr_res["flags"].index("--cache-reuse") + 1] == "256"

    lock_res = advisor.advise_memory_lock()
    assert "--load-mode" in lock_res["flags"]
    assert lock_res["flags"][lock_res["flags"].index("--load-mode") + 1] == "mmap+mlock"


# ------------------------------------------------------------------ 7. 遥测与 TPS / 采纳率计算
def test_telemetry_tracker_moving_average():
    tracker = TelemetryTracker(alpha=0.5)

    tracker.record_timings({
        "prompt_per_second": 800.0,
        "predicted_per_second": 40.0,
        "draft_n": 20,
        "draft_accepted": 16,
        "prompt_n": 100,
        "predicted_n": 50,
        "n_cache_hit": 20,
    })

    s1 = tracker.benchmark_summary()
    assert s1["prompt_tps"] == 800.0
    assert s1["eval_tps"] == 40.0
    assert s1["spec_accept_rate"] == 0.8
    assert s1["cache_hit_rate"] == 0.2
    assert s1["total_prompt_tokens"] == 100
    assert s1["total_eval_tokens"] == 50

    # 第二次上报：验证 EMA 平滑更新
    tracker.record_timings({
        "prompt_per_second": 1000.0,
        "predicted_per_second": 50.0,
        "draft_n": 10,
        "draft_accepted": 6,
        "prompt_n": 100,
        "predicted_n": 50,
        "n_cache_hit": 40,
    })

    s2 = tracker.benchmark_summary()
    assert s2["prompt_tps"] == 900.0   # 0.5 * 1000 + 0.5 * 800
    assert s2["eval_tps"] == 45.0     # 0.5 * 50 + 0.5 * 40
    assert s2["spec_accept_rate"] == 0.7  # 0.5 * 0.6 + 0.5 * 0.8
    assert s2["cache_hit_rate"] == 0.3    # 0.5 * 0.4 + 0.5 * 0.2
    assert s2["requests"] == 2


# ------------------------------------------------------------------ 8. 引擎完整整合测试
def test_engine_plan_and_build_args_with_advisor(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    make_gguf.make(models / "m-Q4_K_M.gguf")

    c = Config(tmp_path / "c.json", tmp_path / "s.json")
    eng = Engine(c, models, tmp_path / "bin")
    eng.binary, eng.port, eng.api_key = Path("llama-server"), 8080, "test-key"
    eng._help_text = "--cache-ram --fit --n-cpu-moe --threads-batch --cache-reuse --spec-type --spec-draft-n-max --moe-cache-mib"

    plan = eng.plan()
    assert "spec" in plan
    assert "kv_budget" in plan
    assert "moe_cache_mib" in plan
    assert "threads_batch" in plan
    assert "cache_reuse" in plan

    # 验证 state() 暴露了 perf
    st = eng.state()
    assert "perf" in st
    assert "prompt_tps" in st["perf"]
    assert "cache_hit_rate" in st["perf"]

    # 验证 build_args 生成了对应标志位
    args = eng.build_args(plan)
    assert "-c" in args
    assert "-ub" in args
    # 因为 help 支持 --spec-type 且无 draft 模型，auto 默认选用 ngram-mod
    if eng.has("--spec-type"):
        assert "--spec-type" in args
        assert args[args.index("--spec-type") + 1] == "ngram-mod"


def test_unrelated_draft_not_paired(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    make_gguf.make(models / "Qwen3-0.6B-Q8_0.gguf", name="Qwen3-0.6B", arch="qwen3", layers=16, experts=0)
    make_gguf.make(models / "eagle3-gpt-oss-20b-Q8_0.gguf", name="gpt-oss-20b", arch="eagle3", layers=4, experts=0)
    scanned, _ = gguf.scan(models)
    qwen = next(m for m in scanned if "Qwen3" in m["file"])
    assert qwen["draft_model"] is None
    assert qwen["compatible_drafts"] == []


def test_load_mode_none_for_hybrid_moe_and_llama_server_timings():
    h = fake_hw(vram_gb=8.0, free_gb=7.5, ram_gb=32.0, ram_free=24.0)
    moe = fake_model(name="gpt-oss-20b", moe=True, size_gb=12.1)
    advisor = InferenceAdvisor(h, moe, has_flag_fn=lambda f: f in ("--load-mode", "--spec-type"))
    ml = advisor.advise_memory_lock(n_cpu_moe=17)
    assert ml["mode"] == "none"
    assert ml["flags"] == ["--load-mode", "none"]

    tt = TelemetryTracker()
    tt.record_timings({
        "prompt_n": 100, "cache_n": 50, "prompt_per_second": 108.8,
        "predicted_n": 250, "predicted_per_second": 31.3,
        "draft_n": 348, "draft_n_accepted": 162,
    })
    s = tt.benchmark_summary()
    assert abs(s["spec_accept_rate"] - 162 / 348) < 0.01
    assert abs(s["cache_hit_rate"] - 50 / 150) < 0.01
