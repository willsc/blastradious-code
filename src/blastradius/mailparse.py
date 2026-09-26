"""RFC 822 -> MailMessage: best-effort text body, calendar parts, forwarded-sender detection.

Carrier notices are frequently HTML-only with the interesting data in (nested) tables, or are
forwarded into the NOC mailbox by a human. Both cases are handled here so the parsers only ever
see plain text plus any iCalendar payloads.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from html.parser import HTMLParser

from .util import UTC


@dataclass
class MailMessage:
    raw: bytes
    message_id: str | None = None
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    original_sender: str | None = None
    original_sender_name: str | None = None
    to: list[str] = field(default_factory=list)
    date: dt.datetime | None = None
    text: str = ""
    calendars: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    in_reply_to: str | None = None

    @property
    def senders(self) -> list[str]:
        """Envelope sender plus, for forwarded mail, the original sender."""
        out = [self.sender]
        if self.original_sender and self.original_sender != self.sender:
            out.append(self.original_sender)
        return [s for s in out if s]

    @property
    def effective_sender(self) -> str:
        return self.original_sender or self.sender

    @property
    def effective_sender_name(self) -> str:
        return (self.original_sender_name if self.original_sender else self.sender_name) or ""

    def searchable_text(self) -> str:
        parts = [self.subject, self.text]
        parts.extend(self.calendars)
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "blockquote", "pre", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "tbody", "thead", "tfoot", "center", "dl", "dt", "dd",
}
_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template"}


class _HtmlToText(HTMLParser):
    """Renders HTML as text; 2-cell table rows become 'label: value' lines so labelled-field
    extraction works the same on HTML tables as on plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        # Stack of open rows; each row is a list of cells; each cell a list of text chunks.
        self.rows: list[list[list[str]]] = []

    # -- output helpers --
    def _emit(self, text: str) -> None:
        if self.rows and self.rows[-1]:
            self.rows[-1][-1].append(text)
        else:
            self.out.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if tag == "br":
            self._emit("\n")
        elif tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append([])
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" or tag == "hr":
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "tr" and self.rows:
            row = self.rows.pop()
            cells = [_collapse("".join(c)) for c in row]
            cells = [c for c in cells if c]
            if not cells:
                return
            if len(cells) == 2 and "\n" not in cells[0] and "\n" not in cells[1] and len(cells[0]) <= 60:
                label = cells[0].rstrip()
                sep = " " if label.endswith(":") else ": "
                line = f"{label}{sep}{cells[1]}"
            elif any("\n" in c for c in cells):
                line = "\n".join(cells)
            else:
                line = " | ".join(cells)
            self._emit("\n" + line + "\n")
        elif tag in ("td", "th"):
            pass
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self._emit(data)

    def text(self) -> str:
        while self.rows:  # unclosed rows
            row = self.rows.pop()
            self.out.append("\n" + " | ".join(_collapse("".join(c)) for c in row if "".join(c).strip()) + "\n")
        return _collapse("".join(self.out))


def _collapse(text: str) -> str:
    lines = [re.sub(r"[ \t\r\f\v\u00a0\u200b]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: fall back to tag stripping
        return _collapse(re.sub(r"<[^>]+>", " ", html))
    return parser.text()


# --------------------------------------------------------------------------
# MIME walking
# --------------------------------------------------------------------------

_FWD_MARKER = re.compile(
    r"^-{2,}\s*(forwarded message|original message)\s*-{2,}|^begin forwarded message:", re.I | re.M
)
_FWD_FROM = re.compile(r"^\s*\*?from:\*?\s*(.+)$", re.I | re.M)


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            return content.decode("utf-8", "replace")
        return content
    except (LookupError, UnicodeDecodeError, AssertionError, KeyError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def _addr(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    name, addr = email.utils.parseaddr(str(value))
    return name.strip().strip('"'), addr.strip().lower()


def parse_mail(raw: bytes) -> MailMessage:
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    sender_name, sender = _addr(msg.get("From"))
    mail = MailMessage(
        raw=raw,
        message_id=(str(msg.get("Message-ID") or "").strip() or None),
        subject=re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip(),
        sender=sender,
        sender_name=sender_name,
        to=[a for _, a in email.utils.getaddresses([str(v) for v in msg.get_all("To", [])]) if a],
        in_reply_to=(str(msg.get("In-Reply-To") or "").strip() or None),
    )
    try:
        date = email.utils.parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        mail.date = date
    except (TypeError, ValueError):
        mail.date = None

    plains: list[str] = []
    htmls: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else inner
            if hasattr(inner, "get") and not mail.original_sender:
                name, addr = _addr(inner.get("From"))
                if addr:
                    mail.original_sender, mail.original_sender_name = addr, name
            continue
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        if ctype in ("text/calendar", "application/ics") or filename.lower().endswith(".ics"):
            mail.calendars.append(_part_text(part))
        elif ctype == "text/plain" and disposition != "attachment":
            plains.append(_part_text(part))
        elif ctype == "text/html" and disposition != "attachment":
            htmls.append(_part_text(part))
        else:
            payload = part.get_payload(decode=True) or b""
            mail.attachments.append({"filename": filename, "content_type": ctype, "size": len(payload)})

    plain = "\n\n".join(p.strip() for p in plains if p.strip())
    html_text = "\n\n".join(html_to_text(h) for h in htmls if h.strip())
    # Prefer text/plain unless it is a stub ("view this message in HTML") next to a rich HTML part.
    if plain and (not html_text or len(plain) >= 0.4 * len(html_text)):
        mail.text = _collapse(plain)
    else:
        mail.text = html_text or _collapse(plain)

    if not mail.original_sender:
        _detect_inline_forward(mail)
    return mail


def _detect_inline_forward(mail: MailMessage) -> None:
    marker = _FWD_MARKER.search(mail.text)
    if not marker and not re.match(r"^\s*(fwd?|fw):", mail.subject, re.I):
        return
    region = mail.text[marker.start():] if marker else mail.text
    match = _FWD_FROM.search(region)
    if match:
        name, addr = _addr(match.group(1).replace("[mailto:", "<").replace("]", ">"))
        if addr and "@" in addr:
            mail.original_sender, mail.original_sender_name = addr, name


def sender_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


_TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.jp", "co.nz", "com.br", "co.za", "com.sg"}


def organisation_from_domain(domain: str) -> str:
    """'notifications.lumen.com' -> 'Lumen', 'noc.colt.net' -> 'Colt', 'bt.co.uk' -> 'Bt'."""
    labels = [label for label in domain.lower().split(".") if label]
    if len(labels) < 2:
        return domain
    if ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES and len(labels) >= 3:
        org = labels[-3]
    else:
        org = labels[-2]
    return org.upper() if len(org) <= 3 else org.capitalize()
