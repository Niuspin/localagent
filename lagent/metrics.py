"""本地运行指标聚合（§8.4 可观测性）：纯粹基于本机日志与存储统计，绝不对外上报。"""
from __future__ import annotations

import time
from typing import Any

from . import __version__


def collect_runtime_metrics(st: Any, *, window_hours: float = 24.0) -> dict[str, Any]:
    """汇总本机运行指标（步数/小时、Token、验证通过率、卡死次数、拒绝请求数、合并冲突率）。"""
    hrs = max(0.25, min(float(window_hours or 24.0), 720.0))
    now = time.time()
    cutoff = now - hrs * 3600.0

    audit_entries = st.audit.tail(2000) if getattr(st, "audit", None) else []
    recent = [e for e in audit_entries if float(e.get("ts") or 0.0) >= cutoff]

    tool_steps_recent = 0
    tool_steps_total = 0
    runs_total = 0
    denied_audit = 0
    stalls_audit = 0
    prompt_tokens = 0
    completion_tokens = 0
    verify_passed = 0
    verify_failed = 0

    for e in audit_entries:
        ev = str(e.get("event") or e.get("kind") or "")
        is_recent = float(e.get("ts") or 0.0) >= cutoff
        if ev == "tool":
            tool_steps_total += 1
            if is_recent:
                tool_steps_recent += 1
            if str(e.get("tool") or "") in ("run_command", "verify", "cite_check"):
                if e.get("status") == "ok":
                    verify_passed += 1
                elif e.get("status") == "error":
                    verify_failed += 1
        elif ev == "run":
            runs_total += 1
        elif ev in ("denied", "hard_deny", "hook_deny"):
            denied_audit += 1
        elif ev == "run_stats":
            stalls_audit += int(e.get("repeat_calls") or 0)
            prompt_tokens += int(e.get("prompt_tokens") or 0)
            completion_tokens += int(e.get("completion_tokens") or 0)
        elif ev in ("stall_detected", "watchdog_stall"):
            stalls_audit += 1

    # 跨会话/当前 Swarm 与 Mission 汇总
    swarms_list: list[Any] = []
    merges_total = 0
    conflicts_total = 0
    if getattr(st, "swarms", None) and getattr(st.swarms, "store", None):
        try:
            for meta in st.swarms.store.list(limit=200):
                sw = st.swarms.store.load(meta["id"])
                if sw is not None:
                    swarms_list.append(sw)
                    merges_total += len(getattr(sw, "merges", []) or [])
                    conflicts_total += len(getattr(sw, "conflicts", []) or [])
                    stalls_audit += int(getattr(sw, "budget_tokens", 0) < 0)
                    for n in (getattr(sw, "graph", None) and sw.graph.nodes.values()) or ():
                        prompt_tokens += int(getattr(n, "tokens_in", 0) or 0)
                        completion_tokens += int(getattr(n, "tokens_out", 0) or 0)
        except Exception:  # noqa: BLE001
            pass

    missions_list: list[Any] = []
    mission_stalls = 0
    mission_gate_ok = 0
    mission_gate_fail = 0
    if getattr(st, "missions", None) and getattr(st.missions, "store", None):
        try:
            for meta in st.missions.store.list(limit=200):
                m = st.missions.store.load(meta["id"])
                if m is None:
                    continue
                missions_list.append(m)
                for ev in getattr(m, "timeline", []) or []:
                    kind = str(ev.get("kind") or "")
                    if "stall" in kind:
                        mission_stalls += 1
                for ms in getattr(m, "milestones", []) or []:
                    for g in getattr(ms, "gate_results", []) or []:
                        if g.get("ok"):
                            mission_gate_ok += 1
                        else:
                            mission_gate_fail += 1
        except Exception:  # noqa: BLE001
            pass

    # 若尚未写入 run_stats token 记录，从最近会话估算已累计消费的 usage
    if prompt_tokens == 0 and completion_tokens == 0 and getattr(st, "store", None):
        try:
            for sm in st.store.list()[:50]:
                sess = st.store.get(sm["id"])
                if sess is None:
                    continue
                for umsg in getattr(sess, "ui", []) or []:
                    u = umsg.get("usage")
                    if isinstance(u, dict):
                        prompt_tokens += int(u.get("prompt_tokens") or 0)
                        completion_tokens += int(u.get("completion_tokens") or 0)
        except Exception:  # noqa: BLE001
            pass

    net_blocked = 0
    net_total = 0
    if getattr(st, "research", None) and getattr(st.research, "ledger", None):
        try:
            ldg = st.research.ledger.query(limit=1)
            net_total = int(ldg.get("summary", {}).get("total", 0))
            net_blocked = int(ldg.get("summary", {}).get("blocked", 0))
        except Exception:  # noqa: BLE001
            pass

    eng_state = st.engine.state() if getattr(st, "engine", None) else {}
    perf = eng_state.get("perf") or {}

    skills_total = 0
    skills_enabled = 0
    lock_ok = True
    if getattr(st, "skills", None):
        try:
            all_sk = st.skills.all()
            skills_total = len(all_sk)
            skills_enabled = sum(1 for s in all_sk.values() if s.get("enabled"))
            lock_ok = bool(st.skills.verify_lock().get("ok"))
        except Exception:  # noqa: BLE001
            pass

    # 若审计窗口尚有 span，则以实际窗口跨越小时数（上限 hrs）折算，防止刚启动 1 分钟把单次动作放大过多或归零
    span_hours = hrs
    if len(recent) >= 2:
        t_first = float(recent[0].get("ts") or now)
        span_hours = max(1.0, min(hrs, (now - t_first) / 3600.0))

    steps_per_hour = round(tool_steps_recent / span_hours, 2)
    total_verifies = verify_passed + verify_failed + mission_gate_ok + mission_gate_fail
    ok_verifies = verify_passed + mission_gate_ok
    verify_pass_rate = round(100.0 * ok_verifies / total_verifies, 1) if total_verifies > 0 else None
    conflict_rate = round(100.0 * conflicts_total / max(1, merges_total + conflicts_total), 1) if (merges_total + conflicts_total) > 0 else 0.0
    total_tokens = prompt_tokens + completion_tokens

    return {
        "version": __version__,
        "window_hours": hrs,
        "local_only": True,
        "steps_per_hour": steps_per_hour,
        "tool_steps_total": tool_steps_total,
        "runs_total": runs_total,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
        },
        "verify_pass_rate": verify_pass_rate,
        "verify_counts": {"passed": ok_verifies, "total": total_verifies},
        "stall_count": stalls_audit + mission_stalls,
        "denied_requests": denied_audit + net_blocked,
        "denied_breakdown": {"tool_policy": denied_audit, "network_policy": net_blocked},
        "merge_conflict_rate": conflict_rate,
        "merge_counts": {"merges": merges_total, "conflicts": conflicts_total},
        "engine": {
            "status": eng_state.get("status", "stopped"),
            "eval_tps": perf.get("eval_tps"),
            "prompt_tps": perf.get("prompt_tps"),
            "ttft_ms": perf.get("ttft_ms"),
            "cache_hit_rate": perf.get("cache_hit_rate"),
            "spec_accept_rate": perf.get("spec_accept_rate"),
        },
        "inventory": {
            "sessions": len(st.store.list()) if getattr(st, "store", None) else 0,
            "swarms": len(swarms_list),
            "missions": len(missions_list),
            "net_requests": net_total,
            "skills_enabled": skills_enabled,
            "skills_total": skills_total,
            "skills_lock_ok": lock_ok,
            "audit_entries": len(audit_entries),
        },
    }
