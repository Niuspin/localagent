"""引用一致性校验（verify.py）：
核查报告中的每一条 `[n]` 引文与直引/论断是否能在对应来源存档原文（含摘录、归档文本、已抽取 claim）中定位，
对编号越界或找不到原文依据的“幻觉引用”予以驳回并给出修复反馈。
"""
from __future__ import annotations

import difflib
import re
from typing import Any

_CITE_BRACKET_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？.!?\n])\s*")
_QUOTED_RE = re.compile(r"[“\"「]([^”\"」]{6,300})[”\"」]")


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _tokens(s: str) -> set[str]:
    low = (s or "").lower()
    words = set(re.findall(r"[a-z0-9]{2,}", low))
    cjk = [c for c in low if "\u4e00" <= c <= "\u9fff"]
    for i in range(max(0, len(cjk) - 1)):
        words.add(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        words.add(cjk[0])
    return words


def _match_score(claim: str, corpus: str) -> float:
    nc = _norm(claim)
    np = _norm(corpus)
    if not nc or not np:
        return 0.0
    if nc in np:
        return 1.0
    # 关键引号优先精准子串匹配
    for q in _QUOTED_RE.findall(claim):
        nq = _norm(q)
        if nq and nq not in np:
            # 允许轻微标点/空格差异，但引号内容至少要高度重合
            sm = difflib.SequenceMatcher(None, nq, np).find_longest_match(0, len(nq), 0, len(np)).size / max(1, len(nq))
            if sm < 0.72:
                return 0.0
    tc = _tokens(claim)
    tp = _tokens(corpus)
    if not tc or not tp:
        return 0.0
    overlap = len(tc & tp) / max(1, len(tc))
    # 数字/年份必须匹配，避免年份或数量篡改
    nums_c = set(re.findall(r"\b\d{2,}\b", claim))
    if nums_c:
        nums_p = set(re.findall(r"\b\d{2,}\b", corpus))
        if not nums_c.issubset(nums_p):
            overlap *= 0.35
    return overlap


def cite_check(
    report_text: str,
    sources: list[dict[str, Any]],
    *,
    claims: list[dict[str, Any]] | None = None,
    archived_texts: dict[str, str] | None = None,
    min_overlap: float = 0.28,
) -> dict[str, Any]:
    """校验报告中的全部 `[n]` 引用。返回 {ok, score, checked, issues, invalid_refs, unsupported}。"""
    text = str(report_text or "").strip()
    if not text:
        return {"ok": False, "score": 0.0, "checked": 0, "issues": ["报告正文为空"], "invalid_refs": [], "unsupported": []}

    # 建立 ref_num -> source 映射（默认按 1-based 序号兼容）
    src_by_ref: dict[int, dict[str, Any]] = {}
    for idx, src in enumerate(sources, 1):
        rn = int(src.get("ref_num") or idx)
        src_by_ref[rn] = src

    claims_by_src: dict[str, list[str]] = {}
    for cl in claims or []:
        sid = str(cl.get("source_id") or "")
        if sid:
            claims_by_src.setdefault(sid, []).append(f"{cl.get('claim_text', '')} {cl.get('quote', '')}")

    # 切分正文部分（排除末尾“参考文献 / References”清单行）
    body = re.split(r"\n#{1,3}\s*(?:参考文献|References)\b", text, maxsplit=1)[0]
    # 把紧跟在句末标点后面的 [n] 归并回原句，兼容 `句末。[1]` 与 `句末 [1]。` 两种写法
    body_norm = re.sub(r"([。！？.!?])\s*(\[\d+(?:\s*,\s*\d+)*\])", r" \2\1", body)
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(body_norm) if s.strip()]

    invalid_refs: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    checked = 0
    passed = 0

    for sent in sentences:
        refs_raw = _CITE_BRACKET_RE.findall(sent)
        if not refs_raw:
            continue
        ref_nums: list[int] = []
        for grp in refs_raw:
            for part in grp.split(","):
                part = part.strip()
                if part.isdigit():
                    ref_nums.append(int(part))
        clean_sent = _CITE_BRACKET_RE.sub("", sent).strip()
        if not clean_sent:
            continue

        for rn in ref_nums:
            checked += 1
            src = src_by_ref.get(rn)
            if src is None:
                invalid_refs.append({"ref": rn, "sentence": clean_sent[:160], "reason": f"引用编号 [{rn}] 不存在"})
                continue
            sid = str(src.get("id") or "")
            arch = (archived_texts or {}).get(sid) or (archived_texts or {}).get(str(src.get("url") or "")) or ""
            corpus_parts = [
                str(src.get("title") or ""),
                str(src.get("excerpt") or ""),
                arch,
                " ".join(claims_by_src.get(sid, [])),
            ]
            corpus = "\n".join(p for p in corpus_parts if p)
            if not corpus.strip():
                unsupported.append({"ref": rn, "sentence": clean_sent[:160], "reason": f"来源 [{rn}] 无可用存档原文（可能受 noarchive/noai 限制）"})
                continue
            sc = _match_score(clean_sent, corpus)
            if sc < min_overlap:
                unsupported.append({
                    "ref": rn,
                    "sentence": clean_sent[:160],
                    "score": round(sc, 3),
                    "reason": f"论断未在来源 [{rn}] ({src.get('domain', '')}) 原文中定位到依据",
                })
            else:
                passed += 1

    issues = [x["reason"] for x in invalid_refs + unsupported]
    if checked == 0:
        issues.append("报告正文中缺少任何 [n] 行内引用标注")
        return {"ok": False, "score": 0.0, "checked": 0, "issues": issues, "invalid_refs": [], "unsupported": []}

    ok = not invalid_refs and not unsupported
    score = round(passed / max(1, checked), 3)
    return {
        "ok": ok,
        "score": score,
        "checked": checked,
        "passed": passed,
        "issues": issues,
        "invalid_refs": invalid_refs,
        "unsupported": unsupported,
    }
