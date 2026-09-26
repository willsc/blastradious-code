"""Parser orchestration: structured parsers first, heuristics always, merged into ParsedNotices."""

from __future__ import annotations

import logging
import re

from ..classify import provider_hint
from ..config import Config, MessageType
from ..mailparse import MailMessage, organisation_from_domain
from . import aws_health, bcop, generic, ntc
from .model import CircuitRef, ParsedNotice

log = logging.getLogger(__name__)

PARSERS = {
    "bcop": bcop.parse,
    "ntc": ntc.parse,
    "aws_health": aws_health.parse,
    "generic": generic.parse,
}

__all__ = ["parse_message", "ParsedNotice", "CircuitRef"]


def _canonical_provider(value: str | None, fallback: str | None) -> str | None:
    if not value:
        return fallback
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):  # "zayo.com" -> "Zayo"
        return organisation_from_domain(value)
    return value


def parse_message(mail: MailMessage, mtype: MessageType, cfg: Config) -> list[ParsedNotice]:
    hint = provider_hint(mail, cfg)
    fallback = generic.parse(mail, hint, cfg)[0]
    primary: list[ParsedNotice] = []
    partial: list[ParsedNotice] = []
    for name in mtype.parsers:
        if name == "generic":
            continue
        try:
            results = PARSERS[name](mail, hint, cfg)
        except Exception as exc:  # a broken parser must never lose the mail
            log.exception("%s parser failed on %r", name, mail.subject)
            fallback.notes.append(f"{name} parser failed: {exc}")
            continue
        for result in results:
            if result.structured or result.parser == "aws_health":
                primary.append(result)
            else:
                partial.append(result)
        if primary:
            break

    if primary:
        exclusive = len(primary) > 1
        for result in primary:
            result.fill_from(fallback)
            result.provider = hint.name if hint else _canonical_provider(result.provider, fallback.provider)
            result.notes.extend(n for n in fallback.notes if n not in result.notes)
            result.exclusive_ids = exclusive
        return primary

    notice = fallback
    for invite in partial:  # plain calendar invite: trust its window over text heuristics
        if invite.start:
            notice.start, notice.end, notice.windows = invite.start, invite.end, invite.windows
            notice.parser = "generic+ical"
            notice.confidence = "medium"
        if invite.status == "CANCELLED":
            notice.status = "CANCELLED"
        notice.summary = notice.summary or invite.summary
        break
    return [notice]
