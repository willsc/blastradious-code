"""Heuristic parser for free-text / HTML carrier notices.

Always runs (even when a structured parser succeeded) so AWS ids, IPs and impact wording
found anywhere in the mail feed correlation. Provider hints from the config add
carrier-specific circuit-id and maintenance-id patterns.
"""

from __future__ import annotations

import ipaddress
import re

from ..config import Config, ProviderHint
from ..mailparse import MailMessage, organisation_from_domain, sender_domain
from ..util import human_duration
from .model import CircuitRef, ParsedNotice
from .timewin import _fields, extract_windows, zone

# --------------------------------------------------------------------------
# Identifier patterns
# --------------------------------------------------------------------------

AWS_ID_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:"
    r"(?:dxcon|dxlag|dxvif)-[a-z0-9]{8,}"
    r"|(?:tgw-attach|tgw-rtb|vpn|cgw|vgw|tgw|vpc|subnet|rtb|eni|i|nat|igw|eigw|pcx|vpce|lgw|eipalloc)-[0-9a-f]{8,17}"
    r")(?![A-Za-z0-9-])",
    re.I,
)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
IPV4_RE = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:/(?:3[0-2]|[12]?\d))?(?![\d.])"
)
IPV6_RE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![0-9A-Fa-f:])")
REGION_RE = re.compile(
    r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:gov-)?(?:north|south|east|west|central|northeast|northwest|southeast|southwest)-\d\b"
)
ACCOUNT_RE = re.compile(r"(?i)account(?:\s*(?:id|number|no\.?))?\s*[:#]?\s*\[?(\d{12})\b|\[AWS Account:\s*(\d{12})\]")


def extract_identifiers(text: str) -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {"aws_ids": [], "ips": [], "cidrs": [], "uuids": [], "regions": [], "accounts": []}

    def add(key: str, value: str) -> None:
        if value not in ids[key]:
            ids[key].append(value)

    for m in AWS_ID_RE.finditer(text):
        add("aws_ids", m.group(0).lower())
    for m in UUID_RE.finditer(text):
        add("uuids", m.group(0).lower())
    for m in IPV4_RE.finditer(text):
        _add_ip(add, m.group(0))
    for m in IPV6_RE.finditer(text):
        token = m.group(0)
        if "::" in token or token.count(":") >= 7:
            _add_ip(add, token)
    for m in REGION_RE.finditer(text):
        add("regions", m.group(0))
    for m in ACCOUNT_RE.finditer(text):
        add("accounts", m.group(1) or m.group(2))
    return {k: v for k, v in ids.items() if v}


def _add_ip(add, token: str) -> None:
    try:
        if "/" in token:
            net = ipaddress.ip_network(token, strict=False)
            add("cidrs", str(net))
        else:
            add("ips", str(ipaddress.ip_address(token)))
    except ValueError:
        pass


# --------------------------------------------------------------------------
# Impact / status wording
# --------------------------------------------------------------------------

_NEGATION_RE = re.compile(r"\b(no|not|without|non|zero|nor)\b[^.;,\n]{0,25}$", re.I)
_NO_IMPACT_RE = re.compile(
    r"no (?:expected )?(?:service )?impact|non[- ]?service[- ]?affecting|not (?:expected to be )?service[- ]?affecting|"
    r"no (?:outage|interruption|disruption|downtime) (?:is )?expected|impact\s*:\s*none|zero impact|"
    r"no impact (?:is )?expected|should not (?:be )?(?:impact|affect)",
    re.I,
)
_HIT_RE = re.compile(
    r"switch[- ]?hits?|protection switch|brief (?:interruption|hit|loss)|momentary|sub[- ]?second|"
    r"\bhits? of\b|<\s*\d+\s*(?:ms|milliseconds)|\bflaps?\b|\bflapping\b",
    re.I,
)
_OUTAGE_RE = re.compile(
    r"\boutage\b|loss of (?:service|connectivity|traffic)|service (?:will be )?(?:unavailable|down|interrupted)|"
    r"\bdowntime\b|hard down|service interruption|service[- ]affecting|traffic will be (?:lost|interrupted|down)|"
    r"will (?:be|go) down|(?:circuit|service|link)s? will be (?:down|unavailable|interrupted)|disconnect",
    re.I,
)
_REDUNDANCY_RE = re.compile(
    r"(?:loss of|lose|reduced) (?:redundancy|resilien\w*|diversity|protection)|unprotected|at risk|"
    r"redundancy (?:will be )?(?:reduced|lost)|reduced[- ]redundancy",
    re.I,
)
_DEGRADED_RE = re.compile(r"degrad\w*|intermittent|latency|jitter|packet loss|reduced (?:capacity|bandwidth|throughput)", re.I)
_OUTAGE_DURATION_RE = re.compile(
    r"(?:outage|interruption|downtime|hit|loss of service|down)[^.\n]{0,40}?"
    r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?|mins?|minutes?|hrs?|hours?)\b",
    re.I,
)


def _positive(regex: re.Pattern, text: str) -> bool:
    for m in regex.finditer(text):
        if not _NEGATION_RE.search(text[max(0, m.start() - 40): m.start()]):
            return True
    return False


def _outage_seconds(text: str) -> float | None:
    longest = None
    for m in _OUTAGE_DURATION_RE.finditer(text):
        amount, unit = float(m.group(1)), m.group(2).lower()
        if unit.startswith("ms") or unit.startswith("milli"):
            seconds = amount / 1000
        elif unit.startswith("s"):
            seconds = amount
        elif unit.startswith("m"):
            seconds = amount * 60
        else:
            seconds = amount * 3600
        longest = seconds if longest is None else max(longest, seconds)
    return longest


def classify_impact(text: str, *, field: bool = False) -> str | None:
    """Map impact wording to BCOP vocabulary. `field=True` for a dedicated 'Impact:' value."""
    if not text:
        return None
    if field and _NO_IMPACT_RE.search(text):
        return "NO-IMPACT"
    seconds = _outage_seconds(text)
    if _positive(_OUTAGE_RE, text):
        if seconds is not None and seconds <= 60:
            return "DEGRADED"
        return "OUTAGE"
    if _HIT_RE.search(text):
        return "DEGRADED"
    if _positive(_REDUNDANCY_RE, text):
        return "REDUCED-REDUNDANCY"
    if _positive(_DEGRADED_RE, text):
        return "DEGRADED"
    if _NO_IMPACT_RE.search(text):
        return "NO-IMPACT"
    return None


_SUBJECT_STATUS = [
    ("CANCELLED", re.compile(r"\bcancel+ed\b|\bcancellation\b|\bwithdrawn\b|\babort(ed)?\b", re.I)),
    ("COMPLETED", re.compile(r"\bcompleted?\b|\bclosed\b|\bresolved\b|\brestored\b|\bfinished\b|\bclosure\b", re.I)),
    ("RE-SCHEDULED", re.compile(r"\bre-?scheduled?\b|\bpostponed\b|\bdate change\b|\bnew date\b|\bamended\b", re.I)),
    ("IN-PROCESS", re.compile(r"\bstarted\b|\bcommenced\b|\bin progress\b|\bunderway\b|\bbegun\b", re.I)),
    ("TENTATIVE", re.compile(r"\btentative\b|\bprovisional\b|\bpre-?notification\b|\badvance notice\b", re.I)),
]
_BODY_STATUS = [
    ("CANCELLED", re.compile(r"(?:has|have) been (?:cancel+ed|withdrawn)|will no longer (?:take place|proceed)|"
                             r"will not (?:take place|go ahead|proceed)", re.I)),
    ("COMPLETED", re.compile(r"(?:has|have) been (?:successfully )?completed|is now complete|"
                             r"(?:service|services) (?:has|have) been restored|has been resolved", re.I)),
    ("RE-SCHEDULED", re.compile(r"(?:has|have) been (?:re-?scheduled|postponed|moved)", re.I)),
    ("IN-PROCESS", re.compile(r"(?:work|maintenance|activity) (?:has|have) (?:now )?(?:started|commenced|begun)", re.I)),
]


_CONDITIONAL_RE = re.compile(r"\b(once|when|after|until|before|if|unless|will|should)\b[^.;\n]*$", re.I)


def classify_status(subject: str, body: str) -> str | None:
    for status, regex in _SUBJECT_STATUS:
        if regex.search(subject):
            return status
    for status, regex in _BODY_STATUS:
        for m in regex.finditer(body):
            # "we will notify you once the work has been completed" is boilerplate, not a status
            if not _CONDITIONAL_RE.search(body[max(0, m.start() - 60): m.start()]):
                return status
    return None


_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|tr)\s*:|\[(?:external|ext|caution)[^\]]*\]|\*+external\*+|(?:external|caution)\s*:)\s*",
    re.I,
)


_LEADING_TAG_RE = re.compile(r"^\s*\[[A-Za-z][A-Za-z ]{2,40}\]\s*")
_AWS_ACCOUNT_TAG_RE = re.compile(r"\s*\[AWS Account:\s*\d{12}\]\s*$", re.I)


def clean_subject(subject: str) -> str:
    """Strip reply/forward prefixes, [EXTERNAL]-style tags, '[Scheduled change]'-style leading tags
    and AWS's trailing '[AWS Account: ...]'."""
    previous = None
    while previous != subject:
        previous = subject
        subject = _SUBJECT_PREFIX_RE.sub("", subject)
        subject = _LEADING_TAG_RE.sub("", subject)
    return _AWS_ACCOUNT_TAG_RE.sub("", subject).strip()


# --------------------------------------------------------------------------
# Maintenance id / circuit extraction
# --------------------------------------------------------------------------

_MID_STRONG_LABEL = re.compile(
    r"\b(maintenance|ticket|change|case|event|work ?order|pwo|pwic|crq|chg|incident|notification|activity|job|"
    r"request|planned works?|engineering works?|tt|trouble ticket)\b.*?(id|no|nr|number|num|#|ref|reference)?\.?\s*$",
    re.I,
)
_MID_WEAK_LABEL = re.compile(r"^(?:our\s+)?(?:ref|reference|reference number|ref\.? no\.?)$", re.I)
_MID_EXCLUDE_LABEL = re.compile(r"customer|your|circuit|service|account|order", re.I)
_MID_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./#:-]{2,48}")
_SUBJECT_MID_RE = re.compile(
    r"[\[(]\s*(?:#|ticket|ref|case)?\s*[:#]?\s*([A-Z0-9]{0,8}[-_]?\d{4,}[A-Z0-9-]*)\s*[\])]|"
    r"\b((?:CRQ|CHG|PWO|PWIC|PEW|TTN|INC|MX|PW|CR)[-_]?\d{4,}[A-Z0-9-]*)\b|"
    r"\b(?:ticket|ref|reference|case|maintenance|notification)\s*(?:#|no\.?|id|number)?\s*[:#]?\s*"
    r"([A-Z]{0,8}[-_]?\d{4,}[A-Z0-9-]*)\b",
    re.I,
)
_CIRCUIT_LABEL = re.compile(
    r"\b(circuit|cct|ckt)s?\b|\bservice\s*(id|ids|ref|reference|number|no)\b|\baffected services?\b|"
    r"\bline (id|ref|reference)\b|\baccess (id|ref)\b|\bport id\b|\bconnection id\b|\blink id\b",
    re.I,
)
_SPLIT_RE = re.compile(r"\s*[,;]\s*|\s{2,}|\s+and\s+|\s+\|\s+")


def _first_id(value: str) -> str | None:
    for m in _MID_VALUE_RE.finditer(value):
        token = m.group(0).rstrip(".:,;#-")
        if any(ch.isdigit() for ch in token) and len(token) >= 3:
            return token
    return None


def find_maintenance_id(mail: MailMessage, fields, hint: ProviderHint | None) -> str | None:
    text = mail.subject + "\n" + mail.text
    if hint:
        for pattern in hint.maintenance_id_patterns:
            m = re.search(pattern, text)
            if m:
                return (m.group(1) if m.groups() else m.group(0)).strip()
    weak = None
    for label, value, _ in fields:
        if _MID_EXCLUDE_LABEL.search(label):
            continue
        if _MID_STRONG_LABEL.search(label):
            token = _first_id(value)
            if token:
                return token
        elif weak is None and _MID_WEAK_LABEL.match(label.strip()):
            weak = _first_id(value)
    if weak:
        return weak
    m = _SUBJECT_MID_RE.search(mail.subject)
    if m:
        return next(g for g in m.groups() if g).strip()
    return None


def _clean_circuit(token: str) -> str | None:
    token = token.strip().strip("()[]{}<>\"'.:;,").strip()
    token = re.sub(r"^(?:circuit|cct|ckt|service)\s*(?:id|ref|no\.?)?\s*[:#]?\s*", "", token, flags=re.I)
    if not (3 <= len(token) <= 60) or not any(c.isdigit() for c in token):
        return None
    if re.fullmatch(r"\d{1,2}(:\d{2})+|\d{4}-\d{2}-\d{2}.*|\d+(\.\d+)?\s*(ms|s|min|mins|h|hours?|gbps|mbps|g|m)", token, re.I):
        return None
    return token


def find_circuits(mail: MailMessage, lines: list[str], fields, hint: ProviderHint | None) -> list[str]:
    text = mail.subject + "\n" + mail.text
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in found:
            found.append(value)

    if hint:
        for pattern in hint.circuit_patterns:
            for m in re.finditer(pattern, text):
                add((m.group(1) if m.groups() else m.group(0)).strip())
    for label, value, idx in fields:
        if not _CIRCUIT_LABEL.search(label) or re.search(r"impact|status|type|speed|bandwidth", label, re.I):
            continue
        for token in _SPLIT_RE.split(value):
            add(_clean_circuit(token))
        # A heading such as "Affected circuits:" followed by one id per line.
        stripped = lines[idx].split(":", 1)[-1].strip() if ":" in lines[idx] else ""
        if not stripped:
            for nxt in lines[idx + 1: idx + 60]:
                if not nxt.strip():
                    break
                if re.match(r"^\s*[A-Za-z][A-Za-z /()]{0,30}:\s", nxt):
                    break
                add(_clean_circuit(_SPLIT_RE.split(nxt.strip(" -*•"))[0]))
    return found


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def provider_name(mail: MailMessage, hint: ProviderHint | None) -> str:
    if hint and hint.name:
        return hint.name
    domain = sender_domain(mail.effective_sender)
    return organisation_from_domain(domain) if domain else (mail.effective_sender_name or "Unknown")


def parse(mail: MailMessage, hint: ProviderHint | None, cfg: Config) -> list[ParsedNotice]:
    body = mail.text or ""
    lines = body.split("\n")
    fields = _fields(lines)
    reference = mail.date
    windows = extract_windows(
        body,
        default_tz=zone(hint.timezone) if hint else zone("UTC"),
        dayfirst=bool(hint and hint.dayfirst),
        reference=reference,
    ) if body else []
    notice = ParsedNotice(parser="generic", provider=provider_name(mail, hint), confidence="low")
    notice.summary = clean_subject(mail.subject) or None
    notice.maintenance_id = find_maintenance_id(mail, fields, hint)
    notice.circuits = [CircuitRef(c) for c in find_circuits(mail, lines, fields, hint)]
    if windows:
        notice.windows = [w.as_dict() for w in windows]
        notice.start, notice.end = windows[0].start, windows[0].end
        notice.confidence = "medium" if windows[0].confidence == "high" else "low"

    impact = None
    for label, value, _ in fields:
        if re.search(r"impact|outage type|service effect|expected effect", label, re.I):
            impact = classify_impact(value, field=True)
            if impact:
                break
    notice.impact = impact or classify_impact(mail.subject + "\n" + body)
    seconds = _outage_seconds(body)
    if seconds:
        notice.notes.append(f"expected interruption up to {human_duration(seconds)}")
    notice.status = classify_status(mail.subject, body)
    notice.emergency = bool(re.search(r"\b(emergency|urgent|unplanned)\b", mail.subject, re.I))
    notice.identifiers = extract_identifiers(mail.searchable_text())
    accounts = notice.identifiers.get("accounts") or []
    if accounts:
        notice.account = accounts[0]
    return [notice]
