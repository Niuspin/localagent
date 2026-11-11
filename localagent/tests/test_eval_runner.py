"""评测脚本本身的自检（不需要真实模型）：场景判定逻辑、无运行时自动跳过。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
import run_eval as ev  # noqa: E402


def S(i):
    return next(s for s in ev.SCENARIOS if s.id == i)


def test_checkers(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there\n")
    assert S("create").check(tmp_path, "")
    (tmp_path / "hello.txt").write_text("hi there!!")
    assert not S("create").check(tmp_path, "")
    assert S("read").check(tmp_path, "数字是 4817") and not S("read").check(tmp_path, "数字是 4818")
    assert S("count").check(tmp_path, "3") and not S("count").check(tmp_path, "13") and not S("count").check(tmp_path, "4")
    (tmp_path / "config.py").write_text("NAME = 'x'\nRETRIES = 5\nDEBUG = False\n")
    assert S("edit").check(tmp_path, "")
    (tmp_path / "config.py").write_text("RETRIES = 5\n")
    assert not S("edit").check(tmp_path, "")           # 误删了别的行
    (tmp_path / "c.txt").write_text("AAA\nBBB\n")
    assert S("multi").check(tmp_path, "")


def test_skips_without_runtime(tmp_path):
    r = subprocess.run([sys.executable, str(Path(ev.__file__)), "--home", str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "SKIP" in r.stdout
