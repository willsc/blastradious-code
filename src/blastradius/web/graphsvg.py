"""Server-side SVG of a blast radius: a layered DAG from the anchor(s) to the applications.

No JS graph library needed; the SVG inherits the page's CSS variables so it follows light/dark
themes. The 'simple' view contracts routing plumbing (VIFs, tunnels, TGW attachments/route tables,
VPC route tables) and per-workload nodes, leaving circuit -> edge device -> gateway -> subnets ->
applications; the 'full' view shows every hop.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape

from ..topology.model import TYPE_INFO, Graph

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 172, 44, 38, 12, 16
SIMPLE_HIDE = frozenset({"dxvif", "vpntun", "tgwatt", "tgwrtb", "rtb", "eni", "instance"})
TYPE_ORDER = ["site", "dxloc", "circuit", "cgw", "dxcon", "dxlag", "vpntun", "vpn", "dxvif", "dxgw", "vgw", "tgw",
              "tgwatt", "tgwrtb", "vpc", "rtb", "subnet", "eni", "instance", "app"]


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_view(blast: dict, graph: Graph | None, simple: bool = True):
    nodes: dict[str, dict] = {}
    for n in blast.get("nodes", []):
        nodes[n["key"]] = {"key": n["key"], "type": n["type"], "id": n["id"], "name": n.get("name") or "",
                           "severity": n["severity"], "reasons": n.get("reasons", []), "kind": "impact",
                           "detail": n.get("detail", {}), "anchor": n.get("anchor", False)}
    edges: set[tuple[str, str, str]] = set()
    for n in blast.get("nodes", []):
        for parent in n.get("via", []):
            if parent in nodes:
                edges.add((parent, n["key"], "impact"))
        for alt in n.get("alternates", [])[:4]:
            node = graph.nodes.get(alt) if graph else None
            nodes.setdefault(alt, {"key": alt, "type": node.type if node else alt.split(":", 1)[0],
                                   "id": node.id if node else alt.split(":", 1)[-1], "name": node.name if node else "",
                                   "severity": 0, "reasons": ["healthy alternate path"], "kind": "alternate",
                                   "detail": {}, "anchor": False})
            edges.add((alt, n["key"], "alternate"))

    if simple:
        # Keep the physical/logical path and what people own; contract routing plumbing and
        # per-workload detail (both are in the resource table and the "every hop" view).
        for key in [k for k, v in nodes.items() if v["kind"] == "impact" and v["type"] in SIMPLE_HIDE]:
            preds = [(s, k) for s, d, k in edges if d == key]
            succs = [(d, k) for s, d, k in edges if s == key]
            edges = {e for e in edges if key not in (e[0], e[1])}
            for s, k1 in preds:
                for d, k2 in succs:
                    if s != d:
                        edges.add((s, d, "alternate" if "alternate" in (k1, k2) else "impact"))
            nodes.pop(key, None)
    edges = {e for e in edges if e[0] in nodes and e[1] in nodes}
    return nodes, edges


def _layers(nodes: dict, edges: set) -> dict[str, int]:
    preds: dict[str, list[str]] = defaultdict(list)
    for s, d, _ in edges:
        preds[d].append(s)
    layer: dict[str, int] = {}

    def depth(key: str, stack: set) -> int:
        if key in layer:
            return layer[key]
        if key in stack:
            return 0
        stack.add(key)
        value = 0 if not preds[key] else 1 + max(depth(p, stack) for p in preds[key])
        stack.discard(key)
        layer[key] = value
        return value

    for key in nodes:
        depth(key, set())
    last = max(layer.values(), default=0)
    succs: dict[str, list[str]] = defaultdict(list)
    for s, d, _ in edges:
        succs[s].append(d)
    for key, n in nodes.items():
        if n["type"] == "app":
            layer[key] = last
        elif n["kind"] == "alternate" and succs[key]:
            # draw a backup path right next to the node it backs up
            layer[key] = max(0, min(layer[d] for d in succs[key]) - 1)
    return layer


def render(blast: dict, graph: Graph | None = None, simple: bool = True, link: bool = True) -> str:
    nodes, edges = build_view(blast, graph, simple)
    if not nodes:
        return ""
    layer = _layers(nodes, edges)
    columns: dict[int, list[str]] = defaultdict(list)
    order_index = {t: i for i, t in enumerate(TYPE_ORDER)}
    for key in sorted(nodes, key=lambda k: (order_index.get(nodes[k]["type"], 99), nodes[k]["id"])):
        columns[layer[key]].append(key)
    preds: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for s, d, _ in edges:
        preds[d].append(s)
        succs[s].append(d)
    position: dict[str, float] = {}
    for col in columns.values():
        for i, key in enumerate(col):
            position[key] = i
    for _ in range(4):  # barycentre sweeps to reduce crossings
        for c in sorted(columns)[1:]:
            columns[c].sort(key=lambda k: sum(position[p] for p in preds[k]) / len(preds[k]) if preds[k] else position[k])
            for i, key in enumerate(columns[c]):
                position[key] = i
        for c in sorted(columns, reverse=True)[1:]:
            columns[c].sort(key=lambda k: sum(position[s] for s in succs[k]) / len(succs[k]) if succs[k] else position[k])
            for i, key in enumerate(columns[c]):
                position[key] = i
    tallest = max(len(col) for col in columns.values())
    height = PAD * 2 + tallest * (NODE_H + GAP_Y) - GAP_Y
    width = PAD * 2 + (max(columns) + 1) * (NODE_W + GAP_X) - GAP_X
    xy: dict[str, tuple[float, float]] = {}
    for c, col in columns.items():
        offset = (height - (len(col) * (NODE_H + GAP_Y) - GAP_Y)) / 2
        for i, key in enumerate(col):
            xy[key] = (PAD + c * (NODE_W + GAP_X), offset + i * (NODE_H + GAP_Y))

    out = [f'<svg class="blastgraph" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" role="img" aria-label="Blast radius graph">',
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" class="arrowhead"/></marker></defs>']
    for s, d, kind in sorted(edges):
        x1, y1 = xy[s][0] + NODE_W, xy[s][1] + NODE_H / 2
        x2, y2 = xy[d][0], xy[d][1] + NODE_H / 2
        dx = max(24.0, (x2 - x1) / 2)
        sev = nodes[s]["severity"] if kind == "impact" else 0
        cls = f"gedge sev-{sev}" + (" alt" if kind == "alternate" else "")
        out.append(f'<path class="{cls}" d="M{x1:.0f},{y1:.0f} C{x1 + dx:.0f},{y1:.0f} {x2 - dx:.0f},{y2:.0f} '
                   f'{x2:.0f},{y2:.0f}" marker-end="url(#arrow)"/>')
    for key, n in nodes.items():
        x, y = xy[key]
        type_label = TYPE_INFO.get(n["type"], (n["type"], False))[0]
        name = n["name"] if n["name"] and n["type"] != "circuit" else n["id"]
        if n["type"] == "app":
            name = n["id"]
        sub = n["id"] if n["name"] and n["type"] not in ("circuit", "app") else ""
        if len(type_label) + min(len(sub), 14) > 28:
            sub = ""  # would collide with the type label; the tooltip still has it
        cls = f"gnode sev-{n['severity']} {n['kind']}" + (" anchor" if n["anchor"] else "")
        title = escape(f"{type_label}: {n['id']} {('(' + n['name'] + ')') if n['name'] else ''}\n" + "\n".join(n["reasons"][:3]))
        body = (f'<g class="{cls}" transform="translate({x:.0f},{y:.0f})"><title>{title}</title>'
                f'<rect width="{NODE_W}" height="{NODE_H}" rx="7"/>'
                f'<rect class="stripe" width="5" height="{NODE_H}" rx="2"/>'
                f'<text class="t-type" x="13" y="16">{escape(type_label)}{" · anchor" if n["anchor"] else ""}</text>'
                f'<text class="t-name" x="13" y="33">{escape(_short(name))}</text>'
                + (f'<text class="t-sub" x="{NODE_W - 8}" y="16" text-anchor="end">{escape(_short(sub, 14))}</text>' if sub else "")
                + "</g>")
        if link:
            body = f'<a href="/topology/node?key={escape(key)}">{body}</a>'
        out.append(body)
    out.append("</svg>")
    return "\n".join(out)
