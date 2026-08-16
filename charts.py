"""Server-rendered inline SVG charts for the admin Trends tab.

No client-side charting library — CSP allows only self-hosted scripts, so
charts are plain SVG built here and dropped into the template as safe markup.
Native <title> elements provide hover tooltips without any JS.
"""

from __future__ import annotations

from html import escape
from typing import Any

WIDTH = 680
HEIGHT = 200
PAD_LEFT = 34
PAD_RIGHT = 12
PAD_TOP = 16
PAD_BOTTOM = 26
PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT
PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM

GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
MUTED_TEXT = "#898781"


def _x(i: int, count: int) -> float:
    if count <= 1:
        return PAD_LEFT + PLOT_W / 2
    return PAD_LEFT + (PLOT_W * i / (count - 1))


def _y(value: float, y_max: float) -> float:
    ratio = 0 if y_max == 0 else max(0.0, min(1.0, value / y_max))
    return PAD_TOP + PLOT_H * (1 - ratio)


def _gridlines(y_max: float, y_suffix: str, steps: int = 4) -> str:
    lines = []
    for step in range(steps + 1):
        value = y_max * step / steps
        y = _y(value, y_max)
        lines.append(f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        lines.append(f'<text x="{PAD_LEFT - 6}" y="{y + 3:.1f}" font-size="9" fill="{MUTED_TEXT}" text-anchor="end">{round(value)}{y_suffix}</text>')
    return "".join(lines)


def _x_labels(labels: list[str], count: int, every: int = 2) -> str:
    out = []
    for i, label in enumerate(labels):
        if i % every != 0 and i != count - 1:
            continue
        x = _x(i, count)
        anchor, label_x = ("end", x - 2) if i == count - 1 else ("middle", x)
        out.append(f'<text x="{label_x:.1f}" y="{HEIGHT - 6}" font-size="9" fill="{MUTED_TEXT}" text-anchor="{anchor}">{escape(label)}</text>')
    return "".join(out)


def line_chart_svg(weeks: list[dict[str, Any]], key: str, color: str, unit: str = "%", y_max: float = 100) -> str:
    count = len(weeks)
    points = [(i, w[key]) for i, w in enumerate(weeks) if w[key] is not None]
    path_parts = []
    for i, value in points:
        cmd = "M" if not path_parts else "L"
        path_parts.append(f"{cmd}{_x(i, count):.1f},{_y(value, y_max):.1f}")
    path = " ".join(path_parts)

    dots = []
    for i, value in points:
        x, y = _x(i, count), _y(value, y_max)
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}">'
            f"<title>{escape(weeks[i]['label'])}: {value}{unit}</title></circle>"
        )
    if points:
        last_i, last_value = points[-1]
        lx, ly = _x(last_i, count), _y(last_value, y_max)
        anchor, label_x = ("end", lx - 6) if last_i == count - 1 else ("middle", lx)
        dots.append(f'<text x="{label_x:.1f}" y="{ly - 8:.1f}" font-size="10" font-weight="700" fill="{color}" text-anchor="{anchor}">{last_value}{unit}</text>')

    svg = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Trend chart" class="chart-svg">',
        _gridlines(y_max, unit),
        f'<line x1="{PAD_LEFT}" y1="{HEIGHT - PAD_BOTTOM}" x2="{WIDTH - PAD_RIGHT}" y2="{HEIGHT - PAD_BOTTOM}" stroke="{AXIS_COLOR}" stroke-width="1"/>',
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' if path else "",
        "".join(dots),
        _x_labels([w["label"] for w in weeks], count),
        "</svg>",
    ]
    return "".join(svg)


def bar_chart_svg(weeks: list[dict[str, Any]], key: str, color: str) -> str:
    count = len(weeks)
    values = [w[key] for w in weeks]
    y_max = max(4, max(values) if values else 0)
    band = PLOT_W / count
    bar_w = band * 0.5

    bars = []
    for i, value in enumerate(values):
        x = PAD_LEFT + band * i + (band - bar_w) / 2
        y = _y(value, y_max)
        h = (HEIGHT - PAD_BOTTOM) - y
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(0.0, h):.1f}" rx="2" fill="{color}">'
            f"<title>{escape(weeks[i]['label'])}: {value}</title></rect>"
        )
        if value:
            bars.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" font-size="9" font-weight="700" fill="{color}" text-anchor="middle">{value}</text>')

    svg = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Bar chart" class="chart-svg">',
        _gridlines(y_max, "", steps=4),
        f'<line x1="{PAD_LEFT}" y1="{HEIGHT - PAD_BOTTOM}" x2="{WIDTH - PAD_RIGHT}" y2="{HEIGHT - PAD_BOTTOM}" stroke="{AXIS_COLOR}" stroke-width="1"/>',
        "".join(bars),
        _x_labels([w["label"] for w in weeks], count),
        "</svg>",
    ]
    return "".join(svg)


def grouped_bar_chart_svg(weeks: list[dict[str, Any]], series: list[tuple[str, str, str]]) -> str:
    """series: list of (data_key, color, legend_label)."""
    count = len(weeks)
    all_values = [w[key] for w in weeks for key, _, _ in series]
    y_max = max(4, max(all_values) if all_values else 0)
    band = PLOT_W / count
    group_w = band * 0.7
    bar_w = group_w / len(series)

    bars = []
    for i, week in enumerate(weeks):
        group_x = PAD_LEFT + band * i + (band - group_w) / 2
        for s_index, (key, color, legend_label) in enumerate(series):
            value = week[key]
            x = group_x + bar_w * s_index
            y = _y(value, y_max)
            h = (HEIGHT - PAD_BOTTOM) - y
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1.0, bar_w - 1.5):.1f}" height="{max(0.0, h):.1f}" rx="1.5" fill="{color}">'
                f"<title>{escape(week['label'])} · {escape(legend_label)}: {value}</title></rect>"
            )

    svg = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Grouped bar chart" class="chart-svg">',
        _gridlines(y_max, "", steps=4),
        f'<line x1="{PAD_LEFT}" y1="{HEIGHT - PAD_BOTTOM}" x2="{WIDTH - PAD_RIGHT}" y2="{HEIGHT - PAD_BOTTOM}" stroke="{AXIS_COLOR}" stroke-width="1"/>',
        "".join(bars),
        _x_labels([w["label"] for w in weeks], count),
        "</svg>",
    ]
    return "".join(svg)
