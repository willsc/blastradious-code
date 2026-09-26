from blastradius.impact import Anchor, ImpactEngine, prune_anchors, what_if
from blastradius.topology.build import infer_site_alternates
from blastradius.topology.index import IdentifierIndex
from blastradius.topology.model import Graph, Node, make_key
from blastradius.util import AT_RISK, DEGRADED, OUTAGE


def graph(*edges, modes=None, nodes=()):
    g = Graph()
    for node in nodes:
        g.add(node)
    for edge in edges:
        src, dst, *rest = edge
        kind = rest[0] if rest else "dep"
        prefix = rest[1] if len(rest) > 1 else None
        g.link(src, dst, kind, prefix)
    for key, mode in (modes or {}).items():
        node = g.ensure(key)
        node.mode, node.quorum = mode if isinstance(mode, tuple) else (mode, 1)
    return g


def run(g, *anchors):
    return ImpactEngine(g).run([Anchor(k, s, "test") for k, s in anchors])


# --------------------------------------------------------------------------- engine


def redundant_dx():
    return graph(
        ("circuit:a", "dxcon:dxcon-a"), ("dxcon:dxcon-a", "dxvif:dxvif-a"), ("dxvif:dxvif-a", "dxgw:gw"),
        ("circuit:b", "dxcon:dxcon-b"), ("dxcon:dxcon-b", "dxvif:dxvif-b"), ("dxvif:dxvif-b", "dxgw:gw"),
        ("dxgw:gw", "tgwatt:tgw-attach-dx"),
        modes={"dxgw:gw": "any"},
    )


def test_one_leg_down_loses_redundancy_not_connectivity():
    state = run(redundant_dx(), ("circuit:a", OUTAGE))
    assert state["dxvif:dxvif-a"].severity == OUTAGE
    assert state["dxgw:gw"].severity == DEGRADED
    assert "redundancy lost: 1 of 2" in state["dxgw:gw"].reasons[0]
    assert state["dxgw:gw"].alternates == ["dxvif:dxvif-b"]
    assert state["tgwatt:tgw-attach-dx"].severity == DEGRADED


def test_both_legs_down_is_an_outage():
    state = run(redundant_dx(), ("circuit:a", OUTAGE), ("circuit:b", OUTAGE))
    assert state["dxgw:gw"].severity == OUTAGE
    assert state["tgwatt:tgw-attach-dx"].severity == OUTAGE


def test_remaining_leg_at_risk_is_called_out():
    state = run(redundant_dx(), ("circuit:a", OUTAGE), ("circuit:b", AT_RISK))
    assert state["dxgw:gw"].severity == DEGRADED
    assert "itself at risk" in state["dxgw:gw"].reasons[0]


def test_lag_minimum_links():
    edges = [("dxcon:m1", "dxlag:lag"), ("dxcon:m2", "dxlag:lag"), ("dxlag:lag", "dxvif:v")]
    strict = graph(*edges, modes={"dxlag:lag": ("quorum", 2)})
    assert run(strict, ("dxcon:m1", OUTAGE))["dxvif:v"].severity == OUTAGE
    lenient = graph(*edges, modes={"dxlag:lag": ("quorum", 1)})
    state = run(lenient, ("dxcon:m1", OUTAGE))
    assert state["dxlag:lag"].severity == DEGRADED and state["dxvif:v"].severity == DEGRADED


def test_route_tables_scope_impact_per_destination():
    g = graph(
        ("tgwatt:dx", "tgwrtb:rt", "route", "10.100.0.0/16"),
        ("tgwatt:vpn", "tgwrtb:rt", "route", "10.200.0.0/16"),
        ("tgwrtb:rt", "tgwatt:vpc", "assoc"),
        ("tgwatt:vpc", "rtb:app", "route", "10.0.0.0/8"),
        ("tgwatt:vpc", "rtb:db", "route", "10.100.5.0/24"),
        ("tgwatt:vpc", "rtb:web", "route", "192.168.0.0/16"),
        ("rtb:app", "subnet:app"), ("rtb:db", "subnet:db"), ("rtb:web", "subnet:web"),
        modes={"tgwrtb:rt": "routes", "rtb:app": "routes", "rtb:db": "routes", "rtb:web": "routes"},
    )
    state = run(g, ("tgwatt:dx", DEGRADED), ("tgwatt:vpn", OUTAGE))
    assert state["tgwatt:vpc"].prefixes == {"10.100.0.0/16": DEGRADED, "10.200.0.0/16": OUTAGE}
    assert state["subnet:app"].severity == OUTAGE
    assert state["subnet:app"].prefixes == {"10.100.0.0/16": DEGRADED, "10.200.0.0/16": OUTAGE}
    # the db subnet only routes a /24 inside the DEGRADED prefix: it must not inherit the OUTAGE
    assert state["subnet:db"].severity == DEGRADED
    assert state["subnet:db"].prefixes == {"10.100.5.0/24": DEGRADED}
    assert "subnet:web" not in state


def test_ecmp_routes_are_redundant():
    g = graph(("tgwatt:vpn1", "tgwrtb:rt", "route", "10.0.0.0/16"), ("tgwatt:vpn2", "tgwrtb:rt", "route", "10.0.0.0/16"),
              modes={"tgwrtb:rt": "routes"})
    assert run(g, ("tgwatt:vpn1", OUTAGE))["tgwrtb:rt"].severity == DEGRADED
    assert run(g, ("tgwatt:vpn1", OUTAGE), ("tgwatt:vpn2", OUTAGE))["tgwrtb:rt"].severity == OUTAGE


def test_what_if_reports_consumers_only_downstream():
    g = redundant_dx()
    g.link("tgwatt:tgw-attach-dx", "app:payments", "app")
    report = what_if(g, ["circuit:a"])
    assert report["severity"] == DEGRADED  # the app is what counts, not the dead circuit
    assert report["infra_severity"] == OUTAGE
    assert [a["name"] for a in report["apps"]] == ["payments"]
    assert report["apps"][0]["chain"][0] == "circuit:a"


# --------------------------------------------------------------------------- anchor pruning


def site_graph():
    g = graph(
        ("site:ld5", "circuit:zayo"), ("site:ld5", "dxloc:EqLD5"), ("dxloc:EqLD5", "dxcon:dxcon-a"),
        ("circuit:zayo", "dxcon:dxcon-a"), ("dxcon:dxcon-a", "dxlag:lag"), ("dxcon:dxcon-b", "dxlag:lag"),
        ("site:thn", "circuit:colt"), ("circuit:colt", "dxcon:dxcon-c"),
        ("cgw:cgw-1", "vpntun:vpn-1/1.1.1.1"), ("vpntun:vpn-1/1.1.1.1", "vpn:vpn-1"),
    )
    return g


def pruned(g, *keys):
    kept, dropped = prune_anchors(g, [Anchor(k, OUTAGE, "") for k in keys])
    return sorted(a.key for a in kept), sorted(d["key"] for d in dropped)


def test_context_mentions_are_dropped():
    g = site_graph()
    assert pruned(g, "site:ld5", "circuit:zayo") == (["circuit:zayo"], ["site:ld5"])
    # DX location and circuit feed the same connection: location is context
    assert pruned(g, "dxloc:EqLD5", "circuit:zayo") == (["circuit:zayo"], ["dxloc:EqLD5"])
    # LAG named alongside one member: the member is the target
    assert pruned(g, "dxlag:lag", "dxcon:dxcon-b") == (["dxcon:dxcon-b"], ["dxlag:lag"])
    # a site unrelated to the circuit stays anchored
    assert pruned(g, "site:ld5", "circuit:colt") == (["circuit:colt", "site:ld5"], [])


def test_downstream_components_are_derived():
    g = site_graph()
    assert pruned(g, "circuit:zayo", "dxcon:dxcon-a") == (["circuit:zayo"], ["dxcon:dxcon-a"])
    assert pruned(g, "vpntun:vpn-1/1.1.1.1", "vpn:vpn-1") == (["vpntun:vpn-1/1.1.1.1"], ["vpn:vpn-1"])


# --------------------------------------------------------------------------- backup inference


def test_site_prefixes_turn_propagating_vpn_into_alternate():
    g = graph(
        ("site:dc", "circuit:dx"), ("circuit:dx", "dxgw:gw"), ("dxgw:gw", "tgwatt:dxatt"),
        ("site:dc", "cgw:cgw-dc"), ("cgw:cgw-dc", "vpn:vpn-dc"), ("vpn:vpn-dc", "tgwatt:vpnatt"),
        ("site:hq", "cgw:cgw-hq"), ("cgw:cgw-hq", "vpn:vpn-hq"), ("vpn:vpn-hq", "tgwatt:hqatt"),
        ("tgwatt:dxatt", "tgwrtb:rt", "route", "10.100.0.0/16"),
        ("tgwatt:hqatt", "tgwrtb:rt", "route", "10.200.0.0/16"),
        modes={"tgwrtb:rt": "routes", "dxgw:gw": "any"},
    )
    g.nodes["site:dc"].attrs["prefixes"] = ["10.100.0.0/16"]
    g.nodes["site:hq"].attrs["prefixes"] = ["10.200.0.0/16"]
    g.nodes["tgwrtb:rt"].attrs["propagations"] = [
        {"attachment": a, "resource_type": t} for a, t in
        (("dxatt", "direct-connect-gateway"), ("vpnatt", "vpn"), ("hqatt", "vpn"))]
    infer_site_alternates(g)
    alternates = [(e.src, e.prefix) for e in g.up("tgwrtb:rt") if e.alternate]
    assert alternates == [("tgwatt:vpnatt", "10.100.0.0/16")]  # HQ VPN does not reach the DC
    state = run(g, ("circuit:dx", OUTAGE))
    assert state["tgwrtb:rt"].severity == DEGRADED
    assert "still via" in state["tgwrtb:rt"].reasons[0]


# --------------------------------------------------------------------------- identifier index


def index_graph():
    g = Graph()
    g.add(Node(key=make_key("circuit", "OGYX/123456//ZYO"), type="circuit", id="OGYX/123456//ZYO",
               aliases=["LD5", "ZYO-123456"], ips=["203.0.113.8/29"]))
    g.add(Node(key="dxcon:dxcon-fgzy1a01", type="dxcon", id="dxcon-fgzy1a01", name="ld5-zayo-10g"))
    g.add(Node(key="cgw:cgw-1", type="cgw", id="cgw-1", ips=["198.51.100.10"]))
    g.add(Node(key="subnet:subnet-1", type="subnet", id="subnet-1", name="prod"))
    g.add(Node(key="app:payments", type="app", id="payments"))
    return IdentifierIndex(g)


def test_scan_is_separator_insensitive_but_respects_boundaries():
    idx = index_graph()
    for text in ("Circuit OGYX/123456//ZYO affected", "circuit ogyx 123456 zyo", "ref OGYX123456ZYO.",
                 "IDs: ZYO-123456"):
        assert {m.key for m in idx.scan(text)} == {"circuit:ogyx123456zyo"}, text
    for text in ("OGYX1234567ZYO", "XOGYX/123456//ZYO2", "LD5 Slough", "payments team", "prod network"):
        assert idx.scan(text) == [], text


def test_names_ids_and_ips():
    idx = index_graph()
    assert {m.key for m in idx.scan("connection ld5-zayo-10g and dxcon-fgzy1a01")} == {"dxcon:dxcon-fgzy1a01"}
    assert [m.kind for m in idx.scan("peer 198.51.100.10")] == ["ip"]
    assert {(m.key, m.kind) for m in idx.scan("your block 198.51.100.8/29")} == {("cgw:cgw-1", "cidr")}
    assert idx.scan("everything in 198.0.0.0/8") == []  # too broad to mean anything
    assert {(m.key, m.kind) for m in idx.scan("203.0.113.10 will flap")} == {("circuit:ogyx123456zyo", "block")}


def test_lookup_of_declared_identifier_with_decoration():
    idx = index_graph()
    assert [m.key for m in idx.lookup("OGYX/123456//ZYO")] == ["circuit:ogyx123456zyo"]
    assert [m.key for m in idx.lookup("OGYX/123456//ZYO (10G wave)")] == ["circuit:ogyx123456zyo"]
    assert idx.lookup("GTT-99887766") == []
