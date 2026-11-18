"""Shadow Git 隔离检查点管理：在不污染用户真实 .git 分支或暂存区的前提下追踪工作区快照与安全回滚。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import security as sec


def _git_env(shadow_dir: Path, ws: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_DIR"] = str(shadow_dir)
    env["GIT_WORK_TREE"] = str(ws)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_INDEX_FILE", None)
    return env


def _run_shadow(shadow_dir: Path, ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    kw = {"creationflags": 0x08000000} if sec.IS_WIN else {}
    return subprocess.run(
        ["git", *args],
        cwd=str(ws),
        env=_git_env(shadow_dir, ws),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kw,
    )


def has_git() -> bool:
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except OSError:
        return False


def ensure_shadow_repo(ws: Path, shadow_dir: Path) -> bool:
    """在 .localagent/missions/shadow.git 下初始化隔离的 Shadow Git 仓库，排除 .git/ 与 .localagent/。"""
    if not has_git() or not ws.is_dir():
        return False
    shadow_dir.parent.mkdir(parents=True, exist_ok=True)
    if not (shadow_dir / "HEAD").is_file():
        r = _run_shadow(shadow_dir, ws, "init", "--bare")
        if r.returncode != 0:
            # 某些 git 版本对 GIT_DIR+GIT_WORK_TREE init --bare 不兼容，退化到标准 git --git-dir init
            r = subprocess.run(
                ["git", "--git-dir", str(shadow_dir), "init"],
                cwd=str(ws),
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                return False
        _run_shadow(shadow_dir, ws, "config", "core.bare", "false")
        _run_shadow(shadow_dir, ws, "config", "core.worktree", str(ws))
    info_excl = shadow_dir / "info" / "exclude"
    info_excl.parent.mkdir(parents=True, exist_ok=True)
    excl_lines = [".git/", ".git", ".localagent/", "__pycache__/", ".venv/", "node_modules/"]
    info_excl.write_text("\n".join(excl_lines) + "\n", encoding="utf-8")
    return True


def list_user_untracked(ws: Path) -> list[str]:
    """记录 Mission 启动前真实仓库中已存在的未跟踪文件，回滚时严禁误删用户既有未跟踪文件。"""
    if not has_git():
        return []
    r = subprocess.run(
        ["git", "-C", str(ws), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        return []
    return [ln.strip().replace("\\", "/") for ln in (r.stdout or "").splitlines() if ln.strip()]


def snapshot(ws: Path, shadow_dir: Path, message: str) -> str | None:
    """在 Shadow Git 中捕获工作区全量快照（不触碰用户 .git），返回 commit SHA。"""
    if not ensure_shadow_repo(ws, shadow_dir):
        return None
    _run_shadow(shadow_dir, ws, "add", "-A", "--", ".")
    # 无论有无差异均检查是否已有 HEAD；首提交或有变更时提交
    has_head = _run_shadow(shadow_dir, ws, "rev-parse", "--verify", "HEAD").returncode == 0
    if has_head:
        diff_chk = _run_shadow(shadow_dir, ws, "diff", "--cached", "--quiet")
        if diff_chk.returncode == 0:
            return _run_shadow(shadow_dir, ws, "rev-parse", "HEAD").stdout.strip() or None
    cp = _run_shadow(
        shadow_dir,
        ws,
        "-c", "user.name=LocalAgent Shadow",
        "-c", "user.email=shadow@localagent.local",
        "commit",
        "--allow-empty",
        "-m",
        message,
    )
    if cp.returncode != 0:
        return None
    return _run_shadow(shadow_dir, ws, "rev-parse", "HEAD").stdout.strip() or None


def rollback(ws: Path, shadow_dir: Path, target_sha: str, *, preserve_paths: set[str] | None = None) -> bool:
    """用 Shadow Git 安全回滚工作区至目标 SHA，并保留用户 pre_existing 文件与 .git / .localagent。"""
    if not target_sha or not ensure_shadow_repo(ws, shadow_dir):
        return False
    # 找出 target_sha 之后新增的文件（即当前工作树里有但 target_sha 里没有的非保护文件）
    cur_files = set()
    r_ls_now = _run_shadow(shadow_dir, ws, "ls-files", "--cached", "--others", "--exclude-standard")
    if r_ls_now.returncode == 0:
        cur_files = {x.strip().replace("\\", "/") for x in r_ls_now.stdout.splitlines() if x.strip()}
    r_ls_tgt = _run_shadow(shadow_dir, ws, "ls-tree", "-r", "--name-only", target_sha)
    if r_ls_tgt.returncode != 0:
        return False
    tgt_files = {x.strip().replace("\\", "/") for x in r_ls_tgt.stdout.splitlines() if x.strip()}

    pres = {p.replace("\\", "/").strip("/") for p in (preserve_paths or set())}
    res = _run_shadow(shadow_dir, ws, "checkout", target_sha, "--", ".")
    if res.returncode != 0:
        # target_sha 可能是空树，仍需清理之后创建的文件
        if tgt_files:
            return False
    _run_shadow(shadow_dir, ws, "reset", "--soft", target_sha)

    for rel in sorted(cur_files - tgt_files, reverse=True):
        if rel.startswith((".git/", ".localagent/")) or rel in pres:
            continue
        p = (ws / rel).resolve()
        try:
            p.relative_to(ws.resolve())
            if p.is_file() or p.is_symlink():
                p.unlink()
        except Exception:  # noqa: BLE001
            continue
    return True
