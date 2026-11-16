"""纯 Python 零依赖 SVG 图表渲染器（M4 §4.2 / §4.4）。

支持 bar / line / pie / scatter / heatmap 五类常见图表：
- 零外部必选依赖（纯标准库生成规范 XML SVG；可选检测 matplotlib 导出 PNG）；
- 内置 CJK 字符宽度估算与中文字体栈，中文标题与刻度不截断、不乱码；
- 支持浅色/深色主题 CSS 变量与自动暗色媒体查询，兼容设计系统主题切换。
"""
from __future__ import annotations

import math
from html import escape
from typing import Any

CHART_TYPES = ("bar", "line", "pie", "scatter", "heatmap")

# Okabe-Ito 衍生色盲安全高对比色板
_PALETTE = (
    "#C25B38",  # terracotta accent
    "#2563EB",  # royal blue
    "#0D9488",  # teal
    "#D97706",  # amber
    "#7C3AED",  # violet
    "#DB2777",  # rose
    "#059669",  # emerald
    "#475569",  # slate
)


def _cjk_width(text: str, font_px: float = 12.0) -> float:
    """估算含 CJK 字符文本的像素宽度，用于坐标轴留白与图例排版。"""
    units = 0.0
    for ch in str(text or ""):
        o = ord(ch)
        if 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
            units += 1.05
        elif ch.isupper():
            units += 0.72
        elif ch.isspace():
            units += 0.35
        else:
            units += 0.58
    return units * font_px


def _fmt_num(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2g}M"
    if abs(v) >= 10_000:
        return f"{v / 1000:.1f}k"
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    if abs(v) < 0.01 and v != 0:
        return f"{v:.2e}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _nice_ticks(vmin: float, vmax: float, count: int = 5) -> list[float]:
    if vmin == vmax:
        pad = 1.0 if vmin == 0 else abs(vmin) * 0.2
        vmin -= pad
        vmax += pad
    span = max(vmax - vmin, 1e-9)
    raw_step = span / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(raw_step))
    norm = raw_step / mag
    if norm <= 1.5:
        step = 1.0 * mag
    elif norm <= 3.0:
        step = 2.0 * mag
    elif norm <= 7.0:
        step = 5.0 * mag
    else:
        step = 10.0 * mag
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    ticks: list[float] = []
    cur = lo
    while cur <= hi + step * 1e-6 and len(ticks) < 12:
        ticks.append(round(cur, 10))
        cur += step
    return ticks


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _normalize_series(
    *,
    labels: list[Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    values: list[Any] | None = None,
    title: str = "",
) -> tuple[list[str], list[dict[str, Any]]]:
    lbls = [str(x) for x in (labels or [])]
    norm_series: list[dict[str, Any]] = []
    if series:
        for idx, s in enumerate(series):
            if not isinstance(s, dict):
                continue
            raw_vals = s.get("values") if s.get("values") is not None else (s.get("data") or [])
            vals = [_to_float(x) for x in raw_vals]
            if vals:
                norm_series.append({
                    "name": str(s.get("name") or f"系列 {idx + 1}"),
                    "values": vals,
                    "color": str(s.get("color") or _PALETTE[idx % len(_PALETTE)]),
                })
    elif values:
        if all(isinstance(v, dict) for v in values):
            if not lbls:
                lbls = [str(v.get("label") or v.get("name") or f"#{i + 1}") for i, v in enumerate(values)]
            norm_series.append({
                "name": title or "数值",
                "values": [_to_float(v.get("value", 0)) for v in values],
                "color": _PALETTE[0],
            })
        else:
            norm_series.append({
                "name": title or "数值",
                "values": [_to_float(v) for v in values],
                "color": _PALETTE[0],
            })
    if not norm_series:
        raise ValueError("图表缺少可绘制的数据 series / values")
    max_len = max(len(s["values"]) for s in norm_series)
    if not lbls:
        lbls = [str(i + 1) for i in range(max_len)]
    elif len(lbls) < max_len:
        lbls += [str(i + 1) for i in range(len(lbls), max_len)]
    return lbls[:max_len], norm_series


def _theme_style_block(theme: str) -> tuple[str, str]:
    t = (theme or "auto").strip().lower()
    if t not in ("auto", "light", "dark"):
        t = "auto"
    light_vars = (
        "--chart-bg: #FCFBF9; "
        "--chart-panel: #F6F3EC; "
        "--chart-fg: #1F242C; "
        "--chart-muted: #5C626E; "
        "--chart-grid: rgba(31, 36, 44, 0.14); "
        "--chart-border: rgba(31, 36, 44, 0.22); "
        "--chart-tooltip-bg: #FFFFFF;"
    )
    dark_vars = (
        "--chart-bg: #14171D; "
        "--chart-panel: #1C2029; "
        "--chart-fg: #E6E9EF; "
        "--chart-muted: #A1AAB8; "
        "--chart-grid: rgba(230, 233, 239, 0.16); "
        "--chart-border: rgba(230, 233, 239, 0.28); "
        "--chart-tooltip-bg: #242A36;"
    )
    base_css = f"svg.la-chart {{ {dark_vars if t == 'dark' else light_vars} font-family: \"Microsoft YaHei\", \"PingFang SC\", \"Noto Sans CJK SC\", \"Segoe UI\", system-ui, sans-serif; }}\n"
    if t == "auto":
        base_css += (
            f"@media (prefers-color-scheme: dark) {{ svg.la-chart {{ {dark_vars} }} }}\n"
            f":root[data-theme='dark'] svg.la-chart, :root[data-theme='midnight'] svg.la-chart, :root[data-theme='hc'] svg.la-chart, svg.la-chart.theme-dark {{ {dark_vars} }}\n"
        )
    cls = "la-chart" + (" theme-dark" if t == "dark" else " theme-light" if t == "light" else "")
    return cls, base_css


def _wrap_svg(
    *,
    width: int,
    height: int,
    theme: str,
    title: str,
    subtitle: str,
    body_elems: list[str],
    legend_items: list[tuple[str, str]] | None = None,
) -> str:
    cls, css = _theme_style_block(theme)
    safe_title = escape(title or "Chart")
    safe_sub = escape(subtitle or "")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" class="{cls}" role="img" aria-label="{safe_title}">',
        f"  <title>{safe_title}</title>",
    ]
    if safe_sub:
        parts.append(f"  <desc>{safe_sub}</desc>")
    parts.extend([
        "  <defs>",
        f"    <style type=\"text/css\"><![CDATA[\n{css}\n      .la-bg {{ fill: var(--chart-bg); }}\n      .la-title {{ fill: var(--chart-fg); font-size: 16px; font-weight: 700; }}\n      .la-sub {{ fill: var(--chart-muted); font-size: 12px; }}\n      .la-axis {{ stroke: var(--chart-border); stroke-width: 1.2; }}\n      .la-grid {{ stroke: var(--chart-grid); stroke-width: 1; stroke-dasharray: 3 3; }}\n      .la-tick {{ fill: var(--chart-muted); font-size: 11px; }}\n      .la-lbl {{ fill: var(--chart-fg); font-size: 11.5px; }}\n      .la-val {{ fill: var(--chart-fg); font-size: 10.5px; font-weight: 600; }}\n      .la-legend {{ fill: var(--chart-fg); font-size: 11.5px; }}\n    ]]></style>",
        "  </defs>",
        f'  <rect class="la-bg" x="0" y="0" width="{width}" height="{height}" rx="10" />',
    ])
    if title:
        parts.append(f'  <text class="la-title" x="24" y="32">{safe_title}</text>')
    if subtitle:
        parts.append(f'  <text class="la-sub" x="24" y="51">{safe_sub}</text>')
    if legend_items:
        lx = width - 24.0
        ly = 28.0
        for name, color in reversed(legend_items):
            tw = _cjk_width(name, 11.5)
            item_w = tw + 22
            lx -= item_w
            if lx < 180:
                break
            parts.append(f'  <rect x="{lx:.1f}" y="{ly - 9:.1f}" width="10" height="10" rx="2" fill="{escape(color)}" />')
            parts.append(f'  <text class="la-legend" x="{lx + 14:.1f}" y="{ly:.1f}">{escape(name)}</text>')
            lx -= 10
    parts.extend(body_elems)
    parts.append("</svg>")
    return "\n".join(parts)


def _render_bar_or_line(
    kind: str,
    *,
    title: str,
    subtitle: str,
    labels: list[str],
    series: list[dict[str, Any]],
    x_label: str,
    y_label: str,
    theme: str,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any]]:
    all_vals = [v for s in series for v in s["values"]]
    vmin = min(0.0, min(all_vals)) if kind == "bar" else min(all_vals)
    vmax = max(0.0, max(all_vals)) if kind == "bar" else max(all_vals)
    ticks = _nice_ticks(vmin, vmax, 5)
    t_lo, t_hi = ticks[0], ticks[-1]
    max_tick_w = max((_cjk_width(_fmt_num(tv), 11.0) for tv in ticks), default=32.0)

    pad_l = max(56.0, 26.0 + max_tick_w + (16.0 if y_label else 0.0))
    pad_r = 26.0
    pad_t = 68.0 if subtitle else 50.0
    pad_b = 58.0 + (18.0 if x_label else 0.0)
    pw = max(120.0, width - pad_l - pad_r)
    ph = max(100.0, height - pad_t - pad_b)

    def y_of(v: float) -> float:
        ratio = (v - t_lo) / max(t_hi - t_lo, 1e-9)
        return pad_t + ph - ratio * ph

    elems: list[str] = []
    # Grid lines + Y ticks
    for tv in ticks:
        yy = y_of(tv)
        elems.append(f'  <line class="la-grid" x1="{pad_l:.1f}" y1="{yy:.1f}" x2="{pad_l + pw:.1f}" y2="{yy:.1f}" />')
        elems.append(f'  <text class="la-tick" x="{pad_l - 8:.1f}" y="{yy + 4:.1f}" text-anchor="end">{escape(_fmt_num(tv))}</text>')
    # Axes
    y_zero = y_of(0.0) if t_lo <= 0.0 <= t_hi else (pad_t + ph)
    elems.append(f'  <line class="la-axis" x1="{pad_l:.1f}" y1="{pad_t:.1f}" x2="{pad_l:.1f}" y2="{pad_t + ph:.1f}" />')
    elems.append(f'  <line class="la-axis" x1="{pad_l:.1f}" y1="{y_zero:.1f}" x2="{pad_l + pw:.1f}" y2="{y_zero:.1f}" />')

    n_cats = len(labels)
    n_ser = len(series)
    slot_w = pw / max(n_cats, 1)

    # X labels (thin out if dense)
    stride = max(1, math.ceil(n_cats / 14))
    for i, lbl in enumerate(labels):
        cx = pad_l + (i + 0.5) * slot_w
        if i % stride == 0:
            short = lbl if len(lbl) <= 14 else lbl[:13] + "…"
            elems.append(f'  <text class="la-tick" x="{cx:.1f}" y="{pad_t + ph + 18:.1f}" text-anchor="middle">{escape(short)}</text>')

    if kind == "bar":
        group_w = slot_w * 0.72
        bar_w = max(4.0, (group_w - max(0, n_ser - 1) * 3.0) / max(n_ser, 1))
        for si, ser in enumerate(series):
            col = escape(ser["color"])
            for ci in range(n_cats):
                val = ser["values"][ci] if ci < len(ser["values"]) else 0.0
                gx = pad_l + ci * slot_w + (slot_w - group_w) / 2 + si * (bar_w + 3.0)
                yv = y_of(val)
                top = min(yv, y_zero)
                bh = max(1.5, abs(y_zero - yv))
                elems.append(f'  <rect x="{gx:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="3" fill="{col}" opacity="0.92"><title>{escape(labels[ci])} · {escape(ser["name"])}: {escape(_fmt_num(val))}</title></rect>')
                if n_cats <= 12 and n_ser <= 2:
                    vy = top - 5 if val >= 0 else top + bh + 12
                    elems.append(f'  <text class="la-val" x="{gx + bar_w / 2:.1f}" y="{vy:.1f}" text-anchor="middle">{escape(_fmt_num(val))}</text>')
    else:
        for si, ser in enumerate(series):
            col = escape(ser["color"])
            pts: list[tuple[float, float, float]] = []
            for ci in range(n_cats):
                val = ser["values"][ci] if ci < len(ser["values"]) else 0.0
                cx = pad_l + (ci + 0.5) * slot_w
                cy = y_of(val)
                pts.append((cx, cy, val))
            pt_str = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
            elems.append(f'  <polyline fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="{pt_str}" />')
            for ci, (cx, cy, val) in enumerate(pts):
                elems.append(f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{col}" stroke="var(--chart-bg)" stroke-width="1.5"><title>{escape(labels[ci])} · {escape(ser["name"])}: {escape(_fmt_num(val))}</title></circle>')
                if n_cats <= 10 and n_ser == 1:
                    elems.append(f'  <text class="la-val" x="{cx:.1f}" y="{cy - 8:.1f}" text-anchor="middle">{escape(_fmt_num(val))}</text>')

    if x_label:
        elems.append(f'  <text class="la-lbl" x="{pad_l + pw / 2:.1f}" y="{height - 14:.1f}" text-anchor="middle">{escape(x_label)}</text>')
    if y_label:
        cy_mid = pad_t + ph / 2
        elems.append(f'  <text class="la-lbl" x="18" y="{cy_mid:.1f}" text-anchor="middle" transform="rotate(-90 18 {cy_mid:.1f})">{escape(y_label)}</text>')

    legend = [(s["name"], s["color"]) for s in series] if (len(series) > 1 or series[0]["name"] != (title or "数值")) else None
    svg = _wrap_svg(width=width, height=height, theme=theme, title=title, subtitle=subtitle, body_elems=elems, legend_items=legend)
    return svg, {"chart_type": kind, "series_count": n_ser, "point_count": n_cats, "min": min(all_vals), "max": max(all_vals)}


def _render_pie(
    *,
    title: str,
    subtitle: str,
    labels: list[str],
    series: list[dict[str, Any]],
    theme: str,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any]]:
    vals = [max(0.0, v) for v in series[0]["values"]]
    total = sum(vals)
    if total <= 0:
        raise ValueError("饼图需要至少一个大于 0 的数值")

    top_y = 66.0 if subtitle else 48.0
    r = min((width * 0.48) / 2.0, (height - top_y - 26.0) / 2.0)
    r = max(48.0, r)
    cx = width * 0.36
    cy = top_y + (height - top_y - 18.0) / 2.0
    r_in = r * 0.42

    elems: list[str] = []
    angle = -math.pi / 2.0
    non_zero = [(lbl, v) for lbl, v in zip(labels, vals) if v > 0]
    legend_rows: list[tuple[str, float, str]] = []
    for idx, (lbl, v) in enumerate(non_zero):
        col = _PALETTE[idx % len(_PALETTE)]
        frac = v / total
        if len(non_zero) == 1:
            elems.append(f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{col}" />')
            elems.append(f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r_in:.1f}" fill="var(--chart-bg)" />')
        else:
            sweep = frac * 2.0 * math.pi
            a1, a2 = angle, angle + sweep
            large = 1 if sweep > math.pi else 0
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
            xi2, yi2 = cx + r_in * math.cos(a2), cy + r_in * math.sin(a2)
            xi1, yi1 = cx + r_in * math.cos(a1), cy + r_in * math.sin(a1)
            d = (
                f"M {x1:.2f} {y1:.2f} "
                f"A {r:.2f} {r:.2f} 0 {large} 1 {x2:.2f} {y2:.2f} "
                f"L {xi2:.2f} {yi2:.2f} "
                f"A {r_in:.2f} {r_in:.2f} 0 {large} 0 {xi1:.2f} {yi1:.2f} Z"
            )
            elems.append(f'  <path d="{d}" fill="{col}" stroke="var(--chart-bg)" stroke-width="2"><title>{escape(lbl)}: {escape(_fmt_num(v))} ({frac * 100:.1f}%)</title></path>')
            angle = a2
        legend_rows.append((lbl, frac * 100.0, col))

    # Center total
    elems.append(f'  <text class="la-title" x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle">{escape(_fmt_num(total))}</text>')

    # Right legend
    lx = max(cx + r + 28.0, width * 0.62)
    ly = top_y + 22.0
    for lbl, pct, col in legend_rows[:12]:
        short = lbl if len(lbl) <= 18 else lbl[:17] + "…"
        elems.append(f'  <rect x="{lx:.1f}" y="{ly - 9:.1f}" width="11" height="11" rx="2.5" fill="{col}" />')
        elems.append(f'  <text class="la-lbl" x="{lx + 18:.1f}" y="{ly + 1:.1f}">{escape(short)} · {pct:.1f}%</text>')
        ly += 24.0

    svg = _wrap_svg(width=width, height=height, theme=theme, title=title, subtitle=subtitle, body_elems=elems)
    return svg, {"chart_type": "pie", "slices": len(non_zero), "total": total}


def _render_scatter(
    *,
    title: str,
    subtitle: str,
    points: list[Any] | None,
    series: list[dict[str, Any]] | None,
    x_label: str,
    y_label: str,
    theme: str,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if series:
        for idx, s in enumerate(series):
            raw_pts = s.get("points") or s.get("data") or []
            parsed = _parse_xy_points(raw_pts)
            if parsed:
                groups.append({"name": str(s.get("name") or f"系列 {idx + 1}"), "points": parsed, "color": str(s.get("color") or _PALETTE[idx % len(_PALETTE)])})
    elif points:
        parsed = _parse_xy_points(points)
        if parsed:
            groups.append({"name": title or "数据点", "points": parsed, "color": _PALETTE[0]})
    if not groups:
        raise ValueError("散点图缺少有效的 (x, y) 坐标数据 points")

    xs = [p[0] for g in groups for p in g["points"]]
    ys = [p[1] for g in groups for p in g["points"]]
    xticks = _nice_ticks(min(xs), max(xs), 5)
    yticks = _nice_ticks(min(ys), max(ys), 5)
    x_lo, x_hi = xticks[0], xticks[-1]
    y_lo, y_hi = yticks[0], yticks[-1]

    pad_l = max(56.0, 26.0 + max((_cjk_width(_fmt_num(t), 11.0) for t in yticks), default=32.0))
    pad_r = 26.0
    pad_t = 68.0 if subtitle else 50.0
    pad_b = 58.0 + (18.0 if x_label else 0.0)
    pw = max(120.0, width - pad_l - pad_r)
    ph = max(100.0, height - pad_t - pad_b)

    def x_of(v: float) -> float:
        return pad_l + ((v - x_lo) / max(x_hi - x_lo, 1e-9)) * pw

    def y_of(v: float) -> float:
        return pad_t + ph - ((v - y_lo) / max(y_hi - y_lo, 1e-9)) * ph

    elems: list[str] = []
    for tx in xticks:
        xx = x_of(tx)
        elems.append(f'  <line class="la-grid" x1="{xx:.1f}" y1="{pad_t:.1f}" x2="{xx:.1f}" y2="{pad_t + ph:.1f}" />')
        elems.append(f'  <text class="la-tick" x="{xx:.1f}" y="{pad_t + ph + 18:.1f}" text-anchor="middle">{escape(_fmt_num(tx))}</text>')
    for ty in yticks:
        yy = y_of(ty)
        elems.append(f'  <line class="la-grid" x1="{pad_l:.1f}" y1="{yy:.1f}" x2="{pad_l + pw:.1f}" y2="{yy:.1f}" />')
        elems.append(f'  <text class="la-tick" x="{pad_l - 8:.1f}" y="{yy + 4:.1f}" text-anchor="end">{escape(_fmt_num(ty))}</text>')
    elems.append(f'  <rect x="{pad_l:.1f}" y="{pad_t:.1f}" width="{pw:.1f}" height="{ph:.1f}" fill="none" class="la-axis" />')

    total_pts = 0
    for g in groups:
        col = escape(g["color"])
        for x, y, lbl in g["points"]:
            total_pts += 1
            tip = f"{lbl}: ({_fmt_num(x)}, {_fmt_num(y)})" if lbl else f"({x}, {y})"
            elems.append(f'  <circle cx="{x_of(x):.1f}" cy="{y_of(y):.1f}" r="5" fill="{col}" opacity="0.86"><title>{escape(tip)}</title></circle>')

    if x_label:
        elems.append(f'  <text class="la-lbl" x="{pad_l + pw / 2:.1f}" y="{height - 14:.1f}" text-anchor="middle">{escape(x_label)}</text>')
    if y_label:
        cy_mid = pad_t + ph / 2
        elems.append(f'  <text class="la-lbl" x="18" y="{cy_mid:.1f}" text-anchor="middle" transform="rotate(-90 18 {cy_mid:.1f})">{escape(y_label)}</text>')

    legend = [(g["name"], g["color"]) for g in groups] if len(groups) > 1 else None
    svg = _wrap_svg(width=width, height=height, theme=theme, title=title, subtitle=subtitle, body_elems=elems, legend_items=legend)
    return svg, {"chart_type": "scatter", "series_count": len(groups), "point_count": total_pts}


def _parse_xy_points(raw: list[Any]) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for it in raw or []:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            out.append((_to_float(it[0]), _to_float(it[1]), str(it[2]) if len(it) > 2 else ""))
        elif isinstance(it, dict) and ("x" in it and "y" in it):
            out.append((_to_float(it["x"]), _to_float(it["y"]), str(it.get("label") or "")))
    return out


def _heat_color(t: float) -> str:
    t = max(0.0, min(1.0, t))
    # 浅暖米 -> 琥珀 -> 深陶红
    stops = [(243, 236, 224), (235, 168, 92), (172, 78, 47)]
    if t < 0.5:
        u = t * 2.0
        a, b = stops[0], stops[1]
    else:
        u = (t - 0.5) * 2.0
        a, b = stops[1], stops[2]
    r = int(round(a[0] + (b[0] - a[0]) * u))
    g = int(round(a[1] + (b[1] - a[1]) * u))
    bl = int(round(a[2] + (b[2] - a[2]) * u))
    return f"#{r:02x}{g:02x}{bl:02x}"


def _render_heatmap(
    *,
    title: str,
    subtitle: str,
    matrix: list[list[Any]],
    x_labels: list[str] | None,
    y_labels: list[str] | None,
    theme: str,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any]]:
    if not matrix or not isinstance(matrix, list):
        raise ValueError("热力图缺少二维矩阵 matrix")
    rows = [[_to_float(c) for c in (r if isinstance(r, list) else [r])] for r in matrix]
    nrows = len(rows)
    ncols = max((len(r) for r in rows), default=0)
    if nrows == 0 or ncols == 0:
        raise ValueError("热力图 matrix 不能为空")
    for r in rows:
        while len(r) < ncols:
            r.append(0.0)

    xlbls = [str(x) for x in (x_labels or [])] + [str(i + 1) for i in range(ncols)]
    xlbls = xlbls[:ncols]
    ylbls = [str(y) for y in (y_labels or [])] + [str(i + 1) for i in range(nrows)]
    ylbls = ylbls[:nrows]

    max_yw = max((_cjk_width(y, 11.5) for y in ylbls), default=24.0)
    pad_l = max(56.0, min(140.0, max_yw + 24.0))
    pad_r = 28.0
    pad_t = 68.0 if subtitle else 50.0
    pad_b = 50.0
    pw = max(120.0, width - pad_l - pad_r)
    ph = max(100.0, height - pad_t - pad_b)
    cw = pw / ncols
    ch = ph / nrows

    flat = [v for r in rows for v in r]
    vlo, vhi = min(flat), max(flat)
    span = max(vhi - vlo, 1e-9)

    elems: list[str] = []
    for ri, row in enumerate(rows):
        y = pad_t + ri * ch
        short_y = ylbls[ri] if len(ylbls[ri]) <= 14 else ylbls[ri][:13] + "…"
        elems.append(f'  <text class="la-tick" x="{pad_l - 8:.1f}" y="{y + ch / 2 + 4:.1f}" text-anchor="end">{escape(short_y)}</text>')
        for ci, val in enumerate(row):
            x = pad_l + ci * cw
            ratio = (val - vlo) / span
            fill = _heat_color(ratio)
            elems.append(f'  <rect x="{x + 1:.1f}" y="{y + 1:.1f}" width="{max(2.0, cw - 2):.1f}" height="{max(2.0, ch - 2):.1f}" rx="3" fill="{fill}"><title>{escape(ylbls[ri])} × {escape(xlbls[ci])}: {escape(_fmt_num(val))}</title></rect>')
            if cw >= 34 and ch >= 22:
                txt_col = "#FFFFFF" if ratio >= 0.58 else "#1F242C"
                elems.append(f'  <text x="{x + cw / 2:.1f}" y="{y + ch / 2 + 4:.1f}" font-size="10.5" font-weight="600" fill="{txt_col}" text-anchor="middle">{escape(_fmt_num(val))}</text>')
    for ci, xl in enumerate(xlbls):
        x = pad_l + (ci + 0.5) * cw
        short_x = xl if len(xl) <= 12 else xl[:11] + "…"
        elems.append(f'  <text class="la-tick" x="{x:.1f}" y="{pad_t + ph + 18:.1f}" text-anchor="middle">{escape(short_x)}</text>')

    svg = _wrap_svg(width=width, height=height, theme=theme, title=title, subtitle=subtitle, body_elems=elems)
    return svg, {"chart_type": "heatmap", "rows": nrows, "cols": ncols, "min": vlo, "max": vhi}


def make_chart_svg(
    chart_type: str = "bar",
    *,
    title: str = "",
    subtitle: str = "",
    labels: list[Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    values: list[Any] | None = None,
    points: list[Any] | None = None,
    matrix: list[list[Any]] | None = None,
    x_labels: list[str] | None = None,
    y_labels: list[str] | None = None,
    x_label: str = "",
    y_label: str = "",
    theme: str = "auto",
    width: int = 720,
    height: int = 440,
) -> tuple[str, dict[str, Any]]:
    """生成规范 XML SVG 图表并返回 `(svg_text, stats_meta)`。"""
    kind = (chart_type or "bar").strip().lower()
    if kind not in CHART_TYPES:
        raise ValueError(f"不支持的图表类型 {chart_type!r}，可选: {', '.join(CHART_TYPES)}")
    w = max(320, min(1600, int(width or 720)))
    h = max(240, min(1200, int(height or 440)))

    if kind == "heatmap":
        return _render_heatmap(
            title=title,
            subtitle=subtitle,
            matrix=matrix or [],
            x_labels=x_labels or ([str(x) for x in labels] if labels else None),
            y_labels=y_labels,
            theme=theme,
            width=w,
            height=h,
        )
    if kind == "scatter":
        return _render_scatter(
            title=title,
            subtitle=subtitle,
            points=points,
            series=series,
            x_label=x_label,
            y_label=y_label,
            theme=theme,
            width=w,
            height=h,
        )
    lbls, norm_ser = _normalize_series(labels=labels, series=series, values=values, title=title)
    if kind == "pie":
        return _render_pie(title=title, subtitle=subtitle, labels=lbls, series=norm_ser, theme=theme, width=w, height=h)
    return _render_bar_or_line(
        kind,
        title=title,
        subtitle=subtitle,
        labels=lbls,
        series=norm_ser,
        x_label=x_label,
        y_label=y_label,
        theme=theme,
        width=w,
        height=h,
    )
