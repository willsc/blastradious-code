"""BCOP maintenance-notification iCalendar (https://github.com/jda/maintnote-std).

X-MAINTNOTE-PROVIDER / -ACCOUNT / -MAINTENANCE-ID / -OBJECT-ID (1..n) / -IMPACT / -STATUS,
plus DTSTART/DTEND/SEQUENCE/UID. Plain calendar invites without X-MAINTNOTE properties are
returned as partial results (window + summary) for the generic parser to complete.
"""

from __future__ import annotations

from ..config import Config, ProviderHint
from ..mailparse import MailMessage
from . import ical
from .model import CircuitRef, ParsedNotice
from .timewin import zone

_IMPACT_ALIASES = {"NONE": "NO-IMPACT", "NO_IMPACT": "NO-IMPACT", "REDUCED_REDUNDANCY": "REDUCED-REDUNDANCY"}
_STATUS_ALIASES = {"CANCELED": "CANCELLED", "RESCHEDULED": "RE-SCHEDULED", "IN-PROGRESS": "IN-PROCESS",
                   "IN_PROCESS": "IN-PROCESS", "COMPLETE": "COMPLETED"}


def _norm_impact(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    return _IMPACT_ALIASES.get(value, value)


def _norm_status(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    return _STATUS_ALIASES.get(value, value)


def parse(mail: MailMessage, hint: ProviderHint | None, cfg: Config) -> list[ParsedNotice]:
    results: list[ParsedNotice] = []
    default_tz = zone(hint.timezone) if hint else zone("UTC")
    for payload in mail.calendars:
        for calendar in ical.parse_ical(payload):
            method = (calendar.text("METHOD") or "").upper()
            for event in calendar.walk("VEVENT"):
                results.append(_event(event, calendar, method, default_tz))
    return [r for r in results if r is not None]


def _event(event: ical.Component, calendar: ical.Component, method: str, default_tz) -> ParsedNotice:
    has_bcop = any(p.name.startswith("X-MAINTNOTE-") for p in event.props)
    start = ical.parse_datetime(event.get("DTSTART"), calendar, default_tz)
    end = ical.parse_datetime(event.get("DTEND"), calendar, default_tz)
    notice = ParsedNotice(
        parser="bcop" if has_bcop else "ical",
        provider=event.text("X-MAINTNOTE-PROVIDER"),
        account=event.text("X-MAINTNOTE-ACCOUNT"),
        maintenance_id=event.text("X-MAINTNOTE-MAINTENANCE-ID"),
        impact=_norm_impact(event.text("X-MAINTNOTE-IMPACT")),
        status=_norm_status(event.text("X-MAINTNOTE-STATUS")),
        start=start,
        end=end,
        summary=event.text("SUMMARY"),
        uid=event.text("UID"),
        stamp=ical.parse_datetime(event.get("DTSTAMP"), calendar, default_tz),
        organizer=(event.text("ORGANIZER") or "").replace("mailto:", "").replace("MAILTO:", "") or None,
        confidence="high" if has_bcop else "medium",
        structured=has_bcop,
    )
    seq = event.text("SEQUENCE")
    if seq and seq.strip().lstrip("-").isdigit():
        notice.sequence = int(seq.strip())
    for prop in event.get_all("X-MAINTNOTE-OBJECT-ID"):
        circuit_id = prop.text().strip()
        if circuit_id:
            # Some providers attach a per-object impact as a parameter.
            notice.circuits.append(CircuitRef(circuit_id, _norm_impact(prop.params.get("X-MAINTNOTE-IMPACT"))))
    if method == "CANCEL" or (event.text("STATUS") or "").upper() == "CANCELLED":
        notice.status = "CANCELLED"
    if start:
        notice.windows = [{"start": start, "end": end, "label": "primary", "backup": False}]
    if has_bcop and not notice.circuits:
        notice.notes.append("BCOP event without X-MAINTNOTE-OBJECT-ID")
    return notice
