"""Small shared helpers: time, JSON, identifier normalisation, severities."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import zlib
from typing import Any

UTC = dt.timezone.utc

# --------------------------------------------------------------------------
# Severity scale shared by the impact engine, notices and subscriptions.
# --------------------------------------------------------------------------

NONE, AT_RISK, DEGRADED, OUTAGE = 0, 1, 2, 3
SEVERITY_NAMES = {NONE: "NONE", AT_RISK: "AT_RISK", DEGRADED: "DEGRADED", OUTAGE: "OUTAGE"}
SEVERITY_LABELS = {NONE: "No impact", AT_RISK: "At risk", DEGRADED: "Degraded", OUTAGE: "Outage"}
_SEVERITY_BY_NAME = {v: k for k, v in SEVERITY_NAMES.items()}

# Provider-declared impact (BCOP vocabulary) -> severity of the directly affected object.
PROVIDER_IMPACT_SEVERITY = {
    "OUTAGE": OUTAGE,
    "DEGRADED": DEGRADED,
    "REDUCED-REDUNDANCY": AT_RISK,
    "NO-IMPACT": AT_RISK,
}


def severity_from_name(name: str | int | None, default: int = NONE) -> int:
    if name is None:
        return default
    if isinstance(name, int):
        return name
    key = name.strip().upper().replace(" ", "_").replace("-", "_")
    return _SEVERITY_BY_NAME.get(key, default)


def severity_name(sev: int) -> str:
    return SEVERITY_NAMES.get(sev, "NONE")


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str | int | float) -> int:
    """'90' -> 90, '15m' -> 900, '24h' -> 86400."""
    if isinstance(value, (int, float)):
        return int(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    return int(float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()])


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "open-ended"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def norm(value: str) -> str:
    """Separator-insensitive identifier form: 'OGYX/123456//ZYO' -> 'ogyx123456zyo'."""
    return _NON_ALNUM.sub("", str(value).lower())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def short_hash(*parts: Any, length: int = 16) -> str:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return digest[:length]


# --------------------------------------------------------------------------
# JSON / compression
# --------------------------------------------------------------------------


def json_default(obj: Any) -> Any:
    if isinstance(obj, dt.datetime):
        return iso(obj)
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def dumps(obj: Any, **kwargs: Any) -> str:
    return json.dumps(obj, default=json_default, separators=(",", ":"), **kwargs)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def compress(data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return zlib.compress(data, 6)


def decompress(data: bytes | None) -> bytes:
    return zlib.decompress(data) if data else b""
