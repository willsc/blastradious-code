"""HTML pages, JSON API, feeds, auth modes and CSRF protection through FastAPI's TestClient."""

import xml.etree.ElementTree as ET

import pytest
from conftest import NOW, demo_mail
from fastapi.testclient import TestClient

from blastradius import demo
from blastradius.config import load_config
from blastradius.context import build_context
from blastradius.parsing import ical
from blastradius.web.app import create_app

PAGES = ["/", "/notices", "/notices?phase=upcoming&min_severity=OUTAGE", "/apps", "/apps/payments-api",
         "/collisions", "/topology", "/topology?q=zayo", "/topology/node?key=dxlag:dxlag-ffab1234",
         "/topology/node?key=cgw-01000000000000001", "/whatif?key=circuit:bt-il-778899",
         "/whatif?key=dxcon-fgzy1a01&key=dxcon-fgct2b02&view=full", "/subscriptions", "/messages",
         "/healthz", "/metrics", "/api/docs"]


@pytest.fixture
def client(seeded):
    return TestClient(create_app(seeded))


def test_every_page_renders(client, seeded):
    ids = [r["id"] for r in seeded.db.query("SELECT id FROM notices")]
    for path in PAGES + [f"/notices/{i}" for i in ids] + [f"/notices/{i}?view=full" for i in ids] + \
            [f"/notices/{i}/preview?event=reminder" for i in ids]:
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text[:500])


def test_notice_page_explains_the_blast_radius(client, seeded):
    zayo = seeded.db.one("SELECT id FROM notices WHERE provider = 'Zayo'")["id"]
    html = client.get(f"/notices/{zayo}").text
    assert "redundancy lost: 1 of 2 paths down" in html
    assert "Collision:" in html and "svg" in html and "OGYX/123456//ZYO" in html


def test_api(client, seeded):
    items = client.get("/api/notices", params={"app": "data-warehouse"}).json()["notices"]
    assert {n["provider"] for n in items} == {"Lumen", "Equinix"}
    outage = client.get("/api/notices", params={"min_severity": "OUTAGE", "phase": "upcoming"}).json()["notices"]
    assert {n["provider"] for n in outage} == {"Lumen", "BT"}
    detail = client.get(f"/api/notices/{items[0]['id']}").json()
    assert detail["blast"]["nodes"] and detail["messages"] and detail["events"]
    assert client.get("/api/collisions").json()["collisions"][0]["severity"] == 3
    whatif = client.post("/api/whatif", json={"targets": ["LUMEN/441123"]}).json()
    assert [a["name"] for a in whatif["apps"]] == ["data-warehouse"]
    assert client.post("/api/whatif", json={"targets": ["nope-1"]}).status_code == 404
    found = client.get("/api/topology/search", params={"q": "203.0.113.10"}).json()["results"]
    assert found[0]["key"] == "cgw:cgw-01000000000000001"
    assert "blastradius_collisions_active 1" in client.get("/metrics").text


def test_subscription_lifecycle_and_feeds(client):
    resp = client.post("/api/subscriptions", json={"name": "dw", "filter": {"applications": ["data-warehouse"]}})
    assert resp.status_code == 201
    sub = resp.json()
    ics = client.get(f"/feeds/{sub['token']}.ics")
    assert ics.headers["content-type"].startswith("text/calendar")
    [cal] = ical.parse_ical(ics.text)
    summaries = sorted(e.text("SUMMARY") for e in cal.walk("VEVENT"))
    assert summaries == ["[AT_RISK] Equinix maintenance — data-warehouse, ledger, payments-api, reporting",
                         "[OUTAGE] Lumen maintenance — data-warehouse"]
    atom = ET.fromstring(client.get(f"/feeds/{sub['token']}.atom").text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert len(atom.findall("a:entry", ns)) == 2
    assert client.get("/feeds/not-a-token.ics").status_code == 404
    bad = client.post("/api/subscriptions", json={"name": "x", "channels": [{"type": "slack", "url": "nope"}]})
    assert bad.status_code == 422
    assert client.put(f"/api/subscriptions/{sub['id']}", json={"name": "dw2"}).json()["name"] == "dw2"
    assert client.delete(f"/api/subscriptions/{sub['id']}").status_code == 204
    managed = next(s for s in client.get("/api/subscriptions").json()["subscriptions"] if s["managed"])
    assert client.delete(f"/api/subscriptions/{managed['id']}").status_code == 409


def test_form_subscription_and_upload(client, seeded):
    resp = client.post("/subscriptions", data={"name": "ledger oncall", "applications": ["ledger"],
                                               "min_severity": "OUTAGE", "events": ["new", "reminder"],
                                               "channel_type": "email", "channel_target": "a@example.com, b@example.com"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/subscriptions#sub-")
    sub = seeded.db.one("SELECT * FROM subscriptions WHERE name = 'ledger oncall'")
    assert '"a@example.com"' in sub["channels"] and '"min_severity":"OUTAGE"' in sub["filter"]
    upload = client.post("/messages/upload", files={"file": ("x.eml", demo_mail("07-gtt-unmapped.eml", NOW))},
                         follow_redirects=False)
    assert upload.status_code == 303


def test_header_auth_and_csrf(tmp_path):
    overrides = demo.config_overrides(tmp_path / "auth.db")
    overrides["web"] = {"auth": {"mode": "header", "header": "X-Forwarded-Email", "admins": ["boss@example.com"],
                                 "api_tokens": {"ci": "t0ken"}}}
    overrides["base_url"] = "https://br.example.com"
    ctx = build_context(load_config(None, overrides))
    demo.seed(ctx)
    client = TestClient(create_app(ctx))
    assert client.get("/").status_code == 401
    user = {"X-Forwarded-Email": "dev@example.com"}
    assert client.get("/", headers=user).status_code == 200
    assert client.get("/messages", headers=user).status_code == 403  # admin only
    assert client.get("/messages", headers={"X-Forwarded-Email": "Boss@example.com"}).status_code == 200
    assert client.get("/api/notices", headers={"Authorization": "Bearer t0ken"}).status_code == 200
    assert client.get("/api/notices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    form = {"name": "mine", "min_severity": "DEGRADED"}
    assert client.post("/subscriptions", data=form, headers=user).status_code == 403  # no Origin
    evil = user | {"Origin": "https://evil.example.com"}
    assert client.post("/subscriptions", data=form, headers=evil).status_code == 403
    ok = client.post("/subscriptions", data=form, headers=user | {"Origin": "https://br.example.com"},
                     follow_redirects=False)
    assert ok.status_code == 303
    # users see their own and managed subscriptions, not other people's
    other = {"X-Forwarded-Email": "someone@example.com"}
    names = [s["name"] for s in client.get("/api/subscriptions", headers=other).json()["subscriptions"]]
    assert "mine" not in names
    sub_id = ctx.db.one("SELECT id FROM subscriptions WHERE name = 'mine'")["id"]
    assert client.delete(f"/api/subscriptions/{sub_id}", headers=other | {"Origin": "https://br.example.com"}).status_code == 403
    assert client.get("/feeds/all.ics").status_code == 404  # global feed only without auth


def test_hostile_mail_content_is_escaped(client, seeded):
    from conftest import mail

    evil = mail("Planned maintenance <script>alert(1)</script>",
                "Circuit: BT-IL-778899\nStart: 2026-10-10 01:00 UTC\nEnd: 2026-10-10 02:00 UTC\n<img src=x onerror=alert(2)>",
                sender="x@bt.com")
    notice_id = seeded.pipeline.ingest(evil, source="t").notice_ids[0]
    for path in (f"/notices/{notice_id}", "/", "/notices", f"/notices/{notice_id}/preview", "/messages",
                 "/feeds/all.atom"):
        body = client.get(path).text
        assert "<script>alert(1)" not in body and "<img src=x" not in body, path
