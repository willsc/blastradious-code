"""Server-side SVG timeline of maintenance windows (with collision bands)."""

from __future__ import annotations

import datetime as dt
from html import escape

from ..util import parse_iso, severity_name, utcnow

ROW_H, LABEL_W, WIDTH, TOP = 24, 250, 1180, 30


def render(items: list[dict], collisions: list[dict], *, days: int = 14, now: dt.datetime | None = None,
           tz: dt.tzinfo = dt.timezone.utc, open_hours: int = 72) -> str:
    """items: notice dicts (to_dict) incl. 'mapped'. Returns an SVG string ('' when nothing to show)."""
    now = now or utcnow()
    start = (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=days + 1)
    span = (end - start).total_seconds()
    plot_w = WIDTH - LABEL_W - 10

    def x(t: dt.datetime) -> float:
        return LABEL_W + max(0.0, min(1.0, (t - start).total_seconds() / span)) * plot_w

    rows = []
    for n in items:
        s, e = parse_iso(n.get("start_at")), parse_iso(n.get("end_at"))
        if s is None or n.get("status") == "CANCELLED":
            continue
        e = e or s + dt.timedelta(hours=open_hours)
        if e < start or s > end:
            continue
        rows.append((s, e, n))
    if not rows:
        return ""
    rows.sort(key=lambda r: r[0])
    height = TOP + len(rows) * ROW_H + 10
    out = [f'<svg class="timeline" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
           f'width="100%" preserveAspectRatio="xMinYMin meet" role="img" aria-label="Maintenance timeline">']
    day = start
    while day <= end:
        px = x(day)
        local = day.astimezone(tz)
        weekend = local.weekday() >= 5
        if day < end:
            nxt = x(min(day + dt.timedelta(days=1), end))
            if weekend:
                out.append(f'<rect class="tl-weekend" x="{px:.1f}" y="{TOP - 6}" width="{nxt - px:.1f}" height="{height - TOP}"/>')
            out.append(f'<text class="tl-day" x="{px + 3:.1f}" y="14">{local.strftime("%a %d")}</text>')
        out.append(f'<line class="tl-grid" x1="{px:.1f}" y1="{TOP - 8}" x2="{px:.1f}" y2="{height}"/>')
        day += dt.timedelta(days=1)
    for c in collisions:
        cs, ce = parse_iso(c["start"]), parse_iso(c["end"])
        if ce < start or cs > end:
            continue
        x1, x2 = x(cs), x(ce)
        out.append(f'<rect class="tl-collision" x="{x1:.1f}" y="{TOP - 6}" width="{max(3.0, x2 - x1):.1f}" '
                   f'height="{height - TOP}"><title>{escape(c["summary"])}</title></rect>')
    for i, (s, e, n) in enumerate(rows):
        y = TOP + i * ROW_H
        sev = n["severity"] if n.get("mapped") else 0
        ref = n.get("maintenance_id") or ""
        label = f"{n.get('provider') or ''} {ref}" if ref and len(ref) <= 20 else f"{n.get('provider') or ''}: {n.get('title') or ''}"
        label = label.strip()[:36]
        apps = ", ".join(a["name"] for a in n.get("apps", [])[:3])
        tip = f"{label}\n{n.get('title') or ''}\n{severity_name(sev) if n.get('mapped') else 'UNMAPPED'}" + (f" — {apps}" if apps else "")
        x1, x2 = x(s), x(e)
        out.append(f'<a href="/notices/{n["id"]}"><g class="tl-row">'
                   f'<title>{escape(tip)}</title>'
                   f'<text class="tl-label" x="4" y="{y + 15}">{escape(label)}</text>'
                   f'<rect class="tl-bar sev-{sev}{" unmapped" if not n.get("mapped") else ""}" x="{x1:.1f}" y="{y + 4}" '
                   f'width="{max(4.0, x2 - x1):.1f}" height="{ROW_H - 8}" rx="3"/>'
                   + (f'<text class="tl-apps" x="{min(x2 + 5, WIDTH - 150):.1f}" y="{y + 15}">{escape(apps[:40])}</text>' if apps else "")
                   + "</g></a>")
    nx = x(now)
    out.append(f'<line class="tl-now" x1="{nx:.1f}" y1="{TOP - 10}" x2="{nx:.1f}" y2="{height}"/>')
    out.append(f'<text class="tl-now-label" x="{nx + 3:.1f}" y="{TOP - 12}">now</text>')
    out.append("</svg>")
    return "\n".join(out)
