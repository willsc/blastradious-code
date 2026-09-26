"""AWS Health notification emails (scheduled changes, issues, account notifications).

These name the affected resources directly (dxcon-..., vpn-..., i-...), so they anchor on the
resources themselves rather than on a carrier circuit.
"""

from __future__ import annotations

import re

from ..config import Config, ProviderHint
from ..mailparse import MailMessage, sender_domain
from ..util import short_hash
from .generic import clean_subject, extract_identifiers
from .model import ParsedNotice
from .timewin import extract_windows

_AWS_DOMAINS = ("aws.com", "amazon.com", "amazonaws.com")
EVENT_CODE_RE = re.compile(r"\bAWS_[A-Z0-9]+_[A-Z0-9_]+\b")
EVENT_ARN_RE = re.compile(r"arn:aws:health:[a-z0-9-]*:\d*:event/[A-Za-z0-9_/.:-]+")
_CATEGORY_RE = re.compile(r"event type category\s*:\s*(\w+)", re.I)


def is_aws_mail(mail: MailMessage) -> bool:
    domains = [sender_domain(s) for s in mail.senders]
    from_aws = any(d == dom or d.endswith("." + dom) for d in domains for dom in _AWS_DOMAINS)
    text = mail.subject + "\n" + mail.text
    return from_aws and bool(EVENT_CODE_RE.search(text) or EVENT_ARN_RE.search(text)
                             or re.search(r"AWS Health|scheduled (maintenance|change)|affected resources", text, re.I))


def impact_for_event(code: str | None, category: str | None, text: str) -> str | None:
    code = (code or "").upper()
    if "REDUNDANCY" in code or "TUNNEL" in code:
        return "DEGRADED"
    if any(word in code for word in ("MAINTENANCE", "RETIREMENT", "REBOOT", "SCHEDULED", "DECOMMISSION")):
        return "OUTAGE"
    if "ISSUE" in code or "OPERATIONAL" in code or (category or "").lower() == "issue":
        return "DEGRADED"
    if (category or "").lower() == "accountnotification":
        return "NO-IMPACT"
    if re.search(r"may (become|be) unavailable|will be unavailable|loss of connectivity", text, re.I):
        return "OUTAGE"
    return None


def parse(mail: MailMessage, hint: ProviderHint | None, cfg: Config) -> list[ParsedNotice]:
    if not is_aws_mail(mail):
        return []
    text = mail.subject + "\n" + mail.text
    code_match = EVENT_CODE_RE.search(text)
    arn_match = EVENT_ARN_RE.search(text)
    category_match = _CATEGORY_RE.search(text)
    code = code_match.group(0) if code_match else None
    category = category_match.group(1) if category_match else None
    identifiers = extract_identifiers(mail.searchable_text())
    resources = identifiers.get("aws_ids", []) + identifiers.get("uuids", [])
    windows = extract_windows(mail.text, reference=mail.date)

    notice = ParsedNotice(parser="aws_health", provider="AWS", confidence="high" if code else "medium")
    notice.summary = clean_subject(mail.subject) or code
    notice.identifiers = identifiers
    notice.declared_resources = resources
    notice.structured = bool(resources)
    notice.impact = impact_for_event(code, category, text)
    if windows:
        notice.windows = [w.as_dict() for w in windows]
        notice.start, notice.end = windows[0].start, windows[0].end
    accounts = identifiers.get("accounts") or []
    notice.account = accounts[0] if accounts else None
    subject = mail.subject.lower()
    if "cancel" in subject:
        notice.status = "CANCELLED"
    elif re.search(r"\b(completed|resolved)\b", subject):
        notice.status = "COMPLETED"
    else:
        notice.status = "CONFIRMED"
    if arn_match:
        notice.maintenance_id = arn_match.group(0)
    elif code:
        start_key = notice.start.date().isoformat() if notice.start else ""
        notice.maintenance_id = f"{code}:{start_key}:{short_hash(*sorted(resources), length=8)}"
    if code:
        notice.notes.append(f"AWS Health event {code}" + (f" ({category})" if category else ""))
        identifiers.setdefault("event_codes", []).append(code)
    return [notice]
