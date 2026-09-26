"""A fictional three-account AWS estate, expressed as raw describe_* API responses so it runs
through exactly the same normalisation code as a live collection.

    network 111111111111  DX (Zayo @ EqLD5, Colt @ THN1, Lumen LAG @ EqLD5), DXGW, TGW, HQ VPN
    prod    222222222222  payments-api / reporting (app subnets), ledger + RDS (db subnets), bastion, ALB
    data    333333333333  data-warehouse, reaching the Slough DC over a private VIF on the Lumen LAG

On-prem: Slough DC owns 10.100.0.0/16 (DX), London HQ owns 10.200.0.0/16 (VPN over a BT leased line).
"""

from __future__ import annotations

NET, PROD, DATA = "111111111111", "222222222222", "333333333333"
REGION = "eu-west-2"
DXGW = "5f3e8a6c-2b1d-4c7e-9a0f-1b2c3d4e5f60"
TGW = "tgw-01000000000000001"
ATT_DXGW, ATT_VPN = "tgw-attach-01000000000000d01", "tgw-attach-01000000000000e01"
ATT_PROD, ATT_DATA = "tgw-attach-01000000000000a01", "tgw-attach-01000000000000a02"
RT_PROD, RT_ONPREM = "tgw-rtb-01000000000000a00", "tgw-rtb-01000000000000b00"
VPC_PROD, VPC_DATA = "vpc-02000000000000001", "vpc-03000000000000001"
VGW_DATA = "vgw-03000000000000001"
CGW_HQ, VPN_HQ = "cgw-01000000000000001", "vpn-01000000000000001"


def _tags(**kw) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in kw.items()]


def _dxtags(**kw) -> list[dict]:
    return [{"key": k, "value": v} for k, v in kw.items()]


def _subnet(sid, vpc, cidr, az, name, owner):
    return {"SubnetId": sid, "VpcId": vpc, "CidrBlock": cidr, "AvailabilityZone": az, "OwnerId": owner,
            "MapPublicIpOnLaunch": name.startswith("prod-public"), "Tags": _tags(Name=name)}


def _route(dest, **target):
    return {"DestinationCidrBlock": dest, "State": "active", "Origin": target.pop("origin", "CreateRoute"), **target}


def _instance(iid, subnet, vpc, ip, name, **tags):
    return {"InstanceId": iid, "SubnetId": subnet, "VpcId": vpc, "PrivateIpAddress": ip, "InstanceType": "m6i.large",
            "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "eu-west-2a" if subnet[-1] in "1" else "eu-west-2b"},
            "PlatformDetails": "Linux/UNIX", "Tags": _tags(Name=name, **tags)}


def _eni(eid, subnet, vpc, ip, owner, instance=None, description="", itype="interface", requester=None, public=None):
    eni = {"NetworkInterfaceId": eid, "SubnetId": subnet, "VpcId": vpc, "OwnerId": owner, "Status": "in-use",
           "PrivateIpAddress": ip, "PrivateIpAddresses": [{"PrivateIpAddress": ip, "Primary": True}],
           "Description": description, "InterfaceType": itype, "TagSet": []}
    if instance:
        eni["Attachment"] = {"InstanceId": instance, "Status": "attached"}
    if requester:
        eni["RequesterId"], eni["RequesterManaged"] = requester, True
    if public:
        eni["Association"] = {"PublicIp": public}
    return eni


def aws_raw(collected_at: str) -> dict:
    tgw_attachments = [
        {"TransitGatewayAttachmentId": ATT_DXGW, "TransitGatewayId": TGW, "TransitGatewayOwnerId": NET,
         "ResourceType": "direct-connect-gateway", "ResourceId": DXGW, "State": "available",
         "Association": {"TransitGatewayRouteTableId": RT_ONPREM, "State": "associated"}, "Tags": _tags(Name="dx-core")},
        {"TransitGatewayAttachmentId": ATT_VPN, "TransitGatewayId": TGW, "TransitGatewayOwnerId": NET,
         "ResourceType": "vpn", "ResourceId": VPN_HQ, "State": "available",
         "Association": {"TransitGatewayRouteTableId": RT_ONPREM, "State": "associated"}, "Tags": _tags(Name="vpn-london-hq")},
        {"TransitGatewayAttachmentId": ATT_PROD, "TransitGatewayId": TGW, "TransitGatewayOwnerId": NET,
         "ResourceType": "vpc", "ResourceId": VPC_PROD, "ResourceOwnerId": PROD, "State": "available",
         "Association": {"TransitGatewayRouteTableId": RT_PROD, "State": "associated"}, "Tags": _tags(Name="prod")},
        {"TransitGatewayAttachmentId": ATT_DATA, "TransitGatewayId": TGW, "TransitGatewayOwnerId": NET,
         "ResourceType": "vpc", "ResourceId": VPC_DATA, "ResourceOwnerId": DATA, "State": "available",
         "Association": {"TransitGatewayRouteTableId": RT_PROD, "State": "associated"}, "Tags": _tags(Name="data")},
    ]

    def tgw_route(dest, att, rtype, rid, kind="propagated"):
        return {"DestinationCidrBlock": dest, "Type": kind, "State": "active",
                "TransitGatewayAttachments": [{"TransitGatewayAttachmentId": att, "ResourceType": rtype, "ResourceId": rid}]}

    network = {
        "dx_locations": [
            {"locationCode": "EqLD5", "locationName": "Equinix LD5, Slough, United Kingdom", "region": REGION},
            {"locationCode": "THN1", "locationName": "Telehouse North, London, United Kingdom", "region": REGION},
        ],
        "dx_connections": [
            {"connectionId": "dxcon-fgzy1a01", "connectionName": "ld5-zayo-10g", "connectionState": "available",
             "location": "EqLD5", "bandwidth": "10Gbps", "ownerAccount": NET, "region": REGION, "providerName": "Zayo",
             "awsDeviceV2": "EqLD5-3k2zv8ft6w0f", "hasLogicalRedundancy": "no",
             "tags": _dxtags(CircuitId="OGYX/123456//ZYO", Carrier="Zayo")},
            {"connectionId": "dxcon-fgct2b02", "connectionName": "thn-colt-10g", "connectionState": "available",
             "location": "THN1", "bandwidth": "10Gbps", "ownerAccount": NET, "region": REGION, "providerName": "Colt",
             "awsDeviceV2": "THN1-1p9s0d7nq3yr", "hasLogicalRedundancy": "no", "tags": []},
        ],
        "dx_lags": [
            {"lagId": "dxlag-ffab1234", "lagName": "ld5-lumen-lag", "lagState": "available", "location": "EqLD5",
             "connectionsBandwidth": "10Gbps", "numberOfConnections": 2, "minimumLinks": 2, "ownerAccount": NET,
             "region": REGION, "tags": [],
             "connections": [
                 {"connectionId": "dxcon-fglm3c03", "connectionName": "ld5-lumen-lag-a", "connectionState": "available",
                  "location": "EqLD5", "bandwidth": "10Gbps", "ownerAccount": NET, "region": REGION, "lagId": "dxlag-ffab1234",
                  "providerName": "Lumen", "awsDeviceV2": "EqLD5-2h8wc6s4b7za", "tags": _dxtags(CircuitId="LUMEN/441122")},
                 {"connectionId": "dxcon-fglm4c04", "connectionName": "ld5-lumen-lag-b", "connectionState": "available",
                  "location": "EqLD5", "bandwidth": "10Gbps", "ownerAccount": NET, "region": REGION, "lagId": "dxlag-ffab1234",
                  "providerName": "Lumen", "awsDeviceV2": "EqLD5-2h8wc6s4b7za", "tags": _dxtags(CircuitId="LUMEN/441123")},
             ]},
        ],
        "dx_vifs": [
            {"virtualInterfaceId": "dxvif-fgta1a01", "virtualInterfaceName": "transit-zayo", "virtualInterfaceType": "transit",
             "virtualInterfaceState": "available", "connectionId": "dxcon-fgzy1a01", "vlan": 101, "asn": 65010,
             "amazonSideAsn": 64512, "directConnectGatewayId": DXGW, "ownerAccount": NET, "region": REGION,
             "bgpPeers": [{"customerAddress": "169.254.10.1/30", "amazonAddress": "169.254.10.2/30", "bgpStatus": "up",
                           "addressFamily": "ipv4"}]},
            {"virtualInterfaceId": "dxvif-fgtb2b02", "virtualInterfaceName": "transit-colt", "virtualInterfaceType": "transit",
             "virtualInterfaceState": "available", "connectionId": "dxcon-fgct2b02", "vlan": 102, "asn": 65010,
             "amazonSideAsn": 64512, "directConnectGatewayId": DXGW, "ownerAccount": NET, "region": REGION,
             "bgpPeers": [{"customerAddress": "169.254.20.1/30", "amazonAddress": "169.254.20.2/30", "bgpStatus": "up",
                           "addressFamily": "ipv4"}]},
            {"virtualInterfaceId": "dxvif-fgpc3c03", "virtualInterfaceName": "private-data-lag", "virtualInterfaceType": "private",
             "virtualInterfaceState": "available", "connectionId": "dxlag-ffab1234", "vlan": 201, "asn": 65010,
             "amazonSideAsn": 64513, "virtualGatewayId": VGW_DATA, "ownerAccount": DATA, "region": REGION,
             "bgpPeers": [{"customerAddress": "169.254.30.1/30", "amazonAddress": "169.254.30.2/30", "bgpStatus": "up",
                           "addressFamily": "ipv4"}]},
        ],
        "tgws": [{"TransitGatewayId": TGW, "State": "available", "OwnerId": NET, "Tags": _tags(Name="core-tgw"),
                  "Options": {"AmazonSideAsn": 64512, "AssociationDefaultRouteTableId": RT_PROD,
                              "PropagationDefaultRouteTableId": RT_PROD}}],
        "tgw_attachments": tgw_attachments,
        "tgw_route_tables": [
            {"TransitGatewayRouteTableId": RT_PROD, "TransitGatewayId": TGW, "State": "available",
             "DefaultAssociationRouteTable": True, "Tags": _tags(Name="prod-rt")},
            {"TransitGatewayRouteTableId": RT_ONPREM, "TransitGatewayId": TGW, "State": "available",
             "Tags": _tags(Name="onprem-rt")},
        ],
        "tgw_routes": {
            RT_PROD: [
                tgw_route("10.100.0.0/16", ATT_DXGW, "direct-connect-gateway", DXGW),
                tgw_route("10.200.0.0/16", ATT_VPN, "vpn", VPN_HQ),
                tgw_route("10.20.0.0/16", ATT_PROD, "vpc", VPC_PROD),
                tgw_route("10.30.0.0/16", ATT_DATA, "vpc", VPC_DATA),
            ],
            RT_ONPREM: [
                tgw_route("10.20.0.0/16", ATT_PROD, "vpc", VPC_PROD),
                tgw_route("10.30.0.0/16", ATT_DATA, "vpc", VPC_DATA),
            ],
        },
        "tgw_propagations": {
            RT_PROD: [{"TransitGatewayAttachmentId": a, "ResourceType": t, "ResourceId": r, "State": "enabled"}
                      for a, t, r in ((ATT_DXGW, "direct-connect-gateway", DXGW), (ATT_VPN, "vpn", VPN_HQ),
                                      (ATT_PROD, "vpc", VPC_PROD), (ATT_DATA, "vpc", VPC_DATA))],
            RT_ONPREM: [{"TransitGatewayAttachmentId": a, "ResourceType": "vpc", "ResourceId": r, "State": "enabled"}
                        for a, r in ((ATT_PROD, VPC_PROD), (ATT_DATA, VPC_DATA))],
        },
        "tgw_associations": {
            RT_PROD: [{"TransitGatewayAttachmentId": ATT_PROD, "ResourceType": "vpc", "ResourceId": VPC_PROD, "State": "associated"},
                      {"TransitGatewayAttachmentId": ATT_DATA, "ResourceType": "vpc", "ResourceId": VPC_DATA, "State": "associated"}],
            RT_ONPREM: [{"TransitGatewayAttachmentId": ATT_DXGW, "ResourceType": "direct-connect-gateway",
                         "ResourceId": DXGW, "State": "associated"},
                        {"TransitGatewayAttachmentId": ATT_VPN, "ResourceType": "vpn", "ResourceId": VPN_HQ,
                         "State": "associated"}],
        },
        "cgws": [{"CustomerGatewayId": CGW_HQ, "IpAddress": "203.0.113.10", "BgpAsn": "65020", "State": "available",
                  "Type": "ipsec.1", "DeviceName": "lon-hq-fw01", "Tags": _tags(Name="london-hq-fw")}],
        "vpns": [{"VpnConnectionId": VPN_HQ, "CustomerGatewayId": CGW_HQ, "TransitGatewayId": TGW, "State": "available",
                  "Type": "ipsec.1", "Category": "VPN", "Tags": _tags(Name="hq-vpn"),
                  "Options": {"StaticRoutesOnly": False, "TunnelOptions": [
                      {"OutsideIpAddress": "198.51.100.11", "TunnelInsideCidr": "169.254.100.0/30"},
                      {"OutsideIpAddress": "198.51.100.22", "TunnelInsideCidr": "169.254.100.4/30"}]},
                  "VgwTelemetry": [{"OutsideIpAddress": "198.51.100.11", "Status": "UP"},
                                   {"OutsideIpAddress": "198.51.100.22", "Status": "UP"}]}],
    }
    network_global = {
        "dx_gateways": [{"directConnectGatewayId": DXGW, "directConnectGatewayName": "core-dxgw", "amazonSideAsn": 64512,
                         "ownerAccount": NET, "directConnectGatewayState": "available"}],
        "dx_gateway_associations": {DXGW: [{
            "directConnectGatewayId": DXGW, "associationState": "associated",
            "associatedGateway": {"id": TGW, "type": "transitGateway", "ownerAccount": NET, "region": REGION},
            "allowedPrefixesToDirectConnectGateway": [{"cidr": "10.20.0.0/16"}, {"cidr": "10.30.0.0/16"}]}]},
        "dx_gateway_attachments": {DXGW: [
            {"directConnectGatewayId": DXGW, "virtualInterfaceId": "dxvif-fgta1a01", "attachmentState": "attached",
             "attachmentType": "TransitVirtualInterface"},
            {"directConnectGatewayId": DXGW, "virtualInterfaceId": "dxvif-fgtb2b02", "attachmentState": "attached",
             "attachmentType": "TransitVirtualInterface"}]},
    }

    a1, a2 = "subnet-02000000000000a01", "subnet-02000000000000a02"
    d1, d2 = "subnet-02000000000000d01", "subnet-02000000000000d02"
    e1, e2 = "subnet-02000000000000e01", "subnet-02000000000000e02"
    f1 = "subnet-02000000000000f01"
    prod = {
        "vpcs": [{"VpcId": VPC_PROD, "CidrBlock": "10.20.0.0/16", "OwnerId": PROD, "IsDefault": False,
                  "CidrBlockAssociationSet": [{"CidrBlock": "10.20.0.0/16", "CidrBlockState": {"State": "associated"}}],
                  "Tags": _tags(Name="prod")}],
        "subnets": [
            _subnet(a1, VPC_PROD, "10.20.1.0/24", "eu-west-2a", "prod-app-a", PROD),
            _subnet(a2, VPC_PROD, "10.20.2.0/24", "eu-west-2b", "prod-app-b", PROD),
            _subnet(d1, VPC_PROD, "10.20.11.0/24", "eu-west-2a", "prod-db-a", PROD),
            _subnet(d2, VPC_PROD, "10.20.12.0/24", "eu-west-2b", "prod-db-b", PROD),
            _subnet(e1, VPC_PROD, "10.20.101.0/24", "eu-west-2a", "prod-public-a", PROD),
            _subnet(e2, VPC_PROD, "10.20.102.0/24", "eu-west-2b", "prod-public-b", PROD),
            _subnet(f1, VPC_PROD, "10.20.200.0/28", "eu-west-2a", "prod-tgw-a", PROD),
        ],
        "route_tables": [
            {"RouteTableId": "rtb-02000000000000a00", "VpcId": VPC_PROD, "OwnerId": PROD, "Tags": _tags(Name="prod-app-rt"),
             "Associations": [{"SubnetId": a1, "Main": False}, {"SubnetId": a2, "Main": False}],
             "Routes": [_route("10.20.0.0/16", GatewayId="local", origin="CreateRouteTable"),
                        _route("0.0.0.0/0", NatGatewayId="nat-02000000000000001"),
                        _route("10.100.0.0/16", TransitGatewayId=TGW), _route("10.200.0.0/16", TransitGatewayId=TGW),
                        _route("10.30.0.0/16", TransitGatewayId=TGW)]},
            {"RouteTableId": "rtb-02000000000000d00", "VpcId": VPC_PROD, "OwnerId": PROD, "Tags": _tags(Name="prod-db-rt"),
             "Associations": [{"SubnetId": d1, "Main": False}, {"SubnetId": d2, "Main": False}],
             "Routes": [_route("10.20.0.0/16", GatewayId="local", origin="CreateRouteTable"),
                        _route("10.100.0.0/16", TransitGatewayId=TGW)]},
            {"RouteTableId": "rtb-02000000000000e00", "VpcId": VPC_PROD, "OwnerId": PROD, "Tags": _tags(Name="prod-public-rt"),
             "Associations": [{"SubnetId": e1, "Main": False}, {"SubnetId": e2, "Main": False}],
             "Routes": [_route("10.20.0.0/16", GatewayId="local", origin="CreateRouteTable"),
                        _route("0.0.0.0/0", GatewayId="igw-02000000000000001")]},
            {"RouteTableId": "rtb-02000000000000000", "VpcId": VPC_PROD, "OwnerId": PROD, "Tags": _tags(Name="prod-main"),
             "Associations": [{"Main": True, "RouteTableAssociationId": "rtbassoc-0200000000000000"}],
             "Routes": [_route("10.20.0.0/16", GatewayId="local", origin="CreateRouteTable")]},
        ],
        "instances": [
            _instance("i-02000000000000a11", a1, VPC_PROD, "10.20.1.11", "payments-api-1", Application="payments-api",
                      Owner="team-payments"),
            _instance("i-02000000000000a12", a2, VPC_PROD, "10.20.2.12", "payments-api-2", Application="payments-api",
                      Owner="team-payments"),
            _instance("i-02000000000000a21", a1, VPC_PROD, "10.20.1.21", "reporting-1", Application="reporting",
                      Owner="team-finance"),
            _instance("i-02000000000000d11", d1, VPC_PROD, "10.20.11.11", "ledger-1", Application="ledger"),
            _instance("i-02000000000000d12", d2, VPC_PROD, "10.20.12.12", "ledger-2", Application="ledger"),
            _instance("i-02000000000000e11", e1, VPC_PROD, "10.20.101.11", "bastion-1", Application="bastion"),
        ],
        "enis": [
            _eni("eni-02000000000000a11", a1, VPC_PROD, "10.20.1.11", PROD, "i-02000000000000a11"),
            _eni("eni-02000000000000a12", a2, VPC_PROD, "10.20.2.12", PROD, "i-02000000000000a12"),
            _eni("eni-02000000000000a21", a1, VPC_PROD, "10.20.1.21", PROD, "i-02000000000000a21"),
            _eni("eni-02000000000000d11", d1, VPC_PROD, "10.20.11.11", PROD, "i-02000000000000d11"),
            _eni("eni-02000000000000d12", d2, VPC_PROD, "10.20.12.12", PROD, "i-02000000000000d12"),
            _eni("eni-02000000000000e11", e1, VPC_PROD, "10.20.101.11", PROD, "i-02000000000000e11", public="18.130.0.11"),
            _eni("eni-02000000000000d31", d1, VPC_PROD, "10.20.11.31", PROD, description="RDSNetworkInterface",
                 requester="amazon-rds"),
            _eni("eni-02000000000000d32", d2, VPC_PROD, "10.20.12.32", PROD, description="RDSNetworkInterface",
                 requester="amazon-rds"),
            _eni("eni-02000000000000e21", e1, VPC_PROD, "10.20.101.21", PROD,
                 description="ELB app/payments-alb/50dc6c495c0c9188", requester="amazon-elb"),
            _eni("eni-02000000000000e22", e2, VPC_PROD, "10.20.102.22", PROD,
                 description="ELB app/payments-alb/50dc6c495c0c9188", requester="amazon-elb"),
            _eni("eni-02000000000000e31", e1, VPC_PROD, "10.20.101.31", PROD,
                 description="Interface for NAT Gateway nat-02000000000000001", itype="nat_gateway"),
            _eni("eni-02000000000000f11", f1, VPC_PROD, "10.20.200.4", PROD,
                 description=f"Network Interface for Transit Gateway Attachment {ATT_PROD}", itype="transit_gateway"),
        ],
        "tgw_attachments": [tgw_attachments[2]],
    }

    da, db_, df = "subnet-03000000000000a01", "subnet-03000000000000a02", "subnet-03000000000000f01"
    data = {
        "vpcs": [{"VpcId": VPC_DATA, "CidrBlock": "10.30.0.0/16", "OwnerId": DATA, "IsDefault": False,
                  "Tags": _tags(Name="data")}],
        "subnets": [
            _subnet(da, VPC_DATA, "10.30.1.0/24", "eu-west-2a", "data-a", DATA),
            _subnet(db_, VPC_DATA, "10.30.2.0/24", "eu-west-2b", "data-b", DATA),
            _subnet(df, VPC_DATA, "10.30.200.0/28", "eu-west-2a", "data-tgw-a", DATA),
        ],
        "route_tables": [
            {"RouteTableId": "rtb-03000000000000a00", "VpcId": VPC_DATA, "OwnerId": DATA, "Tags": _tags(Name="data-main"),
             "Associations": [{"Main": True}],
             "Routes": [_route("10.30.0.0/16", GatewayId="local", origin="CreateRouteTable"),
                        _route("10.100.0.0/16", GatewayId=VGW_DATA, origin="EnableVgwRoutePropagation"),
                        _route("10.20.0.0/16", TransitGatewayId=TGW)]},
        ],
        "instances": [
            _instance("i-03000000000000a11", da, VPC_DATA, "10.30.1.11", "etl-1", Application="data-warehouse",
                      Owner="data-eng"),
            _instance("i-03000000000000a12", db_, VPC_DATA, "10.30.2.12", "etl-2", Application="data-warehouse",
                      Owner="data-eng"),
        ],
        "enis": [
            _eni("eni-03000000000000a11", da, VPC_DATA, "10.30.1.11", DATA, "i-03000000000000a11"),
            _eni("eni-03000000000000a12", db_, VPC_DATA, "10.30.2.12", DATA, "i-03000000000000a12"),
        ],
        "vgws": [{"VpnGatewayId": VGW_DATA, "State": "available", "Type": "ipsec.1", "AmazonSideAsn": 64513,
                  "VpcAttachments": [{"VpcId": VPC_DATA, "State": "attached"}], "Tags": _tags(Name="data-vgw")}],
        "tgw_attachments": [tgw_attachments[3]],
    }
    return {
        "version": 1,
        "collected_at": collected_at,
        "demo": True,
        "accounts": [
            {"name": "network", "account_id": NET, "errors": [], "regions": {REGION: network}, "global": network_global},
            {"name": "prod", "account_id": PROD, "errors": [], "regions": {REGION: prod}, "global": {}},
            {"name": "data", "account_id": DATA, "errors": [], "regions": {REGION: data}, "global": {}},
        ],
    }
