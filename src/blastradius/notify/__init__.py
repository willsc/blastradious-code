"""Fan-out of notice events to subscriptions, with per-channel dedupe and retry."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

from .. import notices as notices_mod
from ..config import Config
from ..db import Database
from ..util import dumps, iso, loads, severity_from_name, utcnow
from . import channels, render, subscriptions

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config, db: Database, sender: Callable = channels.send):
        self.cfg = cfg
        self.db = db
        self._send = sender

    # -- events ------------------------------------------------------------

    def notice_event(self, notice_id: int, event: str, changes: dict | None = None, note: str = "",
                     dedupe_key: str | None = None) -> int:
        """Fan out one notice event. The default dedupe key is per notice version, so the same
        event is never delivered twice to a channel; reminders pass their own key."""
        row = notices_mod.get(self.db, notice_id)
        if row is None:
            return 0
        notice = notices_mod.to_dict(row)
        blast = loads(row["blast"], None)
        dedupe = dedupe_key or f"notice:{notice_id}:{event}:v{row['version']}"
        rendered = None
        sent = 0
        for sub in subscriptions.enabled(self.db):
            if event not in sub["events"] or not sub["channels"]:
                continue
            if not subscriptions.matches(sub["filter"], notice, blast, self.cfg.notifications.default_min_severity):
                continue
            rendered = rendered or render.render_notice(self.cfg, notice, blast, event, changes, note)
            sent += self._deliver(sub, dedupe, rendered, event, notice_id=notice_id)
        return sent

    def collision_event(self, collision: dict) -> int:
        rows = [notices_mod.get(self.db, nid) for nid in collision["notice_ids"]]
        rows = [r for r in rows if r is not None]
        dedupe = f"collision:{collision['id']}:{collision['start']}"
        rendered = None
        sent = 0
        for sub in subscriptions.enabled(self.db):
            if "collision" not in sub["events"] or not sub["channels"]:
                continue
            flt = sub["filter"]
            if collision["severity"] < severity_from_name(flt.get("min_severity")
                                                          or self.cfg.notifications.default_min_severity, 1):
                continue
            relaxed = {**flt, "min_severity": "AT_RISK"}
            if not any(subscriptions.matches(relaxed, notices_mod.to_dict(r), loads(r["blast"], None)) for r in rows):
                continue
            rendered = rendered or render.render_collision(self.cfg, collision)
            sent += self._deliver(sub, dedupe, rendered, "collision", collision_id=collision["id"])
        return sent

    # -- delivery ----------------------------------------------------------

    def _deliver(self, sub: dict, dedupe: str, rendered: render.Rendered, event: str, *,
                 notice_id: int | None = None, collision_id: str | None = None) -> int:
        sent = 0
        now = iso(utcnow())
        for index, channel in enumerate(sub["channels"]):
            cur = self.db.execute(
                "INSERT OR IGNORE INTO deliveries (subscription_id, notice_id, collision_id, event, dedupe_key,"
                " channel_index, channel_type, target, status, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (sub["id"], notice_id, collision_id, event, dedupe, index, channel.get("type"),
                 channels.describe_target(channel), dumps(rendered.as_dict()), now, now),
            )
            if cur.rowcount == 0:
                continue  # already delivered (or queued) for this subscription + event + version
            if self._attempt(cur.lastrowid, channel, rendered, attempts=0):
                sent += 1
        return sent

    def _attempt(self, delivery_id: int, channel: dict, rendered: render.Rendered, attempts: int) -> bool:
        attempts += 1
        now = utcnow()
        try:
            result = self._send(channel, rendered, self.cfg, subscriptions.secret_key(self.cfg, self.db))
        except Exception as exc:
            final = attempts >= self.cfg.notifications.max_attempts or isinstance(exc, KeyError)
            delay = dt.timedelta(seconds=60 * (2 ** (attempts - 1)))
            self.db.execute(
                "UPDATE deliveries SET status = ?, attempts = ?, error = ?, updated_at = ?, next_attempt_at = ?"
                " WHERE id = ?",
                ("failed" if final else "retry", attempts, str(exc)[:500], iso(now),
                 None if final else iso(now + delay), delivery_id),
            )
            log.warning("delivery %s via %s failed (attempt %d): %s", delivery_id, channel.get("type"), attempts, exc)
            return False
        self.db.execute(
            "UPDATE deliveries SET status = 'sent', attempts = ?, error = ?, updated_at = ?, next_attempt_at = NULL"
            " WHERE id = ?",
            (attempts, str(result)[:200], iso(now), delivery_id),
        )
        return True

    def retry_due(self) -> int:
        now = iso(utcnow())
        rows = self.db.query(
            "SELECT d.*, s.channels, s.enabled FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id"
            " WHERE d.status = 'retry' AND d.next_attempt_at <= ? ORDER BY d.id LIMIT 200",
            (now,),
        )
        done = 0
        for row in rows:
            chans = loads(row["channels"], [])
            if not row["enabled"] or row["channel_index"] >= len(chans):
                self.db.execute("UPDATE deliveries SET status = 'skipped', updated_at = ? WHERE id = ?", (now, row["id"]))
                continue
            rendered = render.Rendered.from_dict(loads(row["payload"]))
            if self._attempt(row["id"], chans[row["channel_index"]], rendered, row["attempts"]):
                done += 1
        return done
