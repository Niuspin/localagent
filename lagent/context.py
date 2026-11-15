"""上下文管理：token 估算（用服务端真实用量自校准）、旧工具结果/图片修剪、对话压缩、硬裁剪。

分三级（越往后代价越大）：
 1. prune   ：把较旧的工具输出/图片替换成简短占位（只影响一次 KV 前缀缓存）
 2. grow    ：自动提高 ctx 并重启引擎（由 loop 调用 engine，保留全部信息）
 3. compact ：让模型把旧对话压缩成摘要，放入系统提示
 4. trim    ：兜底，直接丢弃最旧的回合
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

_CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯＀-￯]")
API_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def est_text(s: str) -> float:
    if not s:
        return 0.0
    cjk = len(_CJK.findall(s))
    return cjk * 0.85 + (len(s) - cjk) / 3.4


def est_msg(m: dict, image_tokens: int = 700) -> float:
    n = 6.0
    c = m.get("content")
    if isinstance(c, str):
        n += est_text(c)
    elif isinstance(c, list):
        for p in c:
            if p.get("type") == "text":
                n += est_text(p.get("text", ""))
            elif p.get("type") == "image_url":
                n += image_tokens
    for tc in m.get("tool_calls") or []:
        f = tc.get("function", {})
        n += est_text(f.get("name", "")) + est_text(f.get("arguments", "")) + 8
    return n


class Meter:
    """估算值 × 校准系数 ≈ 真实 token；系数由服务端返回的 prompt_tokens 持续修正。"""

    def __init__(self, calib: float = 1.0, image_tokens: int = 700):
        self.calib, self.image_tokens = calib, image_tokens
        self.last_raw = 0.0

    def raw(self, msgs: list[dict], tools: list[dict] | None = None) -> float:
        return sum(est_msg(m, self.image_tokens) for m in msgs) + (est_text(json.dumps(tools, ensure_ascii=False)) if tools else 0)

    def estimate(self, msgs: list[dict], tools: list[dict] | None = None) -> int:
        self.last_raw = self.raw(msgs, tools)
        return int(self.last_raw * self.calib)

    def observe(self, raw_est: float, actual_prompt_tokens: int) -> None:
        if raw_est > 200 and actual_prompt_tokens > 50:
            r = max(0.5, min(2.5, actual_prompt_tokens / raw_est))
            self.calib = 0.5 * self.calib + 0.5 * r


def api_messages(history: list[dict]) -> list[dict]:
    """去掉内部字段，只保留服务端认识的键。"""
    return [{k: v for k, v in m.items() if k in API_KEYS} for m in history]


def turn_starts(history: list[dict]) -> list[int]:
    return [i for i, m in enumerate(history) if m.get("role") == "user" and not m.get("_internal")]


def prune_old(history: list[dict], keep_recent_tools: int = 4, min_len: int = 500) -> int:
    """就地修剪：保留最近 N 条工具结果，其余较长的替换为占位；清理旧图片。返回节省的粗略字符数。"""
    saved = 0
    tool_idx = [i for i, m in enumerate(history) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_recent_tools] if keep_recent_tools else tool_idx:
        m = history[i]
        c = m.get("content")
        if isinstance(c, str) and len(c) > min_len and not m.get("_pruned"):
            head = c[:260].replace("\n", " ")
            hid = m.get("_out")
            if hid:     # 完整输出已落盘：给出取回办法，不必重跑（重跑对 web_fetch / 命令 / 搜索既慢又可能结果不同）
                m["content"] = (f"[旧工具结果已省略以节省上下文（原 {len(c)} 字符，完整内容已保存为 {hid}）。开头: {head}…  "
                                f"需要时调用 read_tool_output(id=\"{hid}\")，可加 contains 关键词查找；无需重新执行原工具]")
            else:
                m["content"] = f"[旧工具结果已省略以节省上下文（原 {len(c)} 字符）。开头: {head}…  需要时请重新调用工具获取]"
            m["_pruned"] = True
            saved += len(c)
    starts = turn_starts(history)
    cutoff = starts[-2] if len(starts) >= 2 else 0
    for m in history[:cutoff]:
        c = m.get("content")
        if isinstance(c, list) and any(p.get("type") == "image_url" for p in c):
            m["content"] = [p if p.get("type") != "image_url" else {"type": "text", "text": "[图片已省略]"} for p in c]
            saved += 50_000
    return saved


def render_for_summary(history: list[dict], limit_chars: int) -> str:
    lines = []
    for m in history:
        r, c = m.get("role"), m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "[图片]") if p.get("type") == "text" else "[图片]" for p in c)
        c = c or ""
        if r == "user":
            lines.append("用户: " + c[:1500])
        elif r == "assistant":
            t = c[:1200]
            for tc in m.get("tool_calls") or []:
                f = tc["function"]
                t += f"\n  → 调用 {f['name']}({f.get('arguments', '')[:200]})"
            lines.append("助手: " + t)
        elif r == "tool":
            lines.append(f"工具结果: {c[:500]}")
    text = "\n".join(lines)
    if len(text) > limit_chars:
        half = limit_chars // 2
        text = text[:half] + "\n…[中间省略]…\n" + text[-half:]
    return text


SUMMARY_PROMPT = ("请把下面的对话压缩成一份“交接备忘”，供稍后继续工作时参考。要求：\n"
                  "1. 用户的总目标与偏好/约束\n2. 已完成的事项（保留文件路径、命令、关键数字原文）\n3. 重要发现、决定及其理由\n"
                  "4. 尚未完成的事项与下一步\n5. 遇到的错误与已排除的方案\n"
                  "只输出备忘本身，用条目列出，≤700 字，不要寒暄。若已有“此前摘要”，请把它与新内容合并。")


async def compact(history: list[dict], prev_summary: str | None, summarize: Callable[[str], Awaitable[str]],
                  keep_turns: int = 2, limit_chars: int = 24000) -> tuple[list[dict], str | None]:
    """把较早的回合交给 summarize 压缩；返回 (保留的历史, 新摘要)。无可压缩内容时返回原样。"""
    starts = turn_starts(history)
    if len(starts) <= keep_turns:
        # 只有一个超长回合：保留最近的消息，压缩更早的部分（在 assistant 边界切开）
        cut = None
        for i in range(len(history) - 6, 0, -1):
            if history[i].get("role") == "assistant" and history[i - 1].get("role") in ("tool", "user"):
                cut = i
                break
        if cut is None or cut < 4:
            return history, prev_summary
    else:
        cut = starts[-keep_turns]
    old, keep = history[:cut], history[cut:]
    body = render_for_summary(old, limit_chars)
    if prev_summary:
        body = "此前摘要:\n" + prev_summary + "\n\n新增对话:\n" + body
    summary = (await summarize(body)).strip()
    if not summary:
        return history, prev_summary
    return keep, summary


def hard_trim(history: list[dict], budget_tokens: int, meter: Meter) -> int:
    """兜底：从最旧的回合开始整段丢弃，直到估算量低于预算。永不删除最后一个回合；
    若只剩一个回合仍超预算，则把其中的工具输出压成短占位。返回丢弃/压缩的消息数。"""
    dropped = 0
    while True:
        if meter.raw(history) * meter.calib <= budget_tokens:
            return dropped
        starts = turn_starts(history)
        if len(starts) < 2:
            break
        del history[:starts[1]]
        dropped += starts[1]
    for m in history:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 300:
            tail = f"；完整内容见 {m['_out']}，可用 read_tool_output 取回" if m.get("_out") else ""
            m["content"] = m["content"][:240].replace("\n", " ") + f"…[已为腾出上下文而截断，原 {len(m['content'])} 字符{tail}]"
            m["_pruned"] = True
            dropped += 1
            if meter.raw(history) * meter.calib <= budget_tokens:
                break
    return dropped
