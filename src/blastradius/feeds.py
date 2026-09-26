"""Pull-based subscriptions: Atom (feed readers, Slack/Teams RSS apps) and iCalendar (put the
maintenance windows that affect *your* apps straight into Google Calendar / Outlook)."""

from __future__ import annotations

import datetime as dt
from xml.sax.saxutils import escape

from .config import Config
from .notify.render import app_explanation, fmt_window
from .parsing.ical import escape_text, fold
from .util import iso, loads, parse_iso, severity_name, utcnow


def _summary_lines(cfg: Config, notice: dict, blast: dict | None) -> list[str]:
    blast = blast or {}
    lines = [
        f"Severity: {severity_name(notice['severity']) if blast.get('mapped') else 'UNMAPPED'}",
        f"Window: {fmt_window(notice.get('start_at'), notice.get('end_at'), cfg)}",
        f"Provider: {notice.get('provider') or '—'} {notice.get('maintenance_id') or ''}".rstrip(),
        f"Status: {notice.get('status')} ({notice.get('phase')})",
    ]
    circuits = ", ".join(c["id"] for c in notice.get("circuits", []))
    if circuits:
        lines.append(f"Circuits: {circuits}")
    for app in blast.get("apps", [])[:15]:
        lines.append(f"- {app['name']}: {severity_name(app['severity'])} — {app_explanation(blast, app)}")
    return lines


def atom(cfg: Config, title: str, feed_path: str, items: list[tuple[dict, dict | None]]) -> str:
    base = cfg.base_url.rstrip("/")
    updated = max((n["updated_at"] for n, _ in items), default=iso(utcnow()))
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"<title>{escape(title)}</title>",
        f'<link rel="self" href="{escape(base + feed_path)}"/>',
        f'<link rel="alternate" href="{escape(base + "/")}"/>',
        f"<id>{escape(base + feed_path)}</id>",
        f"<updated>{updated}</updated>",
        "<generator>blastradius</generator>",
    ]
    for notice, blast in items:
        url = f"{base}/notices/{notice['id']}"
        sev = severity_name(notice["severity"]) if (blast or {}).get("mapped") else "UNMAPPED"
        body = "\n".join(_summary_lines(cfg, notice, blast))
        out += [
            "<entry>",
            f"<title>[{sev}] {escape(notice.get('provider') or '')} — {escape(notice.get('title') or '')}</title>",
            f'<link href="{escape(url)}"/>',
            f"<id>{escape(url)}#v{notice['version']}</id>",
            f"<updated>{notice['updated_at']}</updated>",
            f"<published>{notice['first_seen']}</published>",
            f'<category term="{sev}"/>',
            *[f'<category term="{escape(a["name"])}"/>' for a in (blast or {}).get("apps", [])[:20]],
            f'<content type="text">{escape(body)}</content>',
            "</entry>",
        ]
    out.append("</feed>")
    return "\n".join(out) + "\n"


def ical(cfg: Config, name: str, items: list[tuple[dict, dict | None]]) -> str:
    base = cfg.base_url.rstrip("/")
    host = base.split("://", 1)[-1].split("/")[0] or "blastradius"
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//blastradius//maintenance feed//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{escape_text(name)}", "X-PUBLISHED-TTL:PT15M",
    ]
    for notice, blast in items:
        start = parse_iso(notice.get("start_at"))
        if start is None:
            continue
        end = parse_iso(notice.get("end_at")) or start + dt.timedelta(hours=cfg.correlation.open_outage_hours)
        sev = severity_name(notice["severity"]) if (blast or {}).get("mapped") else "UNMAPPED"
        apps = ", ".join(a["name"] for a in (blast or {}).get("apps", [])[:5])
        summary = f"[{sev}] {notice.get('provider') or ''} maintenance" + (f" — {apps}" if apps else "")
        description = "\n".join(_summary_lines(cfg, notice, blast)) + f"\n\n{base}/notices/{notice['id']}"
        status = "CANCELLED" if notice.get("status") == "CANCELLED" else (
            "TENTATIVE" if notice.get("status") == "TENTATIVE" else "CONFIRMED")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{notice['key'].replace(' ', '')}@{host}",
            f"SEQUENCE:{notice['version']}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{escape_text(summary)}",
            f"DESCRIPTION:{escape_text(description)}",
            f"URL:{base}/notices/{notice['id']}",
            f"STATUS:{status}",
            f"CATEGORIES:{escape_text(sev)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def rows_to_items(rows, now: dt.datetime | None = None) -> list[tuple[dict, dict | None]]:
    from .notices import to_dict

    return [(to_dict(r, now), loads(r["blast"], None)) for r in rows]
