#!/usr/bin/env python3
"""真实模型的小型回归评测：同一批典型任务，换模型/换参数/改提示词前后各跑一次，对比通过率与“摩擦”指标。

    python tests/eval/run_eval.py                       # 用 ./models 与 ./bin 里的真实模型和 llama-server
    python tests/eval/run_eval.py --home ~/la --repeat 3 --only edit,read
    python tests/eval/run_eval.py --json out.json       # 保存结果便于对比

没有 llama-server 或没有 .gguf 模型时直接退出（返回码 0，打印 SKIP），所以可以安全地放进 CI。
评测在一个临时 LAGENT_HOME 里进行（模型/引擎用符号链接复用），不会碰你的会话、记忆和配置。

“摩擦”来自 run_end.stats：name_fixed（工具名被纠正）、args_fixed、json_repaired、unknown_tool、arg_errors、repeat_calls、
forced_retry 等。通过率相同时，摩擦越低说明模型越省心。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


@dataclass
class Scenario:
    id: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    check: Callable[[Path, str], bool] = lambda ws, text: True      # (工作区, 最终回答文本) → 是否通过


def _read(ws: Path, name: str) -> str:
    p = ws / name
    return p.read_text("utf-8", "replace") if p.exists() else ""


SCENARIOS: list[Scenario] = [
    Scenario("create", "在工作区创建文件 hello.txt，内容只写 hi there。", check=lambda ws, t: _read(ws, "hello.txt").strip() == "hi there"),
    Scenario("read", "读取 data.txt，告诉我里面的数字是多少。", {"data.txt": "the secret number is 4817\n"}, lambda ws, t: "4817" in t),
    Scenario("edit", "把 config.py 里的 RETRIES 改成 5，其他内容不要动。", {"config.py": "NAME = 'x'\nRETRIES = 3\nDEBUG = False\n"},
             lambda ws, t: "RETRIES = 5" in _read(ws, "config.py") and "DEBUG = False" in _read(ws, "config.py") and "NAME = 'x'" in _read(ws, "config.py")),
    Scenario("count", "工作区里有几个 .py 文件？只回答数字。", {"a.py": "", "b.py": "", "sub/c.py": "", "n.txt": ""}, lambda ws, t: bool(re.search(r"(?<!\d)3(?!\d)", t))),
    Scenario("search", "哪个文件里包含 TODO_MARKER？告诉我文件名。", {"x.txt": "nothing", "deep/y.md": "a TODO_MARKER here", "z.txt": "none"}, lambda ws, t: "y.md" in t),
    Scenario("command", "运行命令 python3 -c \"print(6*7)\" 并告诉我输出。", check=lambda ws, t: "42" in t),
    Scenario("multi", "读取 a.txt 和 b.txt，把两个文件的内容拼在一起写入 c.txt（a 在前）。", {"a.txt": "AAA\n", "b.txt": "BBB\n"},
             lambda ws, t: _read(ws, "c.txt").replace("\n", "").replace(" ", "") == "AAABBB"),
]


def find_runtime(home: Path) -> tuple[bool, str]:
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    if not ((home / "bin").exists() and any((home / "bin").rglob(exe))) and not shutil.which("llama-server"):
        return False, "没有找到 llama-server（放进 bin/ 或加入 PATH）"
    if not (home / "models").exists() or not [m for m in (home / "models").glob("*.gguf") if not m.name.lower().startswith("mmproj")]:
        return False, "models/ 里没有 .gguf 模型"
    return True, ""


def start(home: Path, tmp: Path) -> tuple[subprocess.Popen, "object"]:
    from conftest import Api, free_port
    h = tmp / "home"
    h.mkdir()
    for d in ("models", "bin"):
        if (home / d).exists():
            os.symlink(home / d, h / d)
    port = free_port()
    p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)], cwd=str(ROOT), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env={**os.environ, "LAGENT_HOME": str(h), "NO_PROXY": "127.0.0.1,localhost"})
    token, t0 = None, time.time()
    while time.time() - t0 < 60:
        m = re.search(r"\?t=(\S+)", p.stdout.readline() or "")
        if m:
            token = m.group(1)
            break
        if p.poll() is not None:
            raise RuntimeError("服务启动失败")
    if not token:
        raise RuntimeError("没有拿到访问令牌")
    api = Api(port, token)
    for _ in range(50):
        try:
            if api.get("/api/state")[0] == 200:
                break
        except OSError:
            time.sleep(0.2)
    return p, api


def run_one(api, sc: Scenario, base: Path, timeout: int) -> dict:
    ws = base / f"{sc.id}-{int(time.time() * 1000) % 100000}"
    ws.mkdir(parents=True)
    for rel, content in sc.files.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(content, "utf-8")
    s, r = api.post("/api/sessions", {"workspace": str(ws)})
    assert s == 200, r
    sid, t0 = r["id"], time.time()
    evs = api.chat(sid, sc.prompt, {"*": "allow"}) if timeout <= 0 else _chat(api, sid, sc.prompt, timeout)
    text = "".join(d.get("text", "") for e, d in evs if e == "text_delta")
    end = next((d for e, d in reversed(evs) if e == "run_end"), {})
    tools = [d.get("name") for e, d in evs if e == "tool_start"]
    ok = end.get("status") == "done" and bool(sc.check(ws, text))
    return {"id": sc.id, "ok": ok, "status": end.get("status"), "seconds": round(time.time() - t0, 1), "tool_calls": len(tools),
            "stats": end.get("stats", {}), "error": end.get("error", "")}


def _chat(api, sid, text, timeout):
    s, r = api.post(f"/api/sessions/{sid}/messages", {"text": text})
    assert s == 200, (s, r)

    def on(ev, d):
        if ev == "approval_request":
            api.post(f"/api/approvals/{d['approval_id']}", {"decision": "allow"})
    return api.stream(r["run_id"], on, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=str(ROOT), help="含 models/ 与 bin/ 的目录（默认：项目根目录）")
    ap.add_argument("--repeat", type=int, default=1, help="每个场景重复次数（小模型有随机性，建议 ≥3）")
    ap.add_argument("--only", default="", help="只跑这些场景 id，逗号分隔")
    ap.add_argument("--timeout", type=int, default=300, help="单个场景的超时秒数")
    ap.add_argument("--json", default="", help="把结果写到这个文件")
    a = ap.parse_args()
    home = Path(a.home).expanduser().resolve()
    ok, why = find_runtime(home)
    if not ok:
        print("SKIP:", why)
        return 0
    chosen = [s for s in SCENARIOS if not a.only or s.id in a.only.split(",")]
    with tempfile.TemporaryDirectory(prefix="la-eval-") as td:
        tmp = Path(td)
        proc, api = start(home, tmp)
        try:
            api.patch("/api/config", {"agent": {"permission_mode": "auto_edit"}})
            s, r = api.post("/api/engine/start", {})
            if s != 200:
                print("引擎启动失败:", r)
                return 1
            api.wait_engine("ready", timeout=600)
            results = []
            for sc in chosen:
                for i in range(a.repeat):
                    res = run_one(api, sc, tmp / "ws", a.timeout)
                    results.append(res)
                    fr = {k: v for k, v in res["stats"].items() if k not in ("tool_calls",) and v}
                    print(f"{'PASS' if res['ok'] else 'FAIL'}  {sc.id:<8} #{i + 1}  {res['seconds']:>6}s  工具调用 {res['tool_calls']}  {fr or ''}")
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    n = len(results)
    passed = sum(r["ok"] for r in results)
    friction = {}
    for r in results:
        for k, v in r["stats"].items():
            if k != "tool_calls":
                friction[k] = friction.get(k, 0) + v
    print(f"\n通过 {passed}/{n}（{100 * passed // max(n, 1)}%）  摩擦合计: {friction or '无'}")
    if a.json:
        Path(a.json).write_text(json.dumps({"passed": passed, "total": n, "friction": friction, "results": results}, ensure_ascii=False, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
