"""Estate topology map: structure, links, overlays, filters and layout sanity."""

import datetime as dt
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from blastradius.util import OUTAGE, utcnow
from blastradius.web import mapsvg
from blastradius.web.app import create_app


def build(ctx, overlay=None, **filters):
    topo = ctx.store.current()
    return mapsvg.EstateMap(topo.graph, overlay, account_names=topo.inventory.accounts, **filters).build().layout()


def links(estate):
    return {(link.src, link.dst): link for link in estate.links}


def test_structure_and_links(ctx):
    estate = build(ctx)
    col = {name: [b.key for b in boxes] for name, boxes in estate.columns.items()}
    assert sorted(col["sites"]) == ["site:london-hq", "site:slough-dc"]
    assert len(col["circuits"]) == 5
    assert sorted(col["edge"]) == ["dxloc:EqLD5", "dxloc:THN1", "internet"]
    assert col["dxgw"] == ["dxgw:5f3e8a6c-2b1d-4c7e-9a0f-1b2c3d4e5f60"]
    assert sorted(col["gateways"]) == ["tgw:tgw-01000000000000001", "vgw:vgw-03000000000000001"]
    assert sorted(col["vpcs"]) == ["vpc:vpc-02000000000000001", "vpc:vpc-03000000000000001"]
    ld5 = estate.boxes["dxloc:EqLD5"]
    assert ld5.title == "Equinix LD5 · EqLD5"  # colo site name from the inventory
    assert [c.key for c in ld5.children] == ["dxlag:dxlag-ffab1234", "dxcon:dxcon-fgzy1a01"] or \
        [c.key for c in ld5.children] == ["dxcon:dxcon-fgzy1a01", "dxlag:dxlag-ffab1234"]
    lag = estate.boxes["dxlag:dxlag-ffab1234"]
    assert [c.key for c in lag.children] == ["dxcon:dxcon-fglm3c03", "dxcon:dxcon-fglm4c04"]
    prod = estate.boxes["vpc:vpc-02000000000000001"]
    app_a = next(s for s in prod.children if s.title == "prod-app-a")
    assert sorted(c.title for c in app_a.children) == ["payments-api-1", "reporting-1"]
    public_a = next(s for s in prod.children if s.title == "prod-public-a")
    assert sorted(c.title for c in public_a.children) == ["ALB payments-alb", "bastion-1"]  # NAT ENI is infra
    ls = links(estate)
    assert ls[("circuit:ogyx123456zyo", "dxcon:dxcon-fgzy1a01")].kind == "circuit"
    assert ls[("site:slough-dc", "circuit:ogyx123456zyo")].kind == "site"
    assert ls[("dxcon:dxcon-fgzy1a01", "dxgw:5f3e8a6c-2b1d-4c7e-9a0f-1b2c3d4e5f60")].label == "VIF transit-zayo · VLAN 101"
    assert ls[("dxlag:dxlag-ffab1234", "vgw:vgw-03000000000000001")].label == "VIF private-data-lag · VLAN 201"
    assert ls[("cgw:cgw-01000000000000001", "tgw:tgw-01000000000000001")].label == "VPN hq-vpn · 2 tunnels"
    # VIF / VPN details are listed inside the boxes rather than on the (short) links
    assert "VIF transit-zayo · VLAN 101" in estate.boxes["dxcon:dxcon-fgzy1a01"].lines
    assert "VIF private-data-lag · VLAN 201" in estate.boxes["dxlag:dxlag-ffab1234"].lines
    assert "VPN hq-vpn · 2 tunnels" in estate.boxes["cgw:cgw-01000000000000001"].lines
    assert ls[("tgw:tgw-01000000000000001", "vpc:vpc-02000000000000001")].label == "prod-rt"
    assert ("vgw:vgw-03000000000000001", "vpc:vpc-03000000000000001") in ls
    assert estate.host_count == 12


def test_single_notice_overlay_paints_only_the_real_path(seeded):
    bt = seeded.db.one("SELECT * FROM notices WHERE provider = 'BT'")
    from blastradius.util import loads

    blast = loads(bt["blast"])
    overlay = mapsvg.Overlay(badges={a["key"]: ["BT"] for a in blast["anchors"]}, selected=True)
    for node in blast["nodes"]:
        overlay.add(node["key"], node["severity"], node.get("prefixes"))
    estate = build(seeded, overlay)
    ls = links(estate)
    assert ls[("cgw:cgw-01000000000000001", "tgw:tgw-01000000000000001")].severity == OUTAGE
    assert ls[("tgw:tgw-01000000000000001", "vpc:vpc-02000000000000001")].severity == OUTAGE
    # data VPC shares the TGW route table but does not route to HQ
    assert ls[("tgw:tgw-01000000000000001", "vpc:vpc-03000000000000001")].severity == 0
    assert "dim" in estate.boxes["vpc:vpc-03000000000000001"].classes
    assert "dim" not in estate.boxes["tgw:tgw-01000000000000001"].classes  # on the path
    assert "dim" not in estate.boxes["site:london-hq"].classes
    assert estate.boxes["circuit:btil778899"].badges == ["BT"]
    db_a = next(s for s in estate.boxes["vpc:vpc-02000000000000001"].children if s.title == "prod-db-a")
    assert db_a.severity == 0 and "dim" in db_a.classes


def test_filters(ctx):
    data = build(ctx, vpc="vpc-03000000000000001")
    assert [b.key for b in data.columns["vpcs"]] == ["vpc:vpc-03000000000000001"]
    assert sorted(b.key for b in data.columns["circuits"]) == ["circuit:lumen441122", "circuit:lumen441123"]
    assert data.columns["dxgw"] == [] and "internet" not in data.boxes
    by_account = build(ctx, account="prod")
    assert [b.key for b in by_account.columns["vpcs"]] == ["vpc:vpc-02000000000000001"]
    app = build(ctx, app="ledger")
    chips = [b for b in app.boxes.values() if b.kind == "host"]
    assert {c.title for c in chips if "other-app" not in c.classes} >= {"ledger-1", "ledger-2", "RDS"}
    assert {c.title for c in chips if "other-app" in c.classes} >= {"payments-api-1", "bastion-1"}
    hidden = build(ctx, hosts=False)
    assert not [b for b in hidden.boxes.values() if b.kind == "host"]
    assert any("host" in line for b in hidden.boxes.values() if b.kind == "subnet" for line in b.lines)


def test_layout_has_no_overlaps(ctx):
    estate = build(ctx)
    for name, boxes in estate.columns.items():
        spans = sorted((b.y, b.y + b.h) for b in boxes)
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), name
        for box in boxes:
            for child in box.walk():
                assert child.w > 0 and child.h > 0
                if child is not box:
                    assert box.x <= child.x and child.x + child.w <= box.x + box.w + 0.5
                    assert box.y <= child.y and child.y + child.h <= box.y + box.h + 0.5
    ET.fromstring(estate.svg(standalone=True))  # well-formed XML


def test_map_pages(seeded):
    client = TestClient(create_app(seeded))
    for path in ("/map", "/map?overlay=none", "/map?days=0", "/map?notice=1", "/map?vpc=vpc-03000000000000001",
                 "/map?app=payments-api&hosts=0", "/map?focus=cgw-01000000000000001", "/map?account=prod"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    assert 'id="map-focus"' in client.get("/map?focus=BT-IL-778899").text
    svg = client.get("/map.svg?days=30")
    assert svg.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(svg.text)
    assert root.find("{http://www.w3.org/2000/svg}style") is not None
    # 30-day live overlay includes the Zayo/Colt collision: the DX gateway is badged and goes to outage
    assert "collision ·" in svg.text


@pytest.mark.parametrize("days,expect_bt", [(7, False), (30, True)])
def test_live_window(seeded, days, expect_bt):
    client = TestClient(create_app(seeded))
    text = client.get(f"/map?days={days}").text
    # the demo BT window is 7 days after the seed date, so its label's weekday moves with the calendar
    weekday = (utcnow() + dt.timedelta(days=7)).strftime("%a")
    assert (f"BT · {weekday}" in text) == expect_bt


def test_vpc_link_coloured_only_by_what_that_vpc_routes(seeded):
    """The data VPC shares the TGW route table with prod but only sends 10.20/16 through it: an outage of
    the HQ VPN (10.200/16) or the DX gateway (10.100/16) must not paint its TGW link, even though the data
    VPC is itself red from the Lumen LAG via its VGW."""
    client = TestClient(create_app(seeded))
    html = client.get("/map?days=30").text
    assert html.count("prod-rt") >= 2
    topo = seeded.store.current()
    overlay = mapsvg.Overlay()
    from blastradius.util import loads

    for row in seeded.db.query("SELECT blast FROM notices WHERE provider IN ('BT', 'Lumen')"):
        for node in loads(row["blast"])["nodes"]:
            overlay.add(node["key"], node["severity"], node.get("prefixes"))
    estate = mapsvg.EstateMap(topo.graph, overlay).build().layout()
    ls = links(estate)
    assert estate.boxes["vpc:vpc-03000000000000001"].severity == OUTAGE  # via the LAG / VGW
    assert ls[("vgw:vgw-03000000000000001", "vpc:vpc-03000000000000001")].severity == OUTAGE
    assert ls[("tgw:tgw-01000000000000001", "vpc:vpc-03000000000000001")].severity == 0
    assert ls[("tgw:tgw-01000000000000001", "vpc:vpc-02000000000000001")].severity == OUTAGE


def test_overlay_merge():
    o = mapsvg.Overlay()
    o.add("tgwatt:a", 2, {"10.100.0.0/16": 2})
    o.add("tgwatt:a", 3, {"10.200.0.0/16": 3})
    assert o.severity["tgwatt:a"] == 3 and o.prefixes["tgwatt:a"] == {"10.100.0.0/16": 2, "10.200.0.0/16": 3}
    o.add("tgwatt:a", 1, None)  # an unscoped hit makes the whole attachment affected
    assert "tgwatt:a" not in o.prefixes and o.severity["tgwatt:a"] == 3
    o.add("tgwatt:a", 3, {"10.1.0.0/16": 3})
    assert "tgwatt:a" not in o.prefixes
