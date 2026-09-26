"""Shared runtime objects for the web app, the worker and the CLI."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import Database
from .notify import Notifier
from .pipeline import Pipeline
from .topology.store import TopologyStore


@dataclass
class Context:
    cfg: Config
    db: Database
    store: TopologyStore
    notifier: Notifier
    pipeline: Pipeline


def build_context(cfg: Config, notifier: Notifier | None = None) -> Context:
    db = Database(cfg.path(cfg.database))
    store = TopologyStore(cfg, db)
    notifier = notifier or Notifier(cfg, db)
    return Context(cfg=cfg, db=db, store=store, notifier=notifier, pipeline=Pipeline(cfg, db, store, notifier))
