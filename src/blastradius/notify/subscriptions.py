"""Subscriptions: who wants to hear about what, through which channels.

Three sources, one table: users (web/API), `subscriptions:` in config ("subscriptions as code"),
and `applications[].notify` in the inventory (app owners are subscribed to their own app).
Every subscription has a secret token that doubles as its Atom/iCalendar feed URL.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from ..config import Config
from ..db import Database
from ..topology.inventory import Inventory
from ..util import dumps, iso, loads, norm, severity_from_name, utcnow

EVENTS = ("new", "updated", "rescheduled", "cancelled", "completed", "reminder", "started", "impact_changed", "collision")
DEFAULT_EVENTS = ["new", "updated", "rescheduled", "cancelled", "reminder", "impact_changed", "collision"]
FILTER_KEYS = ("applications", "owners", "providers", "circuits", "resources", "accounts", "regions",
               "message_types", "min_severity", "include_unmapped")
CHANNEL_TYPES = ("slack", "teams", "webhook", "email", "sns")


class SubscriptionError(ValueError):
    pass


def validate(data: dict) -> dict:
    """Normalise a subscription payload from the API / config."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise SubscriptionError("name is required")
    flt = dict(data.get("filter") or {})
    unknown = set(flt) - set(FILTER_KEYS)
    if unknown:
        raise SubscriptionError(f"unknown filter keys: {sorted(unknown)}")
    for key in FILTER_KEYS:
        if key in ("min_severity", "include_unmapped"):
            continue
        value = flt.get(key)
        if value is None:
            flt.pop(key, None)
            continue
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        flt[key] = [str(v) for v in value]
        if not flt[key]:
            flt.pop(key)
    if "min_severity" in flt and flt["min_severity"] not in (None, ""):
        if severity_from_name(flt["min_severity"], -1) < 0:
            raise SubscriptionError("min_severity must be AT_RISK, DEGRADED or OUTAGE")
    else:
        flt.pop("min_severity", None)
    flt["include_unmapped"] = bool(flt.get("include_unmapped", False))
    events = data.get("events") or DEFAULT_EVENTS
    if isinstance(events, str):
        events = [e.strip() for e in events.split(",") if e.strip()]
    bad = set(events) - set(EVENTS)
    if bad:
        raise SubscriptionError(f"unknown events: {sorted(bad)}")
    channels = []
    for i, ch in enumerate(data.get("channels") or []):
        ctype = ch.get("type")
        if ctype not in CHANNEL_TYPES:
            raise SubscriptionError(f"channels[{i}]: type must be one of {CHANNEL_TYPES}")
        if ctype in ("slack", "teams", "webhook") and not str(ch.get("url", "")).startswith(("https://", "http://")):
            raise SubscriptionError(f"channels[{i}]: {ctype} needs an http(s) url")
        if ctype == "email":
            to = ch.get("to")
            to = [to] if isinstance(to, str) else list(to or [])
            if not to or not all("@" in t for t in to):
                raise SubscriptionError(f"channels[{i}]: email needs 'to' addresses")
            ch = {**ch, "to": to}
        if ctype == "sns" and not str(ch.get("topic_arn", "")).startswith("arn:aws"):
            raise SubscriptionError(f"channels[{i}]: sns needs topic_arn")
        channels.append(dict(ch))
    return {"name": name, "description": str(data.get("description") or ""), "filter": flt,
            "channels": channels, "events": list(dict.fromkeys(events)), "enabled": bool(data.get("enabled", True))}


def secret_key(cfg: Config, db: Database) -> str:
    if cfg.secret_key:
        return cfg.secret_key
    key = db.kv_get("secret_key")
    if not key:
        key = secrets.token_urlsafe(32)
        db.kv_set("secret_key", key)
    return key


def derived_token(cfg: Config, db: Database, managed_key: str) -> str:
    return hmac.new(secret_key(cfg, db).encode(), managed_key.encode(), hashlib.sha256).hexdigest()[:32]


def create(db: Database, data: dict, owner: str | None) -> int:
    sub = validate(data)
    now = iso(utcnow())
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO subscriptions (name, owner, description, filter, channels, events, token, enabled, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sub["name"], owner, sub["description"], dumps(sub["filter"]), dumps(sub["channels"]), dumps(sub["events"]),
             secrets.token_urlsafe(24), int(sub["enabled"]), now, now),
        )
        return cur.lastrowid


def update(db: Database, sub_id: int, data: dict) -> None:
    sub = validate(data)
    db.execute(
        "UPDATE subscriptions SET name = ?, description = ?, filter = ?, channels = ?, events = ?, enabled = ?,"
        " updated_at = ? WHERE id = ? AND managed_key IS NULL",
        (sub["name"], sub["description"], dumps(sub["filter"]), dumps(sub["channels"]), dumps(sub["events"]),
         int(sub["enabled"]), iso(utcnow()), sub_id),
    )


def delete(db: Database, sub_id: int) -> None:
    db.execute("DELETE FROM subscriptions WHERE id = ? AND managed_key IS NULL", (sub_id,))


def sync_managed(db: Database, cfg: Config, inventory: Inventory) -> None:
    """Upsert config- and inventory-defined subscriptions; disable ones that were removed."""
    desired: dict[str, dict] = {}
    for raw in cfg.subscriptions:
        sub = validate(raw)
        sub["token"] = raw.get("token")
        desired[f"config:{sub['name']}"] = sub
    for app in inventory.applications:
        if app.notify:
            desired[f"app:{app.name}"] = validate({
                "name": f"{app.name} owners", "description": f"Owners of {app.name} (inventory)",
                "filter": {"applications": [app.name]}, "channels": app.notify,
            })
    now = iso(utcnow())
    with db.tx() as conn:
        existing = {r["managed_key"]: r for r in conn.execute(
            "SELECT id, managed_key, token FROM subscriptions WHERE managed_key IS NOT NULL").fetchall()}
        for managed_key, sub in desired.items():
            token = sub.get("token") or derived_token(cfg, db, managed_key)
            values = (sub["name"], sub["description"], dumps(sub["filter"]), dumps(sub["channels"]),
                      dumps(sub["events"]), int(sub["enabled"]), now)
            if managed_key in existing:
                conn.execute("UPDATE subscriptions SET name = ?, description = ?, filter = ?, channels = ?, events = ?,"
                             " enabled = ?, updated_at = ?, token = ? WHERE managed_key = ?",
                             (*values, token, managed_key))
            else:
                conn.execute("INSERT INTO subscriptions (name, description, filter, channels, events, enabled, updated_at,"
                             " token, managed_key, owner, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (*values, token, managed_key, managed_key.split(":", 1)[0], now))
        for managed_key in set(existing) - set(desired):
            conn.execute("UPDATE subscriptions SET enabled = 0, updated_at = ? WHERE managed_key = ?", (now, managed_key))


def to_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "owner": row["owner"], "description": row["description"],
        "filter": loads(row["filter"], {}), "channels": loads(row["channels"], []), "events": loads(row["events"], []),
        "token": row["token"], "managed": row["managed_key"], "enabled": bool(row["enabled"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def enabled(db: Database) -> list[dict]:
    return [to_dict(r) for r in db.query("SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY id")]


def by_token(db: Database, token: str) -> dict | None:
    row = db.one("SELECT * FROM subscriptions WHERE token = ?", (token,))
    return to_dict(row) if row else None


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _any(wanted: list[str], have: set[str]) -> bool:
    wanted_norm = {norm(w) for w in wanted}
    return bool(wanted_norm & {norm(h) for h in have if h})


def matches(flt: dict, notice: dict[str, Any], blast: dict | None, default_min: str = "AT_RISK") -> bool:
    """`notice` is notices.to_dict(row); `blast` the stored report (may be None)."""
    blast = blast or {}
    mapped = bool(blast.get("mapped"))
    if not mapped and not flt.get("include_unmapped"):
        return False
    if mapped:
        if notice.get("severity", 0) < severity_from_name(flt.get("min_severity") or default_min, 1):
            return False
    apps = {a["name"] for a in blast.get("apps", [])}
    if flt.get("applications") and not _any(flt["applications"], apps):
        return False
    if flt.get("owners"):
        owners = {o for a in blast.get("apps", []) for o in a.get("owners") or []}
        if not _any(flt["owners"], owners):
            return False
    if flt.get("providers") and not _any(flt["providers"], {notice.get("provider") or ""}):
        return False
    if flt.get("message_types") and notice.get("message_type") not in flt["message_types"]:
        return False
    nodes = blast.get("nodes", [])
    if flt.get("circuits"):
        circuits = {c["id"] for c in notice.get("circuits", [])} | {n["id"] for n in nodes if n["type"] == "circuit"}
        if not _any(flt["circuits"], circuits):
            return False
    if flt.get("resources"):
        if not _any(flt["resources"], {n["id"] for n in nodes} | {n["key"] for n in nodes}):
            return False
    if flt.get("accounts"):
        accounts = {n.get("account") for n in nodes} | {n.get("account_name") for n in nodes}
        if not _any(flt["accounts"], {a for a in accounts if a}):
            return False
    if flt.get("regions"):
        if not _any(flt["regions"], {n.get("region") for n in nodes if n.get("region")}):
            return False
    return True
