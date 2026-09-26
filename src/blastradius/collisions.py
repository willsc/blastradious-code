"""Maintenance collisions: overlapping windows whose *combined* impact is worse than either alone.

Classic case: two carriers schedule work on both legs of a redundant DX pair on the same night.
Each notice alone says "redundancy lost" (DEGRADED); together they are an outage. We sweep the
timeline, re-run the impact engine with the union of anchors for every set of simultaneously
active notices, and report nodes whose severity escalates.
"""

from __future__ import annotations

import datetime as dt
import logging

from .config import Config
from .db import Database
from .impact import Anchor, ImpactEngine, build_report
from .topology.build import Topology
from .topology.model import CONSUMER_TYPES
from .util import NONE, dumps, iso, loads, parse_iso, severity_name, short_hash, utcnow

log = logging.getLogger(__name__)
_ORDER = {t: i for i, t in enumerate(["app", "dxgw", "vgw", "dxlag", "vpn", "tgwatt", "tgwrtb", "rtb", "subnet",
                                      "instance", "eni"])}


def _candidates(db: Database, cfg: Config, topo: Topology, now: dt.datetime) -> list[dict]:
    horizon = now + dt.timedelta(days=cfg.correlation.collision_lookahead_days)
    open_span = dt.timedelta(hours=cfg.correlation.open_outage_hours)
    out = []
    rows = db.query(
        "SELECT id, title, provider, maintenance_id, start_at, end_at, blast FROM notices"
        " WHERE mapped = 1 AND start_at IS NOT NULL AND status NOT IN ('CANCELLED', 'COMPLETED')"
    )
    for row in rows:
        start = parse_iso(row["start_at"])
        end = parse_iso(row["end_at"]) or start + open_span
        if end <= now or start > horizon:
            continue
        blast = loads(row["blast"], None)
        if not blast:
            continue
        anchors = [Anchor(a["key"], a["severity"], a["reason"]) for a in blast.get("anchors", [])
                   if a["key"] in topo.graph.nodes]
        if not anchors:
            continue
        out.append({"id": row["id"], "title": row["title"], "provider": row["provider"], "start": start, "end": end,
                    "anchors": anchors, "sev": {n["key"]: n["severity"] for n in blast.get("nodes", [])}})
    return out


def compute(db: Database, cfg: Config, topo: Topology, now: dt.datetime | None = None) -> list[dict]:
    now = now or utcnow()
    candidates = _candidates(db, cfg, topo, now)
    by_id = {c["id"]: c for c in candidates}
    points = sorted({c["start"] for c in candidates} | {c["end"] for c in candidates})
    groups: dict[frozenset, list[tuple[dt.datetime, dt.datetime]]] = {}
    for a, b in zip(points, points[1:]):
        active = frozenset(c["id"] for c in candidates if c["start"] < b and c["end"] > a)
        if len(active) < 2:
            continue
        spans = groups.setdefault(active, [])
        if spans and spans[-1][1] == a:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))

    engine = ImpactEngine(topo.graph)
    results = []
    for ids, spans in groups.items():
        members = [by_id[i] for i in sorted(ids)]
        seen: dict[str, int] = {}
        shared = False
        for m in members:
            for key in m["sev"]:
                seen[key] = seen.get(key, 0) + 1
                shared = shared or seen[key] > 1
        if not shared:
            continue
        merged: dict[str, Anchor] = {}
        for m in members:
            for anchor in m["anchors"]:
                if anchor.key not in merged or anchor.severity > merged[anchor.key].severity:
                    merged[anchor.key] = anchor
        state = engine.run(list(merged.values()))
        escalations = []
        for key, impact in state.items():
            if impact.severity <= NONE:
                continue
            individually = max(m["sev"].get(key, NONE) for m in members)
            if impact.severity > individually:
                node = topo.graph.nodes[key]
                escalations.append({"key": key, "label": node.label, "type": node.type, "from": individually,
                                    "to": impact.severity, "reasons": impact.reasons,
                                    "prefixes": impact.prefixes})
        if not escalations:
            continue
        consumer = [e["to"] for e in escalations if e["type"] in CONSUMER_TYPES]
        severity = max(consumer) if consumer else max(e["to"] for e in escalations)
        apps = [{"name": topo.graph.nodes[e["key"]].id, "from": e["from"], "to": e["to"]}
                for e in escalations if e["type"] == "app"]
        report = build_report(topo.graph, state, list(merged.values()), account_names=topo.inventory.accounts)
        start, end = spans[0][0], spans[-1][1]
        app_text = ", ".join(f"{a['name']} ({severity_name(a['from'])}→{severity_name(a['to'])})" for a in apps)
        results.append({
            "id": short_hash(*sorted(ids)),
            "notice_ids": sorted(ids),
            "notices": [{"id": m["id"], "title": m["title"], "provider": m["provider"],
                         "start": iso(m["start"]), "end": iso(m["end"])} for m in members],
            "start": iso(start), "end": iso(end), "spans": [[iso(a), iso(b)] for a, b in spans],
            "severity": severity,
            "escalations": sorted(escalations, key=lambda e: (_ORDER.get(e["type"], 50), -e["to"], e["label"]))[:300],
            "apps": apps,
            "summary": (f"{len(members)} overlapping notices escalate to {severity_name(severity)}"
                        + (f": {app_text}" if app_text else "")),
            "report_nodes": len(report["nodes"]),
        })
    results.sort(key=lambda c: c["start"])
    return results


def refresh(db: Database, cfg: Config, topo: Topology, now: dt.datetime | None = None) -> list[dict]:
    """Recompute and persist; returns collisions that are new (or re-activated) since last time."""
    computed = compute(db, cfg, topo, now)
    stamp = iso(utcnow())
    fresh = []
    with db.tx() as conn:
        existing = {r["id"]: r["active"] for r in conn.execute("SELECT id, active FROM collisions").fetchall()}
        for c in computed:
            values = (dumps(c["notice_ids"]), c["start"], c["end"], c["severity"], dumps(c), stamp)
            if c["id"] in existing:
                conn.execute("UPDATE collisions SET notice_ids = ?, start_at = ?, end_at = ?, severity = ?, data = ?,"
                             " updated_at = ?, active = 1 WHERE id = ?", (*values, c["id"]))
                if not existing[c["id"]]:
                    fresh.append(c)
            else:
                conn.execute("INSERT INTO collisions (notice_ids, start_at, end_at, severity, data, updated_at, id,"
                             " first_seen, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)", (*values, c["id"], stamp))
                fresh.append(c)
        live = {c["id"] for c in computed}
        for cid, active in existing.items():
            if active and cid not in live:
                conn.execute("UPDATE collisions SET active = 0, updated_at = ? WHERE id = ?", (stamp, cid))
    for c in fresh:
        log.warning("collision %s: %s (%s - %s)", c["id"], c["summary"], c["start"], c["end"])
    return fresh


def active(db: Database) -> list[dict]:
    return [loads(r["data"]) | {"first_seen": r["first_seen"]}
            for r in db.query("SELECT data, first_seen FROM collisions WHERE active = 1 ORDER BY start_at")]


def for_notice(db: Database, notice_id: int) -> list[dict]:
    return [c for c in active(db) if notice_id in c["notice_ids"]]
