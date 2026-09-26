"""Blast-radius engine: propagate impact from anchors through the dependency graph.

Severity per node is computed as a least fixpoint (severities and prefix scopes only grow),
honouring redundancy: a DX gateway with two VIFs loses redundancy — not connectivity — when one
VIF's circuit is in maintenance; a LAG with minimumLinks=2 goes down when one member does.
Route tables are prefix-aware, so a subnet only inherits impact for destinations that actually
route over the affected path.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .topology.index import IdentifierIndex, Match
from .topology.model import CONSUMER_TYPES, CONTEXT_TYPES, Graph
from .util import AT_RISK, DEGRADED, NONE, OUTAGE, PROVIDER_IMPACT_SEVERITY, severity_name

log = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LOCATION_TYPES = frozenset({"site", "dxloc"})


@dataclass
class Anchor:
    key: str
    severity: int
    reason: str
    evidence: list[dict] = field(default_factory=list)


Scope = "dict[str, int] | None"  # destination prefix -> severity; None = not destination-scoped


@dataclass
class NodeImpact:
    key: str
    severity: int
    reasons: list[str] = field(default_factory=list)
    prefixes: dict[str, int] | None = None  # per-destination severity; None = whole node affected
    via: list[str] = field(default_factory=list)
    anchor: bool = False
    alternates: list[str] = field(default_factory=list)  # healthy redundant dependencies


def _intersect(prefix: str, scope: dict[str, int] | None, severity: int) -> dict[str, int]:
    """Which part of route `prefix` is affected, given the next hop's per-prefix impact."""
    if scope is None:
        return {prefix: severity}
    try:
        route = ipaddress.ip_network(prefix, strict=False)
    except ValueError:  # prefix-list id: assume it covers whatever is affected
        return dict(scope)
    out: dict[str, int] = {}
    for item, sev in scope.items():
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            out[prefix] = max(out.get(prefix, NONE), sev)
            continue
        if net.version != route.version:
            continue
        if net.subnet_of(route):
            key = str(net)
        elif route.subnet_of(net):
            key = str(route)
        else:
            continue
        out[key] = max(out.get(key, NONE), sev)
    return out


def _merge(scopes: Iterable[dict[str, int] | None]) -> dict[str, int] | None:
    out: dict[str, int] = {}
    for scope in scopes:
        if scope is None:
            return None
        for prefix, sev in scope.items():
            out[prefix] = max(out.get(prefix, NONE), sev)
    return out


class ImpactEngine:
    def __init__(self, graph: Graph, max_steps: int = 500_000):
        self.g = graph
        self.max_steps = max_steps

    def label(self, key: str) -> str:
        return self.g.label(key)

    def run(self, anchors: list[Anchor]) -> dict[str, NodeImpact]:
        g = self.g
        state: dict[str, NodeImpact] = {}
        by_key = {a.key: a for a in anchors if a.key in g.nodes}
        queue: deque[str] = deque()
        queued: set[str] = set()

        def push(key: str) -> None:
            if key not in queued:
                queued.add(key)
                queue.append(key)

        for anchor in by_key.values():
            state[anchor.key] = NodeImpact(anchor.key, anchor.severity, [anchor.reason], None, [], True)
            for edge in g.down(anchor.key):
                push(edge.dst)
        steps = 0
        while queue:
            key = queue.popleft()
            queued.discard(key)
            steps += 1
            if steps > self.max_steps:
                log.warning("impact propagation stopped after %d steps", steps)
                break
            new = self._evaluate(key, state)
            anchor = by_key.get(key)
            if anchor:
                if new is None or new.severity <= anchor.severity:
                    continue
                new.anchor = True
                new.prefixes = None
                new.reasons = [anchor.reason] + new.reasons
            if new is None:
                continue
            old = state.get(key)
            state[key] = new
            if old is None or new.severity != old.severity or new.prefixes != old.prefixes:
                for edge in g.down(key):
                    push(edge.dst)
        return state

    # -- evaluation ----------------------------------------------------------

    def _evaluate(self, key: str, state: dict[str, NodeImpact]) -> NodeImpact | None:
        node = self.g.nodes[key]
        edges = self.g.up(key)
        if not edges:
            return None
        if node.mode == "routes":
            return self._eval_routes(key, edges, state)
        deps: list[str] = list(dict.fromkeys(e.src for e in edges))
        impacted = [d for d in deps if d in state and state[d].severity > NONE]
        if not impacted:
            return None
        if node.mode == "all":
            worst = max(state[d].severity for d in impacted)
            ordered = sorted(impacted, key=lambda d: -state[d].severity)
            if node.type == "app":
                shown = ", ".join(self.label(d) for d in ordered[:3])
                more = f" +{len(ordered) - 3} more" if len(ordered) > 3 else ""
                reason = f"{len(ordered)} member resource(s) affected: {shown}{more}"
            else:
                reason = "depends on " + ", ".join(self.label(d) for d in ordered[:3])
            return NodeImpact(key, worst, [reason], _merge(state[d].prefixes for d in impacted), ordered)

        # any / quorum: redundant dependencies
        total = len(deps)
        down = [d for d in deps if d in state and state[d].severity >= OUTAGE]
        impaired = [d for d in impacted if d not in down]
        healthy = [d for d in deps if d not in impacted]
        required = max(1, node.quorum) if node.mode == "quorum" else 1
        up = total - len(down)
        labels = lambda keys: ", ".join(self.label(k) for k in keys[:4]) + (f" +{len(keys) - 4}" if len(keys) > 4 else "")
        if up < required:
            if node.mode == "quorum":
                reason = (f"only {up} of {total} members up; minimumLinks={required} "
                          f"(down: {labels(down)})")
            else:
                reason = f"all {total} redundant path(s) down: {labels(down)}"
            return NodeImpact(key, OUTAGE, [reason], None, down, alternates=[])
        if down:
            if node.mode == "quorum":
                reason = f"{len(down)} of {total} members down ({labels(down)}); {up} remain, minimumLinks={required}"
            else:
                reason = f"redundancy lost: {len(down)} of {total} paths down ({labels(down)})"
            still = [d for d in deps if d not in down]
            reason += f"; still served by {labels(still)}"
            if impaired:
                reason += f" — which is itself {severity_name(max(state[d].severity for d in impaired)).lower().replace('_', ' ')}"
            return NodeImpact(key, DEGRADED, [reason], None, down + impaired, alternates=healthy)
        severity = max(state[d].severity for d in impaired)
        reason = f"{labels(impaired)} {severity_name(severity).lower().replace('_', ' ')}; redundancy intact"
        return NodeImpact(key, severity, [reason], None, impaired, alternates=healthy)

    def _eval_routes(self, key: str, edges, state: dict[str, NodeImpact]) -> NodeImpact | None:
        by_prefix: dict[str, list[str]] = {}
        other: list[str] = []
        for edge in edges:
            if edge.kind == "route" and edge.prefix:
                targets = by_prefix.setdefault(edge.prefix, [])
                if edge.src not in targets:
                    targets.append(edge.src)
            else:
                other.append(edge.src)
        affected: dict[str, int] = {}
        reasons: list[str] = []
        via: list[str] = []
        alternates: list[str] = []
        for prefix, targets in by_prefix.items():
            # How badly each next hop is impaired *for this destination*.
            parts = {t: _intersect(prefix, state[t].prefixes, state[t].severity)
                     for t in targets if t in state and state[t].severity > NONE}
            parts = {t: p for t, p in parts.items() if p}
            if not parts:
                continue
            if len(targets) == 1:
                target, sub = next(iter(parts.items()))
                severity = max(sub.values())
                scope = "" if set(sub) == {prefix} else f" (only {', '.join(sorted(sub))})"
                reasons.append(f"{prefix}{scope} via {self.label(target)}: {severity_name(severity)}")
            else:
                level = {t: max(p.values()) for t, p in parts.items()}
                down = [t for t, sev in level.items() if sev >= OUTAGE]
                healthy = [t for t in targets if t not in level]
                if len(down) == len(targets):
                    severity = OUTAGE
                    reasons.append(f"{prefix}: all {len(targets)} paths down")
                elif down:
                    severity = DEGRADED
                    still = [t for t in targets if t not in down]
                    reasons.append(f"{prefix}: {len(down)} of {len(targets)} paths down; still via "
                                   + ", ".join(self.label(t) for t in still))
                else:
                    severity = max(level.values())
                    reasons.append(f"{prefix}: {', '.join(self.label(t) for t in level)} "
                                   f"{severity_name(severity)}; alternate paths intact")
                alternates.extend(h for h in healthy if h not in alternates)
                sub = {prefix: severity}
            for p, sev in sub.items():
                affected[p] = max(affected.get(p, NONE), sev)
            via.extend(t for t in parts if t not in via)
        worst = max(affected.values(), default=NONE)
        scoped = True
        for dep in dict.fromkeys(other):  # manual edges into a route table act as hard dependencies
            if dep in state and state[dep].severity > NONE:
                worst = max(worst, state[dep].severity)
                reasons.append(f"depends on {self.label(dep)}")
                via.append(dep)
                scoped = False
        if worst == NONE:
            return None
        return NodeImpact(key, worst, reasons, affected if scoped else None, via, alternates=alternates)


# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------


def impact_severity(impact: str | None, default: str | None = "OUTAGE") -> int:
    if impact and impact.upper() in PROVIDER_IMPACT_SEVERITY:
        return PROVIDER_IMPACT_SEVERITY[impact.upper()]
    return PROVIDER_IMPACT_SEVERITY.get((default or "OUTAGE").upper(), OUTAGE)


def prune_anchors(graph: Graph, anchors: list[Anchor]) -> tuple[list[Anchor], list[dict]]:
    """When a mail names both a container and its component, anchor on the most specific
    thing: 'Equinix LD5' + circuit X -> circuit X; 'dxlag-1' + member 'dxcon-a' -> dxcon-a;
    circuit X + the dxcon it carries -> circuit X (the dxcon's impact is derived)."""
    keys = {a.key for a in anchors}
    kept, dropped = [], []
    for anchor in anchors:
        node = graph.nodes[anchor.key]
        others = keys - {anchor.key}
        if node.type in CONTEXT_TYPES and others:
            # The container is context if a more specific match sits inside it, or feeds the same
            # nearby things (a circuit and its DX location both feed the DX connection).
            near = graph.downstream(anchor.key, max_depth=3)
            related = near & others or any(
                {e.dst for e in graph.down(other)} & near for other in others
                if graph.nodes[other].type not in CONTEXT_TYPES)
            if related:
                dropped.append({"key": anchor.key, "reason": "context for a more specific match"})
                continue
        ancestors = graph.upstream(anchor.key) & others
        if any(graph.nodes[a].type not in CONTEXT_TYPES for a in ancestors):
            dropped.append({"key": anchor.key, "reason": "derived from an upstream match"})
            continue
        kept.append(anchor)
    return kept, dropped


@dataclass
class CorrelationInput:
    circuits: list[dict]  # [{"id": ..., "impact": ...}]
    declared: list[str]  # AWS ids / IPs listed as affected by a structured source
    texts: list[str]  # subject + body + calendar text of the linked mails
    impact: str | None
    default_impact: str = "OUTAGE"
    exclusive: bool = False


def select_anchors(index: IdentifierIndex, graph: Graph, inp: CorrelationInput
                   ) -> tuple[list[Anchor], list[dict], list[str], list[dict]]:
    """-> (anchors, dropped, unmapped declared ids, all matches as evidence)"""
    evidence: dict[str, list[Match]] = {}
    unmapped: list[str] = []
    circuit_impact: dict[str, str | None] = {}
    for circuit in inp.circuits:
        matches = index.lookup(circuit["id"])
        if not matches:
            unmapped.append(circuit["id"])
        for m in matches:
            evidence.setdefault(m.key, []).append(m)
            circuit_impact[m.key] = circuit.get("impact")
    for ident in inp.declared:
        matches = index.lookup(ident)
        if not matches and not _UUID_RE.match(ident):  # unknown UUIDs are noise (tracking ids)
            unmapped.append(ident)
        for m in matches:
            evidence.setdefault(m.key, []).append(m)
    if not inp.exclusive or not evidence:
        for text in inp.texts:
            for m in index.scan(text):
                evidence.setdefault(m.key, []).append(m)
    anchors = []
    for key, matches in evidence.items():
        if key not in graph.nodes:
            continue
        impact = circuit_impact.get(key) or inp.impact
        severity = impact_severity(impact, inp.default_impact)
        best = sorted(matches, key=lambda m: (m.via != "declared", m.confidence != "high"))[0]
        declared_by = "declared" if best.via == "declared" else "mentioned"
        reason = f"{declared_by} in notice as '{best.text}' — provider impact {impact or inp.default_impact + ' (assumed)'}"
        anchors.append(Anchor(key, severity, reason, [m.as_dict() for m in matches]))
    dropped: list[dict] = []
    if inp.circuits:
        # A notice about specific circuits is not facility-wide work: a site / DX location it
        # mentions is where those circuits land (possibly one we do not know), not the target.
        located = [a for a in anchors if graph.nodes[a.key].type in LOCATION_TYPES]
        anchors = [a for a in anchors if a not in located]
        dropped += [{"key": a.key, "reason": "location of a circuit named in the notice"} for a in located]
    kept, context = prune_anchors(graph, anchors)
    dropped = context + dropped
    flat = [m.as_dict() for ms in evidence.values() for m in ms]
    return kept, dropped, list(dict.fromkeys(unmapped)), flat


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

_TYPE_ORDER = ["site", "dxloc", "circuit", "cgw", "dxcon", "dxlag", "vpntun", "vpn", "dxvif", "dxgw", "vgw", "tgw",
               "tgwatt", "tgwrtb", "vpc", "rtb", "subnet", "eni", "instance", "nat", "igw", "pcx", "vpce", "app"]


def chain_to(state: dict[str, NodeImpact], key: str) -> list[str]:
    path = [key]
    seen = {key}
    current = key
    while True:
        impact = state.get(current)
        if impact is None or impact.anchor or not impact.via:
            break
        nxt = max(impact.via, key=lambda k: state[k].severity if k in state else 0)
        if nxt in seen:
            break
        path.append(nxt)
        seen.add(nxt)
        current = nxt
    return list(reversed(path))


def build_report(graph: Graph, state: dict[str, NodeImpact], anchors: list[Anchor], *, dropped: list[dict] | None = None,
                 unmapped: list[str] | None = None, evidence: list[dict] | None = None,
                 account_names: dict[str, str] | None = None) -> dict:
    account_names = account_names or {}
    nodes = []
    for key, impact in state.items():
        if impact.severity <= NONE:
            continue
        node = graph.nodes[key]
        nodes.append({
            "key": key, "type": node.type, "type_label": node.type_label, "id": node.id, "name": node.name,
            "label": node.label, "account": node.account,
            "account_name": account_names.get(node.account or "") or node.attrs.get("account_name"),
            "region": node.region, "severity": impact.severity, "reasons": impact.reasons,
            "prefixes": dict(sorted(impact.prefixes.items())) if impact.prefixes is not None else None,
            "via": impact.via, "anchor": impact.anchor, "apps": node.apps, "alternates": impact.alternates,
            "detail": _detail(node),
        })
    order = {t: i for i, t in enumerate(_TYPE_ORDER)}
    nodes.sort(key=lambda n: (order.get(n["type"], 99), -n["severity"], n["id"]))
    apps = []
    for n in nodes:
        if n["type"] != "app":
            continue
        app_node = graph.nodes[n["key"]]
        members = [k for k in state[n["key"]].via]
        apps.append({
            "name": app_node.id, "key": n["key"], "severity": n["severity"], "reasons": n["reasons"],
            "owners": app_node.attrs.get("owners", []), "tier": app_node.attrs.get("tier"),
            "members": members, "prefixes": n["prefixes"],
            "chain": chain_to(state, n["key"]),
        })
    apps.sort(key=lambda a: (-a["severity"], a["name"]))
    consumer = [n["severity"] for n in nodes if n["type"] in CONSUMER_TYPES]
    all_sev = [n["severity"] for n in nodes]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    return {
        "severity": max(consumer) if consumer else (max(all_sev) if all_sev else NONE),
        "infra_severity": max(all_sev) if all_sev else NONE,
        "anchors": [{"key": a.key, "label": graph.label(a.key), "type": graph.nodes[a.key].type,
                     "severity": a.severity, "reason": a.reason, "evidence": a.evidence} for a in anchors],
        "dropped": dropped or [],
        "unmapped": unmapped or [],
        "evidence": evidence or [],
        "nodes": nodes,
        "apps": apps,
        "counts": counts,
        "mapped": bool(anchors),
    }


def _detail(node) -> dict:
    a = node.attrs
    keep = {
        "circuit": ("provider", "bandwidth", "description"),
        "dxcon": ("location", "bandwidth", "state", "provider", "partner", "aws_device"),
        "dxlag": ("location", "minimum_links", "members"),
        "dxvif": ("vif_type", "vlan", "state"),
        "vpn": ("state", "type"),
        "vpntun": ("outside_ip", "status"),
        "cgw": ("ip", "device"),
        "tgwatt": ("resource_type", "resource_id"),
        "subnet": ("cidr", "az", "vpc"),
        "eni": ("kind", "detail", "private_ips", "infra"),
        "instance": ("state", "instance_type", "private_ip", "az"),
        "rtb": ("vpc", "main"),
        "vpc": ("cidrs",),
        "app": ("owners", "tier"),
    }.get(node.type, ())
    return {k: a[k] for k in keep if a.get(k) not in (None, "", [], False)}


def blast_radius(graph: Graph, index: IdentifierIndex, inp: CorrelationInput,
                 account_names: dict[str, str] | None = None) -> dict:
    anchors, dropped, unmapped, evidence = select_anchors(index, graph, inp)
    state = ImpactEngine(graph).run(anchors)
    return build_report(graph, state, anchors, dropped=dropped, unmapped=unmapped, evidence=evidence,
                        account_names=account_names)


def what_if(graph: Graph, keys: list[str], severity: int = OUTAGE, account_names: dict[str, str] | None = None) -> dict:
    anchors = [Anchor(k, severity, f"what-if: {severity_name(severity).lower()} of {graph.label(k)}")
               for k in keys if k in graph.nodes]
    state = ImpactEngine(graph).run(anchors)
    return build_report(graph, state, anchors, account_names=account_names)


__all__ = ["Anchor", "NodeImpact", "ImpactEngine", "CorrelationInput", "blast_radius", "what_if", "build_report",
           "prune_anchors", "select_anchors", "impact_severity", "AT_RISK", "DEGRADED", "OUTAGE"]
