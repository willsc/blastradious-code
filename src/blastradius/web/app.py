"""FastAPI application: HTML UI, JSON API, Atom/iCalendar feeds, health and metrics."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, collisions, feeds, notices
from ..context import Context
from ..impact import what_if
from ..notify import render as render_mod
from ..notify import subscriptions
from ..topology.inventory import load_inventory
from ..topology.model import TYPE_INFO, key_for
from ..util import (SEVERITY_LABELS, human_duration, iso, loads, parse_iso, severity_from_name, severity_name,
                    utcnow)
from . import graphsvg, mapsvg, timeline
from .auth import User, check_origin, current_user

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PHASES = ("in_progress", "upcoming", "unscheduled", "completed", "cancelled")
PHASE_LABELS = {"in_progress": "In progress", "upcoming": "Upcoming", "unscheduled": "No window",
                "completed": "Completed", "cancelled": "Cancelled"}


def create_app(ctx: Context, worker=None) -> FastAPI:
    cfg, db = ctx.cfg, ctx.db
    try:
        display_tz = ZoneInfo(cfg.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        display_tz = dt.timezone.utc

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        thread = None
        if worker is not None:
            thread = threading.Thread(target=worker.run, args=(stop,), name="blastradius-worker", daemon=True)
            thread.start()
        yield
        stop.set()
        if thread is not None:
            thread.join(timeout=15)

    app = FastAPI(title="blastradius", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env

    # -- template helpers -------------------------------------------------------

    def fmt_dt(value, with_utc: bool = False) -> str:
        t = parse_iso(value) if isinstance(value, str) else value
        if t is None:
            return "—"
        local = t.astimezone(display_tz)
        text = local.strftime("%a %d %b %H:%M ") + (local.tzname() or "")
        if with_utc and getattr(display_tz, "key", "UTC") != "UTC":
            text += t.astimezone(dt.timezone.utc).strftime(" (%H:%M UTC)")
        return text

    def rel(value) -> str:
        t = parse_iso(value) if isinstance(value, str) else value
        if t is None:
            return ""
        delta = (t - utcnow()).total_seconds()
        return f"in {human_duration(delta)}" if delta >= 0 else f"{human_duration(-delta)} ago"

    env.filters.update({
        "sev": severity_name,
        "sevlabel": lambda s: SEVERITY_LABELS.get(s, ""),
        "dt": fmt_dt,
        "rel": rel,
        "phase": lambda p: PHASE_LABELS.get(p, p),
        "typelabel": lambda t: TYPE_INFO.get(t, (t, False))[0],
        "pretty": lambda v: json.dumps(v, indent=2, default=str),
        "duration": lambda n: human_duration((parse_iso(n["end_at"]) - parse_iso(n["start_at"])).total_seconds())
        if n.get("start_at") and n.get("end_at") else "open-ended",
    })
    env.globals.update({"version": __version__, "fmt_window": lambda s, e: render_mod.fmt_window(s, e, cfg),
                        "why": render_mod.app_explanation, "PHASES": PHASES, "PHASE_LABELS": PHASE_LABELS,
                        "TYPE_INFO": TYPE_INFO})

    def user_dep(request: Request) -> User:
        user = current_user(request, cfg)
        check_origin(request, user, cfg)
        return user

    def admin_dep(user: User = Depends(user_dep)) -> User:
        if not user.admin:
            raise HTTPException(status_code=403, detail="admin only")
        return user

    def page(request: Request, name: str, user: User, **context) -> HTMLResponse:
        context.update(user=user, cfg=cfg, now=utcnow(), topo_summary=ctx.store.summary())
        return templates.TemplateResponse(request, name, context)

    # -- data helpers -----------------------------------------------------------

    def all_notices(now=None) -> list[dict]:
        now = now or utcnow()
        return [notices.to_dict(r, now) for r in db.query("SELECT * FROM notices")]

    def filter_notices(items: list[dict], *, phase=None, min_severity=None, app=None, provider=None, q=None,
                       mapped=None) -> list[dict]:
        out = []
        min_sev = severity_from_name(min_severity, 0) if min_severity else 0
        for n in items:
            if phase and n["phase"] not in (phase if isinstance(phase, (list, tuple, set)) else [phase]):
                continue
            if min_sev and (n["severity"] < min_sev or not n["mapped"]):
                continue
            if app and app not in {a["name"] for a in n["apps"]}:
                continue
            if provider and (n["provider"] or "").lower() != provider.lower():
                continue
            if mapped is not None and bool(n["mapped"]) != mapped:
                continue
            if q:
                hay = " ".join(str(v) for v in (n["title"], n["maintenance_id"], n["provider"],
                                                " ".join(c["id"] for c in n["circuits"]))).lower()
                if q.lower() not in hay:
                    continue
            out.append(n)
        return out

    def sort_key(n: dict):
        order = {"in_progress": 0, "upcoming": 1, "unscheduled": 2, "completed": 3, "cancelled": 4}
        start = n["start_at"] or n["first_seen"]
        return (order.get(n["phase"], 5), start if n["phase"] in ("upcoming", "in_progress") else "~" + start)

    def notice_or_404(notice_id: int):
        row = notices.get(db, notice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="notice not found")
        return row

    def visible_subs(user: User) -> list[dict]:
        rows = db.query("SELECT * FROM subscriptions ORDER BY managed_key IS NULL, name")
        subs = [subscriptions.to_dict(r) for r in rows]
        return subs if user.admin or user.kind == "anonymous" else [
            s for s in subs if s["owner"] == user.name or s["managed"]]

    def app_names() -> list[str]:
        return sorted(n.id for n in ctx.store.current().graph.of_type("app"))

    # -- pages --------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: User = Depends(user_dep)):
        now = utcnow()
        items = all_notices(now)
        live = sorted([n for n in items if n["phase"] in ("in_progress", "upcoming", "unscheduled")], key=sort_key)
        horizon = iso(now + dt.timedelta(days=14))
        soon = [n for n in live if n["phase"] != "upcoming" or (n["start_at"] or "") <= horizon]
        active_collisions = collisions.active(db)
        app_risk: dict[str, dict] = {}
        for n in live:
            if n["status"] == "CANCELLED":
                continue
            for a in n["apps"]:
                entry = app_risk.setdefault(a["name"], {"name": a["name"], "severity": 0, "next": None, "count": 0})
                entry["severity"] = max(entry["severity"], a["severity"])
                entry["count"] += 1
                if n["start_at"] and (entry["next"] is None or n["start_at"] < entry["next"]["start_at"]):
                    entry["next"] = n
        recent_events = db.query(
            "SELECT e.*, n.title, n.provider FROM notice_events e JOIN notices n ON n.id = e.notice_id"
            " WHERE e.kind != 'reminder' ORDER BY e.at DESC, e.id DESC LIMIT 12")
        svg = timeline.render(soon, active_collisions, now=now, tz=display_tz,
                              open_hours=cfg.correlation.open_outage_hours)
        counts = {
            "in_progress": sum(1 for n in live if n["phase"] == "in_progress"),
            "upcoming7": sum(1 for n in live if n["phase"] == "upcoming" and n["start_at"] <= iso(now + dt.timedelta(days=7))),
            "unmapped": sum(1 for n in live if not n["mapped"] and n["status"] != "CANCELLED"),
            "outage": sum(1 for n in live if n["mapped"] and n["severity"] >= 3 and n["status"] != "CANCELLED"),
        }
        worker_errors = []
        for task in ("startup", "gmail", "imap", "directory", "topology", "tick", "collisions"):
            error = db.kv_get(f"worker.{task}.error")
            if error and error.get("at", "") > (db.kv_get(f"worker.{task}.ok") or ""):
                worker_errors.append({"task": task, **error})
        return page(request, "dashboard.html", user, live=soon, counts=counts, collisions=active_collisions,
                    worker_errors=worker_errors,
                    timeline_svg=svg, app_risk=sorted(app_risk.values(), key=lambda a: (-a["severity"], a["name"])),
                    recent_events=recent_events, heartbeat=db.kv_get("worker.heartbeat"),
                    gmail_stats=db.kv_get("gmail.last_stats"), imap_stats=db.kv_get("imap.last_stats"))

    @app.get("/notices", response_class=HTMLResponse)
    def notice_list(request: Request, phase: str = "", min_severity: str = "", app_name: str = Query("", alias="app"),
                    provider: str = "", q: str = "", user: User = Depends(user_dep)):
        items = filter_notices(all_notices(), phase=phase or None, min_severity=min_severity or None,
                               app=app_name or None, provider=provider or None, q=q or None)
        providers = sorted({r["provider"] for r in db.query("SELECT DISTINCT provider FROM notices") if r["provider"]})
        return page(request, "notices.html", user, items=sorted(items, key=sort_key)[:500],
                    filters={"phase": phase, "min_severity": min_severity, "app": app_name, "provider": provider, "q": q},
                    providers=providers, apps=app_names())

    @app.get("/notices/{notice_id}", response_class=HTMLResponse)
    def notice_detail(request: Request, notice_id: int, view: str = "simple", user: User = Depends(user_dep)):
        row = notice_or_404(notice_id)
        notice = notices.to_dict(row, with_blast=True)
        blast = notice["blast"] or {}
        topo = ctx.store.current()
        svg = graphsvg.render(blast, topo.graph, simple=(view != "full")) if blast.get("nodes") else ""
        msgs = notices.messages_for(db, notice_id)
        events = [dict(e) | {"detail": loads(e["detail"], {})} for e in notices.events_for(db, notice_id)]
        deliveries = db.query(
            "SELECT d.*, s.name AS sub_name FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id"
            " WHERE d.notice_id = ? ORDER BY d.id DESC LIMIT 50", (notice_id,))
        groups: dict[str, list[dict]] = {}
        for n in blast.get("nodes", []):
            groups.setdefault(n["type"], []).append(n)
        return page(request, "notice.html", user, n=notice, blast=blast, svg=svg, view=view, messages=msgs,
                    events=events, collisions=collisions.for_notice(db, notice_id), groups=groups,
                    deliveries=deliveries)

    @app.get("/notices/{notice_id}/preview", response_class=HTMLResponse)
    def notice_preview(request: Request, notice_id: int, event: str = "new", user: User = Depends(user_dep)):
        row = notice_or_404(notice_id)
        notice = notices.to_dict(row)
        rendered = render_mod.render_notice(cfg, notice, loads(row["blast"], None), event)
        return page(request, "preview.html", user, n=notice, r=rendered, event=event)

    @app.get("/notices/{notice_id}/messages/{message_id}", response_class=PlainTextResponse)
    def message_text(notice_id: int, message_id: int, user: User = Depends(user_dep)):
        row = db.one("SELECT m.* FROM messages m JOIN notice_messages nm ON nm.message_id = m.id"
                     " WHERE nm.notice_id = ? AND m.id = ?", (notice_id, message_id))
        if row is None:
            raise HTTPException(status_code=404)
        header = f"From: {row['sender_name']} <{row['sender']}>\nSubject: {row['subject']}\nDate: {row['received_at']}\n\n"
        return header + (row["body_text"] or "") + ("\n\n--- calendar ---\n" + row["calendar_text"] if row["calendar_text"] else "")

    @app.get("/apps", response_class=HTMLResponse)
    def app_list(request: Request, user: User = Depends(user_dep)):
        topo = ctx.store.current()
        live = {n["id"]: n for n in all_notices() if n["phase"] in ("in_progress", "upcoming") and n["status"] != "CANCELLED"}
        rows = db.query("SELECT app, notice_id, severity FROM notice_apps")
        per_app: dict[str, list] = {}
        for r in rows:
            if r["notice_id"] in live:
                per_app.setdefault(r["app"], []).append((live[r["notice_id"]], r["severity"]))
        apps = []
        for node in sorted(topo.graph.of_type("app"), key=lambda n: n.id):
            members = topo.graph.parents(node.key)
            entries = sorted(per_app.get(node.id, []), key=lambda e: e[0]["start_at"] or "")
            apps.append({"name": node.id, "description": node.attrs.get("description") or "", "tier": node.attrs.get("tier"),
                         "owners": node.attrs.get("owners") or [], "members": len(members),
                         "worst": max((s for _, s in entries), default=0), "count": len(entries),
                         "next": entries[0][0] if entries else None, "source": node.attrs.get("source")})
        return page(request, "apps.html", user, apps=apps)

    @app.get("/apps/{name}", response_class=HTMLResponse)
    def app_detail(request: Request, name: str, user: User = Depends(user_dep)):
        topo = ctx.store.current()
        node = topo.graph.get(f"app:{name.lower()}")
        if node is None:
            raise HTTPException(status_code=404, detail="unknown application")
        ids = [r["notice_id"] for r in db.query("SELECT notice_id FROM notice_apps WHERE app = ?", (node.id,))]
        items = sorted([n for n in all_notices() if n["id"] in ids], key=sort_key)
        members = sorted(topo.graph.parents(node.key), key=lambda m: (m.type, m.id))
        return page(request, "app.html", user, app=node, items=items, members=members,
                    subs=[s for s in visible_subs(user) if node.id in s["filter"].get("applications", [])])

    @app.get("/topology", response_class=HTMLResponse)
    def topology_page(request: Request, q: str = "", user: User = Depends(user_dep)):
        topo = ctx.store.current()
        results = search_nodes(q) if q else []
        return page(request, "topology.html", user, summary=ctx.store.summary(), q=q, results=results,
                    counts=topo.graph.counts(), warnings=topo.warnings)

    def search_nodes(q: str, limit: int = 100) -> list:
        topo = ctx.store.current()
        ql = q.strip().lower()
        if not ql:
            return []
        exact = key_for(q.strip())
        hits = []
        for node in topo.graph.nodes.values():
            hay = " ".join([node.key, node.id, node.name, *node.aliases, *node.ips,
                            *(str(v) for v in node.tags.values())]).lower()
            if ql in hay:
                hits.append(node)
        hits.sort(key=lambda n: (n.key != exact, n.id.lower() != ql, n.type, n.id))
        return hits[:limit]

    @app.get("/topology/node", response_class=HTMLResponse)
    def node_page(request: Request, key: str, user: User = Depends(user_dep)):
        topo = ctx.store.current()
        resolved = topo.resolve(key)
        node = topo.graph.get(resolved) if resolved else None
        if node is None:
            raise HTTPException(status_code=404, detail="node not found in current topology")
        ups = [(e, topo.graph.nodes[e.src]) for e in topo.graph.up(node.key)]
        downs = [(e, topo.graph.nodes[e.dst]) for e in topo.graph.down(node.key)]
        affecting = db.query(
            "SELECT i.severity AS node_severity, i.reason, i.anchor, n.* FROM impacts i JOIN notices n ON n.id = i.notice_id"
            " WHERE i.node_key = ? ORDER BY n.start_at DESC LIMIT 100", (node.key,))
        affecting = [dict(notices.to_dict(r)) | {"node_severity": r["node_severity"], "node_reason": r["reason"],
                                                  "node_anchor": r["anchor"]} for r in affecting]
        return page(request, "node.html", user, node=node, ups=ups, downs=downs, affecting=affecting)

    @app.get("/whatif", response_class=HTMLResponse)
    def whatif_page(request: Request, key: list[str] = Query(default=[]), severity: str = "OUTAGE",
                    view: str = "simple", user: User = Depends(user_dep)):
        topo = ctx.store.current()
        keys = [k for k in (topo.resolve(ref) for ref in key) if k]
        if not keys:
            raise HTTPException(status_code=404, detail="no such node")
        sev = severity_from_name(severity, 3) or 3
        report = what_if(topo.graph, keys, sev, account_names=topo.inventory.accounts)
        svg = graphsvg.render(report, topo.graph, simple=(view != "full")) if report["nodes"] else ""
        groups: dict[str, list[dict]] = {}
        for n in report["nodes"]:
            groups.setdefault(n["type"], []).append(n)
        return page(request, "whatif.html", user, report=report, keys=keys, severity=sev, svg=svg, view=view,
                    groups=groups, labels=[topo.graph.label(k) for k in keys])

    def map_overlay(notice_id: int | None, days: int) -> mapsvg.Overlay:
        now = utcnow()

        def label(n) -> str:
            when = "in progress" if notices.phase(n, now) == "in_progress" else fmt_dt(n["start_at"])
            return f"{n['provider'] or 'notice'} · {when}"

        def add_notice(overlay: mapsvg.Overlay, n: dict) -> None:
            blast = n.get("blast") or {}
            for x in blast.get("nodes", []):
                overlay.add(x["key"], x["severity"], x.get("prefixes"))
            for a in blast.get("anchors", []):
                text = label(n)
                if text not in overlay.badges.setdefault(a["key"], []):
                    overlay.badges[a["key"]].append(text)

        if notice_id:
            n = notices.to_dict(notice_or_404(notice_id), now, with_blast=True)
            overlay = mapsvg.Overlay(selected=True, title=n["title"] or "")
            add_notice(overlay, n)
            return overlay
        horizon = iso(now + dt.timedelta(days=days))
        overlay = mapsvg.Overlay()
        for row in db.query("SELECT * FROM notices WHERE mapped = 1 AND status NOT IN ('CANCELLED', 'COMPLETED')"):
            n = notices.to_dict(row, now, with_blast=True)
            if n["phase"] == "in_progress" or (n["phase"] == "upcoming" and (n["start_at"] or "") <= horizon):
                add_notice(overlay, n)
        for c in collisions.active(db):
            if c["start"] > horizon and days:
                continue
            for e in c["escalations"]:
                overlay.add(e["key"], e["to"], e.get("prefixes"))
                if e["type"] in ("dxgw", "vgw", "dxlag", "vpn"):
                    text = f"collision · {fmt_dt(c['start'])}"
                    if text not in overlay.badges.setdefault(e["key"], []):
                        overlay.badges[e["key"]].append(text)
        return overlay

    def render_map(notice: int | None, overlay: str, account: str, vpc: str, app_name: str, hosts: str, focus: str,
                   days: int, standalone: bool = False):
        topo = ctx.store.current()
        ov = map_overlay(notice, days) if (notice or overlay != "none") else mapsvg.Overlay()
        focus_key = topo.resolve(focus) if focus else ""
        options = dict(account=account, vpc=vpc, app=app_name, focus=focus_key or "",
                       account_names=topo.inventory.accounts)
        svg, estate = mapsvg.render(topo.graph, ov, standalone, hosts=hosts != "0", **options)
        auto_hidden = False
        if hosts == "" and estate.host_count > 400:  # keep huge estates legible until someone filters
            svg, estate = mapsvg.render(topo.graph, ov, standalone, hosts=False, **options)
            auto_hidden = True
        return svg, estate, ov, auto_hidden

    @app.get("/map", response_class=HTMLResponse)
    def map_page(request: Request, notice: int | None = None, overlay: str = "live", account: str = "", vpc: str = "",
                 app_name: str = Query("", alias="app"), hosts: str = "", focus: str = "", days: int = 7,
                 user: User = Depends(user_dep)):
        svg, estate, ov, auto_hidden = render_map(notice, overlay, account, vpc, app_name, hosts, focus, days)
        graph = ctx.store.current().graph
        accounts = sorted({(n.account, n.attrs.get("account_name") or n.account) for n in graph.of_type("vpc") if n.account},
                          key=lambda a: a[1])
        vpcs = sorted(((n.id, n.name or n.id) for n in graph.of_type("vpc")), key=lambda v: v[1])
        now = utcnow()
        live = sorted((n for n in all_notices(now) if n["phase"] in ("in_progress", "upcoming") and n["mapped"]
                       and n["status"] != "CANCELLED"), key=lambda n: n["start_at"] or "")
        counts: dict[str, int] = {}
        for box in estate.boxes.values():
            counts[box.kind] = counts.get(box.kind, 0) + 1
        return page(request, "map.html", user, svg=svg, estate=estate, overlay=ov, counts=counts, live=live,
                    accounts=accounts, vpcs=vpcs, apps=app_names(), auto_hidden=auto_hidden,
                    filters={"notice": notice, "overlay": overlay, "account": account, "vpc": vpc, "app": app_name,
                             "hosts": hosts, "focus": focus, "days": days},
                    query=request.url.query)

    @app.get("/map.svg")
    def map_download(notice: int | None = None, overlay: str = "live", account: str = "", vpc: str = "",
                     app_name: str = Query("", alias="app"), hosts: str = "", focus: str = "", days: int = 7,
                     user: User = Depends(user_dep)):
        svg, *_ = render_map(notice, overlay, account, vpc, app_name, hosts, focus, days, standalone=True)
        return Response(svg, media_type="image/svg+xml",
                        headers={"Content-Disposition": 'inline; filename="blastradius-map.svg"'})

    @app.get("/collisions", response_class=HTMLResponse)
    def collisions_page(request: Request, user: User = Depends(user_dep)):
        return page(request, "collisions.html", user, collisions=collisions.active(db))

    @app.get("/subscriptions", response_class=HTMLResponse)
    def subscriptions_page(request: Request, app_name: str = Query("", alias="app"), error: str = "",
                           user: User = Depends(user_dep)):
        return page(request, "subscriptions.html", user, subs=visible_subs(user), apps=app_names(),
                    preset_app=app_name, events=subscriptions.EVENTS, default_events=subscriptions.DEFAULT_EVENTS,
                    error=error)

    @app.post("/subscriptions")
    def subscriptions_create(
        request: Request,
        name: str = Form(...), description: str = Form(""), applications: list[str] = Form(default=[]),
        providers: str = Form(""), resources: str = Form(""), circuits: str = Form(""), accounts: str = Form(""),
        owners: str = Form(""), min_severity: str = Form("DEGRADED"), include_unmapped: str = Form(""),
        events: list[str] = Form(default=[]), channel_type: str = Form(""), channel_target: str = Form(""),
        channel_secret: str = Form(""), user: User = Depends(user_dep),
    ):
        channels = []
        target = channel_target.strip()
        if channel_type in ("slack", "teams", "webhook"):
            channels.append({"type": channel_type, "url": target, **({"secret": channel_secret} if channel_secret else {})})
        elif channel_type == "email":
            channels.append({"type": "email", "to": [t.strip() for t in target.split(",") if t.strip()]})
        elif channel_type == "sns":
            channels.append({"type": "sns", "topic_arn": target})
        data = {"name": name, "description": description, "events": events or subscriptions.DEFAULT_EVENTS,
                "channels": channels,
                "filter": {"applications": applications, "providers": providers, "resources": resources,
                           "circuits": circuits, "accounts": accounts, "owners": owners,
                           "min_severity": min_severity or None, "include_unmapped": bool(include_unmapped)}}
        try:
            sub_id = subscriptions.create(db, data, owner=user.name)
        except subscriptions.SubscriptionError as exc:
            return RedirectResponse(f"/subscriptions?error={exc}", status_code=303)
        return RedirectResponse(f"/subscriptions#sub-{sub_id}", status_code=303)

    def own_sub(sub_id: int, user: User) -> dict:
        row = db.one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
        if row is None:
            raise HTTPException(status_code=404)
        sub = subscriptions.to_dict(row)
        if sub["managed"]:
            raise HTTPException(status_code=409, detail="managed by config/inventory; edit it there")
        if not (user.admin or sub["owner"] == user.name):
            raise HTTPException(status_code=403)
        return sub

    @app.post("/subscriptions/{sub_id}/delete")
    def subscriptions_delete(sub_id: int, user: User = Depends(user_dep)):
        own_sub(sub_id, user)
        subscriptions.delete(db, sub_id)
        return RedirectResponse("/subscriptions", status_code=303)

    @app.post("/subscriptions/{sub_id}/toggle")
    def subscriptions_toggle(sub_id: int, user: User = Depends(user_dep)):
        own_sub(sub_id, user)
        db.execute("UPDATE subscriptions SET enabled = 1 - enabled, updated_at = ? WHERE id = ?", (iso(utcnow()), sub_id))
        return RedirectResponse(f"/subscriptions#sub-{sub_id}", status_code=303)

    @app.get("/messages", response_class=HTMLResponse)
    def messages_page(request: Request, status: str = "", user: User = Depends(admin_dep)):
        sql = ("SELECT m.id, m.source, m.received_at, m.sender, m.sender_name, m.subject, m.message_type, m.status,"
               " m.error, group_concat(nm.notice_id) AS notice_ids FROM messages m"
               " LEFT JOIN notice_messages nm ON nm.message_id = m.id")
        params: tuple = ()
        if status:
            sql += " WHERE m.status = ?"
            params = (status,)
        sql += " GROUP BY m.id ORDER BY m.received_at DESC, m.id DESC LIMIT 300"
        stats = {r["status"]: r["n"] for r in db.query("SELECT status, count(*) AS n FROM messages GROUP BY status")}
        return page(request, "messages.html", user, rows=db.query(sql, params), status=status, stats=stats,
                    types=cfg.message_types, gmail_stats=db.kv_get("gmail.last_stats"),
                    imap_stats=db.kv_get("imap.last_stats"))

    @app.post("/messages/upload")
    async def messages_upload(file: UploadFile = File(...), user: User = Depends(admin_dep)):
        raw = await file.read()
        result = ctx.pipeline.ingest(raw, source="upload")
        target = f"/notices/{result.notice_ids[0]}" if result.notice_ids else "/messages"
        return RedirectResponse(target, status_code=303)

    @app.post("/admin/topology/refresh")
    def admin_refresh(collect: str = Form("inventory"), user: User = Depends(admin_dep)):
        ctx.store.refresh(collect_aws=(collect == "aws"))
        subscriptions.sync_managed(db, cfg, load_inventory(cfg.path(cfg.inventory)))
        ctx.pipeline.recorrelate()
        return RedirectResponse("/topology", status_code=303)

    @app.post("/admin/recorrelate")
    def admin_recorrelate(user: User = Depends(admin_dep)):
        ctx.pipeline.recorrelate()
        return RedirectResponse("/", status_code=303)

    # -- feeds -----------------------------------------------------------------

    def feed_items(flt: dict | None, limit: int = 150) -> list:
        horizon = iso(utcnow() - dt.timedelta(days=60))
        rows = db.query("SELECT * FROM notices WHERE COALESCE(end_at, updated_at) >= ? ORDER BY updated_at DESC", (horizon,))
        items = feeds.rows_to_items(rows)
        if flt is not None:
            items = [(n, b) for n, b in items if subscriptions.matches(flt, n, b, cfg.notifications.default_min_severity)]
        return items[:limit]

    def feed_for_token(token: str) -> tuple[str, dict | None]:
        if token == "all":
            if cfg.web.auth.mode != "none":
                raise HTTPException(status_code=404)
            return "All maintenance notices", None
        sub = subscriptions.by_token(db, token)
        if sub is None:
            raise HTTPException(status_code=404)
        return f"blastradius — {sub['name']}", sub["filter"]

    @app.get("/feeds/{token}.atom")
    def feed_atom(token: str):
        title, flt = feed_for_token(token)
        body = feeds.atom(cfg, title, f"/feeds/{token}.atom", feed_items(flt))
        return Response(body, media_type="application/atom+xml; charset=utf-8")

    @app.get("/feeds/{token}.ics")
    def feed_ics(token: str):
        title, flt = feed_for_token(token)
        body = feeds.ical(cfg, title, feed_items(flt))
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'inline; filename="{token[:8]}.ics"'})

    # -- JSON API ---------------------------------------------------------------

    @app.get("/api/notices")
    def api_notices(phase: str = "", min_severity: str = "", app_name: str = Query("", alias="app"), provider: str = "",
                    q: str = "", limit: int = 200, user: User = Depends(user_dep)):
        items = filter_notices(all_notices(), phase=phase.split(",") if phase else None,
                               min_severity=min_severity or None, app=app_name or None, provider=provider or None,
                               q=q or None)
        return {"notices": sorted(items, key=sort_key)[:limit]}

    @app.get("/api/notices/{notice_id}")
    def api_notice(notice_id: int, user: User = Depends(user_dep)):
        row = notice_or_404(notice_id)
        out = notices.to_dict(row, with_blast=True)
        out["messages"] = [{k: m[k] for k in ("id", "source", "received_at", "sender", "subject", "role")}
                           for m in notices.messages_for(db, notice_id)]
        out["events"] = [dict(e) | {"detail": loads(e["detail"], {})} for e in notices.events_for(db, notice_id)]
        out["collisions"] = collisions.for_notice(db, notice_id)
        return out

    @app.get("/api/collisions")
    def api_collisions(user: User = Depends(user_dep)):
        return {"collisions": collisions.active(db)}

    @app.get("/api/apps")
    def api_apps(user: User = Depends(user_dep)):
        graph = ctx.store.current().graph
        return {"apps": [{"name": n.id, "owners": n.attrs.get("owners", []), "tier": n.attrs.get("tier"),
                          "members": [m.key for m in graph.parents(n.key)]} for n in graph.of_type("app")]}

    @app.get("/api/topology")
    def api_topology(user: User = Depends(user_dep)):
        return ctx.store.summary()

    @app.get("/api/topology/search")
    def api_topology_search(q: str, user: User = Depends(user_dep)):
        return {"results": [{"key": n.key, "type": n.type, "id": n.id, "name": n.name, "account": n.account,
                             "region": n.region} for n in search_nodes(q)]}

    @app.get("/api/topology/node")
    def api_node(key: str, user: User = Depends(user_dep)):
        topo = ctx.store.current()
        graph = topo.graph
        resolved = topo.resolve(key)
        node = graph.get(resolved) if resolved else None
        if node is None:
            raise HTTPException(status_code=404)
        return {"node": node.__dict__, "upstream": [e.__dict__ for e in graph.up(node.key)],
                "downstream": [e.__dict__ for e in graph.down(node.key)]}

    @app.post("/api/whatif")
    async def api_whatif(request: Request, user: User = Depends(user_dep)):
        body = await request.json()
        topo = ctx.store.current()
        targets = body.get("targets", [])
        keys = [topo.resolve(t) for t in targets]
        missing = [t for t, k in zip(targets, keys) if k is None]
        if missing or not keys:
            raise HTTPException(status_code=404, detail=f"unknown targets: {missing}")
        return what_if(topo.graph, keys, severity_from_name(body.get("severity", "OUTAGE"), 3),
                       account_names=topo.inventory.accounts)

    @app.get("/api/subscriptions")
    def api_subscriptions(user: User = Depends(user_dep)):
        return {"subscriptions": visible_subs(user)}

    @app.post("/api/subscriptions", status_code=201)
    async def api_subscription_create(request: Request, user: User = Depends(user_dep)):
        try:
            sub_id = subscriptions.create(db, await request.json(), owner=user.name)
        except subscriptions.SubscriptionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return subscriptions.to_dict(db.one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)))

    @app.put("/api/subscriptions/{sub_id}")
    async def api_subscription_update(sub_id: int, request: Request, user: User = Depends(user_dep)):
        own_sub(sub_id, user)
        try:
            subscriptions.update(db, sub_id, await request.json())
        except subscriptions.SubscriptionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return subscriptions.to_dict(db.one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)))

    @app.delete("/api/subscriptions/{sub_id}", status_code=204)
    def api_subscription_delete(sub_id: int, user: User = Depends(user_dep)):
        own_sub(sub_id, user)
        subscriptions.delete(db, sub_id)
        return Response(status_code=204)

    @app.post("/api/ingest")
    async def api_ingest(request: Request, user: User = Depends(admin_dep)):
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="send the raw RFC 822 message as the request body")
        result = ctx.pipeline.ingest(raw, source="api")
        return JSONResponse(result.__dict__, status_code=200 if result.status != "error" else 422)

    @app.post("/api/topology/refresh")
    def api_refresh(collect_aws: bool = False, user: User = Depends(admin_dep)):
        topo = ctx.store.refresh(collect_aws=collect_aws)
        subscriptions.sync_managed(db, cfg, load_inventory(cfg.path(cfg.inventory)))
        changed = ctx.pipeline.recorrelate()
        return {"snapshot_id": topo.snapshot_id, "counts": topo.graph.counts(), "recorrelated_changed": changed}

    # -- ops ---------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        now = utcnow()
        heartbeat = parse_iso(db.kv_get("worker.heartbeat"))
        summary = ctx.store.summary()
        created = parse_iso(summary.get("created_at"))
        body = {
            "status": "ok",
            "version": __version__,
            "worker_heartbeat_age": (now - heartbeat).total_seconds() if heartbeat else None,
            "topology_snapshot": summary.get("id"),
            "topology_age": (now - created).total_seconds() if created else None,
            "gmail": db.kv_get("gmail.last_stats"),
            "imap": db.kv_get("imap.last_stats"),
        }
        if worker is not None and (heartbeat is None or (now - heartbeat).total_seconds() > 300):
            body["status"] = "degraded"
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        now = utcnow()
        lines = ["# HELP blastradius_notices Notices by phase and computed severity", "# TYPE blastradius_notices gauge"]
        tally: dict[tuple[str, str], int] = {}
        for n in all_notices(now):
            key = (n["phase"], severity_name(n["severity"]) if n["mapped"] else "UNMAPPED")
            tally[key] = tally.get(key, 0) + 1
        lines += [f'blastradius_notices{{phase="{p}",severity="{s}"}} {c}' for (p, s), c in sorted(tally.items())]
        lines += ["# TYPE blastradius_messages gauge"]
        lines += [f'blastradius_messages{{status="{r["status"]}"}} {r["n"]}'
                  for r in db.query("SELECT status, count(*) AS n FROM messages GROUP BY status")]
        lines += ["# TYPE blastradius_deliveries gauge"]
        lines += [f'blastradius_deliveries{{status="{r["status"]}"}} {r["n"]}'
                  for r in db.query("SELECT status, count(*) AS n FROM deliveries GROUP BY status")]
        lines += ["# TYPE blastradius_collisions_active gauge",
                  f"blastradius_collisions_active {db.scalar('SELECT count(*) FROM collisions WHERE active = 1', default=0)}"]
        summary = ctx.store.summary()
        created = parse_iso(summary.get("created_at"))
        lines += ["# TYPE blastradius_topology_nodes gauge",
                  f"blastradius_topology_nodes {sum((summary.get('counts') or {}).values())}"]
        if created:
            lines += ["# TYPE blastradius_topology_age_seconds gauge",
                      f"blastradius_topology_age_seconds {(now - created).total_seconds():.0f}"]
        heartbeat = parse_iso(db.kv_get("worker.heartbeat"))
        if heartbeat:
            lines += ["# TYPE blastradius_worker_heartbeat_age_seconds gauge",
                      f"blastradius_worker_heartbeat_age_seconds {(now - heartbeat).total_seconds():.0f}"]
        for name in ("gmail", "imap"):
            last_poll = parse_iso(db.kv_get(f"{name}.last_poll"))
            if last_poll:
                lines += [f"# TYPE blastradius_{name}_last_poll_age_seconds gauge",
                          f"blastradius_{name}_last_poll_age_seconds {(now - last_poll).total_seconds():.0f}"]
        return "\n".join(lines) + "\n"

    return app
