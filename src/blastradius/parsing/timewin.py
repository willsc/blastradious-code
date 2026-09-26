"""Maintenance-window extraction from free text.

Carriers express windows in dozens of ways ("Start Date/Time: 03-Oct-2026 01:00 GMT",
"Window: 03/10/2026 22:00 - 04/10/2026 04:00 (UTC)", "22:00 - 02:00 BST" next to a separate
"Date:" line, "from 01:00 to 05:00 UTC on 3rd October 2026", ...). Strategy:

1. labelled fields (start/end, window, date + time) — highest confidence;
2. range expressions anywhere in the text;
3. sanity checks (end after start, bounded duration) and backup-window detection.

Timezone abbreviations are resolved to IANA zones ourselves; dateutil's POSIX handling of
"UTC+1" (sign inverted) is deliberately bypassed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as du_parser

from ..util import UTC

TZ_ABBREVIATIONS = {
    "UTC": "UTC", "GMT": "UTC", "UT": "UTC", "Z": "UTC", "ZULU": "UTC",
    "BST": "Europe/London", "WET": "Europe/Lisbon", "WEST": "Europe/Lisbon",
    "CET": "Europe/Paris", "CEST": "Europe/Paris", "MET": "Europe/Paris", "MEST": "Europe/Paris",
    "EET": "Europe/Athens", "EEST": "Europe/Athens", "MSK": "Europe/Moscow",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage", "HST": "Pacific/Honolulu",
    "IST": "Asia/Kolkata", "SGT": "Asia/Singapore", "HKT": "Asia/Hong_Kong", "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "ACST": "Australia/Adelaide", "AWST": "Australia/Perth", "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland", "SAST": "Africa/Johannesburg", "BRT": "America/Sao_Paulo",
    "GST": "Asia/Dubai",
}

MAX_WINDOW = dt.timedelta(days=14)

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_ABBR_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(TZ_ABBREVIATIONS, key=len, reverse=True)) + r")(?![A-Za-z])")
_UTC_WORD_RE = re.compile(r"(?<![A-Za-z])(utc|gmt)(?![A-Za-z])", re.I)
_OFFSET_RE = re.compile(r"(?<![A-Za-z])(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?(?!\d)", re.I)
_IANA_RE = re.compile(r"\b([A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+(?:/[A-Z][A-Za-z_]+)?)\b")
_WEEKDAY_RE = re.compile(r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|nesday|rsday|urday|sday)?\b\.?,?", re.I)
_ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)
_HRS_RE = re.compile(r"\b(\d{1,2})[:.]?(\d{2})\s*(?:hrs|hours|hr|h)\b", re.I)
_DOT_TIME_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2})(?![\d.])")
_YMD_RE = re.compile(r"\b\d{4}[-/.](?:\d{1,2}|" + _MONTHS + r")[-/.]\d{1,2}\b", re.I)
_HAS_DATE_RE = re.compile(
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{8}T\d{4}|" + _MONTHS + r"(?![a-z])",
    re.I,
)
_HAS_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}|\b\d{1,2}\s*[ap]\.?m\b", re.I)
_TIME_ONLY_RE = re.compile(
    r"^\s*(?:(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*([ap])\.?m\.?|(\d{1,2}):(\d{2})(?::(\d{2}))?|(\d{2})(\d{2}))\s*$",
    re.I,
)
_RANGE_SPLIT_RE = re.compile(r"\s+(?:-|to|until|till|through|thru|and)\s+|\s*-\s*(?=\d{1,2}:\d{2})", re.I)
_ON_DATE_RE = re.compile(r"\s+on\s+(?P<date>.+)$", re.I)
_ISO_OFFSET_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})")


@dataclass
class Window:
    start: dt.datetime
    end: dt.datetime | None
    label: str = "primary"
    backup: bool = False
    source: str = ""
    confidence: str = "medium"

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "label": self.label, "backup": self.backup}


# --------------------------------------------------------------------------
# Timezones
# --------------------------------------------------------------------------


def zone(name: str | None, default: dt.tzinfo = UTC) -> dt.tzinfo:
    if not name:
        return default
    upper = name.strip().upper()
    if upper in TZ_ABBREVIATIONS:
        return ZoneInfo(TZ_ABBREVIATIONS[upper]) if TZ_ABBREVIATIONS[upper] != "UTC" else UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return default


def extract_tz(text: str) -> tuple[str, dt.tzinfo | None]:
    """Remove a timezone designation from `text`, returning (rest, tzinfo)."""
    m = _OFFSET_RE.search(text)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        offset = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return _strip(text[: m.start()] + " " + text[m.end():]), dt.timezone(sign * offset)
    m = _IANA_RE.search(text)
    if m:
        try:
            tz = ZoneInfo(m.group(1))
            return _strip(text[: m.start()] + " " + text[m.end():]), tz
        except (ZoneInfoNotFoundError, ValueError):
            pass
    m = _UTC_WORD_RE.search(text)
    if m:
        return _strip(text[: m.start()] + " " + text[m.end():]), UTC
    m = _ABBR_RE.search(text)
    if m:
        return _strip(text[: m.start()] + " " + text[m.end():]), zone(m.group(1))
    return text, None


def _strip(text: str) -> str:
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"\b(local time|time zone|timezone)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,;()[]")


# --------------------------------------------------------------------------
# Single values
# --------------------------------------------------------------------------


def _clean(text: str) -> str:
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = _ORDINAL_RE.sub(r"\1", text)
    text = _WEEKDAY_RE.sub(" ", text)
    text = _HRS_RE.sub(r"\1:\2", text)
    text = _DOT_TIME_RE.sub(r"\1:\2", text)
    text = re.sub(r"\bat\b|\bhrs?\b|\bhours\b|@", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,;")


def parse_time_only(text: str) -> tuple[int, int, int] | None:
    m = _TIME_ONLY_RE.match(text)
    if not m:
        return None
    if m.group(4):  # 12h clock
        hour, minute, second = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
        if hour > 12:
            return None
        if m.group(4).lower() == "p" and hour != 12:
            hour += 12
        if m.group(4).lower() == "a" and hour == 12:
            hour = 0
    elif m.group(5):
        hour, minute, second = int(m.group(5)), int(m.group(6)), int(m.group(7) or 0)
    else:
        hour, minute, second = int(m.group(8)), int(m.group(9)), 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute, second


@dataclass
class Parsed:
    value: dt.datetime | None = None  # full datetime (aware)
    date: dt.date | None = None  # date-only value
    time: tuple[int, int, int] | None = None  # time-only value
    tz: dt.tzinfo | None = None  # explicit tz found in the value

    @property
    def kind(self) -> str:
        if self.value is not None:
            return "datetime"
        if self.date is not None:
            return "date"
        if self.time is not None:
            return "time"
        return "none"


def parse_value(text: str, *, default_tz: dt.tzinfo = UTC, dayfirst: bool = False,
                reference: dt.datetime | None = None) -> Parsed:
    """Parse a single date, time or datetime expression."""
    text = _clean(text)
    rest, tz = extract_tz(text)
    rest = rest.strip()
    if not rest:
        return Parsed(tz=tz)
    t = parse_time_only(rest)
    if t:
        return Parsed(time=t, tz=tz)
    if not _HAS_DATE_RE.search(rest):
        return Parsed(tz=tz)
    reference = reference or dt.datetime.now(UTC)
    has_time = bool(_HAS_TIME_RE.search(rest)) or bool(re.search(r"T\d{2}:?\d{2}", rest))
    use_dayfirst = dayfirst and not _YMD_RE.search(rest)
    default = dt.datetime(reference.year, 1, 1)
    try:
        parsed = du_parser.parse(rest, dayfirst=use_dayfirst, fuzzy=True, default=default)
    except (ValueError, OverflowError, TypeError):
        return Parsed(tz=tz)
    if not re.search(r"\b\d{4}\b|\d{8}T|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2}\b", rest):
        # No year given: choose the occurrence nearest the reference date.
        candidate = parsed.replace(year=reference.year)
        if candidate.date() < (reference - dt.timedelta(days=180)).date():
            candidate = candidate.replace(year=reference.year + 1)
        parsed = candidate
    if not has_time:
        return Parsed(date=parsed.date(), tz=tz)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or default_tz)
    return Parsed(value=parsed, tz=tz or parsed.tzinfo)


def _combine(date: dt.date, time: tuple[int, int, int], tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, *time, tzinfo=tz)


def parse_range(text: str, *, default_tz: dt.tzinfo = UTC, dayfirst: bool = False,
                reference: dt.datetime | None = None, date_hint: dt.date | None = None
                ) -> tuple[dt.datetime, dt.datetime | None] | None:
    """Parse '<start> - <end>' where end may be a bare time; returns aware datetimes."""
    isos = _ISO_OFFSET_RE.findall(text)
    if len(isos) == 2:  # "2026-10-03T01:00:00-05:00 to ..." — a '-' offset must not split the range
        start, end = (du_parser.isoparse(v) for v in isos)
        return start, end
    text = _clean(text)
    whole_rest, whole_tz = extract_tz(text)
    on_date = None
    m = _ON_DATE_RE.search(whole_rest)
    if m:
        on = parse_value(m.group("date"), default_tz=default_tz, dayfirst=dayfirst, reference=reference)
        if on.kind in ("date", "datetime"):
            on_date = on.date or on.value.date()
            text = text[: text.lower().rfind(" on ")] if " on " in text.lower() else text
    text = re.sub(r"^\s*(from|between)\s+", "", text, flags=re.I)
    parts = _RANGE_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) != 2:
        single = parse_value(text, default_tz=default_tz, dayfirst=dayfirst, reference=reference)
        if single.kind == "datetime":
            return single.value, None
        return None
    left_raw, right_raw = parts
    right = parse_value(right_raw, default_tz=default_tz, dayfirst=dayfirst, reference=reference)
    left = parse_value(left_raw, default_tz=default_tz, dayfirst=dayfirst, reference=reference)
    tz = left.tz or right.tz or whole_tz or default_tz
    base_date = on_date or date_hint

    if left.kind == "datetime":
        start = left.value if left.tz else left.value.replace(tzinfo=tz)
    elif left.kind == "time" and base_date:
        start = _combine(base_date, left.time, tz)
    elif left.kind == "time" and right.kind == "datetime":
        start = _combine(right.value.date(), left.time, right.tz or tz)
    else:
        return None

    if right.kind == "datetime":
        end = right.value if right.tz else right.value.replace(tzinfo=right.tz or tz)
    elif right.kind == "time":
        end = _combine(start.date(), right.time, right.tz or start.tzinfo or tz)
        if end <= start:
            end += dt.timedelta(days=1)
    elif right.kind == "date":
        end = None
    else:
        return None
    if left.kind == "time" and right.kind == "datetime" and start > end:
        start -= dt.timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------
# Labelled fields
# --------------------------------------------------------------------------

_LABEL_LINE_RE = re.compile(
    r"^\s*[-*•>]*\s*(?P<label>[A-Za-z][A-Za-z0-9 /()&.,'#-]{0,50}?)\s*(?::|\|)\s*(?P<value>.*)$"
)
_BACKUP_RE = re.compile(r"backup|alternat|fallback|secondary|contingency|reserve|rain ?date", re.I)
_LABEL_TZ_RE = re.compile(r"\(([^)]*)\)|\b(utc|gmt|local)\b", re.I)


def classify_label(label: str) -> str | None:
    lab = label.lower().strip()
    if re.search(r"\b(sent|received|reply|subject|from|to|cc)\s*$", lab) and not re.search(r"start|end|window", lab):
        return None
    if re.search(r"duration|length", lab):
        return "duration"
    if re.search(r"\b(end|ends|finish|until|stop|complet\w*)\b", lab):
        return "end"
    if re.search(r"\b(start|starts|begin|begins|commenc\w*)\b", lab):
        return "start"
    if re.search(
        r"window|schedul|period|time\s*frame|timeframe|\bwhen\b|date\s*(/|&|and)\s*time|"
        r"(maintenance|outage|work|event|activity|planned|change)\s+(date|time)", lab):
        return "window"
    if re.search(r"^(\w+\s)?dates?(\s*\(.*\))?$", lab):
        return "date"
    if re.search(r"^(\w+\s)?times?(\s*\(.*\))?$", lab):
        return "time"
    return None


def _label_tz(label: str, default: dt.tzinfo) -> dt.tzinfo:
    for m in _LABEL_TZ_RE.finditer(label):
        token = (m.group(1) or m.group(2) or "").strip()
        if token.lower() == "local":
            continue
        _, tz = extract_tz(token)
        if tz is not None:
            return tz
    return default


_DURATION_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|h|days?)\b", re.I)


def _parse_duration_value(value: str) -> dt.timedelta | None:
    total = dt.timedelta()
    found = False
    for m in _DURATION_VALUE_RE.finditer(value):
        found = True
        amount, unit = float(m.group(1)), m.group(2).lower()
        if unit.startswith("m"):
            total += dt.timedelta(minutes=amount)
        elif unit.startswith("d"):
            total += dt.timedelta(days=amount)
        else:
            total += dt.timedelta(hours=amount)
    return total if found and total > dt.timedelta(0) else None


def _fields(lines: list[str]) -> list[tuple[str, str, int]]:
    out = []
    for i, line in enumerate(lines):
        m = _LABEL_LINE_RE.match(line)
        if not m:
            continue
        label, value = m.group("label").strip(), m.group("value").strip()
        if re.search(r"\d{3,}|\d[-/.]\d", label):
            continue  # the ':' belonged to a time inside a sentence, not to a label
        if not value:
            # Value on the following non-empty line ("Start Date/Time:\n  03-Oct-2026 01:00").
            for nxt in lines[i + 1: i + 3]:
                if nxt.strip():
                    if not _LABEL_LINE_RE.match(nxt) or _HAS_DATE_RE.search(nxt):
                        value = nxt.strip()
                    break
        if value:
            out.append((label, value, i))
    return out


def _in_forward_header(lines: list[str], idx: int) -> bool:
    window = " ".join(lines[max(0, idx - 3): idx + 4]).lower()
    return ("from:" in window and ("subject:" in window or "to:" in window)) or "sent:" in window


def extract_windows(text: str, *, default_tz: dt.tzinfo | str = UTC, dayfirst: bool = False,
                    reference: dt.datetime | None = None) -> list[Window]:
    if isinstance(default_tz, str):
        default_tz = zone(default_tz)
    reference = reference or dt.datetime.now(UTC)
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    kw = {"dayfirst": dayfirst, "reference": reference}

    windows: list[Window] = []
    starts: list[dict] = []
    ends: list[dict] = []
    dates: list[tuple[dt.date, int, bool]] = []
    times: list[tuple[str, dt.tzinfo, int, bool]] = []
    durations: list[dt.timedelta] = []

    for label, value, idx in _fields(lines):
        kind = classify_label(label)
        if kind is None:
            continue
        backup = bool(_BACKUP_RE.search(label))
        tz = _label_tz(label, default_tz)
        if kind == "duration":
            d = _parse_duration_value(value)
            if d:
                durations.append(d)
            continue
        if kind in ("window", "date", "time"):
            if kind == "date" and _in_forward_header(lines, idx):
                continue
            rng = parse_range(value, default_tz=tz, **kw)
            if rng and (rng[1] is not None or kind == "window"):
                windows.append(Window(rng[0], rng[1], label=label, backup=backup, source=f"{label}: {value}",
                                      confidence="high"))
                continue
            single = parse_value(value, default_tz=tz, **kw)
            if kind in ("date", "window") and single.kind == "date":
                dates.append((single.date, idx, backup))
            elif kind in ("time", "window"):
                times.append((value, tz, idx, backup))
            continue
        # start / end
        parsed = parse_value(value, default_tz=tz, **kw)
        bucket = starts if kind == "start" else ends
        entry = {"label": label, "backup": backup, "tz": parsed.tz or tz, "idx": idx}
        if parsed.kind == "datetime":
            entry["dt"] = parsed.value
            bucket.append(entry)
        elif parsed.kind == "date":
            last = bucket[-1] if bucket else None
            if last and "time" in last and "date" not in last and "dt" not in last:
                last["date"] = parsed.date
            else:
                entry["date"] = parsed.date
                bucket.append(entry)
        elif parsed.kind == "time":
            last = bucket[-1] if bucket else None
            if last and "date" in last and "time" not in last and "dt" not in last:
                last["time"] = parsed.time
                if parsed.tz:
                    last["tz"] = parsed.tz
            else:
                entry["time"] = parsed.time
                bucket.append(entry)

    def resolve(entry: dict, fallback_date: dt.date | None) -> dt.datetime | None:
        if "dt" in entry:
            return entry["dt"]
        date = entry.get("date") or fallback_date
        if date and "time" in entry:
            return _combine(date, entry["time"], entry["tz"])
        if date:
            return _combine(date, (0, 0, 0), entry["tz"])
        return None

    date_default = dates[0][0] if dates else None
    for i, s in enumerate(starts):
        start = resolve(s, date_default)
        if start is None:
            continue
        end = None
        if i < len(ends):
            end = resolve(ends[i], start.date())
            if end is not None and "dt" not in ends[i] and "date" not in ends[i] and end <= start:
                end += dt.timedelta(days=1)
        if end is None and durations:
            end = start + durations[0]
        windows.append(Window(start, end, label=s["label"], backup=s["backup"], source=s["label"], confidence="high"))

    # "Date: 3 Oct 2026" + "Time: 22:00 - 02:00 BST"
    for value, tz, idx, backup in times:
        date = None
        for d, didx, dbackup in dates:
            if didx <= idx + 3:
                date = d
                backup = backup or dbackup
        if date is None:
            continue
        rng = parse_range(value, default_tz=tz, date_hint=date, **kw)
        if rng:
            windows.append(Window(rng[0], rng[1], label="time", backup=backup, source=value, confidence="high"))

    if not windows:
        windows = _scan_free_text(text, default_tz, dayfirst, reference)
    return _finalise(windows, reference)


# --------------------------------------------------------------------------
# Free-text fallback
# --------------------------------------------------------------------------

_TIME_TOKEN = r"\d{1,2}(?::\d{2}){1,2}(?:\s*[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?"
_DATE_TOKEN = (
    r"\d{4}-\d{2}-\d{2}|\d{4}-" + _MONTHS + r"-\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
    r"\d{1,2}(?:st|nd|rd|th)?[ -]" + _MONTHS + r"[ -,]*\d{4}|" + _MONTHS + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
)
_TZ_TOKEN = r"(?:\(?\s*(?:UTC|GMT)(?:\s*[+-]\s*\d{1,2}(?::?\d{2})?)?\s*\)?|\(?[A-Z]{2,5}\)?)"
_DT_TOKEN = (
    r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?|"
    r"(?:" + _DATE_TOKEN + r")[,T\s]+(?:at\s+)?(?:" + _TIME_TOKEN + r"))"
)
_FREE_RANGE_RE = re.compile(
    r"(?P<a>" + _DT_TOKEN + r")(?:\s*" + _TZ_TOKEN + r")?\s*(?:-|–|—|to|until|till|and)\s*"
    r"(?P<b>" + _DT_TOKEN + r"|" + _TIME_TOKEN + r")(?P<tz>\s*" + _TZ_TOKEN + r")?",
    re.I,
)
_FROM_TO_ON_RE = re.compile(
    r"(?:from|between)\s+(?P<a>" + _TIME_TOKEN + r")\s*(?:-|to|and|until)\s*(?P<b>" + _TIME_TOKEN + r")"
    r"(?P<tz>\s*" + _TZ_TOKEN + r")?\s*(?:on|,)\s+(?:\w+day,?\s+)?(?P<date>" + _DATE_TOKEN + r")",
    re.I,
)


def _scan_free_text(text: str, default_tz: dt.tzinfo, dayfirst: bool, reference: dt.datetime) -> list[Window]:
    out: list[Window] = []
    for m in _FROM_TO_ON_RE.finditer(text):
        expr = f"{m.group('a')} - {m.group('b')} {m.group('tz') or ''} on {m.group('date')}"
        rng = parse_range(expr, default_tz=default_tz, dayfirst=dayfirst, reference=reference)
        if rng:
            out.append(Window(rng[0], rng[1], label="text", source=m.group(0), confidence="medium"))
    for m in _FREE_RANGE_RE.finditer(text):
        expr = m.group(0)
        rng = parse_range(expr, default_tz=default_tz, dayfirst=dayfirst, reference=reference)
        if rng:
            ctx = text[max(0, m.start() - 60): m.start()]
            out.append(Window(rng[0], rng[1], label="text", backup=bool(_BACKUP_RE.search(ctx)),
                              source=expr, confidence="medium"))
    return out


def _finalise(windows: list[Window], reference: dt.datetime) -> list[Window]:
    seen = set()
    out: list[Window] = []
    for w in windows:
        if w.end is not None and (w.end <= w.start or w.end - w.start > MAX_WINDOW):
            continue
        if abs(w.start - reference) > dt.timedelta(days=800):
            continue
        key = (w.start, w.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    # Windows without an end that share a start with a complete window are redundant.
    complete_starts = {w.start for w in out if w.end}
    out = [w for w in out if w.end or w.start not in complete_starts]
    primaries = [w for w in out if not w.backup]
    backups = [w for w in out if w.backup]
    return primaries + backups
