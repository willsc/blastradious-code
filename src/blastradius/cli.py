"""blastradius command line."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .util import SEVERITY_NAMES, loads, severity_from_name, severity_name

log = logging.getLogger("blastradius")

USE_COLOR = sys.stdout.isatty()
_SEV_COLOR = {0: "90", 1: "34", 2: "33", 3: "31"}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def _sev(sev: int, mapped: bool = True) -> str:
    return _c(_SEV_COLOR.get(sev, "0"), f"{severity_name(sev) if mapped else 'UNMAPPED':8}")


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose > 1 else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "google", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not verbose:
        logging.getLogger("blastradius.topology.store").setLevel(logging.WARNING)


def _context(args, overrides: dict | None = None):
    from .context import build_context

    cfg = load_config(args.config, overrides)
    return build_context(cfg)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_run(args, *, with_worker: bool = True) -> int:
    import uvicorn

    from .web.app import create_app
    from .worker import Worker

    ctx = _context(args)
    host = args.host or ctx.cfg.web.host
    port = args.port or ctx.cfg.web.port
    worker = Worker(ctx) if with_worker else None
    app = create_app(ctx, worker)
    log.info("blastradius %s listening on http://%s:%s (worker=%s)", __version__, host, port, bool(worker))
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=args.access_log, proxy_headers=True)
    return 0


def cmd_worker(args) -> int:
    from .worker import Worker

    ctx = _context(args)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Worker(ctx).run(stop)
    return 0


def cmd_demo(args) -> int:
    from . import demo
    from .context import build_context

    db = Path(args.db).resolve()
    if db.exists() and not args.keep:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    overrides = demo.config_overrides(db)
    overrides.setdefault("web", {})["port"] = args.port
    overrides["base_url"] = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}"
    cfg = load_config(args.config, overrides)
    ctx = build_context(cfg)
    results = demo.seed(ctx)
    print(_c("1", f"Seeded demo database {db}"))
    for name, result in results:
        print(f"  {name:32} {result.status:9} {result.message_type or '-':20} notices {result.notice_ids}")
    _print_notices(ctx)
    if args.no_serve:
        return 0
    import uvicorn

    from .web.app import create_app
    from .worker import Worker

    print(_c("1", f"\nOpen {cfg.base_url}/   (Ctrl-C to stop)"))
    uvicorn.run(create_app(ctx, Worker(ctx)), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_gmail_auth(args) -> int:
    from .sources.gmail import authorize

    cfg = load_config(args.config)
    token = authorize(cfg, port=args.port, open_browser=args.open_browser)
    print(f"OAuth token written to {token}")
    return 0


def cmd_gmail_test(args) -> int:
    from .classify import classify
    from .db import Database
    from .mailparse import parse_mail
    from .sources.gmail import GmailSource

    cfg = load_config(args.config)
    source = GmailSource(cfg, Database(":memory:"))
    for mtype in cfg.message_types:
        if not mtype.gmail_query or (args.type and mtype.name != args.type):
            continue
        query = source.query_for(mtype.gmail_query, None)
        ids = source.list_ids(query, args.limit)
        print(_c("1", f"{mtype.name}: {query}  ->  {len(ids)} message(s)"))
        for message_id, _ in ids:
            raw, _meta = source.fetch_raw(message_id)
            mail = parse_mail(raw)
            matched = classify(mail, cfg, mtype.name)
            verdict = matched.name if matched else _c("33", "ignored (no type matched)")
            print(f"  {message_id}  {mail.date:%Y-%m-%d %H:%M}  {mail.sender:35.35}  {mail.subject[:70]:70}  -> {verdict}")
    return 0


def cmd_discover(args) -> int:
    ctx = _context(args)
    topo = ctx.store.refresh(collect_aws=not args.no_aws)
    from .notify import subscriptions
    from .topology.inventory import load_inventory

    subscriptions.sync_managed(ctx.db, ctx.cfg, load_inventory(ctx.cfg.path(ctx.cfg.inventory)))
    summary = ctx.store.summary()
    print(_c("1", f"Snapshot #{topo.snapshot_id} ({summary.get('source')})"))
    for ntype, count in topo.graph.counts().items():
        print(f"  {ntype:10} {count}")
    for account in summary.get("accounts", []):
        print(f"  account {account['name']} {account['account_id']} regions={','.join(account['regions'])} "
              f"errors={account['errors']}")
    for warning in topo.warnings[:50]:
        print(_c("33", f"  ! {warning}"))
    if not args.no_recorrelate:
        changed = ctx.pipeline.recorrelate()
        print(f"re-correlated active notices: {changed} changed")
    return 0


def cmd_ingest(args) -> int:
    ctx = _context(args)
    rc = 0
    for path in args.files:
        result = ctx.pipeline.ingest(Path(path).read_bytes(), source="cli", notify=not args.no_notify)
        print(f"{path}: {result.status} type={result.message_type} notices={result.notice_ids} {result.error or ''}")
        rc |= result.status == "error"
    _print_notices(ctx)
    return rc


def cmd_parse(args) -> int:
    """Dry run: classification, parsed fields and blast radius — nothing is stored."""
    from .classify import classify
    from .impact import CorrelationInput, blast_radius
    from .mailparse import parse_mail
    from .parsing import parse_message

    ctx = _context(args)
    raw = Path(args.file).read_bytes()
    mail = parse_mail(raw)
    mtype = classify(mail, ctx.cfg)
    print(_c("1", f"Subject: {mail.subject}"))
    print(f"From: {mail.sender_name} <{mail.sender}>" + (f" (forwarded from {mail.original_sender})" if mail.original_sender else ""))
    print(f"Calendars: {len(mail.calendars)}  text: {len(mail.text)} chars")
    if mtype is None:
        print(_c("33", "No message type matches — this mail would be ignored."))
        return 1
    print(f"Message type: {mtype.name} ({mtype.kind})")
    topo = ctx.store.current()
    for parsed in parse_message(mail, mtype, ctx.cfg):
        print(_c("1", f"\n[{parsed.parser}] {parsed.provider} {parsed.maintenance_id}  status={parsed.status}  "
                      f"impact={parsed.impact}  confidence={parsed.confidence}"))
        print(f"  window: {parsed.start} -> {parsed.end}   ({len(parsed.windows)} window(s))")
        print(f"  circuits: {[c.id for c in parsed.circuits]}   declared: {parsed.declared_resources}")
        for note in parsed.notes:
            print(f"  note: {note}")
        inp = CorrelationInput(
            circuits=[{"id": c.id, "impact": c.impact} for c in parsed.circuits],
            declared=parsed.declared_resources, texts=[mail.searchable_text()], impact=parsed.impact,
            default_impact=mtype.default_impact, exclusive=parsed.exclusive_ids)
        _print_report(blast_radius(topo.graph, topo.index, inp, account_names=topo.inventory.accounts))
    return 0


def cmd_whatif(args) -> int:
    from .impact import what_if

    ctx = _context(args)
    topo = ctx.store.current()
    keys = []
    for target in args.targets:
        key = topo.resolve(target)
        if key is None:
            print(_c("31", f"unknown target {target!r}"), file=sys.stderr)
            return 2
        keys.append(key)
    _print_report(what_if(topo.graph, keys, severity_from_name(args.severity, 3),
                          account_names=topo.inventory.accounts), verbose=args.verbose > 0)
    return 0


def cmd_notices(args) -> int:
    _print_notices(_context(args), include_all=args.all)
    return 0


def cmd_recorrelate(args) -> int:
    ctx = _context(args)
    changed = ctx.pipeline.recorrelate(notify=not args.no_notify)
    print(f"{changed} notice(s) changed")
    return 0


def cmd_rebuild(args) -> int:
    ctx = _context(args)
    count = ctx.pipeline.rebuild()
    print(f"replayed {count} message(s)")
    _print_notices(ctx)
    return 0


def cmd_check(args) -> int:
    from .topology.inventory import load_inventory

    cfg = load_config(args.config)
    print(_c("1", "Config OK"))
    print(f"  database: {cfg.path(cfg.database)}")
    print(f"  message types: {', '.join(t.name for t in cfg.message_types)}")
    print(f"  gmail: {'enabled (' + cfg.gmail.auth + ')' if cfg.gmail.enabled else 'disabled'}"
          f"   directory: {'enabled' if cfg.directory.enabled else 'disabled'}"
          f"   aws: {'enabled ' + str(len(cfg.aws.accounts)) + ' account(s) x ' + ','.join(cfg.aws.regions) if cfg.aws.enabled else 'disabled'}")
    inv = load_inventory(cfg.path(cfg.inventory))
    print(f"  inventory: {inv.source or 'none'}: {len(inv.sites)} sites, {len(inv.circuits)} circuits, "
          f"{len(inv.applications)} applications")
    from .notify import subscriptions as subs

    for raw in cfg.subscriptions:
        subs.validate(raw)
    print(f"  subscriptions in config: {len(cfg.subscriptions)}")
    from .parsing import ntc

    print(f"  circuit-maintenance-parser (ntc): {'available' if ntc.available() else 'not installed'}")
    return 0


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def _print_notices(ctx, include_all: bool = True) -> None:
    from . import notices

    rows = ctx.db.query("SELECT * FROM notices ORDER BY start_at")
    print(_c("1", f"\n{len(rows)} notice(s)"))
    for row in rows:
        n = notices.to_dict(row)
        if not include_all and n["phase"] in ("completed", "cancelled"):
            continue
        apps = ", ".join(f"{a['name']}" for a in n["apps"]) or ("-" if n["mapped"] else "")
        print(f"  #{n['id']:<3} {_sev(n['severity'], n['mapped'])} {n['phase']:12} {(n['start_at'] or '-'):20} "
              f"{(n['provider'] or '')[:10]:10} {(n['maintenance_id'] or '')[:22]:22} {apps}")
    for c in ctx.db.query("SELECT data FROM collisions WHERE active = 1"):
        data = loads(c["data"])
        print(_c("31", f"  COLLISION {data['summary']} ({data['start']} - {data['end']})"))


def _print_report(report: dict, verbose: bool = True) -> None:
    print(f"  blast radius: {_sev(report['severity'], report['mapped'])} (infrastructure {severity_name(report['infra_severity'])})")
    for anchor in report["anchors"]:
        print(f"  anchor: {anchor['label']} [{severity_name(anchor['severity'])}] — {anchor['reason']}")
    for dropped in report["dropped"]:
        print(_c("90", f"  context: {dropped['key']} ({dropped['reason']})"))
    if report["unmapped"]:
        print(_c("33", f"  not in inventory: {', '.join(report['unmapped'])}"))
    for app in report["apps"]:
        print(f"  app {app['name']:20} {_sev(app['severity'])} {', '.join(app['prefixes'] or []) if app['prefixes'] else ''}")
    if verbose:
        for node in report["nodes"]:
            if node["type"] == "app":
                continue
            scope = ", ".join(node["prefixes"]) if node["prefixes"] else ""
            print(_c("90", f"    {node['type']:8} {node['label'][:48]:48} ") + _sev(node["severity"])
                  + _c("90", f" {scope} {node['reasons'][0][:110] if node['reasons'] else ''}"))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blastradius", description=__doc__)
    p.add_argument("-c", "--config", help="config YAML (default: none; built-in defaults)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=f"blastradius {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    for name, help_text in (("run", "web UI + daemon in one process"), ("serve", "web UI / API only")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--host")
        s.add_argument("--port", type=int)
        s.add_argument("--access-log", action="store_true")
    sub.add_parser("worker", help="daemon only: poll mail, refresh topology, send notifications")

    s = sub.add_parser("demo", help="seed a demo database (fictional AWS estate + sample mails) and serve it")
    s.add_argument("--db", default="var/demo.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8470)
    s.add_argument("--no-serve", action="store_true")
    s.add_argument("--keep", action="store_true", help="do not reset an existing demo database")

    s = sub.add_parser("gmail-auth", help="OAuth consent flow; stores a refresh token")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open-browser", action="store_true")
    s = sub.add_parser("gmail-test", help="read-only: show what each message type's Gmail query finds")
    s.add_argument("--type")
    s.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("discover", help="build a topology snapshot now (AWS + inventory)")
    s.add_argument("--no-aws", action="store_true", help="reuse the last AWS collection; apply inventory only")
    s.add_argument("--no-recorrelate", action="store_true")

    s = sub.add_parser("ingest", help="process .eml files")
    s.add_argument("files", nargs="+")
    s.add_argument("--no-notify", action="store_true")
    s = sub.add_parser("parse", help="dry run on one .eml: classification, fields, blast radius (stores nothing)")
    s.add_argument("file")
    s = sub.add_parser("whatif", help="blast radius of a hypothetical failure")
    s.add_argument("targets", nargs="+", help="circuit id, AWS id, type:id or exact name")
    s.add_argument("--severity", default="OUTAGE", choices=[v for v in SEVERITY_NAMES.values() if v != "NONE"])
    s = sub.add_parser("notices", help="list notices")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("recorrelate", help="re-run correlation for active notices")
    s.add_argument("--no-notify", action="store_true")
    sub.add_parser("rebuild", help="drop notices and replay every stored mail (no notifications)")
    sub.add_parser("check", help="validate config and inventory")
    return p


COMMANDS = {
    "run": lambda a: cmd_run(a, with_worker=True),
    "serve": lambda a: cmd_run(a, with_worker=False),
    "worker": cmd_worker, "demo": cmd_demo, "gmail-auth": cmd_gmail_auth, "gmail-test": cmd_gmail_test,
    "discover": cmd_discover, "ingest": cmd_ingest, "parse": cmd_parse, "whatif": cmd_whatif,
    "notices": cmd_notices, "recorrelate": cmd_recorrelate, "rebuild": cmd_rebuild, "check": cmd_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return COMMANDS[args.command](args) or 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
