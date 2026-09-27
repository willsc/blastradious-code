"""IMAP source: username + password (for Gmail, an app password) over TLS.

Works with any IMAP server. When the server is Gmail (it advertises X-GM-EXT-1), each message
type's `gmail_query` is run server-side with X-GM-RAW, messages are keyed by X-GM-MSGID (stable
across folders) and threaded by X-GM-THRID, and `processed_folder` acts as a Gmail label.
Elsewhere, every message since the watermark is fetched (optionally narrowed by `imap.search`)
and classification decides what is a notice. Mail is read with BODY.PEEK, so it is never marked
as read.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import logging
import re
import ssl
import time

from ..config import Config
from ..db import Database
from ..util import UTC, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID_RE = re.compile(rb"\bUID (\d+)")
_MSGID_RE = re.compile(rb"\bX-GM-MSGID (\d+)")
_THRID_RE = re.compile(rb"\bX-GM-THRID (\d+)")
_CHUNK = 500


class ImapError(RuntimeError):
    pass


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(value: dt.datetime) -> str:
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


def _ok(result: tuple, what: str) -> list:
    status, data = result
    if status != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        raise ImapError(f"IMAP {what} failed: {detail or status}")
    return data


class ImapSource:
    def __init__(self, cfg: Config, db: Database, connect=None):
        self.cfg = cfg
        self.db = db
        self._connect = connect or self._open

    # -- connection -------------------------------------------------------------

    def _open(self):
        c = self.cfg.imap
        if c.ssl:
            return imaplib.IMAP4_SSL(c.host, c.port, ssl_context=ssl.create_default_context(), timeout=c.timeout)
        conn = imaplib.IMAP4(c.host, c.port, timeout=c.timeout)
        if c.starttls:
            conn.starttls(ssl_context=ssl.create_default_context())
        return conn

    def login(self):
        c = self.cfg.imap
        conn = self._connect()
        password = c.password or ""
        if c.host.lower().endswith("gmail.com"):
            password = password.replace(" ", "")  # Google displays app passwords in groups of four
        try:
            conn.login(c.username, password)
        except imaplib.IMAP4.error as exc:
            hint = (" For Gmail, turn on 2-Step Verification and use an app password"
                    " (https://myaccount.google.com/apppasswords), not the account password.")
            raise ImapError(f"IMAP login as {c.username} failed: {exc}.{hint}") from exc
        return conn

    @staticmethod
    def is_gmail(conn) -> bool:
        return "X-GM-EXT-1" in {str(cap).upper() for cap in conn.capabilities}

    # -- searching ----------------------------------------------------------------

    def select(self, conn, folder: str) -> str:
        _ok(conn.select(quote_mailbox(folder), readonly=True), f"SELECT {folder}")
        _, validity = conn.response("UIDVALIDITY")
        return (validity[0] or b"0").decode() if validity and validity[0] else "0"

    def searches(self, gmail: bool, since: dt.datetime | None) -> list[tuple[str | None, list]]:
        """(message type or None, IMAP search arguments) pairs. For X-GM-RAW the query is the
        last argument and is sent as a literal, so quotes in Gmail queries need no escaping."""
        c = self.cfg.imap
        if gmail and c.use_gmail_queries:
            window = f"after:{int(since.timestamp())}" if since else f"newer_than:{c.backfill_days}d"
            out = [(t.name, ["X-GM-RAW", f"({t.gmail_query}) {window}"])
                   for t in self.cfg.message_types if t.gmail_query]
            if out:
                return out
        start = since or (utcnow() - dt.timedelta(days=c.backfill_days))
        criteria = ["SINCE", imap_date(start)]
        if c.search:
            criteria.append(c.search)
        return [(None, criteria)]

    def search(self, conn, args: list) -> list[int]:
        if args and args[0] == "X-GM-RAW":
            conn.literal = args[1].encode()
            data = _ok(conn.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW"), "SEARCH X-GM-RAW")
        else:
            data = _ok(conn.uid("SEARCH", *args), "SEARCH")
        return sorted({int(u) for chunk in data if chunk for u in chunk.split()})

    def identities(self, conn, folder: str, validity: str, uids: list[int], gmail: bool) -> dict:
        """uid -> (source_id, thread_id)."""
        if not gmail:
            return {uid: (f"{folder}:{validity}:{uid}", None) for uid in uids}
        out = {}
        for i in range(0, len(uids), _CHUNK):
            chunk = ",".join(str(u) for u in uids[i:i + _CHUNK])
            for item in _ok(conn.uid("FETCH", chunk, "(X-GM-MSGID X-GM-THRID)"), "FETCH X-GM-MSGID"):
                line = item[0] if isinstance(item, tuple) else item
                if not isinstance(line, bytes):
                    continue
                uid, msgid, thrid = _UID_RE.search(line), _MSGID_RE.search(line), _THRID_RE.search(line)
                if uid and msgid:
                    out[int(uid.group(1))] = (msgid.group(1).decode(), thrid.group(1).decode() if thrid else None)
        return out

    def fetch_raw(self, conn, uid: int) -> tuple[bytes, dt.datetime | None]:
        for item in _ok(conn.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])"), f"FETCH {uid}"):
            if isinstance(item, tuple) and len(item) == 2:
                internal = imaplib.Internaldate2tuple(item[0])
                received = dt.datetime.fromtimestamp(time.mktime(internal), UTC) if internal else None
                return item[1], received
        raise ImapError(f"FETCH {uid}: no message body returned")

    def mark_processed(self, conn, uid: int) -> None:
        folder = quote_mailbox(self.cfg.imap.processed_folder)
        status, _ = conn.uid("COPY", str(uid), folder)
        if status != "OK":
            conn.create(folder)  # missing: create it (a label on Gmail) and retry once
            _ok(conn.uid("COPY", str(uid), folder), f"COPY to {self.cfg.imap.processed_folder}")

    # -- polling --------------------------------------------------------------------

    def poll(self, pipeline) -> dict:
        c = self.cfg.imap
        started = utcnow()
        last = parse_iso(self.db.kv_get("imap.last_poll"))
        since = last - dt.timedelta(hours=c.overlap_hours) if last else None
        stats = {"listed": 0, "new": 0, "processed": 0, "ignored": 0, "duplicate": 0, "error": 0}
        truncated = False
        conn = self.login()
        try:
            gmail = self.is_gmail(conn)
            budget = c.max_messages_per_poll
            for folder in c.folders:
                validity = self.select(conn, folder)
                pending: dict[int, tuple[str, str | None, str | None]] = {}
                for type_name, args in self.searches(gmail, since):
                    uids = [u for u in self.search(conn, args) if u not in pending]
                    stats["listed"] += len(uids)
                    for uid, (source_id, thread_id) in self.identities(conn, folder, validity, uids, gmail).items():
                        if self.db.one("SELECT 1 FROM messages WHERE source = 'imap' AND source_id = ?", (source_id,)):
                            continue
                        pending[uid] = (source_id, thread_id, type_name)
                uids = sorted(pending)  # oldest first; a capped poll resumes where it stopped
                if len(uids) > budget:
                    truncated = True
                    uids = uids[:budget]
                budget -= len(uids)
                for uid in uids:
                    source_id, thread_id, type_name = pending[uid]
                    raw, received = self.fetch_raw(conn, uid)
                    result = pipeline.ingest(raw, source="imap", source_id=source_id, thread_id=thread_id,
                                             received_at=received, preferred_type=type_name)
                    stats["new"] += 1
                    stats[result.status] = stats.get(result.status, 0) + 1
                    if c.processed_folder and result.status == "processed":
                        try:
                            self.mark_processed(conn, uid)
                        except (ImapError, imaplib.IMAP4.error) as exc:
                            log.warning("could not copy uid %s to %s: %s", uid, c.processed_folder, exc)
        finally:
            try:
                conn.logout()
            except Exception as exc:  # noqa: BLE001 - the poll result matters more than a clean goodbye
                log.debug("imap logout failed: %s", exc)
        if not truncated:
            self.db.kv_set("imap.last_poll", iso(started))
        self.db.kv_set("imap.last_stats", stats | {"at": iso(utcnow())})
        if stats["new"]:
            log.info("imap poll: %s%s", stats, " (capped; continuing next poll)" if truncated else "")
        return stats
