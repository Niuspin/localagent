"""Mission 验收门控：支持 command / file_exists / test / manual 四类确定性验证。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import security as sec
from ..projectdoc import verify_command as check_project_verify
from .model import AcceptanceCheck, GateResult


def _clip(text: str, limit: int = 1600) -> str:
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit // 2] + "\n...\n" + s[-limit // 2 :]


def _expect_match(text: str, expect: str) -> bool:
    exp = (expect or "").strip()
    if not exp:
        return True
    if exp.startswith("re:"):
        try:
            return re.search(exp[3:], text, re.MULTILINE) is not None
        except re.error:
            return exp[3:] in text
    return exp in text


def run_single_check(
    chk: AcceptanceCheck,
    workspace: Path,
    guard: sec.PathGuard,
    *,
    cfg: Any = None,
    manual_approvals: set[str] | None = None,
) -> GateResult:
    t0 = time.monotonic()
    kind = (chk.kind or "command").lower()
    target = (chk.target or "").strip()

    if kind == "manual":
        ok = bool(manual_approvals and (chk.id in manual_approvals or target in manual_approvals))
        return GateResult(
            check_id=chk.id,
            kind=kind,
            target=target,
            ok=ok,
            awaiting_manual=not ok,
            summary="manual_approved" if ok else "awaiting_manual_signoff",
            evidence={"ts": time.time(), "duration_ms": 0},
        )

    if kind == "file_exists":
        if not target:
            return GateResult(chk.id, kind, target, False, summary="empty_target")
        try:
            resolved = guard.resolve(target, write=False)
        except sec.SecurityError as e:
            return GateResult(
                chk.id,
                kind,
                target,
                False,
                summary=f"path_guard_rejected: {e}",
                evidence={"error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)},
            )
        exists = resolved.exists()
        ok = exists
        content_sample = ""
        if exists and resolved.is_file() and chk.expect:
            try:
                content_sample = resolved.read_text("utf-8", "replace")[:200_000]
                ok = _expect_match(content_sample, chk.expect)
            except OSError as e:
                ok = False
                content_sample = str(e)
        return GateResult(
            check_id=chk.id,
            kind=kind,
            target=target,
            ok=ok,
            summary="ok" if ok else ("expect_mismatch" if exists else "file_not_found"),
            evidence={
                "path": str(resolved),
                "exists": exists,
                "size": resolved.stat().st_size if exists and resolved.is_file() else 0,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            },
        )

    if kind in ("command", "test"):
        if not target:
            return GateResult(chk.id, kind, target, False, summary="empty_command")
        hd = sec.hard_deny_reason(target)
        if hd:
            return GateResult(
                chk.id,
                kind,
                target,
                False,
                summary=f"hard_deny: {hd}",
                evidence={"hard_deny": hd, "duration_ms": int((time.monotonic() - t0) * 1000)},
            )
        if not check_project_verify(target):
            return GateResult(
                chk.id,
                kind,
                target,
                False,
                summary="unsafe_verify_command",
                evidence={"reason": "blocked_by_verify_command_guard"},
            )
        env = sec.clean_env(str(workspace))
        try:
            cp = subprocess.run(
                target,
                shell=True,
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, float(chk.timeout_s or 60.0)),
            )
            out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
            ok = cp.returncode == 0 and _expect_match(out, chk.expect)
            return GateResult(
                check_id=chk.id,
                kind=kind,
                target=target,
                ok=ok,
                summary="ok" if ok else (f"exit_{cp.returncode}" if cp.returncode != 0 else "expect_mismatch"),
                evidence={
                    "exit_code": cp.returncode,
                    "stdout_tail": _clip(cp.stdout),
                    "stderr_tail": _clip(cp.stderr),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
            )
        except subprocess.TimeoutExpired as e:
            return GateResult(
                check_id=chk.id,
                kind=kind,
                target=target,
                ok=False,
                summary="timeout",
                evidence={
                    "timeout_s": chk.timeout_s,
                    "stdout_tail": _clip(str(e.stdout or "")),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
            )
        except Exception as e:
            return GateResult(
                check_id=chk.id,
                kind=kind,
                target=target,
                ok=False,
                summary=f"exec_error: {e}",
                evidence={"error": str(e), "duration_ms": int((time.monotonic() - t0) * 1000)},
            )

    return GateResult(chk.id, kind, target, False, summary=f"unknown_check_kind: {kind}")


async def run_acceptance_checks(
    checks: list[AcceptanceCheck],
    workspace: Path,
    guard: sec.PathGuard,
    *,
    cfg: Any = None,
    manual_approvals: set[str] | None = None,
) -> tuple[bool, list[GateResult], bool]:
    """执行全部验收检查；返回 (all_required_passed, results, needs_manual)。"""
    results: list[GateResult] = []
    all_ok = True
    needs_manual = False
    for chk in checks:
        res = await asyncio.to_thread(
            run_single_check,
            chk,
            workspace,
            guard,
            cfg=cfg,
            manual_approvals=manual_approvals,
        )
        results.append(res)
        if res.awaiting_manual and chk.required:
            needs_manual = True
        if not res.ok and chk.required:
            all_ok = False
    return all_ok, results, needs_manual


def format_gate_failures(results: list[GateResult]) -> str:
    failed = [r for r in results if not r.ok]
    if not failed:
        return ""
    lines = []
    for r in failed:
        ev = r.evidence or {}
        tail = ev.get("stderr_tail") or ev.get("stdout_tail") or ev.get("error") or ""
        lines.append(f"- [{r.kind}] {r.target}: {r.summary}" + (f" | {tail[:240]}" if tail else ""))
    return "\n".join(lines)
