"""Message-type classification ("which kind of mail is this?") and provider-hint lookup."""

from __future__ import annotations

import re

from .config import Config, MessageType, ProviderHint
from .mailparse import MailMessage


def sender_matches(patterns: list[str], senders: list[str]) -> bool:
    for pattern in patterns:
        for sender in senders:
            if pattern.startswith("re:"):
                if re.search(pattern[3:], sender, re.I):
                    return True
            elif pattern.lower() in sender.lower():
                return True
    return False


def matches(mtype: MessageType, mail: MailMessage) -> bool:
    rule = mtype.match
    if rule.senders and not sender_matches(rule.senders, mail.senders):
        return False
    if rule.subject and not re.search(rule.subject, mail.subject):
        return False
    if rule.not_subject and re.search(rule.not_subject, mail.subject):
        return False
    if rule.body and not re.search(rule.body, mail.subject + "\n" + mail.text):
        return False
    return rule.has_calendar is None or bool(mail.calendars) == rule.has_calendar


def classify(mail: MailMessage, cfg: Config, preferred: str | None = None) -> MessageType | None:
    """First matching type in config order; the type whose Gmail query found the mail is tried first."""
    ordered = list(cfg.message_types)
    if preferred:
        ordered.sort(key=lambda t: t.name != preferred)
    for mtype in ordered:
        if matches(mtype, mail):
            return mtype
    return None


def provider_hint(mail: MailMessage, cfg: Config) -> ProviderHint | None:
    for hint in cfg.providers:
        if hint.senders and sender_matches(hint.senders, mail.senders):
            return hint
    return None
