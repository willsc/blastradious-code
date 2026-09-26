import datetime as dt

import pytest

from blastradius import demo
from blastradius.config import load_config
from blastradius.context import build_context
from blastradius.demo.aws_raw import aws_raw
from blastradius.util import UTC, iso

NOW = dt.datetime(2026, 9, 26, 14, 30, tzinfo=UTC)


@pytest.fixture
def cfg_overrides(tmp_path):
    def make(**extra):
        overrides = demo.config_overrides(tmp_path / "test.db")
        overrides.update(extra)
        return overrides
    return make


@pytest.fixture
def ctx(cfg_overrides):
    """Context with the demo topology loaded but no mail ingested."""
    context = build_context(load_config(None, cfg_overrides()))
    context.store.refresh(aws_raw=aws_raw(iso(NOW)))
    return context


@pytest.fixture
def seeded(cfg_overrides):
    """Context with the full demo (topology + all sample mails)."""
    context = build_context(load_config(None, cfg_overrides()))
    demo.seed(context)
    return context


def demo_mail(name: str, now: dt.datetime | None = None) -> bytes:
    path = demo.EMAIL_DIR / name
    return demo.render_template(path.read_text(), now or dt.datetime.now(UTC)).encode()


def mail(subject: str, body: str, sender: str = "noc@carrier.example", content_type: str = "text/plain",
         date: str = "Sat, 26 Sep 2026 10:00:00 +0000", extra_headers: str = "") -> bytes:
    return (
        f"From: Carrier NOC <{sender}>\r\nTo: noc@example.com\r\nSubject: {subject}\r\nDate: {date}\r\n"
        f"Message-ID: <{abs(hash(subject + body))}@carrier.example>\r\nMIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n{extra_headers}\r\n{body}"
    ).encode()
