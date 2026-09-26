"""Directory source, the worker loop and the CLI entry point."""

import threading
import time

import yaml
from conftest import NOW, demo_mail

from blastradius import cli, demo
from blastradius.sources.maildir import DirectorySource
from blastradius.worker import Worker


def test_directory_source_moves_files(ctx, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.eml").write_bytes(demo_mail("03-bt-leased-line.eml", NOW))
    (inbox / "b.eml").write_bytes(demo_mail("09-zayo-newsletter.eml", NOW))
    (inbox / "notes.txt").write_text("ignored")
    ctx.cfg.directory.path = str(inbox)
    stats = DirectorySource(ctx.cfg).poll(ctx.pipeline)
    assert stats["new"] == 2 and stats["processed"] == 1 and stats["ignored"] == 1
    assert sorted(p.name for p in (inbox / "processed").iterdir()) == ["a.eml", "b.eml"]
    assert (inbox / "notes.txt").exists()


def test_worker_loop_heartbeat_and_directory(ctx, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.eml").write_bytes(demo_mail("04-lumen-lag-member.eml", NOW))
    ctx.cfg.directory.enabled = True
    ctx.cfg.directory.path = str(inbox)
    stop = threading.Event()
    thread = threading.Thread(target=Worker(ctx).run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not (inbox / "processed" / "a.eml").exists():
        time.sleep(0.1)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert ctx.db.kv_get("worker.heartbeat") and ctx.db.kv_get("worker.startup.ok")
    assert ctx.db.scalar("SELECT count(*) FROM notices") == 1


def test_cli_demo_parse_whatif_and_check(tmp_path, capsys):
    db = tmp_path / "cli.db"
    assert cli.main(["demo", "--db", str(db), "--no-serve"]) == 0
    out = capsys.readouterr().out
    assert "8 notice(s)" in out and "COLLISION" in out
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"database": str(db), "inventory": str(demo.INVENTORY),
                                      "providers": [{"name": "Colt", "senders": ["@colt.net"], "dayfirst": True}]}))
    eml = tmp_path / "bt.eml"
    eml.write_bytes(demo_mail("03-bt-leased-line.eml", NOW))
    assert cli.main(["-c", str(config), "parse", str(eml)]) == 0
    out = capsys.readouterr().out
    assert "PEW-7788123" in out and "app payments-api" in out and "10.200.0.0/16" in out
    assert cli.main(["-c", str(config), "whatif", "LUMEN/441122"]) == 0
    assert "app data-warehouse" in capsys.readouterr().out
    assert cli.main(["-c", str(config), "whatif", "no-such-thing"]) == 2
    assert cli.main(["-c", str(config), "check"]) == 0
    assert "5 circuits" in capsys.readouterr().out
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "check"]) == 2
