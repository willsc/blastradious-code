"""Parser output shared by every parser implementation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Normalised status vocabulary (BCOP + incident lifecycle).
STATUSES = ("TENTATIVE", "CONFIRMED", "IN-PROCESS", "RE-SCHEDULED", "CANCELLED", "COMPLETED")
IMPACTS = ("NO-IMPACT", "REDUCED-REDUNDANCY", "DEGRADED", "OUTAGE")


@dataclass
class CircuitRef:
    id: str
    impact: str | None = None


@dataclass
class ParsedNotice:
    parser: str
    provider: str | None = None
    account: str | None = None
    maintenance_id: str | None = None
    status: str | None = None
    impact: str | None = None
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    windows: list[dict] = field(default_factory=list)
    circuits: list[CircuitRef] = field(default_factory=list)
    # AWS resource ids / IPs a structured source explicitly lists as affected.
    declared_resources: list[str] = field(default_factory=list)
    summary: str | None = None
    sequence: int | None = None
    stamp: dt.datetime | None = None
    uid: str | None = None
    organizer: str | None = None
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    confidence: str = "low"
    emergency: bool = False
    structured: bool = False  # parsed from a machine-readable format (BCOP, NTC)
    # One mail carried several maintenances: correlate each only on its own declared ids,
    # not on everything mentioned in the shared body.
    exclusive_ids: bool = False

    def fill_from(self, other: ParsedNotice) -> None:
        """Fill fields this parser could not determine from a (heuristic) fallback."""
        for name in ("provider", "account", "maintenance_id", "status", "impact", "start", "end",
                     "summary", "organizer"):
            if getattr(self, name) in (None, "") and getattr(other, name) not in (None, ""):
                setattr(self, name, getattr(other, name))
        if not self.windows and other.windows:
            self.windows = list(other.windows)
        if not self.circuits and other.circuits:
            self.circuits = list(other.circuits)
        for key, values in other.identifiers.items():
            merged = self.identifiers.setdefault(key, [])
            for value in values:
                if value not in merged:
                    merged.append(value)
        self.emergency = self.emergency or other.emergency
