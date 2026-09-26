"""Inventory YAML: the facts AWS cannot tell us.

Circuits (carrier ids and what they carry), on-prem sites and the prefixes they own,
applications and how to recognise their resources, plus manual dependencies and mode overrides.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import TYPE_INFO, Graph, Node, key_for, make_key

log = logging.getLogger(__name__)


@dataclass
class Site:
    id: str
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    role: str = "onprem"  # onprem | colo
    prefixes: list[str] = field(default_factory=list)
    dx_locations: list[str] = field(default_factory=list)


@dataclass
class Circuit:
    id: str
    provider: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    bandwidth: str = ""
    sites: list[str] = field(default_factory=list)
    carries: list[str] = field(default_factory=list)
    ip_blocks: list[str] = field(default_factory=list)


@dataclass
class Selectors:
    tags: dict[str, str] = field(default_factory=dict)
    resources: list[str] = field(default_factory=list)
    subnets: list[str] = field(default_factory=list)
    vpcs: list[str] = field(default_factory=list)
    cidrs: list[str] = field(default_factory=list)
    name_regex: str | None = None


@dataclass
class Application:
    name: str
    description: str = ""
    owners: list[str] = field(default_factory=list)
    tier: int | None = None
    selectors: Selectors = field(default_factory=Selectors)
    notify: list[dict] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    accounts: dict[str, str] = field(default_factory=dict)
    sites: list[Site] = field(default_factory=list)
    circuits: list[Circuit] = field(default_factory=list)
    applications: list[Application] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    modes: list[dict] = field(default_factory=list)
    source: str | None = None
    digest: str = ""

    def account_name(self, account_id: str | None) -> str | None:
        return self.accounts.get(str(account_id)) if account_id else None


class InventoryError(ValueError):
    pass


def _list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _strs(value: Any) -> list[str]:
    return [str(v) for v in _list(value)]


def _check_cidrs(values: list[str], where: str) -> list[str]:
    for value in values:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise InventoryError(f"{where}: invalid CIDR {value!r}") from exc
    return values


def parse_inventory(data: dict | None, source: str | None = None) -> Inventory:
    data = data or {}
    inv = Inventory(source=source)
    inv.accounts = {str(k): str(v) for k, v in (data.get("accounts") or {}).items()}
    for i, raw in enumerate(_list(data.get("sites"))):
        if "id" not in raw:
            raise InventoryError(f"sites[{i}]: 'id' is required")
        inv.sites.append(Site(
            id=str(raw["id"]), name=str(raw.get("name", raw["id"])), aliases=_strs(raw.get("aliases")),
            role=str(raw.get("role", "onprem")), prefixes=_check_cidrs(_strs(raw.get("prefixes")), f"sites[{i}]"),
            dx_locations=_strs(raw.get("dx_locations")),
        ))
    for i, raw in enumerate(_list(data.get("circuits"))):
        if "id" not in raw:
            raise InventoryError(f"circuits[{i}]: 'id' is required")
        inv.circuits.append(Circuit(
            id=str(raw["id"]), provider=str(raw.get("provider", "")), aliases=_strs(raw.get("aliases")),
            description=str(raw.get("description", "")), bandwidth=str(raw.get("bandwidth", "")),
            sites=_strs(raw.get("sites")), carries=_strs(raw.get("carries")),
            ip_blocks=_check_cidrs(_strs(raw.get("ip_blocks")), f"circuits[{i}]"),
        ))
    for i, raw in enumerate(_list(data.get("applications"))):
        if "name" not in raw:
            raise InventoryError(f"applications[{i}]: 'name' is required")
        sel = raw.get("selectors") or {}
        name_regex = sel.get("name_regex")
        if name_regex:
            try:
                re.compile(name_regex)
            except re.error as exc:
                raise InventoryError(f"applications[{i}].selectors.name_regex: {exc}") from exc
        inv.applications.append(Application(
            name=str(raw["name"]), description=str(raw.get("description", "")), owners=_strs(raw.get("owners")),
            tier=raw.get("tier"), notify=_list(raw.get("notify")), depends_on=_strs(raw.get("depends_on")),
            selectors=Selectors(
                tags={str(k): str(v) for k, v in (sel.get("tags") or {}).items()},
                resources=_strs(sel.get("resources")), subnets=_strs(sel.get("subnets")), vpcs=_strs(sel.get("vpcs")),
                cidrs=_check_cidrs(_strs(sel.get("cidrs")), f"applications[{i}].selectors"), name_regex=name_regex,
            ),
        ))
    inv.dependencies = _list(data.get("dependencies"))
    inv.modes = _list(data.get("modes"))
    return inv


def load_inventory(path: str | Path | None) -> Inventory:
    if not path:
        return Inventory()
    p = Path(path)
    if not p.exists():
        raise InventoryError(f"inventory file not found: {p}")
    text = p.read_text()
    inv = parse_inventory(yaml.safe_load(text), str(p))
    inv.digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return inv


def _resolve(ref: str) -> str | None:
    key = key_for(ref)
    if key is None:
        log.warning("inventory: cannot resolve reference %r (use an AWS id or type:id)", ref)
    return key


def apply_inventory(graph: Graph, inv: Inventory) -> list[str]:
    """Add sites, circuits, application nodes, manual edges and mode overrides."""
    warnings: list[str] = []
    for site in inv.sites:
        key = make_key("site", site.id)
        graph.add(Node(key=key, type="site", id=site.id, name=site.name, aliases=[site.name, *site.aliases],
                       attrs={"role": site.role, "prefixes": site.prefixes, "source": "inventory"}))
        graph.nodes[key].attrs.update({"role": site.role, "prefixes": site.prefixes})
        for code in site.dx_locations:
            graph.link(key, f"dxloc:{code}", "dep")
    for circuit in inv.circuits:
        key = make_key("circuit", circuit.id)
        node = graph.add(Node(key=key, type="circuit", id=circuit.id, aliases=list(circuit.aliases), ips=list(circuit.ip_blocks),
                              attrs={"provider": circuit.provider, "description": circuit.description,
                                     "bandwidth": circuit.bandwidth, "source": "inventory"}))
        node.attrs.update({k: v for k, v in (("provider", circuit.provider), ("description", circuit.description),
                                               ("bandwidth", circuit.bandwidth)) if v})
        for site in circuit.sites:
            skey = make_key("site", site)
            if skey not in graph.nodes:
                warnings.append(f"circuit {circuit.id}: unknown site {site!r}")
            graph.link(skey, key, "dep")
        for ref in circuit.carries:
            target = _resolve(ref)
            if target:
                graph.link(key, target, "dep")
            else:
                warnings.append(f"circuit {circuit.id}: cannot resolve carries entry {ref!r}")
    for app in inv.applications:
        key = make_key("app", app.name)
        graph.add(Node(key=key, type="app", id=app.name,
                       attrs={"owners": app.owners, "tier": app.tier, "description": app.description,
                              "source": "inventory"}))
        for ref in app.depends_on:
            target = _resolve(ref)
            if target:
                graph.link(target, key, "manual")
    for dep in inv.dependencies:
        src, dst = _resolve(str(dep.get("from", ""))), _resolve(str(dep.get("to", "")))
        if src and dst:
            graph.link(src, dst, str(dep.get("kind", "manual")))
        else:
            warnings.append(f"dependency {dep!r} could not be resolved")
    for override in inv.modes:
        key = _resolve(str(override.get("node", "")))
        mode = override.get("mode")
        if not key or mode not in ("all", "any", "quorum", "routes"):
            warnings.append(f"mode override {override!r} is invalid")
            continue
        node = graph.ensure(key)
        node.mode = mode
        node.quorum = int(override.get("quorum", node.quorum or 1))
        node.attrs["mode_override"] = True
    unknown_types = {n.type for n in graph.nodes.values()} - set(TYPE_INFO)
    if unknown_types:
        warnings.append(f"unknown node types: {sorted(unknown_types)}")
    return warnings
