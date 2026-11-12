"""模型档位、引擎规划接入、上下文扩容顺序、运行位置与启动器的单元测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_gguf  # noqa: E402
from lagent import appwindow, paths, toolset  # noqa: E402
from lagent import context as C  # noqa: E402
from lagent import profile as P  # noqa: E402
from lagent.config import Config  # noqa: E402
from lagent.engine import Engine  # noqa: E402
from lagent.launcher import SingleInstance  # noqa: E402
from lagent.loop import Agent  # noqa: E402

GB = 1_000_000_000


def info(size_gb, quant="Q4_K_M", moe=False, experts=None, used=None):
    return {"size_bytes": int(size_gb * GB), "quant": quant, "moe": moe, "experts": experts, "experts_used": used}


def cfg(tmp_path, **patch):
    c = Config(tmp_path / "c.json", tmp_path / "s.json")
    if patch:
        c.patch(patch)
    return c


# ------------------------------------------------------------------ 分档
@pytest.mark.parametrize("model,tier", [
    (info(2.5), "compact"),                                   # 4B 稠密
    (info(18.6, moe=True, experts=128, used=8), "compact"),   # 30B-A3B：激活很小，按“小模型”照顾
    (info(8.5), "standard"),                                  # 14B 稠密
    (info(19.8), "standard"),                                 # 32B 稠密
    (info(42), "large"),                                      # 70B 稠密
    (info(142, moe=True, experts=128, used=8), "large"),      # 235B-A22B
    (info(63, quant="MXFP4", moe=True, experts=128, used=4), "large"),   # 百亿级 MoE（≈117B）
])
def test_classify(model, tier):
    assert P.classify(model)[0] == tier


def test_classify_without_info_is_standard():
    assert P.classify(None)[0] == "standard"


def test_bits_per_weight_families():
    assert P.bits_per_weight("Q4_K_M") == 4.85
    assert P.bits_per_weight("UD-Q2_K_XL") == 2.9      # 动态量化命名按家族估算
    assert P.bits_per_weight("IQ3_XXS") == 3.1
    assert P.bits_per_weight("") == P.bits_per_weight("???")   # 未知 → 默认值


def test_moe_estimate_is_sane():
    total, active = P.estimate(info(142, moe=True, experts=128, used=8))
    assert 200 < total < 260 and 20 < active < 35


def test_manual_and_off_profile(tmp_path):
    big = info(142, moe=True, experts=128, used=8)
    assert P.for_model(cfg(tmp_path, agent={"profile": "compact"}), big).tier == "compact"
    off = P.for_model(cfg(tmp_path, agent={"profile": "off"}), big)
    assert off.tier == "standard" and off.max_steps == 40         # off = 旧版行为


def test_standard_equals_legacy_defaults(tmp_path):
    p = P.for_model(cfg(tmp_path), info(19.8)).tuned(cfg(tmp_path))
    assert (p.recall_k, p.max_steps, p.keep_recent_tools, p.tool_cap, p.ubatch, p.cache_ram_mb) == (6, 40, 4, 30000, 512, 4096)
    assert p.grow_first and not p.preload_tools


def test_user_values_beat_tier(tmp_path):
    c = cfg(tmp_path, agent={"max_steps": 12}, engine={"ubatch": 256})
    big = P.for_model(c, info(142, moe=True, experts=128, used=8)).tuned(c)
    assert big.tier == "large" and big.max_steps == 12 and big.ubatch == 256   # 改过的永远按用户的
    assert big.recall_k == 10 and big.preload_tools and not big.grow_first       # 没改的由档位接管
    small = P.for_model(c, info(2.5)).tuned(c)
    assert small.recall_k == 4 and small.tool_cap == 12000


def test_static_groups_exclude_dynamic():
    toolset.set_dynamic({"mcp:demo": (("mcp__demo__x",), "demo")})
    try:
        assert toolset.static_groups() == set(toolset.EXT_GROUPS)
        assert "mcp:demo" not in toolset.static_groups()
    finally:
        toolset.set_dynamic({})


# ------------------------------------------------------------------ 引擎规划
def make_engine(tmp_path, **model_kw):
    models = tmp_path / "models"
    make_gguf.make(models / "m-Q4_K_M.gguf", **model_kw)
    c = cfg(tmp_path)
    eng = Engine(c, models, tmp_path / "bin")
    return eng


def test_plan_carries_profile_and_batch(tmp_path):
    eng = make_engine(tmp_path)
    plan = eng.plan()
    assert plan["profile"]["tier"] == "compact" and "档位:紧凑" in plan["summary"]
    assert plan["ubatch"] == 512 and plan["batch"] == 2048
    assert plan["cache_ram_mb"] <= 4096


def test_build_args_use_plan_values(tmp_path):
    eng = make_engine(tmp_path)
    eng.binary, eng.port, eng.api_key = Path("llama-server"), 1234, "k"
    eng._help_text = "--cache-ram --fit --n-cpu-moe"
    plan = eng.plan()
    plan.update(ubatch=1024, batch=4096, cache_ram_mb=777)
    a = eng.build_args(plan)
    assert a[a.index("-ub") + 1] == "1024" and a[a.index("-b") + 1] == "4096" and a[a.index("--cache-ram") + 1] == "777"


def test_large_tier_raises_start_ctx_and_ubatch(tmp_path):
    eng = make_engine(tmp_path)
    eng.cfg.patch({"agent": {"profile": "large"}})
    plan = eng.plan()
    assert plan["profile"]["tier"] == "large" and plan["ubatch"] == 1024
    assert plan["ctx"] <= plan["ctx_cap"]


def test_tuned_before_model_is_loaded(tmp_path):
    eng = make_engine(tmp_path)
    assert eng.tuned().tier == "standard"           # 没加载模型时用缺省档位
    eng.profile = P.for_model(eng.cfg, info(142, moe=True, experts=128, used=8))
    assert eng.tuned().tier == "large" and eng.state()["profile"]["label"] == "大型"


# ------------------------------------------------------------------ 上下文：先压缩还是先扩容
class _Eng:
    def __init__(self, prof, order):
        self.ctx, self.ctx_cap, self.order, self._prof, self.error = 1024, 4096, order, prof, None

    def tuned(self):
        return self._prof

    async def restart(self, **kw):
        self.order.append("grow")
        self.ctx = kw["ctx"]

    async def wait_ready(self):
        return True


def _fit_order(tmp_path, tier):
    order: list[str] = []
    prof = P.for_model(cfg(tmp_path, agent={"profile": tier}), None).tuned(cfg(tmp_path))
    stub = SimpleNamespace(cfg=cfg(tmp_path), engine=_Eng(prof, order))
    stub.system_prompt = lambda *a: "sys"

    async def compact(run, sess):
        order.append("compact")
        sess.history = [{"role": "user", "content": "摘要"}]      # 真的压短，才能验证“压缩后放得下就不扩容”
        return True
    stub._compact = compact
    sess = SimpleNamespace(history=[{"role": "user", "content": "字" * 1500}], usage={}, ui=[])
    run = SimpleNamespace(emit=lambda *a, **k: None)
    asyncio.run(Agent._fit_context(stub, run, sess, C.Meter(), [], "ask", ""))
    return order


def test_standard_grows_before_compacting(tmp_path):
    assert _fit_order(tmp_path, "standard") == ["grow", "compact"]


def test_large_compacts_before_growing(tmp_path):
    order = _fit_order(tmp_path, "large")
    assert order[0] == "compact"
    assert "grow" not in order          # 压缩后已经放得下，就不必重载模型


# ------------------------------------------------------------------ 运行位置（源码 / 打包）
def test_source_layout():
    assert (paths.CODE_DIR / "web" / "index.html").is_file()
    assert (paths.CODE_DIR / "lagent" / "builtin_skills").is_dir()


def _freeze(monkeypatch, tmp_path):
    exe = tmp_path / "app" / "LocalAgent.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "app" / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("LAGENT_HOME", raising=False)
    monkeypatch.setattr(paths, "FROZEN", True)
    return exe


def test_frozen_is_portable_next_to_exe(monkeypatch, tmp_path):
    exe = _freeze(monkeypatch, tmp_path)
    assert paths.code_dir() == (tmp_path / "app" / "_internal").resolve()
    assert paths.data_root() == exe.parent.resolve()


def test_frozen_falls_back_to_user_dir_when_readonly(monkeypatch, tmp_path):
    _freeze(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "_writable", lambda p: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.data_root().name == "LocalAgent" and str(tmp_path) in str(paths.data_root())


def test_env_override_wins(monkeypatch, tmp_path):
    _freeze(monkeypatch, tmp_path)
    monkeypatch.setenv("LAGENT_HOME", str(tmp_path / "custom"))
    assert paths.data_root() == (tmp_path / "custom").resolve()


def test_ensure_dirs(tmp_path):
    assert paths.ensure_dirs(tmp_path / "root") == []
    assert all((tmp_path / "root" / d).is_dir() for d in ("models", "bin", "skills", "data"))


# ------------------------------------------------------------------ 启动器
@pytest.mark.skipif(sys.platform == "win32", reason="用 flock 语义验证；Windows 走 msvcrt")
def test_single_instance_lock(tmp_path):
    a, b = SingleInstance(tmp_path / "d" / ".lock"), SingleInstance(tmp_path / "d" / ".lock")
    assert a.acquire() is True
    assert b.acquire() is False
    a.fh.close()
    assert SingleInstance(tmp_path / "d" / ".lock").acquire() is True


def test_app_window_args(monkeypatch, tmp_path):
    a = appwindow.window_args("edge", "http://127.0.0.1:1/?t=x", tmp_path / "prof")
    assert a[0] == "edge" and "--app=http://127.0.0.1:1/?t=x" in a and f"--user-data-dir={tmp_path / 'prof'}" in a
    monkeypatch.setattr(appwindow, "_candidates", lambda: [str(tmp_path / "nope")])
    assert appwindow.find_browser() is None
    assert appwindow.open_window("http://x", tmp_path / "p") is None      # 没有可用浏览器 → 交给启动器退回默认浏览器
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setattr(appwindow, "_candidates", lambda: [str(tmp_path / "nope"), str(fake)])
    assert appwindow.find_browser() == str(fake)


# ------------------------------------------------------------------ 端到端（真实服务进程 + 假 llama-server）
def test_e2e_state_exposes_profile_and_config_validates(ready):
    s, st = ready.get("/api/state")
    assert s == 200 and st["engine"]["profile"]["tier"] in ("compact", "standard", "large")
    assert "档位:" in st["engine"]["plan"]["summary"]
    assert ready.patch("/api/config", {"agent": {"profile": "large"}})[0] == 200
    assert ready.patch("/api/config", {"agent": {"profile": "huge"}})[0] == 400      # 非法档位被拒
    ready.patch("/api/config", {"agent": {"profile": "auto"}})


def test_e2e_quit_endpoint_requires_auth(server):
    assert server.post("/api/app/quit", cookie=False)[0] == 401


def test_e2e_quit_stops_process_and_single_instance(tmp_path):
    """独立进程：第二个实例被拒绝；调用退出接口后进程在数秒内干净退出（SSE 长连接不能拖住）。"""
    import os, re, subprocess, time
    from conftest import Api, free_port
    env = {**os.environ, "LAGENT_HOME": str(tmp_path), "NO_PROXY": "127.0.0.1,localhost"}
    run_py = str(Path(__file__).resolve().parent.parent / "run.py")
    port = free_port()
    p = subprocess.Popen([sys.executable, run_py, "--no-browser", "--port", str(port)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        token = None
        t0 = time.time()
        while time.time() - t0 < 30 and not token:
            m = re.search(r"\?t=(\S+)", p.stdout.readline() or "")
            token = m.group(1) if m else None
        assert token
        api = Api(port, token)
        for _ in range(50):
            try:
                if api.get("/api/state")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        if sys.platform != "win32":
            second = subprocess.run([sys.executable, run_py, "--no-browser", "--port", str(free_port())], env=env,
                                    capture_output=True, text=True, timeout=30)
            assert second.returncode == 1 and "已经在运行" in second.stderr
        assert api.post("/api/app/quit")[0] == 200
        assert p.wait(15) == 0
    finally:
        if p.poll() is None:
            p.kill()
