#!/usr/bin/env python3
"""WCAG 2.2 AA 对比度与 CSS 合规自动校验（M6 §6.5 & §6.8）。

用法: python tests/ui_contrast.py
校验 web/css/themes/*.css 中全部主题的前景/背景/强调/语义色组合是否达到 WCAG AA (>= 4.5:1)，
并校验 web/ 下所有 CSS 文件是否存在禁用写法 `transition: all`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
THEMES_DIR = WEB_DIR / "css" / "themes"

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
_VAR_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")
_TRANSITION_ALL_RE = re.compile(r"transition\s*:\s*all\b", re.I)

PAIRS: tuple[tuple[str, str, float], ...] = (
    ("text", "bg", 4.5),
    ("text", "bg-side", 4.5),
    ("text", "bg-elev", 4.5),
    ("text", "bg-soft", 4.5),
    ("muted", "bg", 4.5),
    ("muted", "bg-side", 4.5),
    ("accent-fg", "accent", 4.5),
    ("ok", "bg", 4.5),
    ("warn", "bg", 4.5),
    ("danger", "bg", 4.5),
    ("info", "bg", 4.5),
)


def parse_hex(val: str) -> tuple[int, int, int] | None:
    m = _HEX_RE.match(val.strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rel_luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg: str, bg: str) -> float:
    c1, c2 = parse_hex(fg), parse_hex(bg)
    if not c1 or not c2:
        return 0.0
    l1, l2 = rel_luminance(c1), rel_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def extract_theme_vars(path: Path) -> dict[str, str]:
    txt = path.read_text("utf-8")
    out: dict[str, str] = {}
    for k, v in _VAR_RE.findall(txt):
        if k not in out:
            out[k] = v.strip()
    return out


def verify_all() -> list[str]:
    failures: list[str] = []
    for theme_file in sorted(THEMES_DIR.glob("*.css")):
        vars_map = extract_theme_vars(theme_file)
        for fg_k, bg_k, min_ratio in PAIRS:
            fg, bg = vars_map.get(fg_k, ""), vars_map.get(bg_k, "")
            ratio = contrast_ratio(fg, bg)
            if ratio < min_ratio:
                failures.append(f"{theme_file.name}: --{fg_k} ({fg}) on --{bg_k} ({bg}) = {ratio:.2f}:1 < {min_ratio}:1")

    for css_file in sorted(WEB_DIR.rglob("*.css")):
        txt = css_file.read_text("utf-8")
        if _TRANSITION_ALL_RE.search(txt):
            failures.append(f"{css_file.relative_to(WEB_DIR)}: 检测到禁用属性 transition: all")
    return failures


if __name__ == "__main__":
    errs = verify_all()
    if errs:
        print("UI Contrast / CSS Audit FAILED:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print(f"OK: {len(list(THEMES_DIR.glob('*.css')))} themes pass WCAG 2.2 AA & zero transition:all")
