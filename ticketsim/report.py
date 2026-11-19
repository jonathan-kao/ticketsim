"""A self-contained HTML report of one run.

No JavaScript, no external assets: the charts are plain SVG, so the file can
be opened from disk, mailed, or archived next to the JSON result.
"""
from __future__ import annotations

import html
from typing import Any, Dict, List, Sequence, Tuple

from .engine import RunResult

WIDTH = 960
SESSION_HEIGHT = 130
MARGIN_LEFT = 64
MARGIN_RIGHT = 16


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _scale_x(t: float, horizon: float, width: int) -> float:
    if horizon <= 0:
        return MARGIN_LEFT
    return MARGIN_LEFT + (width - MARGIN_LEFT - MARGIN_RIGHT) * (t / horizon)


def _scale_y(value: float, maximum: float, top: float, height: int) -> float:
    if maximum <= 0:
        return top + height
    return top + height - height * (value / maximum)


def _downsample(points: Sequence[Tuple[float, float]], limit: int) -> List[Tuple[float, float]]:
    """Keep the shape of a long series readable (and the file small)."""
    if len(points) <= limit:
        return list(points)
    step = len(points) / limit
    kept = [points[int(i * step)] for i in range(limit)]
    kept[-1] = points[-1]
    return kept


def _session_svg(key: str, session: Dict[str, Any], history: Sequence[Tuple[float, str, int]],
                 trace: Sequence[Dict[str, Any]], horizon: float, top: float) -> str:
    seats = max(1, session.get("seats") or max((v for _t, k, v in history if k == key), default=1))
    points = [(0.0, float(seats))] + [(t, remaining) for t, name, remaining in history
                                      if name == key]
    points = _downsample(points, 400)
    # a step line: the stock only changes at the recorded moments
    steps: List[str] = []
    previous = points[0][1]
    for t, value in points:
        x = _scale_x(t, horizon, WIDTH)
        steps.append(f"{x:.1f},{_scale_y(previous, seats, top, SESSION_HEIGHT):.1f}")
        steps.append(f"{x:.1f},{_scale_y(value, seats, top, SESSION_HEIGHT):.1f}")
        previous = value
    end = _scale_x(horizon, horizon, WIDTH)
    steps.append(f"{end:.1f},{_scale_y(previous, seats, top, SESSION_HEIGHT):.1f}")
    baseline = _scale_y(0, seats, top, SESSION_HEIGHT)
    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {SESSION_HEIGHT + 34}" '
        f'preserveAspectRatio="xMidYMid meet">',
        f'<rect x="{MARGIN_LEFT}" y="{top}" width="{WIDTH - MARGIN_LEFT - MARGIN_RIGHT}" '
        f'height="{SESSION_HEIGHT}" class="frame"/>',
        f'<polygon points="{MARGIN_LEFT:.1f},{baseline:.1f} {" ".join(steps)} '
        f'{end:.1f},{baseline:.1f}" class="stock"/>',
    ]
    sold_out = session.get("sold_out_at")
    if sold_out is not None:
        x = _scale_x(sold_out, horizon, WIDTH)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + SESSION_HEIGHT}" '
                     f'class="soldout"/>')
        parts.append(f'<text x="{x + 4:.1f}" y="{top + 12}" class="note">sold out '
                     f'{sold_out:.1f}s</text>')
    for event in trace:
        if event.get("kind") != "cleared" or event.get("session") != key:
            continue
        x = _scale_x(event["t"], horizon, WIDTH)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + SESSION_HEIGHT}" '
                     f'class="batch"/>')
        parts.append(f'<text x="{x + 4:.1f}" y="{top + 26}" class="note">+{event["qty"]} '
                     f'returned</text>')
    parts.append(f'<text x="8" y="{top + 14}" class="label">{_esc(key)}</text>')
    parts.append(f'<text x="8" y="{top + 30}" class="label small">'
                 f'{_esc(session.get("name", ""))}</text>')
    parts.append(f'<text x="{MARGIN_LEFT}" y="{top + SESSION_HEIGHT + 16}" class="note">'
                 f'{seats} seats · held {session.get("held", 0)} · on sale at the end '
                 f'{session.get("remaining", 0)} · {session.get("buyer_attempts", 0)} requests, '
                 f'{session.get("buyer_failures", 0)} refused</text>')
    parts.append("</svg>")
    return "".join(parts)


def _cards(summary: Dict[str, Any]) -> str:
    wanted = (("sell_through", "sell-through"), ("first_sold_out_at", "first sell-out (s)"),
              ("tickets_sold", "tickets granted"), ("stranded_stock", "unsold at the end"),
              ("refunded", "cancelled (back at once)"), ("cleared", "unpaid (back in batches)"),
              ("buyer_attempts", "requests"), ("buyer_failure_rate", "refused share"),
              ("buyer_stale_failures", "refused: stale view"),
              ("traffic_total", "platform requests"), ("requests_per_ticket", "requests per ticket"))
    cells = "".join(
        f'<div class="card"><div class="value">{_esc(summary.get(key, "-"))}</div>'
        f'<div class="caption">{_esc(label)}</div></div>'
        for key, label in wanted)
    return f'<div class="cards">{cells}</div>'


def _traffic_table(summary: Dict[str, Any]) -> str:
    stages = summary.get("traffic", {}).get("stages", {})
    rows = "".join(f"<tr><td>{_esc(stage)}</td><td>{value:,}</td></tr>"
                   for stage, value in stages.items())
    return ("<h2>Platform load (requests by stage)</h2><table><tr><th>stage</th>"
            "<th>requests</th></tr>" + rows + "</table>")


def _constants(result: RunResult) -> str:
    rows = "".join(f"<tr><td>{_esc(name)}</td><td>{_esc(value)}</td><td>{_esc(source)}</td></tr>"
                   for name, value, source in result.config.constants_table())
    return ("<h2>Parameters</h2><table><tr><th>parameter</th><th>value</th><th>source</th></tr>"
            + rows + "</table>")


def render(result: RunResult) -> str:
    """Render one run as a standalone HTML page."""
    config = result.config
    horizon = config.scenario.horizon
    body = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>ticketsim · {_esc(config.summary())}</title>",
        "<style>",
        "body{font:14px/1.5 -apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;",
        "margin:0;padding:24px;background:#fafafa;color:#222}",
        "h1{font-size:18px}h2{font-size:15px;margin-top:28px}",
        ".cards{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}",
        ".card{background:#fff;border:1px solid #e3e3e3;border-radius:6px;padding:10px 14px;min-width:96px}",
        ".value{font-size:20px;font-weight:600}.caption{color:#777;font-size:12px}",
        ".chart{width:100%;height:auto;background:#fff;border:1px solid #e3e3e3;border-radius:6px;",
        "margin-bottom:10px}",
        ".frame{fill:#fcfcfc;stroke:#eee}.stock{fill:#9ecae1;stroke:#3182bd;stroke-width:1}",
        ".soldout{stroke:#d62728;stroke-width:1;stroke-dasharray:3 3}",
        ".batch{stroke:#2ca02c;stroke-width:1;stroke-dasharray:2 3}",
        ".label{fill:#333}.label.small{font-size:10px;fill:#888}",
        ".note{font-size:10px;fill:#888}",
        "table{border-collapse:collapse;background:#fff;width:100%}",
        "th,td{border:1px solid #eee;padding:4px 8px;text-align:left;font-size:12px}",
        "</style></head><body>",
        f"<h1>ticketsim - {_esc(config.summary())}</h1>",
        _cards(result.summary),
        "<h2>Tickets on sale, per session</h2>",
    ]
    for key, session in result.sessions.items():
        body.append(_session_svg(key, session, result.pool_history, result.trace, horizon, 24))
    body.append(_traffic_table(result.summary))
    body.append(_constants(result))
    body.append("</body></html>")
    return "\n".join(body)


def write(result: RunResult, path: str) -> str:
    """Write the HTML report to ``path`` and return the path."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render(result))
    return path
