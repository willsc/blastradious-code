"""Estate topology map: on-prem sites -> carrier circuits -> AWS edge (DX locations, Internet VPN) ->
DX gateways -> transit / virtual private gateways -> VPCs with their subnets and hosts.

Drawn the way network diagrams usually are — fixed columns, on-prem on the left, VPC boxes on the
right — with barycentric vertical placement (right to left) so links run as straight as possible.
Maintenance is painted on top: boxes take the colour of the worst notice that reaches them and are
badged where the work actually happens (the circuit, the DX connection, the facility, the VPN).
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape
from urllib.parse import quote

from ..topology.model import Graph, Node

COLUMNS = [
    ("sites", "On-prem sites", 160),
    ("circuits", "Circuits", 196),
    ("edge", "AWS edge", 236),
    ("dxgw", "DX gateways", 164),
    ("gateways", "Transit / VPN gateways", 172),
    ("vpcs", "VPCs · subnets · hosts", 560),
]
GAP_X, GAP_Y, PAD, HEAD, LINE = 80, 16, 16, 40, 14
CHIP_W, CHIP_H, CHIP_GAP, CHIPS_PER_ROW, MAX_CHIPS = 116, 22, 6, 3, 9
SUBNET_LABEL_W = 176


@dataclass
class Box:
    key: str
    kind: str
    title: str
    lines: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    children: list[Box] = field(default_factory=list)
    severity: int = 0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    href: str | None = None
    tooltip: str = ""
    classes: set[str] = field(default_factory=set)
    desired: float | None = None

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Link:
    src: str
    dst: str
    label: str = ""
    carrier: str | None = None  # the node whose severity colours this link (VIF, VPN, attachment...)
    kind: str = ""
    severity: int = 0
    show_label: bool = True  # VIF / VPN details live inside the boxes; route-table names ride on the link


@dataclass
class Overlay:
    severity: dict[str, int] = field(default_factory=dict)
    badges: dict[str, list[str]] = field(default_factory=dict)  # node key -> short notice labels
    selected: bool = False  # one notice is shown: everything it does not reach is dimmed
    title: str = ""
    # per-destination severity of route-scoped nodes (TGW attachments...); absent = whole node
    prefixes: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, key: str, severity: int, prefixes: dict[str, int] | None) -> None:
        """Merge one notice's (or collision's) impact on a node."""
        if severity <= 0:
            return
        unscoped = key in self.severity and key not in self.prefixes
        self.severity[key] = max(self.severity.get(key, 0), severity)
        if prefixes is None or unscoped:
            self.prefixes.pop(key, None)
            if prefixes is None:
                return
            if unscoped:
                return
        merged = self.prefixes.setdefault(key, {})
        for prefix, sev in prefixes.items():
            merged[prefix] = max(merged.get(prefix, 0), sev)


def _fit(text: str, width: float, char_w: float) -> str:
    limit = max(4, int((width - 22) / char_w))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    u = 1 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


def _overlaps(a: str, b: str) -> bool:
    try:
        na, nb = ipaddress.ip_network(a, strict=False), ipaddress.ip_network(b, strict=False)
    except ValueError:
        return True  # prefix lists: assume they may overlap
    return na.version == nb.version and na.overlaps(nb)


def _node_href(key: str) -> str:
    return f"/topology/node?key={quote(key, safe=':')}"


class EstateMap:
    def __init__(self, graph: Graph, overlay: Overlay | None = None, *, account: str = "", vpc: str = "",
                 app: str = "", hosts: bool = True, focus: str = "", account_names: dict[str, str] | None = None):
        self.g = graph
        self.overlay = overlay or Overlay()
        self.account, self.vpc, self.app = account.strip(), vpc.strip(), app.strip()
        self.show_hosts = hosts
        self.focus = focus
        self.account_names = account_names or {}
        self.columns: dict[str, list[Box]] = {name: [] for name, _, _ in COLUMNS}
        self.boxes: dict[str, Box] = {}
        self.links: list[Link] = []
        self.width = self.height = 0.0
        self.host_count = 0
        self.app_members: set[str] = set()
        self._link_keys: set[tuple[str, str]] = set()
        self._containers: dict[str, Box] = {}

    # ------------------------------------------------------------------ helpers

    @property
    def filtered(self) -> bool:
        return bool(self.account or self.vpc or self.app)

    def sev(self, key: str) -> int:
        return self.overlay.severity.get(key, 0)

    def _account_label(self, node: Node) -> str:
        return self.account_names.get(node.account or "") or node.attrs.get("account_name") or node.account or ""

    def _link(self, src: str, dst: str, label: str = "", carrier: str | None = None, kind: str = "",
              show_label: bool = True) -> None:
        if (src, dst) in self._link_keys or src not in self.boxes or dst not in self.boxes:
            return
        self._link_keys.add((src, dst))
        self.links.append(Link(src, dst, label, carrier, kind, show_label=show_label))

    def _node(self, key: str) -> Node:
        node = self.g.get(key)
        if node is None:
            ntype, ident = key.split(":", 1)
            node = Node(key=key, type=ntype, id=ident, stub=True)
        return node

    def _register(self, box: Box) -> Box:
        self.boxes[box.key] = box
        return box

    def _leaf(self, node: Node, kind: str, lines: list[str], column: str | None) -> Box:
        if node.key in self.boxes:
            return self.boxes[node.key]
        box = self._register(Box(key=node.key, kind=kind, title=node.name or node.id, lines=[t for t in lines if t],
                                 severity=self.sev(node.key), href=_node_href(node.key),
                                 tooltip=f"{node.type_label} {node.label}"))
        if column:
            self.columns[column].append(box)
        return box

    def hosts_of(self, subnet: Node) -> list[Node]:
        out, seen = [], set()
        for eni in self.g.children(subnet.key, "eni"):
            instances = self.g.children(eni.key, "instance")
            if instances:
                for inst in instances:
                    if inst.key not in seen:
                        seen.add(inst.key)
                        out.append(inst)
            elif not eni.attrs.get("infra"):
                out.append(eni)
        for inst in self.g.children(subnet.key, "instance"):
            if inst.key not in seen:
                seen.add(inst.key)
                out.append(inst)
        return out

    @staticmethod
    def host_title(node: Node) -> str:
        if node.type == "instance":
            return node.name or node.id
        kind, detail = node.attrs.get("kind") or "ENI", node.attrs.get("detail") or ""
        return node.id if kind == "EC2" else f"{kind} {detail}".strip()

    # ------------------------------------------------------------------ build

    def build(self) -> EstateMap:
        g = self.g
        if self.app:
            app_key = f"app:{self.app.lower()}"
            self.app_members = {n.key for n in g.parents(app_key)} if app_key in g.nodes else set()

        # VPCs, subnets, hosts
        vpc_ids: set[str] = set()
        for vpc in sorted(g.of_type("vpc"), key=lambda n: (self._account_label(n), n.name or n.id)):
            if self.account and self.account.lower() not in {str(vpc.account or "").lower(), self._account_label(vpc).lower()}:
                continue
            if self.vpc and self.vpc.lower() not in {vpc.id.lower(), (vpc.name or "").lower()}:
                continue
            subnets = sorted(g.children(vpc.key, "subnet"), key=lambda s: (s.name or s.id))
            hosts = {s.key: self.hosts_of(s) for s in subnets}
            if self.app and not any(s.key in self.app_members or any(h.key in self.app_members for h in hosts[s.key])
                                    for s in subnets):
                continue
            if vpc.attrs.get("is_default") and not any(hosts.values()):
                continue
            vpc_ids.add(vpc.id)
            self._vpc_box(vpc, subnets, hosts)

        # Gateways that serve those VPCs
        gateways: list[str] = []
        vpc_attachments: dict[str, list[Node]] = defaultdict(list)
        for att in g.of_type("tgwatt"):
            if att.attrs.get("resource_type") == "vpc" and att.attrs.get("resource_id") in vpc_ids and att.attrs.get("tgw"):
                tgw = self._node(f"tgw:{att.attrs['tgw']}")
                box = self._leaf(tgw, "tgw", [tgw.id if tgw.name else "", "transit gateway"], "gateways")
                gateways.append(box.key)
                vpc_attachments[tgw.key].append(att)
                table = g.get(f"tgwrtb:{att.attrs.get('association')}") if att.attrs.get("association") else None
                self._link(tgw.key, f"vpc:{att.attrs['resource_id']}",
                           label=(table.name or table.id) if table else "", carrier=att.key, kind="attach")
        for vgw in g.of_type("vgw"):
            attached = [v for v in vgw.attrs.get("vpcs", []) if v in vpc_ids]
            if attached:
                box = self._leaf(vgw, "vgw", [vgw.id if vgw.name else "", "virtual private gateway"], "gateways")
                gateways.append(box.key)
                for vpc_id in attached:
                    self._link(vgw.key, f"vpc:{vpc_id}", carrier=vgw.key, kind="attach")

        # DX gateways, VIFs, VPNs feeding those gateways
        dx_gateways: list[str] = []
        for gw_key in dict.fromkeys(gateways):
            if self.boxes[gw_key].kind == "tgw":
                # Filtered view: only the hybrid attachments these VPCs actually route through.
                relevant = None
                if self.filtered:
                    relevant = set()
                    for att in vpc_attachments.get(gw_key, []):
                        relevant |= self._routed_hybrid(att)
                for att in g.children(gw_key, "tgwatt"):
                    if relevant is not None and att.key not in relevant:
                        continue
                    rtype, rid = att.attrs.get("resource_type"), att.attrs.get("resource_id")
                    if rtype == "direct-connect-gateway" and rid:
                        dx = self._node(f"dxgw:{rid}")
                        self._leaf(dx, "dxgw", [f"ASN {dx.attrs['asn']}" if dx.attrs.get("asn") else ""], "dxgw")
                        dx_gateways.append(dx.key)
                        self._link(dx.key, gw_key, carrier=att.key, kind="assoc")
                    elif rtype == "vpn" and rid and f"vpn:{rid}" in g.nodes:
                        self._vpn(g.nodes[f"vpn:{rid}"], gw_key)
            else:
                for dx in g.parents(gw_key, "dxgw"):
                    self._leaf(dx, "dxgw", [f"ASN {dx.attrs['asn']}" if dx.attrs.get("asn") else ""], "dxgw")
                    dx_gateways.append(dx.key)
                    self._link(dx.key, gw_key, carrier=dx.key, kind="assoc")
                for vpn in g.parents(gw_key, "vpn"):
                    self._vpn(vpn, gw_key)
                for vif in g.parents(gw_key, "dxvif"):
                    self._vif(vif, gw_key)
        for dx_key in dict.fromkeys(dx_gateways):
            for vif in g.parents(dx_key, "dxvif"):
                self._vif(vif, dx_key)

        if not self.filtered:  # the whole estate, including edge devices that serve no VPC yet
            for node in sorted(g.of_type("dxlag", "dxcon"), key=lambda n: n.id):
                self._device(node)
            for node in sorted(g.of_type("cgw"), key=lambda n: n.id):
                self._cgw(node)

        # Circuits and the on-prem sites they come from
        for circuit in sorted(g.of_type("circuit"), key=lambda n: n.id):
            targets = [n for n in g.children(circuit.key) if n.key in self.boxes and n.type in ("dxcon", "dxlag", "cgw")]
            if self.filtered and not targets:
                continue
            provider = circuit.attrs.get("provider") or ""
            bandwidth = circuit.attrs.get("bandwidth") or ""
            box = self._leaf(circuit, "circuit", [" · ".join(v for v in (provider, bandwidth) if v)], "circuits")
            box.title = circuit.id
            box.tooltip = f"Circuit {circuit.id}\n{circuit.attrs.get('description') or ''}".strip()
            if not targets:
                box.classes.add("unlinked")
            for target in targets:
                self._link(circuit.key, target.key, carrier=circuit.key, kind="circuit")
            for site in g.parents(circuit.key, "site"):
                if site.attrs.get("role", "onprem") == "colo":
                    continue
                self._site(site)
                self._link(site.key, circuit.key, carrier=circuit.key, kind="site")
        if not self.filtered:
            for site in sorted(g.of_type("site"), key=lambda n: n.id):
                if site.attrs.get("role", "onprem") != "colo":
                    self._site(site)

        for box in self.boxes.values():
            if box.kind == "dxcon" and not any(link.dst == box.key for link in self.links) and \
                    not any(n.type == "circuit" for n in g.parents(box.key)):
                box.classes.add("nocircuit")
                box.lines.append("no circuit mapped")
        self._place_badges()
        self._link_severities()
        self._dim_and_focus()
        return self

    def _link_severities(self) -> None:
        for link in self.links:
            sev = self.sev(link.carrier) if link.carrier else 0
            if link.kind == "attach" and sev:
                sev = self._attach_severity(link, sev)
            link.severity = sev

    def _attach_severity(self, link: Link, sev: int) -> int:
        """Severity of a gateway -> VPC link: only what this VPC actually routes through it. A TGW route
        table shared with other VPCs can be hit for prefixes this VPC never sends there."""
        vpc_id = self.boxes[link.dst].key.split(":", 1)[1]
        via = link.carrier if link.carrier and link.carrier.startswith("tgwatt:") else link.src
        routed = [e.prefix for rtb in self._rtbs(vpc_id) for e in self.g.up(rtb.key)
                  if e.kind == "route" and e.src == via and e.prefix]
        if not routed:
            return 0
        scoped = self.overlay.prefixes.get(link.carrier or "")
        if scoped is None:
            return sev
        return max((s for q, s in scoped.items() if any(_overlaps(q, p) for p in routed)), default=0)

    def _rtbs(self, vpc_id: str) -> list[Node]:
        if not hasattr(self, "_rtbs_by_vpc"):
            self._rtbs_by_vpc: dict[str, list[Node]] = defaultdict(list)
            for rtb in self.g.of_type("rtb"):
                self._rtbs_by_vpc[rtb.attrs.get("vpc")].append(rtb)
        return self._rtbs_by_vpc.get(vpc_id, [])

    def _routed_hybrid(self, vpc_attachment: Node) -> set[str]:
        """Hybrid TGW attachments that carry prefixes this VPC sends to the TGW."""
        vpc_id = vpc_attachment.attrs.get("resource_id")
        routed = [e.prefix for rtb in self._rtbs(vpc_id) for e in self.g.up(rtb.key)
                  if e.kind == "route" and e.src == vpc_attachment.key and e.prefix]
        table = f"tgwrtb:{vpc_attachment.attrs.get('association')}"
        return {e.src for e in self.g.up(table)
                if e.kind == "route" and e.prefix and any(_overlaps(e.prefix, p) for p in routed)}

    # -- box factories ---------------------------------------------------------

    def _vpc_box(self, vpc: Node, subnets: list[Node], hosts: dict[str, list[Node]]) -> None:
        cidrs = ", ".join(vpc.attrs.get("cidrs") or [])
        vbox = self._register(Box(key=vpc.key, kind="vpc", title=vpc.name or vpc.id, href=_node_href(vpc.key),
                                  lines=[" · ".join(v for v in (vpc.id if vpc.name else "", cidrs,
                                                                 self._account_label(vpc), vpc.region or "") if v)],
                                  tooltip=f"VPC {vpc.label}"))
        for subnet in subnets:
            members = hosts[subnet.key]
            self.host_count += len(members)
            sbox = self._register(Box(key=subnet.key, kind="subnet", title=subnet.name or subnet.id,
                                      href=_node_href(subnet.key), severity=self.sev(subnet.key),
                                      lines=[" · ".join(v for v in (subnet.attrs.get("cidr"), subnet.attrs.get("az")) if v)],
                                      tooltip=f"Subnet {subnet.label} {subnet.attrs.get('cidr') or ''}"))
            if self.show_hosts:
                shown = members if len(members) <= MAX_CHIPS else members[: MAX_CHIPS - 1]
                for host in shown:
                    ips = host.attrs.get("private_ip") or ", ".join(host.attrs.get("private_ips") or [])
                    apps = ", ".join(host.apps)
                    chip = self._register(Box(key=host.key, kind="host", title=self.host_title(host),
                                              severity=self.sev(host.key), href=_node_href(host.key),
                                              tooltip=f"{host.type_label} {host.label}\n{ips}" + (f"\napps: {apps}" if apps else "")))
                    if self.app and host.key not in self.app_members and subnet.key not in self.app_members:
                        chip.classes.add("other-app")
                    sbox.children.append(chip)
                if len(members) > len(shown):
                    sbox.children.append(Box(key=f"{subnet.key}#more", kind="more",
                                             title=f"+{len(members) - len(shown)} more", href=_node_href(subnet.key)))
            else:
                sbox.lines.append(f"{len(members)} host{'s' if len(members) != 1 else ''}")
            vbox.children.append(sbox)
        vbox.severity = max([self.sev(vpc.key)] + [b.severity for s in vbox.children for b in s.walk()])
        self.columns["vpcs"].append(vbox)

    def _container(self, key: str, title: str, lines: list[str], href: str | None) -> Box:
        if key not in self._containers:
            box = self._register(Box(key=key, kind="location", title=title, lines=lines, href=href,
                                     severity=self.sev(key), tooltip=title))
            self._containers[key] = box
            self.columns["edge"].append(box)
        return self._containers[key]

    def _location(self, code: str | None) -> Box:
        if not code:
            return self._container("dxloc:?", "Direct Connect (location unknown)", [], None)
        node = self.g.get(f"dxloc:{code}")
        colo = next((s for s in self.g.parents(f"dxloc:{code}", "site")), None)
        name = (colo.name if colo else "") or (node.name.split(",")[0] if node and node.name else code)
        return self._container(f"dxloc:{code}", f"{name} · {code}", ["Direct Connect location"],
                               _node_href(f"dxloc:{code}"))

    def _device(self, node: Node) -> Box | None:
        if node.key in self.boxes:
            return self.boxes[node.key]
        g = self.g
        if node.type == "dxcon" and node.attrs.get("lag") and f"dxlag:{node.attrs['lag']}" in g.nodes:
            self._device(g.nodes[f"dxlag:{node.attrs['lag']}"])
            return self.boxes.get(node.key)
        if node.type == "dxlag":
            members = sorted(g.parents(node.key, "dxcon"), key=lambda n: n.id)
            location = node.attrs.get("location") or next((m.attrs.get("location") for m in members), None)
            lag = self._register(Box(key=node.key, kind="lag", title=node.name or node.id, href=_node_href(node.key),
                                     severity=self.sev(node.key), tooltip=f"LAG {node.label}",
                                     lines=[f"LAG · minimumLinks {node.attrs.get('minimum_links', 0)}"]))
            for member in members:
                lag.children.append(self._connection_box(member))
            self._location(location).children.append(lag)
            return lag
        if node.type == "dxcon":
            box = self._connection_box(node)
            self._location(node.attrs.get("location")).children.append(box)
            return box
        return None

    def _connection_box(self, node: Node) -> Box:
        lines = [" · ".join(v for v in (node.id if node.name else "", node.attrs.get("bandwidth"),
                                        node.attrs.get("provider") or node.attrs.get("partner")) if v)]
        return self._register(Box(key=node.key, kind="dxcon", title=node.name or node.id, lines=lines,
                                  severity=self.sev(node.key), href=_node_href(node.key),
                                  tooltip=f"DX connection {node.label}\n{node.attrs.get('aws_device') or ''}".strip()))

    def _cgw(self, node: Node) -> Box:
        if node.key in self.boxes:
            return self.boxes[node.key]
        internet = self._container("internet", "Internet · Site-to-Site VPN", ["customer gateways"], None)
        box = self._register(Box(key=node.key, kind="cgw", title=node.name or node.id, severity=self.sev(node.key),
                                 lines=[" · ".join(v for v in (node.attrs.get("ip"), node.attrs.get("device")) if v)],
                                 href=_node_href(node.key), tooltip=f"Customer gateway {node.label}"))
        internet.children.append(box)
        return box

    def _vif(self, vif: Node, target: str) -> None:
        device = next((n for n in self.g.parents(vif.key) if n.type in ("dxcon", "dxlag")), None)
        if device is None:
            return
        box = self._device(device)
        if box is None:
            return
        vlan = vif.attrs.get("vlan")
        label = f"VIF {vif.name or vif.id}" + (f" · VLAN {vlan}" if vlan else "")
        if label not in box.lines:
            box.lines.append(label)
        self._link(box.key, target, label=label, carrier=vif.key, kind="vif", show_label=False)

    def _vpn(self, vpn: Node, target: str) -> None:
        cgw_id = vpn.attrs.get("cgw")
        if not cgw_id or f"cgw:{cgw_id}" not in self.g.nodes:
            return
        box = self._cgw(self.g.nodes[f"cgw:{cgw_id}"])
        tunnels = len(self.g.parents(vpn.key, "vpntun"))
        label = f"VPN {vpn.name or vpn.id}" + (f" · {tunnels} tunnels" if tunnels else "")
        if label not in box.lines:
            box.lines.append(label)
        self._link(box.key, target, label=label, carrier=vpn.key, kind="vpn", show_label=False)

    def _site(self, site: Node) -> Box:
        prefixes = site.attrs.get("prefixes") or []
        return self._leaf(site, "site", [", ".join(prefixes[:2]) + (" …" if len(prefixes) > 2 else "")], "sites")

    # -- overlay ----------------------------------------------------------------

    def _badge_target(self, key: str) -> str | None:
        node = self.g.get(key)
        if node is None:
            return None
        if node.type == "vpntun":
            vpn = next(iter(self.g.children(key, "vpn")), None)
            return self._badge_target(vpn.key) if vpn else None
        if node.type == "vpn":
            cgw = f"cgw:{node.attrs.get('cgw')}"
            return cgw if cgw in self.boxes else None
        if node.type == "dxvif":
            device = next((n for n in self.g.parents(key) if n.type in ("dxcon", "dxlag")), None)
            return device.key if device and device.key in self.boxes else None
        if node.type == "tgwatt":
            tgw = f"tgw:{node.attrs.get('tgw')}"
            return tgw if tgw in self.boxes else None
        if node.type == "site":
            for loc in self.g.children(key, "dxloc"):
                if loc.key in self.boxes:
                    return loc.key
        return None

    def _place_badges(self) -> None:
        for key, labels in self.overlay.badges.items():
            target = key if key in self.boxes else self._badge_target(key)
            if not target:
                continue
            box = self.boxes[target]
            for label in labels:
                if label not in box.badges:
                    box.badges.append(label)
            box.classes.add("anchored")

    def _dim_and_focus(self) -> None:
        on_path = {k for link in self.links if link.severity for k in (link.src, link.dst)}
        for box in self.boxes.values():
            touched = any(b.severity or b.key in on_path for b in box.walk())
            if self.overlay.selected and not touched and "anchored" not in box.classes:
                box.classes.add("dim")
            if box.key == self.focus:
                box.classes.add("focus")

    # ------------------------------------------------------------------ layout

    @staticmethod
    def _head_h(box: Box) -> float:
        return 22 + 12 * max(1, len(box.lines))

    def _size(self, box: Box, width: float) -> None:
        box.w = width
        if box.kind == "vpc":
            y = self._head_h(box) + 6
            for sub in box.children:
                self._size_subnet(sub, width - 16)
                y += sub.h + 6
            box.h = y + 4
        elif box.kind in ("location", "lag"):
            gap = 6 if box.kind == "location" else 5
            y = self._head_h(box) + gap
            for child in box.children:
                self._size(child, width - 14)
                y += child.h + gap
            box.h = max(y + 2, 46)
        else:
            box.h = 24 + LINE * len(box.lines) + LINE * min(len(box.badges), 3) + 6

    def _size_subnet(self, sub: Box, width: float) -> None:
        sub.w = width
        rows = -(-len(sub.children) // CHIPS_PER_ROW) if sub.children else 0
        sub.h = max(40, 10 + rows * (CHIP_H + CHIP_GAP))
        for chip in sub.children:
            chip.w, chip.h = CHIP_W, CHIP_H

    def _position(self, box: Box, x: float, y: float) -> None:
        box.x, box.y = x, y
        if box.kind == "vpc":
            cy = y + self._head_h(box) + 6
            for sub in box.children:
                self._position(sub, x + 8, cy)
                cy += sub.h + 6
        elif box.kind == "subnet":
            for i, chip in enumerate(box.children):
                row, col = divmod(i, CHIPS_PER_ROW)
                chip.x = x + SUBNET_LABEL_W + col * (CHIP_W + CHIP_GAP)
                chip.y = y + 6 + row * (CHIP_H + CHIP_GAP)
        elif box.kind in ("location", "lag"):
            gap = 6 if box.kind == "location" else 5
            cy = y + self._head_h(box) + gap
            for child in box.children:
                self._position(child, x + 7, cy)
                cy += child.h + gap

    def _anchor_y(self, box: Box) -> float:
        return box.y + 20 if box.kind == "vpc" else box.cy

    def _desired(self, box: Box) -> float | None:
        keys = {b.key for b in box.walk()}
        ys = [self._anchor_y(self.boxes[link.dst]) for link in self.links if link.src in keys and self.boxes[link.dst].h]
        return sum(ys) / len(ys) if ys else None

    def layout(self) -> EstateMap:
        widths = {name: width for name, _, width in COLUMNS}
        x = PAD
        self.column_x: dict[str, float] = {}
        for name, _, width in COLUMNS:
            if self.columns[name]:
                self.column_x[name] = x
                x += width + GAP_X
        self.width = max(x - GAP_X + PAD, 400)
        for name, boxes in self.columns.items():
            for box in boxes:
                self._size(box, widths[name])
        order = [name for name, _, _ in COLUMNS if self.columns[name]]
        if not order:
            self.height = HEAD + 2 * PAD
            return self
        # Rightmost column: VPCs grouped by the gateway that serves them.
        right = self.columns[order[-1]]
        first_gateway = {}
        for link in self.links:
            first_gateway.setdefault(link.dst, self.boxes[link.src].title)
        right.sort(key=lambda b: (first_gateway.get(b.key, "~"), b.title))
        y = PAD + HEAD
        for box in right:
            self._position(box, self.column_x[order[-1]], y)
            y += box.h + GAP_Y + 8
        # Remaining columns right-to-left, each box near the mean height of what it connects to.
        for name in reversed(order[:-1]):
            boxes = self.columns[name]
            for box in boxes:
                if box.kind in ("location", "lag"):
                    for child in box.children:
                        child.desired = self._desired(child)
                    box.children.sort(key=lambda c: (c.desired is None, c.desired or 0))
                    self._size(box, widths[name])
                box.desired = self._desired(box)
            boxes.sort(key=lambda b: (b.desired is None, b.desired or 0, b.title))
            bottom = PAD + HEAD - GAP_Y
            for box in boxes:
                top = bottom + GAP_Y if box.desired is None else max(box.desired - box.h / 2, bottom + GAP_Y)
                self._position(box, self.column_x[name], top)
                bottom = top + box.h
        self.height = max(b.y + b.h for b in self.boxes.values() if b.h) + PAD
        return self

    # ------------------------------------------------------------------ render

    def svg(self, standalone: bool = False) -> str:
        w, h = int(self.width), int(self.height)
        out = [f'<svg class="estate" xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
               f'data-width="{w}" role="img" aria-label="Estate topology map">']
        if standalone:
            out.append(f"<style>{STANDALONE_CSS}</style>")
            out.append(f'<rect width="{w}" height="{h}" class="bg"/>')
        out.append('<defs><marker id="map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" '
                   'markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" class="arrowhead"/></marker></defs>')
        widths = {name: width for name, _, width in COLUMNS}
        for name, title, _ in COLUMNS:
            if name in getattr(self, "column_x", {}):
                x = self.column_x[name]
                out.append(f'<text class="t-colhead" x="{x:.0f}" y="{PAD + 14}">{escape(title)}</text>')
                out.append(f'<line class="colrule" x1="{x:.0f}" y1="{PAD + 22}" x2="{x + widths[name]:.0f}" y2="{PAD + 22}"/>')
        labels = []
        fan_out: dict[str, int] = {}
        fan_in: dict[str, int] = {}
        for link in self.links:
            fan_out[link.src] = fan_out.get(link.src, 0) + 1
            fan_in[link.dst] = fan_in.get(link.dst, 0) + 1
        for link in self.links:
            src, dst = self.boxes[link.src], self.boxes[link.dst]
            x1, y1 = src.x + src.w, src.cy
            x2, y2 = dst.x, self._anchor_y(dst)
            dx = max(30.0, (x2 - x1) / 2)
            sev = link.severity
            dim = self.overlay.selected and not sev
            cls = f"mlink sev-{sev} k-{link.kind}" + (" dim" if dim else "")
            title = escape(link.label or f"{src.title} → {dst.title}")
            out.append(f'<path class="{cls}" d="M{x1:.0f},{y1:.0f} C{x1 + dx:.0f},{y1:.0f} {x2 - dx:.0f},{y2:.0f} '
                       f'{x2:.0f},{y2:.0f}" marker-end="url(#map-arrow)"><title>{title}</title></path>')
            if link.label and link.show_label and x2 - x1 >= min(len(link.label), 30) * 5.6 + 30:
                # label where this link is alone: near the target if the source fans out, else near the source
                t = 0.72 if fan_out[link.src] > fan_in[link.dst] else 0.3
                mx = _bezier(t, x1, x1 + dx, x2 - dx, x2)
                my = _bezier(t, y1, y1, y2, y2)
                labels.append((mx, my, link.label, sev, dim))
        for column in self.columns.values():
            for box in column:
                out.append(self._render_box(box))
        for mx, my, text, sev, dim in labels:  # link labels on top of everything
            text = text if len(text) <= 30 else text[:29] + "…"
            tw = len(text) * 5.6 + 8
            out.append(f'<g class="mlabel sev-{sev}{" dim" if dim else ""}"><rect x="{mx - tw / 2:.0f}" y="{my - 8:.0f}" '
                       f'width="{tw:.0f}" height="15" rx="4"/><text x="{mx:.0f}" y="{my + 3:.0f}" '
                       f'text-anchor="middle">{escape(text)}</text></g>')
        out.append("</svg>")
        return "\n".join(out)

    def _render_box(self, box: Box) -> str:
        x, y, w, h = box.x, box.y, box.w, box.h
        if box.kind in ("vpc", "location", "lag"):
            head_h = self._head_h(box)
            face = [f'<rect class="frame" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="9"/>',
                    f'<rect class="head" x="{x + 1:.0f}" y="{y + 1:.0f}" width="{w - 2:.0f}" height="{head_h}" rx="8"/>',
                    f'<text class="t-title" x="{x + 10:.0f}" y="{y + 16:.0f}">'
                    f'{escape(_fit(box.title, w - (60 if box.badges else 0), 7.4))}</text>']
            face += [f'<text class="t-line" x="{x + 10:.0f}" y="{y + 29 + i * 12:.0f}">{escape(_fit(line, w, 5.9))}</text>'
                     for i, line in enumerate(box.lines)]
            if box.badges:
                face.append(f'<text class="t-badge" x="{x + w - 10:.0f}" y="{y + 16:.0f}" text-anchor="end">'
                            f'⚠ {len(box.badges)}</text>')
            inner = "".join(self._render_box(child) for child in box.children)
            return self._group(box, "".join(face), inner)
        if box.kind == "subnet":
            face = [f'<rect class="frame" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="6"/>',
                    f'<rect class="stripe" x="{x:.0f}" y="{y:.0f}" width="4" height="{h:.0f}" rx="2"/>',
                    f'<text class="t-title" x="{x + 12:.0f}" y="{y + 16:.0f}">{escape(_fit(box.title, SUBNET_LABEL_W, 7.2))}</text>']
            face += [f'<text class="t-line" x="{x + 12:.0f}" y="{y + 30 + i * LINE:.0f}">{escape(_fit(line, SUBNET_LABEL_W, 5.9))}</text>'
                     for i, line in enumerate(box.lines)]
            return self._group(box, "".join(face), "".join(self._render_box(c) for c in box.children))
        if box.kind in ("host", "more"):
            mark = "⚠ " if "anchored" in box.classes else ""
            face = (f'<rect class="frame" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="5"/>'
                    f'<text class="t-chip" x="{x + 7:.0f}" y="{y + 15:.0f}">{escape(_fit(mark + box.title, w + 8, 6.1))}</text>')
            return self._group(box, face)
        # leaf boxes: site, circuit, DX connection, customer gateway, DX / transit / virtual gateways
        face = [f'<rect class="frame" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="7"/>',
                f'<rect class="stripe" x="{x:.0f}" y="{y:.0f}" width="5" height="{h:.0f}" rx="2"/>',
                f'<text class="t-title{" mono" if box.kind == "circuit" else ""}" x="{x + 12:.0f}" y="{y + 17:.0f}">'
                f'{escape(_fit(box.title, w, 7.2))}</text>']
        face += [f'<text class="t-line" x="{x + 12:.0f}" y="{y + 31 + i * LINE:.0f}">{escape(_fit(line, w, 5.9))}</text>'
                 for i, line in enumerate(box.lines)]
        base = y + 31 + len(box.lines) * LINE
        badges = box.badges if len(box.badges) <= 3 else box.badges[:2] + [f"+{len(box.badges) - 2} more"]
        face += [f'<text class="t-badge" x="{x + 12:.0f}" y="{base + i * LINE:.0f}">⚠ {escape(_fit(badge, w - 12, 5.9))}</text>'
                 for i, badge in enumerate(badges)]
        return self._group(box, "".join(face))

    @staticmethod
    def _group(box: Box, face: str, inner: str = "") -> str:
        cls = " ".join(["mbox", f"kind-{box.kind}", f"sev-{box.severity}", *sorted(box.classes)])
        ident = ' id="map-focus"' if "focus" in box.classes else ""
        tooltip = box.tooltip + ("\n" + "\n".join(box.badges) if box.badges else "")
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        opener = f'<a class="face" href="{escape(box.href)}">' if box.href else '<g class="face">'
        closer = "</a>" if box.href else "</g>"
        return f'<g class="{cls}"{ident}>{opener}{title}{face}{closer}{inner}</g>'


STANDALONE_CSS = """
svg.estate{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.bg{fill:#f5f6f8}.t-colhead{font-size:12px;font-weight:700;fill:#5d6673;letter-spacing:.04em}
.colrule{stroke:#e1e5ea}.face>.frame{fill:#fff;stroke:#c9d0d8}.face>.head{fill:#f3f5f8}.face>.stripe{fill:#c9d0d8}
.t-title{font-size:12px;font-weight:650;fill:#1a1e24}.t-title.mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.t-line{font-size:10px;fill:#5d6673}.t-chip{font-size:10.5px;fill:#1a1e24}.t-badge{font-size:10px;font-weight:700;fill:#b45309}
.mbox.sev-3>.face>.frame{stroke:#d42f24;stroke-width:1.6}.mbox.sev-3>.face>.stripe{fill:#d42f24}
.mbox.sev-2>.face>.frame{stroke:#d27606;stroke-width:1.6}.mbox.sev-2>.face>.stripe{fill:#d27606}
.mbox.sev-1>.face>.frame{stroke:#2d6bd4;stroke-width:1.4}.mbox.sev-1>.face>.stripe{fill:#2d6bd4}
.kind-host.sev-3>.face>.frame{fill:#fdecea}.kind-host.sev-2>.face>.frame{fill:#fdf1e0}.kind-host.sev-1>.face>.frame{fill:#e9f0fc}
.mbox.nocircuit>.face>.frame{stroke-dasharray:4 3}.mbox.dim>.face,.mlink.dim,.mlabel.dim{opacity:.3}.mbox.other-app{opacity:.35}
.mbox.focus>.face>.frame{stroke:#2764d6;stroke-width:3}
.mlink{fill:none;stroke:#9aa4b1;stroke-width:1.5}.mlink.sev-3{stroke:#d42f24;stroke-width:2.2}
.mlink.sev-2{stroke:#d27606;stroke-width:2.2}.mlink.sev-1{stroke:#2d6bd4;stroke-width:1.8}.mlink.k-vpn{stroke-dasharray:6 4}
.mlabel rect{fill:#fff;stroke:#e1e5ea}.mlabel text{font-size:9.5px;fill:#5d6673}.mlabel.sev-3 text{fill:#d42f24}
.mlabel.sev-2 text{fill:#b45309}.arrowhead{fill:#9aa4b1}
"""


def render(graph: Graph, overlay: Overlay | None = None, standalone: bool = False, **filters) -> tuple[str, EstateMap]:
    estate = EstateMap(graph, overlay, **filters).build().layout()
    return estate.svg(standalone=standalone), estate
