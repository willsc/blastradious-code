"""Human-facing summaries of notices and collisions for every channel (text, HTML, Slack, Teams, JSON)."""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..topology.model import TYPE_INFO
from ..util import SEVERITY_LABELS, human_duration, parse_iso, severity_name

EVENT_TITLES = {
    "new": "New notice", "updated": "Updated", "rescheduled": "Rescheduled", "cancelled": "Cancelled",
    "completed": "Completed", "reminder": "Reminder", "started": "Started", "impact_changed": "Impact changed",
    "collision": "Maintenance collision",
}
SEVERITY_EMOJI = {0: ":white_circle:", 1: ":large_blue_circle:", 2: ":large_orange_circle:", 3: ":red_circle:"}
SEVERITY_COLOUR = {0: "#6b7280", 1: "#2563eb", 2: "#d97706", 3: "#dc2626"}
DECISION_TYPES = ("dxlag", "dxgw", "vgw", "vpn", "tgwrtb", "rtb")


@dataclass
class Rendered:
    subject: str
    text: str
    html: str
    slack: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Rendered:
        return cls(**data)


def _tz(cfg: Config):
    try:
        return ZoneInfo(cfg.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def fmt_window(start: str | None, end: str | None, cfg: Config) -> str:
    s, e = parse_iso(start), parse_iso(end)
    if s is None:
        return "window not stated"
    utc = dt.timezone.utc
    text = s.astimezone(utc).strftime("%a %d %b %Y %H:%M")
    if e is not None:
        same_day = e.astimezone(utc).date() == s.astimezone(utc).date()
        text += "–" + e.astimezone(utc).strftime("%H:%M" if same_day else "%a %d %b %H:%M")
    text += " UTC"
    tz = _tz(cfg)
    if tz is not utc and getattr(tz, "key", "UTC") != "UTC":
        local = s.astimezone(tz).strftime("%H:%M")
        if e is not None:
            local += "–" + e.astimezone(tz).strftime("%H:%M")
        text += f" ({local} {s.astimezone(tz).tzname()})"
    if e is not None:
        text += f", {human_duration((e - s).total_seconds())}"
    else:
        text += ", open-ended"
    return text


def app_explanation(blast: dict, app: dict) -> str:
    """Short 'why' for an application: the hop on its impact chain that decided the severity —
    a redundancy decision (LAG quorum, gateway/VPN path count) if there is one, else the routing
    hop that scoped it, else the anchor."""
    nodes = {n["key"]: n for n in blast.get("nodes", [])}
    chain = [nodes[k] for k in app.get("chain", []) if k in nodes]

    def text(node: dict) -> str:
        return f"{node['name'] or node['id']}: {node['reasons'][0]}"

    for node in chain:
        if node["type"] in ("dxlag", "dxgw", "vgw", "vpn") and node["reasons"] and node["severity"] >= app["severity"]:
            return text(node)
    for node in chain:
        if node["type"] in ("dxlag", "dxgw", "vgw", "vpn") and node["reasons"]:
            return text(node)
    for node in chain:
        if node["type"] in ("tgwrtb", "rtb") and node["reasons"]:
            return text(node)
    anchors = blast.get("anchors", [])
    return f"via {anchors[0]['label']}" if anchors else ""


def counts_text(blast: dict) -> str:
    counts = blast.get("counts", {})
    order = ["circuit", "dxcon", "dxlag", "dxvif", "dxgw", "vgw", "vpn", "tgwatt", "subnet", "instance", "eni"]
    parts = []
    for ntype in order:
        n = counts.get(ntype)
        if n:
            label = TYPE_INFO.get(ntype, (ntype, False))[0]
            parts.append(f"{n} {label}{'s' if n != 1 and not label.endswith('s') else ''}")
    return ", ".join(parts)


def _change_lines(changes: dict | None, cfg: Config) -> list[str]:
    lines = []
    for name, change in (changes or {}).items():
        if name in ("start_at", "end_at"):
            continue
        lines.append(f"{name}: {change.get('from') or '—'} → {change.get('to') or '—'}")
    if changes and ("start_at" in changes or "end_at" in changes):
        old = fmt_window(changes.get("start_at", {}).get("from"), changes.get("end_at", {}).get("from"), cfg) \
            if changes.get("start_at", {}).get("from") else None
        lines.insert(0, f"window moved{' from ' + old if old else ''}")
    return lines


def render_notice(cfg: Config, notice: dict, blast: dict | None, event: str, changes: dict | None = None,
                  note: str = "") -> Rendered:
    blast = blast or {"apps": [], "nodes": [], "anchors": [], "counts": {}, "unmapped": [], "mapped": False}
    sev = notice.get("severity", 0)
    sev_name = severity_name(sev) if blast.get("mapped") else "UNMAPPED"
    url = f"{cfg.base_url.rstrip('/')}/notices/{notice['id']}"
    window = fmt_window(notice.get("start_at"), notice.get("end_at"), cfg)
    apps = blast.get("apps", [])
    app_names = ", ".join(a["name"] for a in apps[:3]) + (f" +{len(apps) - 3}" if len(apps) > 3 else "")
    ref = notice.get("maintenance_id") or ""
    provider = notice.get("provider") or "Unknown provider"
    event_title = EVENT_TITLES.get(event, event)
    subject = f"[{sev_name}] {event_title}: {provider} {ref}".strip()
    subject += f" — {app_names or ('no mapped resources' if not blast.get('mapped') else 'infrastructure only')}"
    subject += f" — {window.split(' UTC')[0]} UTC"

    circuits = ", ".join(c["id"] for c in notice.get("circuits", [])) or "—"
    lines = [
        f"{event_title}: {notice.get('title') or ''}",
        f"Severity: {sev_name}   Provider impact: {notice.get('impact') or 'not stated'}   Status: {notice.get('status')}",
        f"Window: {window}",
        f"Provider: {provider}   Reference: {ref or '—'}   Circuits: {circuits}",
    ]
    if note:
        lines.append(note)
    for change in _change_lines(changes, cfg):
        lines.append(f"Change: {change}")
    if apps:
        lines.append("")
        lines.append("Affected applications:")
        for a in apps:
            why = app_explanation(blast, a)
            lines.append(f"  • {a['name']} — {severity_name(a['severity'])}" + (f" — {why}" if why else ""))
    if blast.get("counts"):
        lines.append(f"AWS/network impact: {counts_text(blast)}")
    if blast.get("anchors"):
        lines.append("Correlated on: " + "; ".join(a["label"] for a in blast["anchors"][:5]))
    if blast.get("unmapped"):
        lines.append("Not in inventory: " + ", ".join(blast["unmapped"][:10]))
    lines += ["", f"Details: {url}"]
    text = "\n".join(lines)

    colour = SEVERITY_COLOUR.get(sev if blast.get("mapped") else 0)
    app_rows = "".join(
        f"<tr><td style='padding:4px 8px'><b>{html.escape(a['name'])}</b></td>"
        f"<td style='padding:4px 8px;color:{SEVERITY_COLOUR[a['severity']]}'>{severity_name(a['severity'])}</td>"
        f"<td style='padding:4px 8px;color:#555'>{html.escape(app_explanation(blast, a))}</td></tr>"
        for a in apps
    )
    html_body = (
        f"<div style='font-family:system-ui,sans-serif;font-size:14px'>"
        f"<div style='border-left:4px solid {colour};padding-left:10px'>"
        f"<div style='font-size:16px;font-weight:600'>{html.escape(event_title)}: {html.escape(notice.get('title') or '')}</div>"
        f"<div><b>{sev_name}</b> · {html.escape(window)}</div>"
        f"<div>{html.escape(provider)} {html.escape(ref)} · circuits {html.escape(circuits)} · status {html.escape(str(notice.get('status')))}</div></div>"
        + "".join(f"<div style='margin-top:6px'>Change: {html.escape(c)}</div>" for c in _change_lines(changes, cfg))
        + (f"<table style='margin-top:10px;border-collapse:collapse'>{app_rows}</table>" if apps else "")
        + (f"<p style='color:#555'>{html.escape(counts_text(blast))}</p>" if blast.get("counts") else "")
        + (f"<p style='color:#b45309'>Not in inventory: {html.escape(', '.join(blast['unmapped'][:10]))}</p>"
           if blast.get("unmapped") else "")
        + f"<p><a href='{html.escape(url)}'>Open blast radius</a></p></div>"
    )

    app_lines = "\n".join(
        f"{SEVERITY_EMOJI[a['severity']]} *{a['name']}* — {severity_name(a['severity'])}"
        + (f"\n      _{app_explanation(blast, a)[:280]}_" if app_explanation(blast, a) else "")
        for a in apps[:10]
    ) or ("_No mapped AWS resources — check the inventory._" if not blast.get("mapped") else "_Infrastructure only._")
    change_text = "\n".join(f"• {c}" for c in _change_lines(changes, cfg))
    slack = {
        "text": subject,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{event_title}: {provider} {ref}"[:150]}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{SEVERITY_EMOJI.get(sev if blast.get('mapped') else 0)} {sev_name}"},
                {"type": "mrkdwn", "text": f"*Window*\n{window}"},
                {"type": "mrkdwn", "text": f"*Provider impact*\n{notice.get('impact') or 'not stated'}"},
                {"type": "mrkdwn", "text": f"*Circuits*\n{circuits[:200]}"},
            ]},
            *([{"type": "section", "text": {"type": "mrkdwn", "text": change_text[:2900]}}] if change_text else []),
            {"type": "section", "text": {"type": "mrkdwn", "text": app_lines[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": (counts_text(blast) or "—")[:300]}]},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Open blast radius"}, "url": url}]},
        ],
    }
    teams = _teams_card(f"{event_title}: {provider} {ref}", sev_name, [
        ("Severity", sev_name), ("Window", window), ("Provider impact", notice.get("impact") or "not stated"),
        ("Circuits", circuits), ("Status", str(notice.get("status"))),
    ], [f"{a['name']} — {severity_name(a['severity'])}: {app_explanation(blast, a)}" for a in apps[:10]]
        + _change_lines(changes, cfg), url)
    data = {
        "event": event,
        "url": url,
        "notice": {k: notice.get(k) for k in ("id", "key", "provider", "maintenance_id", "title", "status", "impact",
                                               "start_at", "end_at", "phase", "severity", "message_type", "kind",
                                               "circuits", "version")},
        "severity": sev_name,
        "applications": [{"name": a["name"], "severity": severity_name(a["severity"]), "owners": a.get("owners", []),
                          "why": app_explanation(blast, a)} for a in apps],
        "counts": blast.get("counts", {}),
        "anchors": [a["label"] for a in blast.get("anchors", [])],
        "unmapped": blast.get("unmapped", []),
        "changes": changes or {},
    }
    return Rendered(subject=subject[:250], text=text, html=html_body, slack=slack, teams=teams, data=data)


def render_collision(cfg: Config, collision: dict) -> Rendered:
    sev = collision["severity"]
    url = f"{cfg.base_url.rstrip('/')}/collisions#{collision['id']}"
    window = fmt_window(collision["start"], collision["end"], cfg)
    notices_text = [f"#{n['id']} {n['provider'] or ''}: {n['title']} ({fmt_window(n['start'], n['end'], cfg)})"
                    for n in collision["notices"]]
    apps = collision.get("apps", [])
    subject = (f"[{severity_name(sev)}] Maintenance collision — {len(collision['notices'])} overlapping notices"
               + (f" — {', '.join(a['name'] for a in apps[:3])}" if apps else "") + f" — {window.split(' UTC')[0]} UTC")
    esc = [f"{e['label']}: {severity_name(e['from'])} → {severity_name(e['to'])}" for e in collision["escalations"]
           if e["type"] in ("app", "dxgw", "vgw", "dxlag", "tgwrtb")][:12]
    text = "\n".join([
        "Overlapping maintenance windows combine into a worse outage than either alone.",
        f"Overlap: {window}",
        "Notices:", *[f"  • {t}" for t in notices_text],
        "Escalations:", *[f"  • {e}" for e in esc],
        "", f"Details: {url}",
    ])
    html_body = (
        f"<div style='font-family:system-ui,sans-serif;font-size:14px;border-left:4px solid {SEVERITY_COLOUR[sev]};padding-left:10px'>"
        f"<div style='font-size:16px;font-weight:600'>Maintenance collision — {severity_name(sev)}</div>"
        f"<div>Overlap: {html.escape(window)}</div><ul>"
        + "".join(f"<li>{html.escape(t)}</li>" for t in notices_text) + "</ul><ul>"
        + "".join(f"<li>{html.escape(e)}</li>" for e in esc) + f"</ul><a href='{html.escape(url)}'>Details</a></div>"
    )
    slack = {"text": subject, "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": f"Maintenance collision — {severity_name(sev)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Overlap:* {window}\n" + "\n".join(f"• {t}" for t in notices_text)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": ("\n".join(f"• {e}" for e in esc) or "—")[:2900]}},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open"}, "url": url}]},
    ]}
    teams = _teams_card(f"Maintenance collision — {severity_name(sev)}", severity_name(sev),
                        [("Overlap", window)], notices_text + esc, url)
    data = {"event": "collision", "url": url, "collision": {k: collision[k] for k in
                                                            ("id", "notice_ids", "start", "end", "severity", "apps", "summary")}}
    return Rendered(subject=subject[:250], text=text, html=html_body, slack=slack, teams=teams, data=data)


def _teams_card(title: str, severity: str, facts: list[tuple[str, str]], bullets: list[str], url: str) -> dict:
    colour = {"OUTAGE": "Attention", "DEGRADED": "Warning", "AT_RISK": "Accent"}.get(severity, "Default")
    body = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title, "wrap": True, "color": colour},
        {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]},
    ]
    body += [{"type": "TextBlock", "text": f"• {b}", "wrap": True, "spacing": "Small"} for b in bullets if b]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4", "body": body,
                "actions": [{"type": "Action.OpenUrl", "title": "Open blast radius", "url": url}],
            },
        }],
    }


def severity_label(sev: int) -> str:
    return SEVERITY_LABELS.get(sev, "")
