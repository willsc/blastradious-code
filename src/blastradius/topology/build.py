"""Assemble the dependency graph from AWS + inventory, map applications, infer backup paths."""

from __future__ import annotations

import ipaddress
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import AwsConfig
from .aws import add_to_graph
from .index import IdentifierIndex
from .inventory import Inventory, apply_inventory
from .model import Graph, Node, key_for, make_key

log = logging.getLogger(__name__)


@dataclass
class Topology:
    graph: Graph
    index: IdentifierIndex
    inventory: Inventory
    warnings: list[str] = field(default_factory=list)
    snapshot_id: int | None = None
    created_at: str | None = None

    def account_name(self, account_id: str | None) -> str | None:
        return self.inventory.account_name(account_id)

    def resolve(self, ref: str) -> str | None:
        """Node key for anything a person might type: a node key, AWS id, circuit id/alias, IP or name."""
        ref = ref.strip()
        if ref in self.graph.nodes:
            return ref
        key = key_for(ref)
        if key and key in self.graph.nodes:
            return key
        matches = self.index.lookup(ref)
        if matches:
            return matches[0].key
        lowered = ref.lower()
        for node in self.graph.nodes.values():
            if lowered in (node.id.lower(), node.name.lower()):
                return node.key
        return None


def build_graph(aws_raw: dict | None, inventory: Inventory, aws_cfg: AwsConfig) -> tuple[Graph, list[str]]:
    graph = Graph()
    warnings: list[str] = []
    if aws_raw:
        warnings += add_to_graph(graph, aws_raw, aws_cfg)
    warnings += apply_inventory(graph, inventory)
    warnings += map_applications(graph, inventory, aws_cfg)
    infer_site_alternates(graph)
    for node in graph.nodes.values():
        if node.account and not node.attrs.get("account_name"):
            name = inventory.account_name(node.account)
            if name:
                node.attrs["account_name"] = name
    return graph, warnings


def build_topology(aws_raw: dict | None, inventory: Inventory, aws_cfg: AwsConfig, *, min_identifier_length: int = 4,
                   min_name_length: int = 8, match_names: bool = True) -> Topology:
    graph, warnings = build_graph(aws_raw, inventory, aws_cfg)
    index = IdentifierIndex(graph, min_length=min_identifier_length, min_name_length=min_name_length,
                            match_names=match_names)
    return Topology(graph=graph, index=index, inventory=inventory, warnings=warnings)


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


def _split_apps(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,;]", value) if v.strip()]


def _app_node(graph: Graph, name: str) -> Node:
    key = make_key("app", name)
    node = graph.nodes.get(key)
    if node is None:
        node = graph.add(Node(key=key, type="app", id=name, attrs={"owners": [], "source": "aws-tag"}))
    return node


def map_applications(graph: Graph, inventory: Inventory, aws_cfg: AwsConfig) -> list[str]:
    """Create app nodes and 'app' edges from member resources (instances, ENIs, subnets).

    Membership sources: application tags on the resource, its subnet or its VPC; inventory
    selectors (explicit ids, subnets, VPCs, CIDRs over ENI/instance IPs, tags, Name regex).
    A VPC-level membership is expanded to its subnets so subnet-scoped impact reaches the app.
    """
    warnings: list[str] = []
    members: dict[str, set[str]] = defaultdict(set)  # app name -> member node keys
    subnets_of_vpc: dict[str, list[str]] = defaultdict(list)
    for node in graph.of_type("subnet"):
        if node.attrs.get("vpc"):
            subnets_of_vpc[node.attrs["vpc"]].append(node.key)

    def add_member(app: str, key: str) -> None:
        node = graph.nodes.get(key)
        if node is None:
            return
        if node.type == "vpc":
            for sub in subnets_of_vpc.get(node.id, []):
                members[app].add(sub)
        elif node.type in ("subnet", "eni", "instance", "dxcon", "dxvif", "vpn", "cgw", "circuit", "external"):
            members[app].add(key)

    tag_keys = aws_cfg.app_tag_keys
    for node in list(graph.of_type("instance", "eni", "subnet", "vpc")):
        for key in tag_keys:
            if node.tags.get(key):
                for app in _split_apps(node.tags[key]):
                    add_member(app, node.key)
                break

    networks: list[tuple[ipaddress._BaseNetwork, str]] = []
    for app in inventory.applications:
        sel = app.selectors
        for ref in sel.resources + sel.subnets + sel.vpcs:
            key = key_for(ref)
            if key and key in graph.nodes:
                add_member(app.name, key)
            else:
                warnings.append(f"application {app.name}: selector {ref!r} matches nothing in the topology")
        for cidr in sel.cidrs:
            networks.append((ipaddress.ip_network(cidr, strict=False), app.name))
        if sel.tags:
            for node in graph.of_type("instance", "eni", "subnet", "vpc"):
                if all(node.tags.get(k) == v for k, v in sel.tags.items()):
                    add_member(app.name, node.key)
        if sel.name_regex:
            regex = re.compile(sel.name_regex)
            for node in graph.of_type("instance", "eni", "subnet"):
                if node.name and regex.search(node.name):
                    add_member(app.name, node.key)

    if networks:
        for node in graph.of_type("eni", "instance", "subnet"):
            addresses = []
            if node.type == "eni":
                addresses = node.attrs.get("private_ips") or []
            elif node.type == "instance" and node.attrs.get("private_ip"):
                addresses = [node.attrs["private_ip"]]
            for net, app in networks:
                if node.type == "subnet":
                    cidr = node.attrs.get("cidr")
                    try:
                        if cidr and ipaddress.ip_network(cidr).subnet_of(net):
                            add_member(app, node.key)
                    except (ValueError, TypeError):
                        continue
                    continue
                for address in addresses:
                    try:
                        if ipaddress.ip_address(address) in net:
                            add_member(app, node.key)
                    except ValueError:
                        continue

    owner_keys = aws_cfg.owner_tag_keys
    for app, keys in members.items():
        app_node = _app_node(graph, app)
        for key in sorted(keys):
            graph.link(key, app_node.key, "app")
            node = graph.nodes[key]
            if app not in node.apps:
                node.apps.append(app)
            for okey in owner_keys:
                owner = node.tags.get(okey)
                if owner and owner not in app_node.attrs.setdefault("owners", []):
                    app_node.attrs["owners"].append(owner)

    # Resources inside member subnets are displayed as belonging to the app too.
    for app, keys in members.items():
        for key in keys:
            if graph.nodes[key].type != "subnet":
                continue
            for child in graph.children(key, "eni", "instance"):
                if app not in child.apps:
                    child.apps.append(app)
                for grandchild in graph.children(child.key, "instance"):
                    if app not in grandchild.apps:
                        grandchild.apps.append(app)
    return warnings


# --------------------------------------------------------------------------
# Backup paths via site prefixes
# --------------------------------------------------------------------------


def _sites_upstream(graph: Graph, key: str) -> set[str]:
    return {k for k in graph.upstream(key) if graph.nodes[k].type == "site"
            and graph.nodes[k].attrs.get("role", "onprem") != "colo"}


def infer_site_alternates(graph: Graph) -> None:
    """A TGW route table shows only the *best* route per prefix. When the inventory says which
    on-prem site owns a prefix, other hybrid attachments propagating into the same table that
    also reach that site are real alternates (e.g. DX primary + VPN backup to the same DC)."""
    owners: list[tuple[ipaddress._BaseNetwork, str]] = []
    for site in graph.of_type("site"):
        for prefix in site.attrs.get("prefixes") or []:
            owners.append((ipaddress.ip_network(prefix, strict=False), site.key))
    if not owners:
        return
    site_cache: dict[str, set[str]] = {}

    def sites_for(att_key: str) -> set[str]:
        if att_key not in site_cache:
            site_cache[att_key] = _sites_upstream(graph, att_key)
        return site_cache[att_key]

    for table in list(graph.of_type("tgwrtb")):
        propagating = [f"tgwatt:{p['attachment']}" for p in table.attrs.get("propagations", [])
                       if p.get("attachment") and p.get("resource_type") in ("vpn", "direct-connect-gateway", "connect")]
        if not propagating:
            continue
        for edge in list(graph.up(table.key)):
            if edge.kind != "route" or not edge.prefix or edge.alternate:
                continue
            try:
                dest = ipaddress.ip_network(edge.prefix, strict=False)
            except ValueError:
                continue
            owning = {site for net, site in owners if net.version == dest.version and (dest.subnet_of(net) or net.subnet_of(dest))}
            if not owning:
                continue
            for candidate in propagating:
                if candidate == edge.src or candidate not in graph.nodes:
                    continue
                if sites_for(candidate) & owning:
                    graph.link(candidate, table.key, "route", prefix=edge.prefix, alternate=True)
