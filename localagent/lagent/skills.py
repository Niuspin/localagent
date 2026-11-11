"""技能系统：兼容 Anthropic 风格的 SKILL.md 目录（frontmatter: name/description + Markdown 正文 + 可选 scripts/ references/）。

渐进式披露：系统提示里只放「名称 + 一句话描述」；模型需要时调用 use_skill 才加载正文，节省宝贵的本地上下文。
目录：
  skills/            用户技能（把文件夹丢进来，或在界面里安装/新建）
  lagent/builtin_skills/   随框架提供的内置技能（只读）
安全：安装 zip 做 zip-slip/符号链接/体积检查；新装技能默认“未启用”；扫描可疑内容给出警告；技能里的脚本只能经 run_command（受审批+沙箱）执行。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .security import sanitize_untrusted, suspicious

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_ZIP = 50 * 1024 * 1024
MAX_FILES = 500

def parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
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
    key = None
    for ln in raw.split("\n"):
        mm = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if mm:
            key, val = mm.group(1), mm.group(2).strip().strip("\"'")
            d[key] = "" if val in (">", "|", ">-", "|-") else val
        elif key and ln.startswith((" ", "\t")):
            d[key] = (str(d[key]) + " " + ln.strip()).strip()
    return d, body


def scan_warnings(root: Path, body: str) -> list[str]:
    w: list[str] = []
    scripts = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".py", ".sh", ".bat", ".ps1", ".js", ".exe", ".dll", ".so", ".cmd")]
    if scripts:
        w.append(f"包含 {len(scripts)} 个可执行脚本/二进制（运行前仍需你逐次审批）")
    texts = [body]
    for p in list(root.rglob("*"))[:200]:
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".ps1", ".bat", ".js"):
            try:
                if p.stat().st_size < 400_000:
                    texts.append(p.read_text("utf-8", "replace"))
            except OSError:
                pass
    blob = "\n".join(texts)
    w += suspicious(blob, risk=True)
    return w


class Skills:
    def __init__(self, user_dir: Path, builtin_dir: Path, state_path: Path):
        self.user_dir, self.builtin_dir, self.state_path = Path(user_dir), Path(builtin_dir), Path(state_path)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, bool] = {}
        try:
            self._state = {k: bool(v) for k, v in json.loads(self.state_path.read_text("utf-8")).items()}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), "utf-8")

    # ------------------------------------------------------------ 发现
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
        files = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file() and not p.is_symlink())[:200]
        enabled = self._state.get(name, source == "builtin")
        return {"name": name, "description": desc[:600], "enabled": enabled, "source": source, "dir": d, "body": body,
                "files": files, "size": sum((d / x).stat().st_size for x in files if (d / x).exists()),
                "warnings": scan_warnings(d, body) if source == "user" else []}

    def all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for base, src in ((self.builtin_dir, "builtin"), (self.user_dir, "user")):
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if d.is_dir():
                        s = self._load_dir(d, src)
                        if s and (s["name"] not in out or src == "user"):
                            out[s["name"]] = s
        return out

    def list(self) -> list[dict]:
        return [{k: v for k, v in s.items() if k not in ("dir", "body")} for s in self.all().values()]

    def get(self, name: str, with_body: bool = True) -> dict | None:
        s = self.all().get(name)
        if not s:
            return None
        return {k: v for k, v in s.items() if k != "dir" and (with_body or k != "body")}

    def enabled(self) -> list[dict]:
        return [s for s in self.all().values() if s["enabled"]]

    def set_enabled(self, name: str, on: bool) -> None:
        if name not in self.all():
            raise KeyError("技能不存在")
        self._state[name] = bool(on)
        self._save()

    def index_text(self) -> str:
        en = self.enabled()
        if not en:
            return ""
        return "\n".join(f"- {s['name']}: {s['description'][:220]}" for s in en)

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
        return self.create(name, description if description is not None else s["description"], body if body is not None else s["body"],
                           enable=s["enabled"])

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
        cands = [root] if (root / "SKILL.md").is_file() else [d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()]
        if len(cands) != 1:
            raise ValueError("需要且仅需要一个包含 SKILL.md 的技能目录" if not cands else "压缩包内含多个技能，请一次装一个")
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
    def load_for_model(self, name: str, ws: Path | None = None) -> tuple[str, dict]:
        """返回 (给模型的文本, meta)。含脚本的技能会被复制到工作区 .la-skills/<name>/ 以便在沙箱内运行。"""
        s = self.all().get(name)
        if not s or not s["enabled"]:
            raise KeyError(f"技能 {name} 不存在或未启用")
        body = sanitize_untrusted(s["body"]) if s["source"] == "user" else s["body"]
        extra = [f for f in s["files"] if f != "SKILL.md"]
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
        return f"# 技能: {name}\n\n{body.strip()}{note}", {"staged": staged}

    def read_file(self, name: str, rel: str, limit: int = 40000) -> str:
        s = self.all().get(name)
        if not s or not s["enabled"]:
            raise KeyError(f"技能 {name} 不存在或未启用")
        p = (s["dir"] / rel).resolve()
        if s["dir"].resolve() not in p.parents or not p.is_file():
            raise ValueError("文件不在该技能目录内")
        return sanitize_untrusted(p.read_text("utf-8", "replace")[:limit])
