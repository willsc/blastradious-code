"""Drop-directory source: *.eml files are ingested then moved to processed/ or failed/.

Useful for fetchmail/procmail/postfix pipes, SES-to-S3 sync jobs, or replaying saved mails.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config

log = logging.getLogger(__name__)


class DirectorySource:
    def __init__(self, cfg: Config):
        self.root = cfg.path(cfg.directory.path) or Path("inbox")

    def poll(self, pipeline) -> dict:
        stats = {"new": 0, "processed": 0, "ignored": 0, "duplicate": 0, "error": 0}
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        files = sorted((p for p in self.root.iterdir() if p.is_file() and p.suffix.lower() == ".eml"),
                       key=lambda p: p.stat().st_mtime)
        for path in files:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                continue
            result = pipeline.ingest(raw, source="directory")
            stats["new"] += 1
            stats[result.status] = stats.get(result.status, 0) + 1
            dest_dir = self.root / ("failed" if result.status == "error" else "processed")
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / path.name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{path.stem}.{n}{path.suffix}"
                n += 1
            path.rename(dest)
        return stats
