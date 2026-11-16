"""技能系统 v2：兼容 Anthropic 风格的 SKILL.md 目录与 Spec v2 元数据、按包按需启用、自动路由与完整性校验。

渐进式披露：
  - 系统提示里仅注入不超过 `skills.default_enabled_limit`（默认 14）条启用技能索引，避免提示词膨胀；
  - 对话输入时通过 `Skills.route()`（关键词 + 文件后缀/glob + MIME + `local_embed` 语义相似度）给出轻量 Top-K(<=3) 建议；
  - compact 小模型档位自动选择 `SKILL.compact.md`（<=600 tokens）；
  - 模型调用 `use_skill` 后再按需加载完整说明，并在需要时把 `allowed_tools` 权限收敛（只能收窄，不能放开）。
目录与完整规范见 `ITERATION_PLAN_2026-11-15_2140.md` §7。
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .memory import _cosine, local_embed, tokens as _lex_tokens
from .security import sanitize_untrusted, suspicious

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_ZIP = 50 * 1024 * 1024
MAX_FILES = 500

PACKS = ("core", "dev", "research", "office", "media", "ops", "meta")
PACK_LABELS: dict[str, str] = {
    "core": "核心基础",
    "dev": "软件工程",
    "research": "研究分析",
    "office": "办公文档",
    "media": "多模态媒体",
    "ops": "系统运维",
    "meta": "智能体元技能",
}
RISKS = ("read", "write", "network")
SKILL_MD_MAX_TOKENS = 2500
COMPACT_MD_MAX_TOKENS = 600
LOCK_FILENAME = "skills.lock"

_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_HIDDEN_UNI_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069]")
_NET_SCRIPT_RE = re.compile(
    r"(?:https?://|\b(?:urllib|requests|httpx|aiohttp|curl|wget|Invoke-WebRequest|Invoke-RestMethod|fetch)\b)",
    re.I,
)
_OOB_PATH_RE = re.compile(
    r"(?:\.\./\.\.|/etc/(?:passwd|shadow|sudoers)|/root/\.ssh|~/\.ssh|C:\\\\?Windows|C:\\\\?Users\\\\)",
    re.I,
)
_REF_PATH_RE = re.compile(r"\b((?:references|templates|scripts|examples|evals)/[A-Za-z0-9._/\-]+)\b")
_SCRIPT_EXTS = (".py", ".sh", ".bat", ".ps1", ".js", ".exe", ".dll", ".so", ".cmd")
_SCAN_EXTS = (".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".ps1", ".bat", ".js")


def estimate_tokens(text: str) -> int:
    """估算中英混排文本 token 数（离线零依赖）。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk * 1.1 + other / 3.6))


def _as_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return []
        if "," in v and not v.startswith("["):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [v]
    if isinstance(val, (list, tuple, set)):
        return [str(x).strip() for x in val if str(x).strip()]
    return [str(val).strip()] if str(val).strip() else []


def _parse_inline_val(val: str) -> Any:
    s = val.strip()
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:  # noqa: BLE001
            if s.startswith("[") and s.endswith("]"):
                return [x.strip().strip("\"'") for x in s[1:-1].split(",") if x.strip()]
    return s.strip("\"'")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("\ufeff")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    try:
        import yaml  # type: ignore

        d = yaml.safe_load(raw)
        if isinstance(d, dict):
            return {str(k): v for k, v in d.items()}, body
    except Exception:  # noqa: BLE001
        pass
    d: dict[str, Any] = {}
    key: str | None = None
    for ln in raw.split("\n"):
        mm = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if mm:
            key, val = mm.group(1), mm.group(2).strip()
            d[key] = "" if val in (">", "|", ">-", "|-") else _parse_inline_val(val)
        elif key and ln.startswith((" ", "\t")):
            sub = ln.strip()
            if sub.startswith("- "):
                cur = d.get(key)
                if not isinstance(cur, list):
                    d[key] = [] if cur == "" else [cur]
                d[key].append(sub[2:].strip().strip("\"'"))
            elif isinstance(d.get(key), str):
                d[key] = (str(d[key]) + " " + sub.strip("\"'")).strip()
    return d, body


def _normalize_triggers(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {"keywords": [], "globs": [], "mimes": []}
    return {
        "keywords": _as_str_list(raw.get("keywords")),
        "globs": _as_str_list(raw.get("globs")),
        "mimes": _as_str_list(raw.get("mimes")),
    }


def _normalize_requires(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {"tools": [], "capabilities": [], "binaries": [], "python": []}
    return {
        "tools": _as_str_list(raw.get("tools")),
        "capabilities": _as_str_list(raw.get("capabilities")),
        "binaries": _as_str_list(raw.get("binaries")),
        "python": _as_str_list(raw.get("python")),
    }


def _normalize_profiles(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


def scan_warnings(root: Path, body: str) -> list[str]:
    """扫描用户/扩展技能的潜在安全风险：可执行脚本、脚本内联网请求、越界路径、隐藏字符与提示注入。"""
    w: list[str] = []
    scripts = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _SCRIPT_EXTS]
    if scripts:
        w.append(f"包含 {len(scripts)} 个可执行脚本/二进制（运行前仍需你逐次审批）")
    texts = [body]
    net_hits = False
    oob_hits = False
    hidden_hits = bool(_HIDDEN_UNI_RE.search(body))
    for p in list(root.rglob("*"))[:200]:
        if p.is_file() and p.suffix.lower() in _SCAN_EXTS:
            try:
                if p.stat().st_size < 400_000:
                    txt = p.read_text("utf-8", "replace")
                    texts.append(txt)
                    if _HIDDEN_UNI_RE.search(txt):
                        hidden_hits = True
                    if p.suffix.lower() in (".py", ".sh", ".ps1", ".js", ".bat"):
                        if _NET_SCRIPT_RE.search(txt):
                            net_hits = True
                        if _OOB_PATH_RE.search(txt):
                            oob_hits = True
            except OSError:
                pass
    if net_hits:
        w.append("脚本内包含外部联网/下载模式（请在运行审批时核对目标地址）")
    if oob_hits:
        w.append("脚本内包含工作区外/敏感系统路径引用（将在运行时被沙箱拦截）")
    if hidden_hits:
        w.append("检测到零宽/双向控制等不可见 Unicode 字符（谨防隐藏提示注入）")
    blob = "\n".join(texts)
    w += suspicious(blob, risk=True)
    return w


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Skills:
    def __init__(self, user_dir: Path, builtin_dir: Path, state_path: Path, default_limit: int = 14):
        self.user_dir, self.builtin_dir, self.state_path = Path(user_dir), Path(builtin_dir), Path(state_path)
        self.default_limit = max(1, min(int(default_limit or 14), 50))
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, bool] = {}
        try:
            self._state = {k: bool(v) for k, v in json.loads(self.state_path.read_text("utf-8")).items()}
        except (OSError, ValueError):
            pass
        self._vec_cache: dict[str, tuple[str, list[float]]] = {}
        self._dir_cache: tuple[tuple[Any, ...], dict[str, dict]] | None = None

    def set_default_limit(self, limit: int) -> None:
        self.default_limit = max(1, min(int(limit or 14), 50))
        self._dir_cache = None

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), "utf-8")
        self._dir_cache = None

    # ------------------------------------------------------------ 发现与解析
    def _load_dir(self, d: Path, source: str) -> dict | None:
        f = d / "SKILL.md"
        if not f.is_file() or d.is_symlink():
            return None
        try:
            if f.stat().st_size > 400_000:
                return None
            fm, body = parse_frontmatter(f.read_text("utf-8", "replace"))
        except OSError:
            return None
        name = str(fm.get("name") or d.name).strip()
        if not NAME_RE.match(name):
            return None
        desc = re.sub(r"\s+", " ", str(fm.get("description") or "")).strip()
        desc_en = re.sub(r"\s+", " ", str(fm.get("description_en") or "")).strip()
        pack = str(fm.get("pack") or "").strip().lower()
        has_v2_meta = ("pack" in fm) or ("default_enabled" in fm) or ("featured" in fm)
        if not pack:
            pack = "core" if source == "builtin" else "custom"
        version = str(fm.get("version") or ("1.0.0" if has_v2_meta else "")).strip()
        tags = _as_str_list(fm.get("tags"))
        when_to_use = re.sub(r"\s+", " ", str(fm.get("when_to_use") or "")).strip()
        triggers = _normalize_triggers(fm.get("triggers"))
        requires = _normalize_requires(fm.get("requires"))
        raw_allowed = fm.get("allowed_tools") if "allowed_tools" in fm else fm.get("allowed-tools")
        allowed_tools = _as_str_list(raw_allowed) if raw_allowed is not None else None
        risk = str(fm.get("risk") or "read").strip().lower()
        if risk not in RISKS:
            risk = "read"
        profiles = _normalize_profiles(fm.get("profiles"))
        license_name = str(fm.get("license") or "").strip()
        if has_v2_meta:
            default_on = bool(fm.get("default_enabled", fm.get("featured", False)))
        else:
            default_on = source == "builtin"

        files = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file() and not p.is_symlink())[:200]
        if name in self._state:
            enabled = self._state[name]
            explicit_state = True
        else:
            enabled = default_on
            explicit_state = False

        warnings = scan_warnings(d, body) if source == "user" else []
        return {
            "name": name,
            "description": desc[:600],
            "description_en": desc_en[:600],
            "version": version,
            "pack": pack,
            "tags": tags,
            "when_to_use": when_to_use[:600],
            "triggers": triggers,
            "requires": requires,
            "allowed_tools": allowed_tools,
            "risk": risk,
            "profiles": profiles,
            "license": license_name,
            "default_enabled": default_on,
            "_has_v2": has_v2_meta,
            "_explicit_state": explicit_state,
            "enabled": enabled,
            "source": source,
            "dir": d,
            "body": body,
            "files": files,
            "size": sum((d / x).stat().st_size for x in files if (d / x).exists()),
            "est_tokens": estimate_tokens(body),
            "warnings": warnings,
        }

    def _cache_signature(self) -> tuple[Any, ...]:
        parts: list[Any] = [self.default_limit, tuple(sorted(self._state.items()))]
        for base in (self.builtin_dir, self.user_dir):
            if base.is_dir():
                try:
                    parts.append((str(base), base.stat().st_mtime_ns))
                    for d in sorted(base.iterdir()):
                        if d.is_dir():
                            md = d / "SKILL.md"
                            st_m = md.stat().st_mtime_ns if md.is_file() else 0
                            parts.append((d.name, d.stat().st_mtime_ns, st_m))
                except OSError:
                    pass
        return tuple(parts)

    def all(self) -> dict[str, dict]:
        sig = self._cache_signature()
        if self._dir_cache and self._dir_cache[0] == sig:
            return self._dir_cache[1]
        out: dict[str, dict] = {}
        for base, src in ((self.builtin_dir, "builtin"), (self.user_dir, "user")):
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if d.is_dir():
                        s = self._load_dir(d, src)
                        if s and (s["name"] not in out or src == "user"):
                            out[s["name"]] = s
        # 默认启用的 Spec v2 内置技能受 default_limit 上限保护（未手动在 state 里覆盖的条目按排序截断）
        auto_slots = self.default_limit
        for s in out.values():
            if s["source"] == "builtin" and s["_has_v2"] and not s["_explicit_state"] and s["enabled"]:
                if auto_slots > 0:
                    auto_slots -= 1
                else:
                    s["enabled"] = False
        self._dir_cache = (sig, out)
        return out

    @staticmethod
    def _public_view(s: dict, with_body: bool = False) -> dict:
        skip = {"dir", "_has_v2", "_explicit_state"}
        if not with_body:
            skip.add("body")
        return {k: v for k, v in s.items() if k not in skip}

    def list(self) -> list[dict]:
        return [self._public_view(s, with_body=False) for s in self.all().values()]

    def get(self, name: str, with_body: bool = True) -> dict | None:
        s = self.all().get(name)
        if not s:
            return None
        return self._public_view(s, with_body=with_body)

    def enabled(self) -> list[dict]:
        return [s for s in self.all().values() if s["enabled"]]

    def set_enabled(self, name: str, on: bool) -> None:
        if name not in self.all():
            raise KeyError("技能不存在")
        self._state[name] = bool(on)
        self._save()

    def packs(self) -> list[dict]:
        items = list(self.all().values())
        pack_order = list(PACKS) + sorted({s["pack"] for s in items if s["pack"] not in PACKS})
        res: list[dict] = []
        for p in pack_order:
            members = [s for s in items if s["pack"] == p]
            if not members and p not in PACKS:
                continue
            res.append({
                "id": p,
                "label": PACK_LABELS.get(p, "自定义" if p == "custom" else p),
                "total": len(members),
                "enabled": sum(1 for m in members if m["enabled"]),
                "skills": [m["name"] for m in members],
            })
        return res

    def set_pack_enabled(self, pack: str, on: bool) -> list[dict]:
        p = str(pack or "").strip().lower()
        items = [s for s in self.all().values() if s["pack"] == p]
        if not items and p not in PACKS:
            raise KeyError(f"未知技能包: {pack}")
        for s in items:
            self._state[s["name"]] = bool(on)
        self._save()
        return [self._public_view(s, with_body=False) for s in self.all().values() if s["pack"] == p]

    def index_text(self, limit: int | None = None) -> str:
        en = self.enabled()
        if not en:
            return ""
        cap = self.default_limit if limit is None else max(1, int(limit))
        # 用户自定义技能与核心默认技能优先进入常驻索引，避免核心入口被长尾扩展挤掉
        ordered = sorted(
            en,
            key=lambda s: (
                0 if s["source"] == "user" else (1 if s.get("default_enabled") else 2),
                0 if s.get("pack") == "core" else 1,
                s["name"],
            ),
        )
        shown = ordered[:cap]
        lines = [f"- {s['name']}: {s['description'][:200]}" for s in shown]
        if len(ordered) > cap:
            lines.append(f"（另有 {len(ordered) - cap} 个已启用技能，可通过自动路由或 `/技能名` 按需触发）")
        return "\n".join(lines)

    # ------------------------------------------------------------ 技能自动路由
    def _skill_vector(self, s: dict) -> list[float]:
        sig = "|".join([
            s["name"],
            s["description"],
            s.get("description_en", ""),
            s.get("when_to_use", ""),
            " ".join(s.get("tags", [])),
            " ".join(s.get("triggers", {}).get("keywords", [])),
        ])
        cached = self._vec_cache.get(s["name"])
        if cached and cached[0] == sig:
            return cached[1]
        vec = local_embed(sig)
        self._vec_cache[s["name"]] = (sig, vec)
        return vec

    def route(
        self,
        text: str,
        files: list[str] | None = None,
        mimes: list[str] | None = None,
        top_k: int = 3,
        include_disabled: bool = False,
    ) -> list[dict]:
        """根据用户输入、附件路径/后缀与 MIME 类型，混合词法触发 + local_embed 相似度选出 Top-K 技能。"""
        q = (text or "").strip()
        f_list = [str(f).strip() for f in (files or []) if str(f).strip()]
        m_list = [str(m).strip().lower() for m in (mimes or []) if str(m).strip()]
        if not q and not f_list and not m_list:
            return []

        q_low = q.lower()
        q_toks = set(_lex_tokens(q))
        q_vec = local_embed(q) if q else []
        candidates = list(self.all().values()) if include_disabled else self.enabled()
        scored: list[dict] = []

        for s in candidates:
            score = 0.0
            reasons: list[str] = []
            name_low = s["name"].lower()

            # 1) 显式提及技能名或 /name / @name
            if name_low in q_low or name_low.replace("-", " ") in q_low:
                score += 1.4
                reasons.append("名称匹配")

            # 2) triggers.keywords 精确命中（短英文词需要词边界，避免误匹配子串）
            kw_hits = 0
            kw_weight = 0.0
            for kw in s.get("triggers", {}).get("keywords", []):
                kl = kw.lower().strip()
                if not kl:
                    continue
                hit = False
                if kl.isascii() and len(kl) <= 4 and kl.isalnum():
                    hit = bool(re.search(rf"(?<![a-z0-9]){re.escape(kl)}(?![a-z0-9])", q_low))
                else:
                    hit = kl in q_low
                if hit:
                    kw_hits += 1
                    kw_weight += 0.48 if len(kl) >= 4 else 0.32
            if kw_hits:
                score += min(1.35, kw_weight)
                reasons.append(f"关键词×{kw_hits}")

            # 3) 文件 glob 匹配（支持按工作区路径和裸文件名匹配，且文本中提及文件名如 .csv/.png 也能辅助）
            glob_hits = 0
            probe_files = list(f_list)
            for m_ext in re.findall(r"[\w./\\-]+\.[a-z0-9]{1,8}\b", q_low):
                probe_files.append(m_ext)
            for pat in s.get("triggers", {}).get("globs", []):
                pl = pat.lower()
                if any(
                    fnmatch.fnmatch(fp.lower(), pl) or fnmatch.fnmatch(Path(fp).name.lower(), pl)
                    for fp in probe_files
                ):
                    glob_hits += 1
            if glob_hits:
                score += min(0.9, 0.55 * glob_hits)
                reasons.append("文件类型匹配")

            # 4) MIME 匹配
            mime_hits = 0
            for mpat in s.get("triggers", {}).get("mimes", []):
                mp = mpat.lower()
                if any(fnmatch.fnmatch(mm, mp) for mm in m_list):
                    mime_hits += 1
            if mime_hits:
                score += min(0.9, 0.55 * mime_hits)
                reasons.append("MIME匹配")

            # 5) 标签与词法重叠
            meta_text = " ".join([
                s.get("description", ""),
                s.get("description_en", ""),
                s.get("when_to_use", ""),
                " ".join(s.get("tags", [])),
            ])
            s_toks = set(_lex_tokens(meta_text))
            if q_toks and s_toks:
                overlap = len(q_toks & s_toks) / max(1.0, min(len(q_toks), 8) ** 0.7)
                if overlap > 0:
                    score += min(0.55, overlap * 0.45)
                    if not reasons and overlap >= 0.42:
                        reasons.append("主题词汇匹配")

            # 6) local_embed 稠密向量相似度
            if q_vec:
                sim = max(0.0, _cosine(q_vec, self._skill_vector(s)))
                if sim > 0.12:
                    score += sim * 0.65
                    if not reasons and sim > 0.22:
                        reasons.append("语义相似")

            if score >= 0.34 and reasons:
                scored.append({
                    "name": s["name"],
                    "score": round(score, 3),
                    "pack": s["pack"],
                    "enabled": s["enabled"],
                    "description": s["description"],
                    "reasons": reasons[:3],
                })

        scored.sort(key=lambda x: (-x["score"], x["name"]))
        return scored[: max(1, min(int(top_k or 3), 5))]

    # ------------------------------------------------------------ 管理
    def create(self, name: str, description: str, body: str, enable: bool = True) -> dict:
        if not NAME_RE.match(name or ""):
            raise ValueError("技能名只能用小写字母、数字、- _，且以字母数字开头（≤64 字符）")
        if name in self.all() and self.all()[name]["source"] == "builtin":
            raise ValueError("不能覆盖内置技能，请换个名字")
        desc = re.sub(r"\s+", " ", description or "").strip()
        if not desc:
            raise ValueError("描述不能为空（模型靠它判断何时使用该技能）")
        if len(body) > 200_000:
            raise ValueError("技能正文过大")
        d = self.user_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n", "utf-8")
        self._state[name] = enable
        self._save()
        return self.get(name)  # type: ignore[return-value]

    def update(self, name: str, description: str | None = None, body: str | None = None) -> dict:
        s = self.all().get(name)
        if not s or s["source"] != "user":
            raise ValueError("只能修改自定义技能")
        return self.create(
            name,
            description if description is not None else s["description"],
            body if body is not None else s["body"],
            enable=s["enabled"],
        )

    def delete(self, name: str) -> None:
        s = self.all().get(name)
        if not s or s["source"] != "user":
            raise ValueError("只能删除自定义技能")
        shutil.rmtree(s["dir"], ignore_errors=True)
        self._state.pop(name, None)
        self._save()

    def install_zip(self, data: bytes) -> dict:
        if len(data) > MAX_ZIP:
            raise ValueError("压缩包过大（>50MB）")
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("不是有效的 zip 文件")
        infos = z.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_ZIP * 2:
            raise ValueError("压缩包文件过多或解压后过大")
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp).resolve()
            for i in infos:
                n = i.filename.replace("\\", "/")
                if n.startswith("/") or ".." in n.split("/") or re.match(r"^[A-Za-z]:", n):
                    raise ValueError(f"压缩包含不安全路径: {n}")
                mode = (i.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError(f"压缩包含符号链接，已拒绝: {n}")
                dest = (tmpd / n).resolve()
                if tmpd not in dest.parents and dest != tmpd:
                    raise ValueError(f"压缩包含不安全路径: {n}")
                if i.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(i))
            return self._install_dir(tmpd)

    def install_dir(self, path: str) -> dict:
        src = Path(path).expanduser().resolve()
        if not src.is_dir():
            raise ValueError("路径不是目录")
        if sum(1 for _ in src.rglob("*")) > MAX_FILES:
            raise ValueError("文件过多")
        if any(p.is_symlink() for p in src.rglob("*")):
            raise ValueError("目录含符号链接，已拒绝")
        return self._install_dir(src)

    def _install_dir(self, root: Path) -> dict:
        cands = (
            [root]
            if (root / "SKILL.md").is_file()
            else [d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()]
        )
        if len(cands) != 1:
            raise ValueError(
                "需要且仅需要一个包含 SKILL.md 的技能目录" if not cands else "压缩包内含多个技能，请一次装一个"
            )
        src = cands[0]
        fm, _ = parse_frontmatter((src / "SKILL.md").read_text("utf-8", "replace"))
        name = str(fm.get("name") or (src.name if src != root else "")).strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("SKILL.md 的 frontmatter 缺少合法的 name（小写字母/数字/-_）")
        if not fm.get("description"):
            raise ValueError("SKILL.md 的 frontmatter 缺少 description")
        ex = self.all().get(name)
        if ex and ex["source"] == "builtin":
            raise ValueError("与内置技能重名")
        dest = self.user_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=False)
        self._state[name] = False  # 新装技能默认不启用，需用户查看警告后手动开启
        self._save()
        return self.get(name)  # type: ignore[return-value]

    # ------------------------------------------------------------ 模型使用
    def load_for_model(self, name: str, ws: Path | None = None, *, tier: str = "standard") -> tuple[str, dict]:
        """返回 (给模型的文本, meta)。compact 档位自动选择精简版；含脚本的技能复制到工作区 .la-skills/<name>/。"""
        s = self.all().get(name)
        if not s or not s["enabled"]:
            raise KeyError(f"技能 {name} 不存在或未启用")

        body_raw = s["body"]
        profile_used = "full"
        if tier == "compact":
            compact_rel = s.get("profiles", {}).get("compact") or "SKILL.compact.md"
            cp = (s["dir"] / compact_rel).resolve()
            if s["dir"].resolve() in cp.parents and cp.is_file():
                _, cbody = parse_frontmatter(cp.read_text("utf-8", "replace"))
                if cbody.strip():
                    body_raw = cbody
                    profile_used = "compact"

        body = sanitize_untrusted(body_raw) if s["source"] == "user" else body_raw
        extra = [f for f in s["files"] if f not in ("SKILL.md", "SKILL.compact.md")]
        note = ""
        staged = ""
        if extra and ws is not None:
            has_scripts = any(f.startswith("scripts/") or f.endswith((".py", ".sh", ".js", ".ps1")) for f in extra)
            if has_scripts:
                staged = f".la-skills/{name}"
                dst = ws / staged
                if not dst.exists():
                    shutil.copytree(s["dir"], dst, symlinks=False)
                note = f"\n\n[技能文件已复制到工作区 `{staged}/`，脚本可在那里通过 run_command 运行（需用户审批）。附带文件: {', '.join(extra[:30])}]"
            else:
                note = f"\n\n[附带参考文件（用 read_skill_file 读取）: {', '.join(extra[:30])}]"
        meta = {
            "staged": staged,
            "profile": profile_used,
            "allowed_tools": s.get("allowed_tools"),
            "risk": s.get("risk", "read"),
            "pack": s.get("pack", ""),
        }
        return f"# 技能: {name}\n\n{body.strip()}{note}", meta

    def read_file(self, name: str, rel: str, limit: int = 40000) -> str:
        s = self.all().get(name)
        if not s or not s["enabled"]:
            raise KeyError(f"技能 {name} 不存在或未启用")
        p = (s["dir"] / rel).resolve()
        if s["dir"].resolve() not in p.parents or not p.is_file():
            raise ValueError("文件不在该技能目录内")
        return sanitize_untrusted(p.read_text("utf-8", "replace")[:limit])

    # ------------------------------------------------------------ 完整性锁定与质量门禁
    def _collect_builtin_hashes(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}
        if not self.builtin_dir.is_dir():
            return data
        for d in sorted(self.builtin_dir.iterdir()):
            if not d.is_dir() or not (d / "SKILL.md").is_file():
                continue
            files: dict[str, str] = {}
            for p in sorted(d.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    rel = p.relative_to(d).as_posix()
                    raw = p.read_bytes()
                    if p.suffix.lower() in _SCAN_EXTS:
                        raw = raw.replace(b"\r\n", b"\n")
                    files[rel] = _sha256_bytes(raw)
            digest_src = "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode("utf-8")
            data[d.name] = {"digest": _sha256_bytes(digest_src), "files": files}
        return data

    def build_lock(self, write: bool = True) -> dict:
        manifest = {
            "version": 2,
            "algorithm": "sha256",
            "skills": self._collect_builtin_hashes(),
        }
        if write:
            payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            (self.builtin_dir / LOCK_FILENAME).write_bytes(payload)
        return manifest

    def verify_lock(self) -> dict:
        lock_path = self.builtin_dir / LOCK_FILENAME
        if not lock_path.is_file():
            return {
                "ok": False,
                "present": False,
                "verified": 0,
                "missing": [],
                "modified": [],
                "extra": [],
                "warnings": [f"缺少 {LOCK_FILENAME}"],
            }
        try:
            lock_data = json.loads(lock_path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            return {
                "ok": False,
                "present": True,
                "verified": 0,
                "missing": [],
                "modified": [LOCK_FILENAME],
                "extra": [],
                "warnings": [f"{LOCK_FILENAME} 解析失败: {e}"],
            }
        expected = lock_data.get("skills") or {}
        actual = self._collect_builtin_hashes()
        missing: list[str] = []
        modified: list[str] = []
        extra: list[str] = []
        verified = 0

        for sname, exp in expected.items():
            if sname not in actual:
                missing.append(sname)
                continue
            act_files = actual[sname]["files"]
            exp_files = exp.get("files") or {}
            skill_bad = False
            for frel, fhash in exp_files.items():
                if frel not in act_files:
                    missing.append(f"{sname}/{frel}")
                    skill_bad = True
                elif act_files[frel] != fhash:
                    modified.append(f"{sname}/{frel}")
                    skill_bad = True
            for frel in act_files:
                if frel not in exp_files:
                    extra.append(f"{sname}/{frel}")
                    skill_bad = True
            if not skill_bad and actual[sname]["digest"] == exp.get("digest"):
                verified += 1
            elif not skill_bad:
                modified.append(sname)

        for sname in actual:
            if sname not in expected:
                extra.append(sname)

        warnings: list[str] = []
        if missing:
            warnings.append(f"缺失文件/技能: {', '.join(missing[:8])}")
        if modified:
            warnings.append(f"校验和不匹配（疑似被篡改）: {', '.join(modified[:8])}")
        if extra:
            warnings.append(f"未在 lock 中登记的新增条目: {', '.join(extra[:8])}")

        ok = not missing and not modified and not extra
        return {
            "ok": ok,
            "present": True,
            "verified": verified,
            "missing": missing,
            "modified": modified,
            "extra": extra,
            "warnings": warnings,
        }

    def lint(self, strict_lock: bool = True) -> dict:
        """检查内置技能规范完整性：Spec v2 字段、长度预算、双语描述、引用文件、evals>=2、安全扫描与 skills.lock。"""
        errors: list[str] = []
        warnings: list[str] = []
        checked = 0
        default_enabled_count = 0

        if not self.builtin_dir.is_dir():
            return {"ok": False, "count": 0, "default_enabled": 0, "errors": ["builtin_skills 目录不存在"], "warnings": [], "lock": self.verify_lock()}

        for d in sorted(self.builtin_dir.iterdir()):
            if not d.is_dir():
                continue
            checked += 1
            md = d / "SKILL.md"
            if not md.is_file():
                errors.append(f"{d.name}: 缺少 SKILL.md")
                continue
            fm, body = parse_frontmatter(md.read_text("utf-8", "replace"))
            sname = str(fm.get("name") or "").strip()
            if sname != d.name or not NAME_RE.match(sname):
                errors.append(f"{d.name}: frontmatter.name={sname!r} 不合法或与目录名不一致")
            if not str(fm.get("description") or "").strip():
                errors.append(f"{d.name}: 缺少 description")
            if not str(fm.get("description_en") or "").strip():
                errors.append(f"{d.name}: 缺少 description_en（双语描述）")
            if not str(fm.get("version") or "").strip():
                errors.append(f"{d.name}: 缺少 version")
            pack = str(fm.get("pack") or "").strip().lower()
            if pack not in PACKS:
                errors.append(f"{d.name}: pack={pack!r} 不在 {PACKS} 内")
            if not _as_str_list(fm.get("tags")):
                errors.append(f"{d.name}: 缺少 tags")
            if not str(fm.get("when_to_use") or "").strip():
                errors.append(f"{d.name}: 缺少 when_to_use")
            trigs = _normalize_triggers(fm.get("triggers"))
            if not trigs["keywords"]:
                errors.append(f"{d.name}: triggers.keywords 不能为空")
            risk = str(fm.get("risk") or "").strip().lower()
            if risk not in RISKS:
                errors.append(f"{d.name}: risk={risk!r} 不在 {RISKS} 内")
            if not str(fm.get("license") or "").strip():
                errors.append(f"{d.name}: 缺少 license")

            if bool(fm.get("default_enabled", False)):
                default_enabled_count += 1

            toks = estimate_tokens(body)
            if toks > SKILL_MD_MAX_TOKENS:
                errors.append(f"{d.name}: SKILL.md 正文约 {toks} tokens > {SKILL_MD_MAX_TOKENS}")

            profs = _normalize_profiles(fm.get("profiles"))
            compact_rel = profs.get("compact") or ""
            if not compact_rel:
                errors.append(f"{d.name}: 缺少 profiles.compact")
            else:
                cp = d / compact_rel
                if not cp.is_file():
                    errors.append(f"{d.name}: profiles.compact 指向的文件不存在: {compact_rel}")
                else:
                    _, cbody = parse_frontmatter(cp.read_text("utf-8", "replace"))
                    ctoks = estimate_tokens(cbody)
                    if ctoks > COMPACT_MD_MAX_TOKENS:
                        errors.append(f"{d.name}: {compact_rel} 约 {ctoks} tokens > {COMPACT_MD_MAX_TOKENS}")

            # 引用文件存在性检查
            for ref in _REF_PATH_RE.findall(body):
                if not (d / ref).exists():
                    errors.append(f"{d.name}: SKILL.md 引用的文件不存在: {ref}")

            # Evals 检查（>= 2 cases）
            ev_json = d / "evals" / "cases.json"
            if not ev_json.is_file():
                errors.append(f"{d.name}: 缺少 evals/cases.json（要求 >= 2 个用例）")
            else:
                try:
                    cases = json.loads(ev_json.read_text("utf-8"))
                    if not isinstance(cases, list) or len(cases) < 2:
                        errors.append(f"{d.name}: evals/cases.json 用例数不足 2 条")
                except ValueError as e:
                    errors.append(f"{d.name}: evals/cases.json JSON 损坏: {e}")

            # 安全性自检（不应触发提示注入警告）
            inj = suspicious(body, risk=False)
            if inj:
                errors.append(f"{d.name}: 触发注入告警 {inj}")

        if default_enabled_count > self.default_limit:
            errors.append(f"默认启用内置技能数 {default_enabled_count} 超过上限 {self.default_limit}")

        lock_res = self.verify_lock()
        if strict_lock and not lock_res["ok"]:
            errors.extend(lock_res["warnings"] or [f"{LOCK_FILENAME} 校验失败"])
        elif not lock_res["ok"]:
            warnings.extend(lock_res["warnings"])

        return {
            "ok": len(errors) == 0,
            "count": checked,
            "default_enabled": default_enabled_count,
            "errors": errors,
            "warnings": warnings,
            "lock": lock_res,
        }


def _cli(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "lint"
    builtin_dir = Path(__file__).resolve().parent / "builtin_skills"
    with tempfile.TemporaryDirectory() as tmp:
        sk = Skills(Path(tmp) / "user", builtin_dir, Path(tmp) / "state.json")
        if cmd == "lock":
            man = sk.build_lock(write=True)
            print(f"Wrote {builtin_dir / LOCK_FILENAME} for {len(man['skills'])} skills.")
            return 0
        if cmd == "lint":
            strict = "--no-lock" not in argv[2:]
            res = sk.lint(strict_lock=strict)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res["ok"] else 1
        print(f"Unknown command: {cmd}. Usage: python -m lagent.skills [lint|lock]", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
