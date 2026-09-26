"""AWS topology: read-only collection (boto3) and normalisation into the dependency graph.

Collection and normalisation are separate so the raw API responses can be stored in the
snapshot (inventory edits re-build the graph without calling AWS) and so tests / the demo can
feed canned responses through exactly the same normalisation code.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from ..config import AwsAccount, AwsConfig
from ..util import iso, utcnow
from .model import HYBRID_ATTACHMENT_TYPES, Graph, Node, make_key, type_for_aws_id

log = logging.getLogger(__name__)

RAW_VERSION = 1

# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def _session(account: AwsAccount):
    import boto3

    base = boto3.Session(profile_name=account.profile) if account.profile else boto3.Session()
    if not account.role_arn:
        return base
    params = {"RoleArn": account.role_arn, "RoleSessionName": "blastradius"}
    if account.external_id:
        params["ExternalId"] = account.external_id
    creds = base.client("sts").assume_role(**params)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _paginate(client, operation: str, key: str, **kwargs) -> list:
    if client.can_paginate(operation):
        out: list = []
        for page in client.get_paginator(operation).paginate(**kwargs):
            out.extend(page.get(key, []))
        return out
    return getattr(client, operation)(**kwargs).get(key, [])


def _dx_all(fn: Callable, key: str, **kwargs) -> list:
    out: list = []
    token = None
    while True:
        resp = fn(**kwargs, **({"nextToken": token} if token else {}))
        out.extend(resp.get(key, []))
        token = resp.get("nextToken")
        if not token:
            return out


def collect(cfg: AwsConfig, session_factory: Callable[[AwsAccount], Any] = _session) -> dict:
    """Describe everything we need in every configured account/region. Never raises for
    per-call failures: errors are recorded so the UI can show coverage gaps."""
    from botocore.config import Config as BotoConfig

    boto_cfg = BotoConfig(retries={"mode": "adaptive", "max_attempts": 8}, connect_timeout=10, read_timeout=60)
    raw: dict = {"version": RAW_VERSION, "collected_at": iso(utcnow()), "accounts": []}
    for account in cfg.accounts:
        entry: dict = {"name": account.name, "account_id": None, "errors": [], "regions": {}, "global": {}}
        raw["accounts"].append(entry)
        try:
            session = session_factory(account)
            entry["account_id"] = session.client("sts", config=boto_cfg).get_caller_identity()["Account"]
        except Exception as exc:
            entry["errors"].append({"scope": "session", "error": str(exc)})
            log.error("aws account %s: cannot obtain session: %s", account.name, exc)
            continue
        regions = account.regions or cfg.regions
        for region in regions:
            entry["regions"][region] = _collect_region(session, region, cfg, entry["errors"], boto_cfg)
        entry["global"] = _collect_dx_gateways(session, regions[0], entry["errors"], boto_cfg)
    return raw


def _collect_region(session, region: str, cfg: AwsConfig, errors: list, boto_cfg) -> dict:
    ec2 = session.client("ec2", region_name=region, config=boto_cfg)
    dx = session.client("directconnect", region_name=region, config=boto_cfg)
    out: dict = {}

    def call(name: str, fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            value = fn()
        except Exception as exc:  # AccessDenied, opt-in region disabled, throttling after retries...
            errors.append({"region": region, "call": name, "error": str(exc)[:500]})
            log.warning("aws %s %s failed: %s", region, name, exc)
            value = [] if default is None else default
        return value

    out["vpcs"] = call("describe_vpcs", lambda: _paginate(ec2, "describe_vpcs", "Vpcs"))
    out["subnets"] = call("describe_subnets", lambda: _paginate(ec2, "describe_subnets", "Subnets"))
    out["route_tables"] = call("describe_route_tables", lambda: _paginate(ec2, "describe_route_tables", "RouteTables"))
    if cfg.collect_enis:
        out["enis"] = call("describe_network_interfaces",
                           lambda: _paginate(ec2, "describe_network_interfaces", "NetworkInterfaces"))
    if cfg.collect_instances:
        out["instances"] = call(
            "describe_instances",
            lambda: [i for r in _paginate(ec2, "describe_instances", "Reservations") for i in r.get("Instances", [])],
        )
    out["vgws"] = call("describe_vpn_gateways", lambda: ec2.describe_vpn_gateways().get("VpnGateways", []))
    out["cgws"] = call("describe_customer_gateways", lambda: ec2.describe_customer_gateways().get("CustomerGateways", []))
    out["vpns"] = call("describe_vpn_connections", lambda: ec2.describe_vpn_connections().get("VpnConnections", []))
    out["tgws"] = call("describe_transit_gateways", lambda: _paginate(ec2, "describe_transit_gateways", "TransitGateways"))
    out["tgw_attachments"] = call(
        "describe_transit_gateway_attachments",
        lambda: _paginate(ec2, "describe_transit_gateway_attachments", "TransitGatewayAttachments"),
    )
    out["tgw_route_tables"] = call(
        "describe_transit_gateway_route_tables",
        lambda: _paginate(ec2, "describe_transit_gateway_route_tables", "TransitGatewayRouteTables"),
    )
    out["tgw_routes"], out["tgw_propagations"], out["tgw_associations"] = {}, {}, {}
    for table in out["tgw_route_tables"]:
        rid = table["TransitGatewayRouteTableId"]
        if table.get("State") not in (None, "available"):
            continue
        out["tgw_routes"][rid] = call(
            f"search_transit_gateway_routes:{rid}",
            lambda rid=rid: ec2.search_transit_gateway_routes(
                TransitGatewayRouteTableId=rid,
                Filters=[{"Name": "state", "Values": ["active", "blackhole"]}],
                MaxResults=1000,
            ).get("Routes", []),
        )
        out["tgw_propagations"][rid] = call(
            f"get_transit_gateway_route_table_propagations:{rid}",
            lambda rid=rid: _paginate(ec2, "get_transit_gateway_route_table_propagations",
                                      "TransitGatewayRouteTablePropagations", TransitGatewayRouteTableId=rid),
        )
        out["tgw_associations"][rid] = call(
            f"get_transit_gateway_route_table_associations:{rid}",
            lambda rid=rid: _paginate(ec2, "get_transit_gateway_route_table_associations",
                                      "Associations", TransitGatewayRouteTableId=rid),
        )
    out["dx_connections"] = call("dx.describe_connections", lambda: dx.describe_connections().get("connections", []))
    out["dx_lags"] = call("dx.describe_lags", lambda: dx.describe_lags().get("lags", []))
    out["dx_vifs"] = call("dx.describe_virtual_interfaces",
                          lambda: dx.describe_virtual_interfaces().get("virtualInterfaces", []))
    out["dx_locations"] = call("dx.describe_locations", lambda: dx.describe_locations().get("locations", []))
    return out


def _collect_dx_gateways(session, region: str, errors: list, boto_cfg) -> dict:
    dx = session.client("directconnect", region_name=region, config=boto_cfg)
    out: dict = {"dx_gateways": [], "dx_gateway_associations": {}, "dx_gateway_attachments": {}}
    try:
        out["dx_gateways"] = _dx_all(dx.describe_direct_connect_gateways, "directConnectGateways")
        for gw in out["dx_gateways"]:
            gid = gw["directConnectGatewayId"]
            out["dx_gateway_associations"][gid] = _dx_all(
                dx.describe_direct_connect_gateway_associations, "directConnectGatewayAssociations",
                directConnectGatewayId=gid)
            out["dx_gateway_attachments"][gid] = _dx_all(
                dx.describe_direct_connect_gateway_attachments, "directConnectGatewayAttachments",
                directConnectGatewayId=gid)
    except Exception as exc:
        errors.append({"region": "global", "call": "dx gateways", "error": str(exc)[:500]})
        log.warning("aws dx gateway collection failed: %s", exc)
    return out


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def _tags(obj: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in obj.get("Tags") or obj.get("tags") or []:
        key = tag.get("Key", tag.get("key"))
        if key is not None:
            out[key] = tag.get("Value", tag.get("value", ""))
    return out


def _first_tag(tags: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value:
            return value.strip()
    return None


_ENI_DESCRIPTIONS = [
    (re.compile(r"^ELB (app|net|gwy)/([^/]+)/"), lambda m: ({"app": "ALB", "net": "NLB", "gwy": "GWLB"}[m.group(1)], m.group(2))),
    (re.compile(r"^ELB (\S+)$"), lambda m: ("CLB", m.group(1))),
    (re.compile(r"^RDSNetworkInterface"), lambda m: ("RDS", "")),
    (re.compile(r"^AWS Lambda VPC ENI-(.+?)(-[0-9a-f-]{36})?$"), lambda m: ("Lambda", m.group(1))),
    (re.compile(r"^Amazon EKS (\S+)"), lambda m: ("EKS", m.group(1))),
    (re.compile(r"^EFS mount target for (fs-[0-9a-f]+)"), lambda m: ("EFS", m.group(1))),
    (re.compile(r"^ElastiCache (\S+)"), lambda m: ("ElastiCache", m.group(1))),
    (re.compile(r"^Interface for NAT Gateway (nat-[0-9a-f]+)"), lambda m: ("NAT gateway", m.group(1))),
    (re.compile(r"^Network Interface for Transit Gateway Attachment (tgw-attach-[0-9a-f]+)"),
     lambda m: ("TGW attachment", m.group(1))),
    (re.compile(r"^VPC Endpoint Interface (vpce-[0-9a-f]+)"), lambda m: ("VPC endpoint", m.group(1))),
]
_INFRA_ENI_TYPES = {"nat_gateway", "transit_gateway", "vpc_endpoint", "gateway_load_balancer_endpoint",
                    "api_gateway_managed", "aws_codestar_connections_managed", "global_accelerator_managed",
                    "iot_rules_managed", "quicksight"}


def eni_role(eni: dict) -> tuple[str, str, bool]:
    """(kind, detail, infrastructure?) for display: ('ALB', 'payments-alb', False)."""
    description = eni.get("Description") or ""
    itype = eni.get("InterfaceType") or "interface"
    for regex, fn in _ENI_DESCRIPTIONS:
        m = regex.match(description)
        if m:
            kind, detail = fn(m)
            return kind, detail, kind in ("NAT gateway", "TGW attachment", "VPC endpoint")
    if eni.get("Attachment", {}).get("InstanceId"):
        return "EC2", eni["Attachment"]["InstanceId"], False
    return itype.replace("_", " "), description, itype in _INFRA_ENI_TYPES


class _Normaliser:
    def __init__(self, graph: Graph, cfg: AwsConfig):
        self.g = graph
        self.cfg = cfg
        self.warnings: list[str] = []
        self.vpc_tgw_attachment: dict[tuple[str, str], str] = {}
        self.dxgw_tgw_attachment: dict[tuple[str, str], str] = {}
        self.pending_routes: list[tuple[str, str, str, tuple[str, str], str]] = []
        self.pending_dxgw_tgw: list[tuple[str, str]] = []
        self.main_rtb: dict[str, str] = {}
        self.explicit_subnets: set[str] = set()
        self.subnet_vpc: dict[str, str] = {}
        self.instance_subnet: dict[str, str] = {}

    # -- helpers --

    def _circuit_and_site_tags(self, key: str, tags: dict, provider: str | None) -> None:
        circuit = _first_tag(tags, self.cfg.circuit_tag_keys)
        if circuit:
            for cid in [c.strip() for c in re.split(r"[,;]", circuit) if c.strip()]:
                ckey = make_key("circuit", cid)
                self.g.add(Node(key=ckey, type="circuit", id=cid, attrs={
                    "provider": _first_tag(tags, self.cfg.provider_tag_keys) or provider or "",
                    "source": "aws-tag"}))
                self.g.link(ckey, key, "dep")
        site = _first_tag(tags, self.cfg.site_tag_keys)
        if site:
            skey = make_key("site", site)
            self.g.add(Node(key=skey, type="site", id=site, name=site))
            self.g.link(skey, key, "dep")

    # -- per region --

    def region(self, data: dict, account: str | None, region: str) -> None:
        g = self.g
        for v in data.get("vpcs", []):
            tags = _tags(v)
            cidrs = [a["CidrBlock"] for a in v.get("CidrBlockAssociationSet", [])
                     if a.get("CidrBlockState", {}).get("State", "associated") == "associated"] or [v.get("CidrBlock")]
            cidrs += [a["Ipv6CidrBlock"] for a in v.get("Ipv6CidrBlockAssociationSet", []) if a.get("Ipv6CidrBlock")]
            g.add(Node(key=f"vpc:{v['VpcId']}", type="vpc", id=v["VpcId"], name=tags.get("Name", ""),
                       account=v.get("OwnerId") or account, region=region, tags=tags,
                       attrs={"cidrs": [c for c in cidrs if c], "is_default": v.get("IsDefault", False)}))

        for s in data.get("subnets", []):
            tags = _tags(s)
            sid, vpc = s["SubnetId"], s["VpcId"]
            self.subnet_vpc[sid] = vpc
            ipv6 = [a["Ipv6CidrBlock"] for a in s.get("Ipv6CidrBlockAssociationSet", []) if a.get("Ipv6CidrBlock")]
            g.add(Node(key=f"subnet:{sid}", type="subnet", id=sid, name=tags.get("Name", ""),
                       account=s.get("OwnerId") or account, region=region, tags=tags,
                       attrs={"cidr": s.get("CidrBlock"), "ipv6": ipv6, "az": s.get("AvailabilityZone"),
                              "vpc": vpc, "public_ip_on_launch": s.get("MapPublicIpOnLaunch", False)}))
            g.link(f"vpc:{vpc}", f"subnet:{sid}", "contains")

        for rt in data.get("route_tables", []):
            self._route_table(rt, account, region)

        for e in data.get("enis", []):
            self._eni(e, account, region)

        for i in data.get("instances", []):
            state = i.get("State", {}).get("Name")
            if state in ("terminated", "shutting-down"):
                continue
            tags = _tags(i)
            key = f"instance:{i['InstanceId']}"
            g.add(Node(key=key, type="instance", id=i["InstanceId"], name=tags.get("Name", ""), account=account,
                       region=region, tags=tags,
                       attrs={"state": state, "instance_type": i.get("InstanceType"), "private_ip": i.get("PrivateIpAddress"),
                              "subnet": i.get("SubnetId"), "vpc": i.get("VpcId"),
                              "az": i.get("Placement", {}).get("AvailabilityZone"), "platform": i.get("PlatformDetails")}))
            if i.get("SubnetId"):
                self.instance_subnet[i["InstanceId"]] = i["SubnetId"]

        for v in data.get("vgws", []):
            if v.get("State") in ("deleted", "deleting"):
                continue
            tags = _tags(v)
            vpcs = [a["VpcId"] for a in v.get("VpcAttachments", []) if a.get("State") == "attached"]
            g.add(Node(key=f"vgw:{v['VpnGatewayId']}", type="vgw", id=v["VpnGatewayId"], name=tags.get("Name", ""),
                       account=account, region=region, mode="any", tags=tags,
                       attrs={"state": v.get("State"), "asn": v.get("AmazonSideAsn"), "vpcs": vpcs}))

        for c in data.get("cgws", []):
            if c.get("State") in ("deleted", "deleting"):
                continue
            tags = _tags(c)
            key = f"cgw:{c['CustomerGatewayId']}"
            aliases = [c["DeviceName"]] if c.get("DeviceName") else []
            g.add(Node(key=key, type="cgw", id=c["CustomerGatewayId"], name=tags.get("Name", ""), account=account,
                       region=region, tags=tags, aliases=aliases, ips=[c["IpAddress"]] if c.get("IpAddress") else [],
                       attrs={"ip": c.get("IpAddress"), "bgp_asn": c.get("BgpAsn"), "device": c.get("DeviceName")}))
            self._circuit_and_site_tags(key, tags, None)

        for v in data.get("vpns", []):
            self._vpn(v, account, region)

        for t in data.get("tgws", []):
            if t.get("State") in ("deleted", "deleting"):
                continue
            tags = _tags(t)
            opts = t.get("Options", {})
            g.add(Node(key=f"tgw:{t['TransitGatewayId']}", type="tgw", id=t["TransitGatewayId"], name=tags.get("Name", ""),
                       account=t.get("OwnerId") or account, region=region, tags=tags,
                       attrs={"asn": opts.get("AmazonSideAsn"),
                              "default_association_rtb": opts.get("AssociationDefaultRouteTableId"),
                              "default_propagation_rtb": opts.get("PropagationDefaultRouteTableId")}))

        for a in data.get("tgw_attachments", []):
            self._tgw_attachment(a, account, region)

        for rt in data.get("tgw_route_tables", []):
            self._tgw_route_table(rt, data, account, region)

        locations = {loc["locationCode"]: loc for loc in data.get("dx_locations", [])}
        for c in data.get("dx_connections", []):
            self._dx_connection(c, account, region, locations)
        for lag in data.get("dx_lags", []):
            self._dx_lag(lag, account, region, locations)
        for vif in data.get("dx_vifs", []):
            self._dx_vif(vif, account, region)

    def _route_table(self, rt: dict, account: str | None, region: str) -> None:
        tags = _tags(rt)
        rid, vpc = rt["RouteTableId"], rt["VpcId"]
        key = f"rtb:{rid}"
        routes_summary = []
        is_main = False
        for assoc in rt.get("Associations", []):
            if assoc.get("AssociationState", {}).get("State", "associated") not in ("associated", "associating"):
                continue
            if assoc.get("Main"):
                is_main = True
                self.main_rtb[vpc] = key
            elif assoc.get("SubnetId"):
                self.explicit_subnets.add(assoc["SubnetId"])
                self.g.link(key, f"subnet:{assoc['SubnetId']}", "assoc")
        for route in rt.get("Routes", []):
            dest = (route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock")
                    or route.get("DestinationPrefixListId"))
            target = _route_target(route)
            routes_summary.append({"dest": dest, "target": target[1] if target else "local",
                                   "origin": route.get("Origin"), "state": route.get("State", "active")})
            if route.get("State") == "blackhole" or target is None or not dest:
                continue
            self.pending_routes.append((key, vpc, dest, target, route.get("Origin", "")))
        self.g.add(Node(key=key, type="rtb", id=rid, name=tags.get("Name", ""), account=rt.get("OwnerId") or account,
                        region=region, mode="routes", tags=tags,
                        attrs={"vpc": vpc, "main": is_main, "routes": routes_summary}))

    def _eni(self, e: dict, account: str | None, region: str) -> None:
        tags = _tags(e) or _tags({"Tags": e.get("TagSet", [])})
        eid = e["NetworkInterfaceId"]
        key = f"eni:{eid}"
        kind, detail, infra = eni_role(e)
        public_ip = e.get("Association", {}).get("PublicIp")
        instance_id = e.get("Attachment", {}).get("InstanceId")
        name = tags.get("Name") or (f"{kind} {detail}".strip() if kind != "EC2" else "")
        self.g.add(Node(key=key, type="eni", id=eid, name=name, account=e.get("OwnerId") or account, region=region,
                        tags=tags, ips=[public_ip] if public_ip else [],
                        attrs={"subnet": e.get("SubnetId"), "vpc": e.get("VpcId"), "kind": kind, "detail": detail,
                               "infra": infra, "private_ips": [p["PrivateIpAddress"] for p in e.get("PrivateIpAddresses", [])]
                               or ([e["PrivateIpAddress"]] if e.get("PrivateIpAddress") else []),
                               "public_ip": public_ip, "description": e.get("Description"),
                               "interface_type": e.get("InterfaceType"), "requester": e.get("RequesterId"),
                               "instance": instance_id, "status": e.get("Status")}))
        if e.get("SubnetId"):
            self.g.link(f"subnet:{e['SubnetId']}", key, "dep")
        if instance_id:
            self.g.link(key, f"instance:{instance_id}", "dep")

    def _vpn(self, v: dict, account: str | None, region: str) -> None:
        if v.get("State") in ("deleted", "deleting"):
            return
        tags = _tags(v)
        vid = v["VpnConnectionId"]
        key = f"vpn:{vid}"
        cgw = v.get("CustomerGatewayId")
        telemetry = {t.get("OutsideIpAddress"): t for t in v.get("VgwTelemetry", []) if t.get("OutsideIpAddress")}
        outside_ips = [t.get("OutsideIpAddress") for t in v.get("Options", {}).get("TunnelOptions", [])
                       if t.get("OutsideIpAddress")] or list(telemetry)
        self.g.add(Node(key=key, type="vpn", id=vid, name=tags.get("Name", ""), account=account, region=region,
                        mode="any", tags=tags,
                        attrs={"state": v.get("State"), "cgw": cgw, "vgw": v.get("VpnGatewayId"),
                               "tgw": v.get("TransitGatewayId"), "type": v.get("Type"),
                               "static_routes": [r.get("DestinationCidrBlock") for r in v.get("Routes", [])],
                               "static_routes_only": v.get("Options", {}).get("StaticRoutesOnly", False)}))
        cgw_key = f"cgw:{cgw}" if cgw else None
        if outside_ips:
            for n, ip in enumerate(outside_ips, 1):
                tkey = f"vpntun:{vid}/{ip}"
                tel = telemetry.get(ip, {})
                self.g.add(Node(key=tkey, type="vpntun", id=f"{vid} tunnel {n}", name=ip, account=account,
                                region=region, ips=[ip],
                                attrs={"outside_ip": ip, "status": tel.get("Status"),
                                       "status_message": tel.get("StatusMessage")}))
                if cgw_key:
                    self.g.link(cgw_key, tkey, "dep")
                self.g.link(tkey, key, "member")
        elif cgw_key:
            self.g.link(cgw_key, key, "dep")
        if v.get("VpnGatewayId"):
            self.g.link(key, f"vgw:{v['VpnGatewayId']}", "dep")
        self._circuit_and_site_tags(key, tags, None)

    def _tgw_attachment(self, a: dict, account: str | None, region: str) -> None:
        if a.get("State") in ("deleted", "deleting", "rejected", "failed", "failing"):
            return
        tags = _tags(a)
        aid, tgw = a["TransitGatewayAttachmentId"], a["TransitGatewayId"]
        rtype, rid = a.get("ResourceType"), a.get("ResourceId")
        key = f"tgwatt:{aid}"
        assoc = (a.get("Association") or {}).get("TransitGatewayRouteTableId")
        self.g.add(Node(key=key, type="tgwatt", id=aid, name=tags.get("Name") or f"{rtype} {rid}",
                        account=a.get("TransitGatewayOwnerId") or account, region=region, tags=tags,
                        attrs={"tgw": tgw, "resource_type": rtype, "resource_id": rid,
                               "resource_owner": a.get("ResourceOwnerId"), "association": assoc,
                               "hybrid": rtype in HYBRID_ATTACHMENT_TYPES, "state": a.get("State")}))
        self.g.link(f"tgw:{tgw}", key, "dep")
        if rtype == "vpc":
            self.vpc_tgw_attachment[(tgw, rid)] = key
            if assoc:
                self.g.link(f"tgwrtb:{assoc}", key, "assoc")
        elif rtype == "vpn" and rid:
            self.g.link(f"vpn:{rid}", key, "dep")
        elif rtype == "direct-connect-gateway" and rid:
            self.dxgw_tgw_attachment[(tgw, rid)] = key
            self.g.link(f"dxgw:{rid}", key, "dep")

    def _tgw_route_table(self, rt: dict, data: dict, account: str | None, region: str) -> None:
        if rt.get("State") not in (None, "available"):
            return
        tags = _tags(rt)
        rid = rt["TransitGatewayRouteTableId"]
        key = f"tgwrtb:{rid}"
        summary = []
        for route in data.get("tgw_routes", {}).get(rid, []):
            dest = route.get("DestinationCidrBlock") or route.get("PrefixListId")
            atts = route.get("TransitGatewayAttachments", [])
            summary.append({"dest": dest, "type": route.get("Type"), "state": route.get("State"),
                            "targets": [f"{x.get('ResourceType')}:{x.get('ResourceId')}" for x in atts]})
            if route.get("State") != "active" or not dest:
                continue
            for att in atts:
                if att.get("ResourceType") in HYBRID_ATTACHMENT_TYPES and att.get("TransitGatewayAttachmentId"):
                    self.g.link(f"tgwatt:{att['TransitGatewayAttachmentId']}", key, "route", prefix=dest)
        propagations = [
            {"attachment": p.get("TransitGatewayAttachmentId"), "resource_type": p.get("ResourceType"),
             "resource_id": p.get("ResourceId")}
            for p in data.get("tgw_propagations", {}).get(rid, []) if p.get("State", "enabled") == "enabled"
        ]
        for assoc in data.get("tgw_associations", {}).get(rid, []):
            if assoc.get("ResourceType") == "vpc" and assoc.get("State", "associated") == "associated":
                self.g.link(key, f"tgwatt:{assoc['TransitGatewayAttachmentId']}", "assoc")
        self.g.add(Node(key=key, type="tgwrtb", id=rid, name=tags.get("Name", ""), account=account, region=region,
                        mode="routes", tags=tags,
                        attrs={"tgw": rt.get("TransitGatewayId"), "routes": summary, "propagations": propagations,
                               "default_association": rt.get("DefaultAssociationRouteTable", False)}))

    def _dx_location(self, code: str | None, region: str, locations: dict) -> str | None:
        if not code:
            return None
        loc = locations.get(code, {})
        name = loc.get("locationName", "")
        aliases = [code]
        if name:
            aliases.append(name)
            aliases.append(name.split(",")[0].strip())
        key = f"dxloc:{code}"
        self.g.add(Node(key=key, type="dxloc", id=code, name=name, region=loc.get("region") or region, aliases=aliases))
        return key

    def _dx_connection(self, c: dict, account: str | None, region: str, locations: dict) -> None:
        if c.get("connectionState") in ("deleted", "rejected"):
            return
        tags = _tags(c)
        cid = c["connectionId"]
        key = f"dxcon:{cid}"
        device = c.get("awsDeviceV2") or c.get("awsDevice")
        self.g.add(Node(key=key, type="dxcon", id=cid, name=c.get("connectionName", ""),
                        account=c.get("ownerAccount") or account, region=c.get("region") or region, tags=tags,
                        aliases=[device] if device else [],
                        attrs={"location": c.get("location"), "bandwidth": c.get("bandwidth"),
                               "state": c.get("connectionState"), "lag": c.get("lagId"),
                               "partner": c.get("partnerName"), "provider": c.get("providerName"),
                               "aws_device": device, "vlan": c.get("vlan"),
                               "logical_redundancy": c.get("hasLogicalRedundancy")}))
        loc_key = self._dx_location(c.get("location"), region, locations)
        if loc_key:
            self.g.link(loc_key, key, "dep")
        self._circuit_and_site_tags(key, tags, c.get("providerName") or c.get("partnerName"))
        if c.get("lagId"):
            self.g.link(key, f"dxlag:{c['lagId']}", "member")

    def _dx_lag(self, lag: dict, account: str | None, region: str, locations: dict) -> None:
        if lag.get("lagState") in ("deleted", "rejected"):
            return
        tags = _tags(lag)
        lid = lag["lagId"]
        key = f"dxlag:{lid}"
        minimum = int(lag.get("minimumLinks") or 0)
        self.g.add(Node(key=key, type="dxlag", id=lid, name=lag.get("lagName", ""),
                        account=lag.get("ownerAccount") or account, region=lag.get("region") or region, tags=tags,
                        mode="quorum", quorum=max(1, minimum),
                        attrs={"location": lag.get("location"), "bandwidth": lag.get("connectionsBandwidth"),
                               "members": lag.get("numberOfConnections"), "minimum_links": minimum,
                               "state": lag.get("lagState")}))
        for member in lag.get("connections", []):
            self._dx_connection(member, account, region, locations)
            self.g.link(f"dxcon:{member['connectionId']}", key, "member")

    def _dx_vif(self, v: dict, account: str | None, region: str) -> None:
        if v.get("virtualInterfaceState") in ("deleted", "rejected", "deleting"):
            return
        tags = _tags(v)
        vid = v["virtualInterfaceId"]
        key = f"dxvif:{vid}"
        peers = v.get("bgpPeers", [])
        ips = []
        for peer in peers:
            for field in ("customerAddress", "amazonAddress"):
                if peer.get(field):
                    ips.append(peer[field].split("/")[0])
        self.g.add(Node(key=key, type="dxvif", id=vid, name=v.get("virtualInterfaceName", ""),
                        account=v.get("ownerAccount") or account, region=v.get("region") or region, tags=tags, ips=ips,
                        attrs={"vif_type": v.get("virtualInterfaceType"), "vlan": v.get("vlan"), "asn": v.get("asn"),
                               "amazon_asn": v.get("amazonSideAsn"), "state": v.get("virtualInterfaceState"),
                               "connection": v.get("connectionId"),
                               "bgp": [{"customer": p.get("customerAddress"), "amazon": p.get("amazonAddress"),
                                        "status": p.get("bgpStatus"), "family": p.get("addressFamily")} for p in peers]}))
        parent = v.get("connectionId") or ""
        if parent:
            self.g.link(f"dxlag:{parent}" if parent.startswith("dxlag-") else f"dxcon:{parent}", key, "dep")
        if v.get("virtualGatewayId"):
            self.g.link(key, f"vgw:{v['virtualGatewayId']}", "dep")
        if v.get("directConnectGatewayId"):
            self.g.link(key, f"dxgw:{v['directConnectGatewayId']}", "dep")

    # -- account-global --

    def dx_global(self, data: dict, account: str | None) -> None:
        for gw in data.get("dx_gateways", []):
            if gw.get("directConnectGatewayState") in ("deleted", "deleting"):
                continue
            gid = gw["directConnectGatewayId"]
            self.g.add(Node(key=f"dxgw:{gid}", type="dxgw", id=gid, name=gw.get("directConnectGatewayName", ""),
                            account=gw.get("ownerAccount") or account, mode="any",
                            aliases=[gw["directConnectGatewayName"]] if gw.get("directConnectGatewayName") else [],
                            attrs={"asn": gw.get("amazonSideAsn"), "state": gw.get("directConnectGatewayState")}))
        for gid, assocs in data.get("dx_gateway_associations", {}).items():
            allowed = []
            for a in assocs:
                if a.get("associationState") not in (None, "associated", "associating", "updating"):
                    continue
                target = a.get("associatedGateway") or {}
                prefixes = [p.get("cidr") for p in a.get("allowedPrefixesToDirectConnectGateway", [])]
                allowed.append({"gateway": target.get("id"), "type": target.get("type"), "prefixes": prefixes})
                if target.get("type") == "virtualPrivateGateway":
                    self.g.link(f"dxgw:{gid}", f"vgw:{target['id']}", "dep")
                elif target.get("type") == "transitGateway":
                    self.pending_dxgw_tgw.append((gid, target["id"]))
            node = self.g.ensure(f"dxgw:{gid}")
            node.attrs["associations"] = allowed
        for gid, atts in data.get("dx_gateway_attachments", {}).items():
            for a in atts:
                if a.get("attachmentState") in ("attached", "attaching", None):
                    self.g.link(f"dxvif:{a['virtualInterfaceId']}", f"dxgw:{gid}", "dep")

    # -- cross-account resolution --

    def finish(self) -> None:
        g = self.g
        for rtb_key, vpc, dest, (ttype, tid), _origin in self.pending_routes:
            if ttype == "tgw":
                src = self.vpc_tgw_attachment.get((tid, vpc), f"tgw:{tid}")
                if src.startswith("tgw:"):
                    self.warnings.append(f"{rtb_key}: route {dest} -> {tid} but the VPC attachment was not collected")
            else:
                src = f"{ttype}:{tid}"
            g.link(src, rtb_key, "route", prefix=dest)
        for subnet, vpc in self.subnet_vpc.items():
            if subnet not in self.explicit_subnets and vpc in self.main_rtb:
                g.link(self.main_rtb[vpc], f"subnet:{subnet}", "assoc")
        for gid, tgw in self.pending_dxgw_tgw:
            if (tgw, gid) not in self.dxgw_tgw_attachment:
                self.warnings.append(f"DX gateway {gid} is associated with {tgw}, whose attachment was not collected")
        for node in list(g.of_type("instance")):
            if not g.up(node.key) and node.key.split(":", 1)[1] in self.instance_subnet:
                g.link(f"subnet:{self.instance_subnet[node.id]}", node.key, "dep")


def _route_target(route: dict) -> tuple[str, str] | None:
    if route.get("TransitGatewayId"):
        return ("tgw", route["TransitGatewayId"])
    gateway = route.get("GatewayId")
    if gateway:
        if gateway == "local":
            return None
        return (type_for_aws_id(gateway) or "external", gateway)
    for field, ntype in (("NatGatewayId", "nat"), ("VpcPeeringConnectionId", "pcx"), ("NetworkInterfaceId", "eni"),
                         ("InstanceId", "instance"), ("EgressOnlyInternetGatewayId", "eigw"),
                         ("LocalGatewayId", "lgw"), ("CoreNetworkArn", "cwan"), ("CarrierGatewayId", "external")):
        if route.get(field):
            return (ntype, route[field])
    return None


def add_to_graph(graph: Graph, raw: dict, cfg: AwsConfig) -> list[str]:
    """Normalise raw collection output into `graph`; returns coverage warnings."""
    norm = _Normaliser(graph, cfg)
    for account in raw.get("accounts", []):
        account_id = account.get("account_id")
        for region, data in account.get("regions", {}).items():
            norm.region(data, account_id, region)
        norm.dx_global(account.get("global", {}), account_id)
        for err in account.get("errors", []):
            norm.warnings.append(f"{account.get('name')}: {err.get('region', err.get('scope', ''))} "
                                 f"{err.get('call', '')}: {err.get('error', '')}".strip())
    norm.finish()
    return norm.warnings
