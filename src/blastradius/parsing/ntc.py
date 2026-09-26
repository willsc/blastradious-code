"""Adapter for Network to Code's circuit-maintenance-parser (optional extra: `pip install .[ntc]`).

It knows ~40 carrier-specific formats (Zayo, Lumen, Equinix, Colt, GTT, Telia, NTT, ...). The
provider parser is chosen by matching the sender's domain against each provider's organizer.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config, ProviderHint
from ..mailparse import MailMessage, sender_domain
from ..util import UTC
from .model import CircuitRef, ParsedNotice

log = logging.getLogger(__name__)

_IMPACT_ORDER = ["NO-IMPACT", "REDUCED-REDUNDANCY", "DEGRADED", "OUTAGE"]

try:  # pragma: no cover - exercised only when the extra is installed
    import circuit_maintenance_parser as _cmp
except Exception:  # ImportError, or its heavy deps failing to import
    _cmp = None


def available() -> bool:
    return _cmp is not None


def _provider_class(mail: MailMessage, hint: ProviderHint | None):
    domains = {sender_domain(s) for s in mail.senders}
    for cls in _cmp.SUPPORTED_PROVIDERS:
        organizer = (cls.get_default_organizer() or "").lower().replace("mailto:", "")
        org_domain = sender_domain(organizer)
        if not org_domain or cls.get_provider_type() == "genericprovider":
            continue
        if any(d == org_domain or d.endswith("." + org_domain) or org_domain.endswith("." + d) for d in domains):
            return cls
        if hint and hint.name and hint.name.lower().replace(" ", "") == cls.get_provider_type():
            return cls
    return None


def _ts(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value), tz=UTC) if value else None
    except (TypeError, ValueError, OSError):
        return None


def parse(mail: MailMessage, hint: ProviderHint | None, cfg: Config) -> list[ParsedNotice]:
    if _cmp is None:
        return []
    cls = _provider_class(mail, hint)
    if cls is None:
        return []
    try:
        data = _cmp.NotificationData.init_from_email_bytes(mail.raw)
        if data is None:
            return []
        maintenances = cls().get_maintenances(data)
    except Exception as exc:  # ProviderError and anything thrown by provider-specific parsers
        log.info("ntc %s parser could not parse %r: %s", cls.get_provider_type(), mail.subject, str(exc).splitlines()[0])
        return []
    out: list[ParsedNotice] = []
    for m in maintenances:
        status = getattr(m.status, "value", str(m.status))
        circuits = [CircuitRef(c.circuit_id, getattr(c.impact, "value", str(c.impact))) for c in m.circuits]
        impacts = [c.impact for c in circuits if c.impact in _IMPACT_ORDER]
        start, end = _ts(m.start), _ts(m.end)
        notice = ParsedNotice(
            parser=f"ntc:{cls.get_provider_type()}",
            provider=m.provider,
            account=m.account or None,
            maintenance_id=m.maintenance_id,
            status=None if status == "NO-CHANGE" else status,
            impact=max(impacts, key=_IMPACT_ORDER.index) if impacts else None,
            start=start,
            end=end,
            circuits=circuits,
            summary=m.summary or None,
            sequence=m.sequence,
            stamp=_ts(m.stamp),
            uid=m.uid if m.uid != "0" else None,
            organizer=m.organizer,
            confidence="high",
            structured=True,
        )
        if start:
            notice.windows = [{"start": start, "end": end, "label": "primary", "backup": False}]
        out.append(notice)
    return out
