"""Known-identifier index: find circuits, AWS resources, devices and IPs mentioned in mail text.

Rather than guessing every carrier's circuit-id format, we already know every identifier that
matters (inventory circuit ids and aliases, AWS ids, DX connection names, CGW addresses...), so
we look for *those* in the text. Matching is separator-insensitive ('OGYX/123456//ZYO' ==
'OGYX 123456 ZYO' == 'ogyx123456zyo') but respects token boundaries.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass

from ..parsing.generic import IPV4_RE, IPV6_RE
from ..util import norm
from .model import Graph

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_GAP_OK_RE = re.compile(r"[ \t\-/_.:#]{0,3}")
_MAX_NGRAM = 8

# Types whose display names are distinctive enough to match in free text.
NAME_TYPES = frozenset({"dxcon", "dxlag", "dxvif", "dxgw", "cgw", "vpn", "site", "dxloc", "circuit"})
# Types whose ids are matchable (apps are never anchored by a mail merely naming them).
ID_TYPES = frozenset({
    "circuit", "dxcon", "dxlag", "dxvif", "dxgw", "cgw", "vpn", "vgw", "tgw", "tgwatt", "tgwrtb", "vpc",
    "subnet", "rtb", "eni", "instance", "nat", "igw", "eigw", "pcx", "vpce", "lgw", "dxloc", "site", "external",
})
_CONFIDENCE = {"id": "high", "alias": "high", "name": "medium", "ip": "high", "block": "high", "cidr": "medium"}


@dataclass
class Match:
    key: str
    text: str
    kind: str  # id | alias | name | ip | block | cidr
    via: str = "text"  # text | declared
    confidence: str = "high"

    def as_dict(self) -> dict:
        return {"key": self.key, "text": self.text, "kind": self.kind, "via": self.via, "confidence": self.confidence}


class IdentifierIndex:
    def __init__(self, graph: Graph, *, min_length: int = 4, min_name_length: int = 8, match_names: bool = True):
        self.graph = graph
        self.min_length = min_length
        self.min_name_length = min_name_length
        self.keys: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.max_tokens = 1
        self.ip_exact: dict[ipaddress._BaseAddress, set[str]] = defaultdict(set)
        self.ip_blocks: list[tuple[ipaddress._BaseNetwork, str]] = []
        for node in graph.nodes.values():
            if node.type in ("site", "dxloc"):
                self._add(node.id, node.key, "alias")
            elif node.type in ID_TYPES:
                self._add(node.id, node.key, "id")
            for alias in node.aliases:
                self._add(alias, node.key, "alias")
            if match_names and node.name and node.type in NAME_TYPES:
                self._add(node.name, node.key, "name")
            for ip in node.ips:
                try:
                    if "/" in ip:
                        net = ipaddress.ip_network(ip, strict=False)
                        if net.num_addresses == 1:
                            self.ip_exact[net.network_address].add(node.key)
                        else:
                            self.ip_blocks.append((net, node.key))
                    else:
                        self.ip_exact[ipaddress.ip_address(ip)].add(node.key)
                except ValueError:
                    continue

    def _add(self, text: str, key: str, kind: str) -> None:
        tokens = _TOKEN_RE.findall(str(text).lower())
        if not tokens:
            return
        joined = "".join(tokens)
        has_digit = any(c.isdigit() for c in joined)
        if kind == "id":
            if len(joined) < 3:
                return
        elif kind == "alias":
            if len(joined) < self.min_length or (joined.isdigit() and len(joined) < 6):
                return
        else:  # display names: must be distinctive
            if len(joined) < self.min_name_length or not (has_digit or len(tokens) >= 2):
                return
        tokens = tokens[:_MAX_NGRAM]
        self.keys["".join(tokens)].add((key, kind))
        self.max_tokens = max(self.max_tokens, len(tokens))

    # -- lookups --

    def lookup(self, identifier: str, via: str = "declared") -> list[Match]:
        """Resolve an identifier a parser extracted (e.g. a BCOP object id)."""
        hits = self.keys.get(norm(identifier))
        if hits:
            return [Match(key, identifier, kind, via, _CONFIDENCE[kind]) for key, kind in sorted(hits)]
        ip_matches = self._ip_matches(identifier.strip(), via)
        if ip_matches:
            return ip_matches
        # "OGYX/123456//ZYO (10G wave)" - look for a known id inside the declared string
        return [Match(m.key, m.text, m.kind, via, m.confidence) for m in self.scan(identifier)]

    def scan(self, text: str) -> list[Match]:
        tokens = [(m.group().lower(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
        found: dict[tuple[str, str], Match] = {}
        for i in range(len(tokens)):
            acc = ""
            for n in range(self.max_tokens):
                j = i + n
                if j >= len(tokens):
                    break
                if n:
                    gap = text[tokens[j - 1][2]: tokens[j][1]]
                    if not _GAP_OK_RE.fullmatch(gap):
                        break
                acc += tokens[j][0]
                hits = self.keys.get(acc)
                if not hits:
                    continue
                snippet = text[tokens[i][1]: tokens[j][2]]
                for key, kind in hits:
                    ident = (key, kind)
                    if ident not in found or len(snippet) > len(found[ident].text):
                        found[ident] = Match(key, snippet, kind, "text", _CONFIDENCE[kind])
        for regex in (IPV4_RE, IPV6_RE):
            for m in regex.finditer(text):
                token = m.group(0)
                if regex is IPV6_RE and "::" not in token and token.count(":") < 7:
                    continue
                for match in self._ip_matches(token, "text"):
                    found.setdefault((match.key, match.kind), match)
        return self._prefer_specific(list(found.values()))

    def _ip_matches(self, token: str, via: str) -> list[Match]:
        out: list[Match] = []
        try:
            if "/" in token:
                net = ipaddress.ip_network(token, strict=False)
                if net.num_addresses == 1:
                    return self._ip_matches(str(net.network_address), via)
                min_len = 16 if net.version == 4 else 48
                if net.prefixlen < min_len:
                    return []
                for address, keys in self.ip_exact.items():
                    if address.version == net.version and address in net:
                        out += [Match(k, token, "cidr", via, "medium") for k in keys]
                for block, key in self.ip_blocks:
                    if block.version == net.version and net.subnet_of(block):
                        out.append(Match(key, token, "block", via, "high"))
            else:
                address = ipaddress.ip_address(token)
                out += [Match(k, token, "ip", via, "high") for k in self.ip_exact.get(address, ())]
                for block, key in self.ip_blocks:
                    if block.version == address.version and address in block:
                        out.append(Match(key, token, "block", via, "high"))
        except ValueError:
            return []
        return out

    @staticmethod
    def _prefer_specific(matches: list[Match]) -> list[Match]:
        """Drop a match whose text is a strict substring of another match for a different node
        at the same position (e.g. a LAG name that is a prefix of a member connection name)."""
        out = []
        for m in matches:
            shadowed = any(
                o is not m and o.key != m.key and m.text in o.text and len(o.text) > len(m.text) and m.kind != "id"
                for o in matches
            )
            if not shadowed:
                out.append(m)
        return out
