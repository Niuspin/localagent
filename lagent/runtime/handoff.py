"""Mission Episode 结构化 Handoff 抽取器：从 <handoff> JSON 或 Markdown 小节提取决策、待决问题与下一步。"""
from __future__ import annotations

import json
import re
from typing import Any


def extract_episode_handoff(
    summary: str,
    loop_status: str,
    *,
    has_acceptance: bool,
    tool_calls: int,
) -> dict[str, Any]:
    raw = str(summary or "").strip()
    decisions: list[str] = []
    open_questions: list[str] = []
    next_actions: list[str] = []
    milestone_ready: bool | None = None
    clean_summary = raw

    m = re.search(r"<handoff>\s*(\{.*?\})\s*</handoff>", raw, re.S | re.I)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                if isinstance(obj.get("decisions"), list):
                    decisions = [str(x).strip()[:240] for x in obj["decisions"] if str(x).strip()][:8]
                if isinstance(obj.get("open_questions"), list):
                    open_questions = [str(x).strip()[:240] for x in obj["open_questions"] if str(x).strip()][:8]
                if isinstance(obj.get("next_actions"), list):
                    next_actions = [str(x).strip()[:240] for x in obj["next_actions"] if str(x).strip()][:8]
                if "milestone_ready" in obj and isinstance(obj["milestone_ready"], bool):
                    milestone_ready = obj["milestone_ready"]
                clean_summary = (raw[:m.start()] + raw[m.end():]).strip() or raw
        except ValueError:
            pass

    if not (decisions or open_questions or next_actions):
        cur_sec = ""
        for ln in clean_summary.splitlines():
            s = ln.strip()
            low = s.lstrip("#").strip().lower().rstrip(":：")
            if s.startswith("#") or low in (
                "decisions", "决策", "open questions", "open_questions", "待决问题",
                "next actions", "next_actions", "下一步", "后续行动",
            ):
                if "decision" in low or "决策" in low:
                    cur_sec = "dec"
                elif "question" in low or "待决" in low or "疑问" in low:
                    cur_sec = "oq"
                elif "next" in low or "下一步" in low or "后续" in low:
                    cur_sec = "na"
                else:
                    cur_sec = ""
                continue
            item = re.sub(r"^(?:[-*]|\d+\.)\s*", "", s)
            if cur_sec and item:
                if cur_sec == "dec" and len(decisions) < 6:
                    decisions.append(item[:240])
                elif cur_sec == "oq" and len(open_questions) < 6:
                    open_questions.append(item[:240])
                elif cur_sec == "na" and len(next_actions) < 6:
                    next_actions.append(item[:240])

    if loop_status == "max_steps":
        milestone_ready = False
        if not next_actions:
            next_actions = ["达到单段步数上限，下一 Episode 需继续完成剩余步骤并验证"]
    elif milestone_ready is None:
        if not has_acceptance and tool_calls == 0 and (not clean_summary or "未完成" in clean_summary or "TODO" in clean_summary):
            milestone_ready = False
        else:
            milestone_ready = True

    return {
        "decisions": decisions,
        "open_questions": open_questions,
        "next_actions": next_actions,
        "milestone_ready": milestone_ready,
        "clean_summary": clean_summary,
    }
