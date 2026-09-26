"""Dependency graph of physical circuits, AWS network constructs, workloads and applications.

Edges point from a dependency to its dependent ("if src fails, dst may be affected").
Each node has an evaluation mode used by the impact engine:

    all     - series: impaired if any dependency is impaired (instance on a subnet)
    any     - redundant: down only if every dependency is down (DX gateway over its VIFs)
    quorum  - down if fewer than `quorum` dependencies are up (LAG minimumLinks)
    routes  - prefix-aware route table: route edges carry the destination prefix
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Iterable, Iterator

from ..util import norm

# type -> (label, is_context). "Context" types are containers that are usually mentioned
# alongside the specific component that is actually affected (the site of a circuit, the
# LAG of a member connection), see impact.prune_anchors.
TYPE_INFO: dict[str, tuple[str, bool]] = {
    "site": ("Site", True),
    "dxloc": ("DX location", True),
    "circuit": ("Circuit", False),
    "cgw": ("Customer gateway", False),
    "vpntun": ("VPN tunnel", False),
    "vpn": ("Site-to-Site VPN", False),
    "dxcon": ("DX connection", False),
    "dxlag": ("DX LAG", True),
    "dxvif": ("DX virtual interface", False),
    "dxgw": ("DX gateway", True),
    "vgw": ("Virtual private gateway", True),
    "tgw": ("Transit gateway", True),
    "tgwatt": ("TGW attachment", False),
    "tgwrtb": ("TGW route table", False),
    "vpc": ("VPC", True),
    "rtb": ("Route table", False),
    "subnet": ("Subnet", False),
    "eni": ("Network interface", False),
    "instance": ("EC2 instance", False),
    "igw": ("Internet gateway", False),
    "eigw": ("Egress-only IGW", False),
    "nat": ("NAT gateway", False),
    "pcx": ("VPC peering", False),
    "vpce": ("VPC endpoint", False),
    "lgw": ("Local gateway", False),
    "cwan": ("Cloud WAN", False),
    "app": ("Application", False),
    "external": ("External dependency", False),
}
CONTEXT_TYPES = frozenset(t for t, (_, ctx) in TYPE_INFO.items() if ctx)
# Where impact lands on something people care about; notice severity is taken from these.
CONSUMER_TYPES = frozenset({"subnet", "eni", "instance", "app"})
HYBRID_ATTACHMENT_TYPES = frozenset({"vpn", "direct-connect-gateway", "connect", "peering", "tgw-peering"})

_AWS_PREFIXES = [
    ("tgw-attach-", "tgwatt"), ("tgw-rtb-", "tgwrtb"), ("dxcon-", "dxcon"), ("dxlag-", "dxlag"),
    ("dxvif-", "dxvif"), ("vpn-", "vpn"), ("cgw-", "cgw"), ("vgw-", "vgw"), ("tgw-", "tgw"),
    ("vpc-", "vpc"), ("subnet-", "subnet"), ("rtb-", "rtb"), ("eni-", "eni"), ("i-", "instance"),
    ("nat-", "nat"), ("igw-", "igw"), ("eigw-", "eigw"), ("pcx-", "pcx"), ("vpce-", "vpce"), ("lgw-", "lgw"),
]
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def type_for_aws_id(resource_id: str) -> str | None:
    rid = resource_id.lower()
    for prefix, ntype in _AWS_PREFIXES:
        if rid.startswith(prefix):
            return ntype
    if _UUID_RE.match(rid):
        return "dxgw"
    return None


def make_key(ntype: str, ident: str) -> str:
    if ntype == "circuit":
        return f"circuit:{norm(ident)}"
    if ntype in ("site", "app"):
        return f"{ntype}:{ident.strip().lower()}"
    return f"{ntype}:{ident}"


def key_for(ref: str) -> str | None:
    """Resolve a user/AWS reference to a node key: 'dxcon-abc' | 'circuit:X' | 'app:payments'."""
    ref = ref.strip()
    if ":" in ref:
        ntype, ident = ref.split(":", 1)
        if ntype in TYPE_INFO:
            return make_key(ntype, ident)
    ntype = type_for_aws_id(ref)
    return make_key(ntype, ref.lower()) if ntype else None


@dataclass
class Node:
    key: str
    type: str
    id: str
    name: str = ""
    account: str | None = None
    region: str | None = None
    mode: str = "all"
    quorum: int = 1
    attrs: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    ips: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    stub: bool = False  # referenced but not described by any source

    @property
    def label(self) -> str:
        if self.name and self.name != self.id:
            return f"{self.id} ({self.name})"
        return self.id

    @property
    def type_label(self) -> str:
        return TYPE_INFO.get(self.type, (self.type, False))[0]

    def merge(self, other: Node) -> None:
        if self.stub and not other.stub:
            self.stub = False
            self.mode, self.quorum = other.mode, other.quorum
        for attr in ("name", "account", "region"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        for k, v in other.attrs.items():
            self.attrs.setdefault(k, v)
        for k, v in other.tags.items():
            self.tags.setdefault(k, v)
        for collection in ("aliases", "ips", "apps"):
            mine = getattr(self, collection)
            for v in getattr(other, collection):
                if v not in mine:
                    mine.append(v)


@dataclass(frozen=True)
class Edge:
    src: str  # dependency
    dst: str  # dependent
    kind: str = "dep"  # dep | member | route | assoc | contains | app | manual
    prefix: str | None = None
    alternate: bool = False  # inferred backup path (site-prefix enrichment)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._up: dict[str, list[Edge]] = defaultdict(list)
        self._down: dict[str, list[Edge]] = defaultdict(list)
        self._edges: set[Edge] = set()

    # -- construction -----------------------------------------------------

    def add(self, node: Node) -> Node:
        existing = self.nodes.get(node.key)
        if existing is None:
            self.nodes[node.key] = node
            return node
        existing.merge(node)
        return existing

    def ensure(self, key: str) -> Node:
        """Return the node, creating a stub for references to undescribed resources."""
        node = self.nodes.get(key)
        if node is None:
            ntype, ident = key.split(":", 1)
            node = Node(key=key, type=ntype, id=ident, stub=True,
                        mode="any" if ntype in ("vgw", "dxgw") else "all")
            self.nodes[key] = node
        return node

    def link(self, src: str, dst: str, kind: str = "dep", prefix: str | None = None,
             alternate: bool = False) -> Edge | None:
        if src == dst:
            return None
        self.ensure(src)
        self.ensure(dst)
        edge = Edge(src, dst, kind, prefix, alternate)
        if edge in self._edges:
            return edge
        self._edges.add(edge)
        self._up[dst].append(edge)
        self._down[src].append(edge)
        return edge

    def unlink(self, edge: Edge) -> None:
        if edge in self._edges:
            self._edges.discard(edge)
            self._up[edge.dst].remove(edge)
            self._down[edge.src].remove(edge)

    # -- queries ----------------------------------------------------------

    def up(self, key: str) -> list[Edge]:
        return self._up.get(key, [])

    def down(self, key: str) -> list[Edge]:
        return self._down.get(key, [])

    def edges(self) -> Iterable[Edge]:
        return self._edges

    def of_type(self, *types: str) -> Iterator[Node]:
        return (n for n in self.nodes.values() if n.type in types)

    def get(self, key: str) -> Node | None:
        return self.nodes.get(key)

    def label(self, key: str) -> str:
        node = self.nodes.get(key)
        return node.label if node else key

    def upstream(self, key: str, max_depth: int = 64) -> set[str]:
        seen: set[str] = set()
        queue = deque([(key, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.up(current):
                if edge.src not in seen:
                    seen.add(edge.src)
                    queue.append((edge.src, depth + 1))
        return seen

    def downstream(self, key: str, max_depth: int = 64) -> set[str]:
        seen: set[str] = set()
        queue = deque([(key, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.down(current):
                if edge.dst not in seen:
                    seen.add(edge.dst)
                    queue.append((edge.dst, depth + 1))
        return seen

    def children(self, key: str, *types: str) -> list[Node]:
        return [self.nodes[e.dst] for e in self.down(key) if not types or self.nodes[e.dst].type in types]

    def parents(self, key: str, *types: str) -> list[Node]:
        return [self.nodes[e.src] for e in self.up(key) if not types or self.nodes[e.src].type in types]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            out[node.type] += 1
        return dict(sorted(out.items()))

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [[e.src, e.dst, e.kind, e.prefix, e.alternate] for e in self._edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Graph:
        graph = cls()
        for raw in data.get("nodes", []):
            graph.nodes[raw["key"]] = Node(**raw)
        for src, dst, kind, prefix, alternate in data.get("edges", []):
            graph.link(src, dst, kind, prefix, alternate)
        return graph
