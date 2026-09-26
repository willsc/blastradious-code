"""Notice lifecycle: identity, merge of follow-up mails (reminders, reschedules, cancellations),
stored blast radius, phases and queries."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field

from .db import Database
from .parsing.generic import clean_subject
from .parsing.model import ParsedNotice
from .util import dumps, iso, loads, norm, parse_iso, short_hash, utcnow

_STATUS_WORDS = re.compile(
    r"\b(reminder|update[d]?|cancel+ed|cancellation|completed?|re-?scheduled?|postponed|amended|revised|"
    r"confirmed|tentative|notification|notice|final|urgent|emergency|advance)\b|[\[\]():]",
    re.I,
)


def notice_key(parsed: ParsedNotice, thread_id: str | None) -> str:
    provider = norm(parsed.provider or "unknown") or "unknown"
    if parsed.maintenance_id:
        return f"{provider}:{norm(parsed.maintenance_id)}"
    if parsed.uid:
        return f"{provider}:uid:{norm(parsed.uid)}"
    circuits = ",".join(sorted(norm(c.id) for c in parsed.circuits))
    if circuits and parsed.start:
        return f"{provider}:c:{short_hash(circuits, parsed.start.date().isoformat())}"
    if thread_id:
        return f"{provider}:thread:{thread_id}"
    subject = norm(_STATUS_WORDS.sub(" ", clean_subject(parsed.summary or "")))
    return f"{provider}:s:{short_hash(subject, circuits)}"


@dataclass
class UpsertResult:
    notice_id: int
    created: bool
    events: list[str] = field(default_factory=list)
    changes: dict = field(default_factory=dict)
    version: int = 1


def phase(row, now: dt.datetime | None = None, open_hours: int = 72) -> str:
    """upcoming | in_progress | completed | cancelled | unscheduled"""
    now = now or utcnow()
    status = (row["status"] or "").upper()
    if status == "CANCELLED":
        return "cancelled"
    if status == "COMPLETED":
        return "completed"
    start, end = parse_iso(row["start_at"]), parse_iso(row["end_at"])
    if start is None:
        return "unscheduled"
    if end is None:  # open-ended outage: in progress until resolved or it goes stale
        last = parse_iso(row["updated_at"]) or start
        if now < start:
            return "upcoming"
        return "in_progress" if now - last < dt.timedelta(hours=open_hours) else "completed"
    if now < start:
        return "upcoming"
    if now < end:
        return "in_progress"
    return "completed"


def _fields(parsed: ParsedNotice, mtype_name: str, kind: str) -> dict:
    identifiers = dict(parsed.identifiers)
    identifiers["declared"] = list(parsed.declared_resources)
    identifiers["exclusive"] = parsed.exclusive_ids
    if parsed.notes:
        identifiers["notes"] = list(parsed.notes)
    return {
        "message_type": mtype_name,
        "kind": kind,
        "provider": parsed.provider,
        "account": parsed.account,
        "maintenance_id": parsed.maintenance_id,
        "title": parsed.summary or (f"{parsed.provider} maintenance" if parsed.provider else "Maintenance notice"),
        "summary": parsed.summary,
        "status": parsed.status,
        "impact": parsed.impact,
        "start_at": iso(parsed.start),
        "end_at": iso(parsed.end),
        "windows": dumps(parsed.windows),
        "circuits": dumps([asdict(c) for c in parsed.circuits]),
        "identifiers": dumps(identifiers),
        "sequence": parsed.sequence,
        "emergency": int(parsed.emergency),
        "parser": parsed.parser,
        "confidence": parsed.confidence,
    }


_TRACKED = ("status", "impact", "start_at", "end_at", "circuits", "provider", "maintenance_id")
_CONFIDENCE = {"low": 1, "medium": 2, "high": 3}


def upsert_notice(db: Database, parsed: ParsedNotice, *, mtype_name: str, kind: str, message_id: int,
                  mail_date: dt.datetime | None, thread_id: str | None = None) -> UpsertResult:
    key = notice_key(parsed, thread_id)
    now = iso(utcnow())
    fields = _fields(parsed, mtype_name, kind)
    mail_at = iso(mail_date) or now
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM notices WHERE key = ?", (key,)).fetchone()
        if row is None:
            fields["status"] = fields["status"] or "CONFIRMED"
            columns = ["key", *fields, "first_seen", "updated_at", "last_message_at"]
            values = [key, *fields.values(), now, now, mail_at]
            cur = conn.execute(
                f"INSERT INTO notices ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", values
            )
            notice_id = cur.lastrowid
            _link(conn, notice_id, message_id, "initial")
            _event(conn, notice_id, "created", {"message_id": message_id, "parser": parsed.parser})
            return UpsertResult(notice_id, True, ["new"], {}, 1)

        notice_id = row["id"]
        if parsed.sequence is not None and row["sequence"] is not None and parsed.sequence < row["sequence"]:
            _link(conn, notice_id, message_id, "stale")
            _event(conn, notice_id, "stale-message", {"message_id": message_id, "sequence": parsed.sequence})
            return UpsertResult(notice_id, False, [], {}, row["version"])
        older_mail = row["last_message_at"] and mail_at < row["last_message_at"] and parsed.sequence is None
        # A heuristic parse of a follow-up mail must not override what a structured source (BCOP,
        # NTC, AWS Health) established — only a cancellation/completion or filling a gap.
        weaker = _CONFIDENCE.get(parsed.confidence, 0) < _CONFIDENCE.get(row["confidence"], 0)
        notes: list[str] = []

        changes: dict = {}
        for name in _TRACKED:
            new, old = fields[name], row[name]
            if new in (None, "", "[]"):
                continue  # a terse reminder must not erase what we already know
            if new == old:
                continue
            if old not in (None, "", "[]"):
                if older_mail:
                    continue  # out-of-order delivery: only fill gaps
                if weaker and not (name == "status" and new in ("CANCELLED", "COMPLETED")):
                    notes.append(f"kept {name} from the {row['parser']} parse (this mail: {new})")
                    continue
            changes[name] = {"from": old, "to": new}
        updates = {k: v["to"] for k, v in changes.items()}
        if not older_mail and not weaker:
            for name in ("title", "summary", "windows", "account", "parser", "confidence"):
                if fields[name] not in (None, "", "[]"):
                    updates.setdefault(name, fields[name])
        updates["identifiers"] = dumps(_merge_identifiers(loads(row["identifiers"], {}), loads(fields["identifiers"], {})))
        if parsed.sequence is not None:
            updates["sequence"] = max(parsed.sequence, row["sequence"] or 0)
        if parsed.emergency:
            updates["emergency"] = 1

        events: list[str] = []
        if "status" in changes and changes["status"]["to"] == "CANCELLED":
            events.append("cancelled")
        elif "status" in changes and changes["status"]["to"] == "COMPLETED":
            events.append("completed")
        elif "start_at" in changes or "end_at" in changes:
            events.append("rescheduled")
        elif {"impact", "circuits"} & changes.keys() or ("status" in changes and changes["status"]["from"] == "CANCELLED"):
            events.append("updated")
        version = row["version"] + (1 if events else 0)
        updates.update({"updated_at": now, "version": version})
        if not older_mail:
            updates["last_message_at"] = mail_at
        conn.execute(
            f"UPDATE notices SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
            [*updates.values(), notice_id],
        )
        _link(conn, notice_id, message_id, "update" if events else "reminder")
        detail = {"message_id": message_id, "changes": changes}
        if notes:
            detail["notes"] = notes
        _event(conn, notice_id, events[0] if events else "reminder", detail)
        return UpsertResult(notice_id, False, events, changes, version)


def _merge_identifiers(old: dict, new: dict) -> dict:
    out = dict(old)
    for key, value in new.items():
        if isinstance(value, list):
            out[key] = list(dict.fromkeys([*old.get(key, []), *value]))
        elif key not in out:
            out[key] = value
    return out


def _link(conn, notice_id: int, message_id: int, role: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO notice_messages (notice_id, message_id, role) VALUES (?, ?, ?)",
        (notice_id, message_id, role),
    )


def _event(conn, notice_id: int, kind: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO notice_events (notice_id, at, kind, detail) VALUES (?, ?, ?, ?)",
        (notice_id, iso(utcnow()), kind, dumps(detail)),
    )


def add_event(db: Database, notice_id: int, kind: str, detail: dict) -> None:
    with db.tx() as conn:
        _event(conn, notice_id, kind, detail)


def store_blast(db: Database, notice_id: int, report: dict, snapshot_id: int | None) -> dict:
    """Persist a blast-radius report; returns what changed versus the previous one."""
    with db.tx() as conn:
        row = conn.execute("SELECT severity, apps, mapped FROM notices WHERE id = ?", (notice_id,)).fetchone()
        old_apps = {a["name"]: a["severity"] for a in loads(row["apps"], [])} if row else {}
        new_apps = {a["name"]: a["severity"] for a in report["apps"]}
        conn.execute(
            "UPDATE notices SET severity = ?, infra_severity = ?, mapped = ?, apps = ?, blast = ?, correlated_at = ?,"
            " snapshot_id = ? WHERE id = ?",
            (report["severity"], report["infra_severity"], int(report["mapped"]),
             dumps([{"name": a["name"], "severity": a["severity"]} for a in report["apps"]]),
             dumps(report), iso(utcnow()), snapshot_id, notice_id),
        )
        conn.execute("DELETE FROM impacts WHERE notice_id = ?", (notice_id,))
        conn.executemany(
            "INSERT INTO impacts (notice_id, node_key, node_type, node_id, name, account, region, severity, anchor, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(notice_id, n["key"], n["type"], n["id"], n["name"], n["account"], n["region"], n["severity"],
              int(n["anchor"]), "; ".join(n["reasons"])[:1000]) for n in report["nodes"]],
        )
        conn.execute("DELETE FROM notice_apps WHERE notice_id = ?", (notice_id,))
        conn.executemany(
            "INSERT INTO notice_apps (notice_id, app, severity) VALUES (?, ?, ?)",
            [(notice_id, name, sev) for name, sev in new_apps.items()],
        )
    old_severity = row["severity"] if row else 0
    escalated_apps = sorted(a for a, s in new_apps.items() if s > old_apps.get(a, 0))
    return {
        "old_severity": old_severity,
        "new_severity": report["severity"],
        "added_apps": sorted(set(new_apps) - set(old_apps)),
        "removed_apps": sorted(set(old_apps) - set(new_apps)),
        "escalated_apps": escalated_apps,
        "escalated": report["severity"] > old_severity or bool(escalated_apps),
    }


def get(db: Database, notice_id: int):
    return db.one("SELECT * FROM notices WHERE id = ?", (notice_id,))


def messages_for(db: Database, notice_id: int) -> list:
    return db.query(
        "SELECT m.*, nm.role FROM messages m JOIN notice_messages nm ON nm.message_id = m.id"
        " WHERE nm.notice_id = ? ORDER BY m.received_at, m.id",
        (notice_id,),
    )


def events_for(db: Database, notice_id: int) -> list:
    return db.query("SELECT * FROM notice_events WHERE notice_id = ? ORDER BY at, id", (notice_id,))


def active(db: Database, now: dt.datetime | None = None, recent_days: int = 7) -> list:
    """Notices that are upcoming, in progress, unscheduled or finished within `recent_days`."""
    now = now or utcnow()
    horizon = iso(now - dt.timedelta(days=recent_days))
    return db.query(
        "SELECT * FROM notices WHERE (end_at IS NULL OR end_at >= ?) OR updated_at >= ? ORDER BY start_at",
        (horizon, horizon),
    )


def to_dict(row, now: dt.datetime | None = None, with_blast: bool = False) -> dict:
    skip = ("blast", "windows", "circuits", "identifiers", "apps")
    out = {k: row[k] for k in row.keys() if k not in skip}  # noqa: SIM118 - `in` on sqlite3.Row tests values
    out["windows"] = loads(row["windows"], [])
    out["circuits"] = loads(row["circuits"], [])
    out["identifiers"] = loads(row["identifiers"], {})
    out["apps"] = loads(row["apps"], [])
    out["phase"] = phase(row, now)
    if with_blast:
        out["blast"] = loads(row["blast"], None)
    return out
