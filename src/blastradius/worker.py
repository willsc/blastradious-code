"""The daemon loop: poll mail sources, refresh topology, send reminders, retry deliveries."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import traceback
from typing import Callable

from .context import Context
from .notify import subscriptions
from .sources.gmail import GmailSource
from .sources.maildir import DirectorySource
from .topology.inventory import load_inventory
from .util import human_duration, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 60
COLLISION_SECONDS = 300


class Worker:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.gmail = GmailSource(ctx.cfg, ctx.db) if ctx.cfg.gmail.enabled else None
        self.directory = DirectorySource(ctx.cfg) if ctx.cfg.directory.enabled else None

    # -- scheduling -----------------------------------------------------------

    def _safe(self, name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
            self.ctx.db.kv_set(f"worker.{name}.ok", iso(utcnow()))
        except Exception as exc:
            log.error("worker task %s failed: %s", name, exc)
            log.debug("%s", traceback.format_exc())
            self.ctx.db.kv_set(f"worker.{name}.error", {"at": iso(utcnow()), "error": str(exc)[:500]})

    def run(self, stop: threading.Event) -> None:
        log.info("worker started (gmail=%s, directory=%s, aws=%s)", bool(self.gmail), bool(self.directory),
                 self.cfg.aws.enabled)
        self._safe("startup", self.startup)
        due = {"gmail": 0.0, "directory": 0.0, "topology": self._first_topology_due(), "tick": 0.0,
               "collisions": 0.0}
        while not stop.is_set():
            now = time.monotonic()
            if self.gmail and now >= due["gmail"]:
                self._safe("gmail", lambda: self.gmail.poll(self.ctx.pipeline))
                due["gmail"] = time.monotonic() + self.cfg.gmail.poll_interval
            if self.directory and now >= due["directory"]:
                self._safe("directory", lambda: self.directory.poll(self.ctx.pipeline))
                due["directory"] = time.monotonic() + self.cfg.directory.poll_interval
            if self.cfg.aws.enabled and now >= due["topology"]:
                self._safe("topology", lambda: self.refresh_topology(collect_aws=True))
                due["topology"] = time.monotonic() + self.cfg.aws.refresh_interval
            if now >= due["tick"]:
                self._safe("tick", self.tick)
                due["tick"] = time.monotonic() + TICK_SECONDS
            if now >= due["collisions"]:
                self._safe("collisions", lambda: self.ctx.pipeline.refresh_collisions())
                due["collisions"] = time.monotonic() + COLLISION_SECONDS
            self.ctx.db.kv_set("worker.heartbeat", iso(utcnow()))
            stop.wait(1.0)
        log.info("worker stopped")

    def _first_topology_due(self) -> float:
        summary = self.ctx.store.summary()
        collected = parse_iso(summary.get("aws_collected_at"))
        if collected is None:
            return 0.0
        age = (utcnow() - collected).total_seconds()
        return time.monotonic() + max(0.0, self.cfg.aws.refresh_interval - age)

    # -- tasks ----------------------------------------------------------------

    def startup(self) -> None:
        topo = self.ctx.store.current()
        inventory = load_inventory(self.cfg.path(self.cfg.inventory))
        subscriptions.sync_managed(self.ctx.db, self.cfg, inventory)
        log.info("topology snapshot %s loaded (%d nodes)", topo.snapshot_id, len(topo.graph.nodes))

    def refresh_topology(self, collect_aws: bool) -> None:
        self.ctx.store.refresh(collect_aws=collect_aws)
        inventory = load_inventory(self.cfg.path(self.cfg.inventory))
        subscriptions.sync_managed(self.ctx.db, self.cfg, inventory)
        changed = self.ctx.pipeline.recorrelate()
        log.info("re-correlated active notices after topology refresh: %d changed", changed)

    def tick(self) -> None:
        if self.ctx.store.inventory_changed():
            log.info("inventory file changed; rebuilding topology from cached AWS data")
            self.refresh_topology(collect_aws=False)
        self.reminders()
        if self.cfg.notifications.notify_on_start:
            self.started()
        self.ctx.notifier.retry_due()

    def reminders(self, now: dt.datetime | None = None) -> int:
        leads = self.cfg.notifications.reminders
        if not leads:
            return 0
        now = now or utcnow()
        rows = self.ctx.db.query(
            "SELECT id, start_at FROM notices WHERE status NOT IN ('CANCELLED', 'COMPLETED')"
            " AND start_at > ? AND start_at <= ?",
            (iso(now), iso(now + dt.timedelta(seconds=max(leads)))),
        )
        sent = 0
        for row in rows:
            lead = (parse_iso(row["start_at"]) - now).total_seconds()
            crossed = [r for r in leads if lead <= r]
            if not crossed:
                continue
            threshold = min(crossed)
            sent += self.ctx.notifier.notice_event(
                row["id"], "reminder", note=f"Starts in {human_duration(lead)}.",
                dedupe_key=f"notice:{row['id']}:reminder:{threshold}:{row['start_at']}")
        return sent

    def started(self, now: dt.datetime | None = None) -> int:
        now = now or utcnow()
        rows = self.ctx.db.query(
            "SELECT id, start_at FROM notices WHERE status NOT IN ('CANCELLED', 'COMPLETED')"
            " AND start_at <= ? AND (end_at IS NULL OR end_at > ?) AND start_at > ?",
            (iso(now), iso(now), iso(now - dt.timedelta(hours=6))),
        )
        return sum(self.ctx.notifier.notice_event(r["id"], "started", note="Maintenance window has started.",
                                                  dedupe_key=f"notice:{r['id']}:started:{r['start_at']}")
                   for r in rows)
