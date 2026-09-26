"""Topology snapshots: build, persist, and serve the current graph to web + worker threads."""

from __future__ import annotations

import logging
import threading

from ..config import Config
from ..db import Database
from ..util import compress, decompress, dumps, iso, loads, utcnow
from . import aws
from .build import Topology, build_topology
from .index import IdentifierIndex
from .inventory import Inventory, load_inventory
from .model import Graph

log = logging.getLogger(__name__)

KEEP_SNAPSHOTS = 20


class TopologyStore:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._lock = threading.RLock()
        self._topo: Topology | None = None

    def _index(self, graph: Graph) -> IdentifierIndex:
        c = self.cfg.correlation
        return IdentifierIndex(graph, min_length=c.min_identifier_length, min_name_length=c.min_name_length,
                               match_names=c.match_names)

    def current(self) -> Topology:
        latest = self.db.scalar("SELECT max(id) FROM snapshots")
        with self._lock:
            if self._topo is not None and self._topo.snapshot_id == latest:
                return self._topo
            if latest is None:
                if self._topo is None:
                    self._topo = self.refresh(collect_aws=False)
                return self._topo
            self._topo = self._load(latest)
            return self._topo

    def _load(self, snapshot_id: int) -> Topology:
        row = self.db.one("SELECT id, created_at, graph, summary FROM snapshots WHERE id = ?", (snapshot_id,))
        graph = Graph.from_dict(loads(decompress(row["graph"])))
        summary = loads(row["summary"], {})
        inventory = Inventory(accounts=summary.get("account_names", {}), digest=summary.get("inventory_digest", ""))
        return Topology(graph=graph, index=self._index(graph), inventory=inventory,
                        warnings=summary.get("warnings", []), snapshot_id=row["id"], created_at=row["created_at"])

    def last_aws_raw(self) -> dict | None:
        row = self.db.one("SELECT aws_raw FROM snapshots WHERE aws_raw IS NOT NULL ORDER BY id DESC LIMIT 1")
        return loads(decompress(row["aws_raw"])) if row else None

    def summary(self) -> dict:
        row = self.db.one("SELECT id, created_at, source, summary FROM snapshots ORDER BY id DESC LIMIT 1")
        if not row:
            return {}
        out = loads(row["summary"], {})
        out.update({"id": row["id"], "created_at": row["created_at"], "source": row["source"]})
        return out

    def inventory_changed(self) -> bool:
        path = self.cfg.path(self.cfg.inventory)
        if not path or not path.exists():
            return False
        try:
            digest = load_inventory(path).digest
        except Exception:
            return False
        return digest != self.summary().get("inventory_digest")

    def refresh(self, collect_aws: bool = True, aws_raw: dict | None = None) -> Topology:
        """Rebuild the graph. AWS is only called when `collect_aws` and aws.enabled; otherwise the
        last collected raw data is reused so inventory edits apply without API calls."""
        inventory = load_inventory(self.cfg.path(self.cfg.inventory))
        if aws_raw is not None:
            raw, source = aws_raw, "provided"
        elif collect_aws and self.cfg.aws.enabled:
            raw, source = aws.collect(self.cfg.aws), "aws"
        else:
            raw = self.last_aws_raw()
            source = "cached-aws" if raw else "inventory-only"
        c = self.cfg.correlation
        topo = build_topology(raw, inventory, self.cfg.aws, min_identifier_length=c.min_identifier_length,
                              min_name_length=c.min_name_length, match_names=c.match_names)
        accounts = [
            {"name": a.get("name"), "account_id": a.get("account_id"), "regions": sorted(a.get("regions", {})),
             "errors": len(a.get("errors", []))}
            for a in (raw or {}).get("accounts", [])
        ]
        summary = {
            "counts": topo.graph.counts(),
            "warnings": topo.warnings[:300],
            "account_names": inventory.accounts,
            "inventory_digest": inventory.digest,
            "inventory_source": inventory.source,
            "aws_collected_at": (raw or {}).get("collected_at"),
            "accounts": accounts,
        }
        with self._lock:
            with self.db.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO snapshots (created_at, source, status, summary, aws_raw, graph, inventory_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (iso(utcnow()), source, "ok", dumps(summary), compress(dumps(raw)) if raw else None,
                     compress(dumps(topo.graph.to_dict())), inventory.digest),
                )
                topo.snapshot_id = cur.lastrowid
                conn.execute(
                    "DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)"
                    " AND id NOT IN (SELECT id FROM snapshots WHERE aws_raw IS NOT NULL ORDER BY id DESC LIMIT 1)",
                    (KEEP_SNAPSHOTS,),
                )
            topo.created_at = iso(utcnow())
            self._topo = topo
        log.info("topology snapshot %s (%s): %s", topo.snapshot_id, source,
                 ", ".join(f"{k}={v}" for k, v in summary["counts"].items()))
        for warning in topo.warnings[:20]:
            log.warning("topology: %s", warning)
        return topo
