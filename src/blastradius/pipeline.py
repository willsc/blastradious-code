"""mail bytes -> message row -> classify -> parse -> notice upsert -> blast radius -> notifications."""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from dataclasses import dataclass, field

from . import collisions, notices
from .classify import classify
from .config import Config
from .db import Database
from .impact import CorrelationInput, blast_radius
from .mailparse import parse_mail
from .notify import Notifier
from .parsing import parse_message
from .topology.build import Topology
from .topology.store import TopologyStore
from .util import compress, decompress, iso, loads, parse_iso, short_hash, utcnow

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    status: str  # processed | ignored | duplicate | error
    message_id: int | None
    notice_ids: list[int] = field(default_factory=list)
    message_type: str | None = None
    error: str | None = None


def correlation_input(db: Database, cfg: Config, row) -> CorrelationInput:
    identifiers = loads(row["identifiers"], {})
    texts = []
    for m in db.query(
        "SELECT m.subject, m.body_text, m.calendar_text FROM messages m JOIN notice_messages nm ON nm.message_id = m.id"
        " WHERE nm.notice_id = ? AND nm.role != 'stale' ORDER BY m.id",
        (row["id"],),
    ):
        texts.append("\n".join(filter(None, [m["subject"], m["body_text"], m["calendar_text"]])))
    mtype = next((t for t in cfg.message_types if t.name == row["message_type"]), None)
    return CorrelationInput(
        circuits=loads(row["circuits"], []),
        declared=list(identifiers.get("declared", [])),
        texts=texts,
        impact=row["impact"],
        default_impact=mtype.default_impact if mtype else "OUTAGE",
        exclusive=bool(identifiers.get("exclusive")),
    )


def correlate_notice(db: Database, cfg: Config, topo: Topology, notice_id: int) -> tuple[dict, dict]:
    row = notices.get(db, notice_id)
    report = blast_radius(topo.graph, topo.index, correlation_input(db, cfg, row),
                          account_names=topo.inventory.accounts)
    diff = notices.store_blast(db, notice_id, report, topo.snapshot_id)
    return report, diff


class Pipeline:
    def __init__(self, cfg: Config, db: Database, store: TopologyStore, notifier: Notifier | None = None):
        self.cfg = cfg
        self.db = db
        self.store = store
        self.notifier = notifier

    def ingest(self, raw: bytes, *, source: str, source_id: str | None = None, thread_id: str | None = None,
               received_at: dt.datetime | None = None, preferred_type: str | None = None,
               notify: bool = True) -> IngestResult:
        source_id = source_id or short_hash(raw, length=32)
        existing = self.db.one("SELECT id FROM messages WHERE source = ? AND source_id = ?", (source, source_id))
        if existing:
            return IngestResult("duplicate", existing["id"])
        mail = parse_mail(raw)
        mtype = classify(mail, self.cfg, preferred_type)
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO messages (source, source_id, thread_id, rfc_message_id, received_at, sender, sender_name,"
                " original_sender, subject, message_type, status, body_text, calendar_text, raw, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (source, source_id, thread_id, mail.message_id, iso(mail.date or received_at or utcnow()),
                 mail.sender, mail.sender_name, mail.original_sender, mail.subject,
                 mtype.name if mtype else None, mail.text, "\n".join(mail.calendars) or None, compress(raw),
                 iso(utcnow())),
            )
            message_id = cur.lastrowid
        if mtype is None:
            self.db.execute("UPDATE messages SET status = 'ignored', processed_at = ? WHERE id = ?",
                            (iso(utcnow()), message_id))
            log.info("ignored %r from %s (no message type matched)", mail.subject, mail.sender)
            return IngestResult("ignored", message_id)
        try:
            topo = self.store.current()
            outcomes = []
            for parsed in parse_message(mail, mtype, self.cfg):
                upsert = notices.upsert_notice(
                    self.db, parsed, mtype_name=mtype.name, kind=mtype.kind, message_id=message_id,
                    mail_date=mail.date or received_at, thread_id=thread_id)
                _, diff = correlate_notice(self.db, self.cfg, topo, upsert.notice_id)
                outcomes.append((upsert, diff))
            self.db.execute("UPDATE messages SET status = 'processed', processed_at = ?, error = NULL WHERE id = ?",
                            (iso(utcnow()), message_id))
        except Exception as exc:
            log.exception("processing %r failed", mail.subject)
            self.db.execute("UPDATE messages SET status = 'error', error = ?, processed_at = ? WHERE id = ?",
                            (traceback.format_exc()[-4000:], iso(utcnow()), message_id))
            return IngestResult("error", message_id, [], mtype.name, str(exc))

        for upsert, diff in outcomes:
            log.info("notice %s %s (%s)", upsert.notice_id, "created" if upsert.created else
                     (",".join(upsert.events) or "unchanged"), mail.subject)
            if notify and self.notifier:
                for event in upsert.events:
                    self.notifier.notice_event(upsert.notice_id, event, changes=upsert.changes)
                if not upsert.created and not upsert.events and diff["escalated"]:
                    self._impact_changed(upsert.notice_id, diff, notify=True)
        self.refresh_collisions(notify=notify)
        return IngestResult("processed", message_id, [u.notice_id for u, _ in outcomes], mtype.name)

    # -- maintenance ---------------------------------------------------------

    def _impact_changed(self, notice_id: int, diff: dict, notify: bool) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE notices SET version = version + 1, updated_at = ? WHERE id = ?",
                         (iso(utcnow()), notice_id))
        notices.add_event(self.db, notice_id, "impact_changed", diff)
        if notify and self.notifier:
            apps = ", ".join(diff.get("escalated_apps") or diff.get("added_apps") or [])
            note = f"Blast radius grew after a topology/inventory change{': ' + apps if apps else ''}."
            self.notifier.notice_event(notice_id, "impact_changed", note=note)

    def refresh_collisions(self, notify: bool = True) -> list[dict]:
        fresh = collisions.refresh(self.db, self.cfg, self.store.current())
        if notify and self.notifier:
            for collision in fresh:
                self.notifier.collision_event(collision)
        return fresh

    def recorrelate(self, notify: bool = True, notice_ids: list[int] | None = None) -> int:
        """Re-run correlation for active notices against the current topology."""
        topo = self.store.current()
        rows = [notices.get(self.db, i) for i in notice_ids] if notice_ids else \
            notices.active(self.db, recent_days=self.cfg.correlation.recent_days)
        changed = 0
        for row in rows:
            if row is None:
                continue
            _, diff = correlate_notice(self.db, self.cfg, topo, row["id"])
            if diff["escalated"] or diff["removed_apps"] or diff["old_severity"] != diff["new_severity"]:
                changed += 1
            if diff["escalated"] and row["status"] not in ("CANCELLED", "COMPLETED"):
                self._impact_changed(row["id"], diff, notify)
        self.refresh_collisions(notify=notify)
        return changed

    def rebuild(self) -> int:
        """Drop all notices and replay every stored message (after parser/config changes). No notifications."""
        rows = self.db.query("SELECT id, source, source_id, thread_id, raw, received_at FROM messages ORDER BY received_at, id")
        with self.db.tx() as conn:
            conn.execute("DELETE FROM notices")
            conn.execute("DELETE FROM collisions")
            conn.execute("DELETE FROM messages")
        for row in rows:
            self.ingest(decompress(row["raw"]), source=row["source"], source_id=row["source_id"],
                        thread_id=row["thread_id"], received_at=parse_iso(row["received_at"]), notify=False)
        return len(rows)
