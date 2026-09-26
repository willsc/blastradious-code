"""Self-contained demo: fictional AWS estate + inventory + a week of carrier/AWS notices.

Email templates carry relative dates ({{+3d@01:00|%Y%m%dT%H%M%SZ}}, {{-2h|...}}) so the demo is
always "live": windows are upcoming, one is in progress, and two overlap into a collision.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from ..context import Context
from ..notify import subscriptions
from ..topology.inventory import load_inventory
from ..util import iso, utcnow
from .aws_raw import aws_raw

HERE = Path(__file__).parent
EMAIL_DIR = HERE / "emails"
INVENTORY = HERE / "inventory.yaml"
_PLACEHOLDER = re.compile(r"\{\{([+-]\d+)([dhm])(?:@(\d{2}):(\d{2}))?(?:\|([^}]*))?\}\}")


def render_template(text: str, now: dt.datetime) -> str:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def repl(m: re.Match) -> str:
        n, unit, hh, mm, fmt = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5) or "%Y-%m-%d %H:%M"
        if unit == "d" and hh is not None:
            value = today + dt.timedelta(days=n, hours=int(hh), minutes=int(mm))
        elif unit == "d":
            value = now.replace(second=0, microsecond=0) + dt.timedelta(days=n)
        elif unit == "h":
            value = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=n)
        else:
            value = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=n)
        return value.strftime(fmt)

    return _PLACEHOLDER.sub(repl, text)


def config_overrides(database: str | Path) -> dict:
    return {
        "database": str(database),
        "inventory": str(INVENTORY),
        "display_timezone": "Europe/London",
        "providers": [
            {"name": "Colt", "senders": ["@colt.net"], "dayfirst": True},
            {"name": "Zayo", "senders": ["@zayo.com"]},
        ],
        "subscriptions": [
            {"name": "NOC - everything", "description": "Every notice, including ones we could not map",
             "filter": {"include_unmapped": True, "min_severity": "AT_RISK"}, "channels": []},
            {"name": "Payments on-call", "description": "payments-api at DEGRADED or worse",
             "filter": {"applications": ["payments-api"], "min_severity": "DEGRADED"}, "channels": []},
        ],
    }


def email_files() -> list[Path]:
    return sorted(EMAIL_DIR.glob("*.eml"))


def seed(ctx: Context, now: dt.datetime | None = None) -> list[tuple[str, object]]:
    now = now or utcnow()
    ctx.store.refresh(aws_raw=aws_raw(iso(now)))
    subscriptions.sync_managed(ctx.db, ctx.cfg, load_inventory(ctx.cfg.path(ctx.cfg.inventory)))
    results = []
    for path in email_files():
        raw = render_template(path.read_text(), now).encode()
        results.append((path.name, ctx.pipeline.ingest(raw, source="demo", source_id=path.name)))
    return results
