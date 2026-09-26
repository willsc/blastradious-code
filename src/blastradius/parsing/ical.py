"""Minimal RFC 5545 reader — enough for BCOP maintenance notifications and calendar invites.

Kept dependency-free on purpose: the optional circuit-maintenance-parser pins an old icalendar
major version, and this subset (line unfolding, parameters, TEXT escapes, DATE/DATE-TIME with
TZID) is small enough to own.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..util import UTC

# Common Windows zone names emitted by Outlook/Exchange-generated invites.
WINDOWS_ZONES = {
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Kiev",
    "gtb standard time": "Europe/Bucharest",
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "utc": "UTC",
    "coordinated universal time": "UTC",
    "india standard time": "Asia/Kolkata",
    "singapore standard time": "Asia/Singapore",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "aus eastern standard time": "Australia/Sydney",
    "south africa standard time": "Africa/Johannesburg",
}


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str

    def text(self) -> str:
        return unescape_text(self.value)


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list[Component] = field(default_factory=list)

    def get(self, name: str) -> Prop | None:
        name = name.upper()
        return next((p for p in self.props if p.name == name), None)

    def get_all(self, name: str) -> list[Prop]:
        name = name.upper()
        return [p for p in self.props if p.name == name]

    def text(self, name: str) -> str | None:
        prop = self.get(name)
        return prop.text() if prop else None

    def walk(self, name: str | None = None):
        if name is None or self.name == name.upper():
            yield self
        for child in self.children:
            yield from child.walk(name)


def unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line.strip():
            lines.append(line)
    return lines


def _split_content_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """NAME;P1=V1;P2="quoted:value":VALUE -> (NAME, {P1: V1, P2: ...}, VALUE)."""
    in_quotes = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon <= 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts: list[str] = []
    buf, in_quotes = "", False
    for ch in head:
        if ch == '"':
            in_quotes = not in_quotes
            buf += ch
        elif ch == ";" and not in_quotes:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for param in parts[1:]:
        if "=" in param:
            key, val = param.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def parse_ical(text: str) -> list[Component]:
    roots: list[Component] = []
    stack: list[Component] = []
    for line in unfold(text):
        parsed = _split_content_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            comp = Component(value.strip().upper())
            if stack:
                stack[-1].children.append(comp)
            else:
                roots.append(comp)
            stack.append(comp)
        elif name == "END":
            if stack:
                stack.pop()
        elif stack:
            stack[-1].props.append(Prop(name, params, value))
    return roots


def unescape_text(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1)."""
    encoded = line.encode()
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    current = b""
    limit = 75
    for ch in line:
        b = ch.encode()
        if len(current) + len(b) > limit:
            chunks.append(current.decode())
            current = b
            limit = 74  # continuation lines start with a space
        else:
            current += b
    chunks.append(current.decode())
    return "\r\n ".join(chunks)


_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$")


def resolve_tz(tzid: str | None, calendar: Component | None = None, default: dt.tzinfo = UTC) -> dt.tzinfo:
    if not tzid:
        return default
    tzid = tzid.strip().strip('"')
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    mapped = WINDOWS_ZONES.get(tzid.lower())
    if mapped:
        return ZoneInfo(mapped)
    # Fall back to a VTIMEZONE definition's STANDARD offset (ignores DST rules).
    if calendar is not None:
        for vtz in calendar.walk("VTIMEZONE"):
            if (vtz.text("TZID") or "").strip() == tzid:
                for sub in vtz.children:
                    offset = sub.text("TZOFFSETTO")
                    if offset and re.match(r"^[+-]\d{4}$", offset.strip()):
                        sign = 1 if offset[0] == "+" else -1
                        hours, minutes = int(offset[1:3]), int(offset[3:5])
                        return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    return default


def parse_datetime(prop: Prop | None, calendar: Component | None = None, default_tz: dt.tzinfo = UTC) -> dt.datetime | None:
    if prop is None:
        return None
    value = prop.value.strip()
    match = _DT_RE.match(value)
    if not match:
        return None
    year, month, day, hh, mm, ss, zulu = match.groups()
    if hh is None:  # VALUE=DATE
        return dt.datetime(int(year), int(month), int(day), tzinfo=resolve_tz(prop.params.get("TZID"), calendar, default_tz))
    naive = dt.datetime(int(year), int(month), int(day), int(hh), int(mm), int(ss or 0))
    if zulu:
        return naive.replace(tzinfo=UTC)
    return naive.replace(tzinfo=resolve_tz(prop.params.get("TZID"), calendar, default_tz))
