"""AWS collection against moto (EC2 / TGW / VPN / DX connections) and normalisation into the graph."""

import boto3
import pytest
from moto import mock_aws

from blastradius.config import AwsAccount, AwsConfig
from blastradius.impact import what_if
from blastradius.topology.aws import add_to_graph, collect, eni_role
from blastradius.topology.model import Graph, make_key
from blastradius.util import OUTAGE

REGION = "eu-west-2"


@pytest.fixture
def aws_env(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _tag(ec2, resource, **tags):
    ec2.create_tags(Resources=[resource], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])


@mock_aws
def test_collect_and_normalise(aws_env):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.20.0.0/16")["Vpc"]["VpcId"]
    _tag(ec2, vpc, Name="prod")
    app_subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.20.1.0/24", AvailabilityZone=f"{REGION}a")["Subnet"]["SubnetId"]
    web_subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.20.2.0/24", AvailabilityZone=f"{REGION}b")["Subnet"]["SubnetId"]
    tgw = ec2.create_transit_gateway(Description="core")["TransitGateway"]["TransitGatewayId"]
    attachment = ec2.create_transit_gateway_vpc_attachment(
        TransitGatewayId=tgw, VpcId=vpc, SubnetIds=[app_subnet])["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
    vgw = ec2.create_vpn_gateway(Type="ipsec.1")["VpnGateway"]["VpnGatewayId"]
    ec2.attach_vpn_gateway(VpcId=vpc, VpnGatewayId=vgw)
    cgw = ec2.create_customer_gateway(Type="ipsec.1", PublicIp="203.0.113.10", BgpAsn=65000)["CustomerGateway"]["CustomerGatewayId"]
    _tag(ec2, cgw, CircuitId="BT-IL-1", Name="hq-fw")
    vpn = ec2.create_vpn_connection(Type="ipsec.1", CustomerGatewayId=cgw, VpnGatewayId=vgw)["VpnConnection"]["VpnConnectionId"]

    rtb = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.associate_route_table(RouteTableId=rtb, SubnetId=app_subnet)
    ec2.create_route(RouteTableId=rtb, DestinationCidrBlock="10.100.0.0/16", TransitGatewayId=tgw)
    ec2.create_route(RouteTableId=rtb, DestinationCidrBlock="10.200.0.0/16", GatewayId=vgw)

    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instance = ec2.run_instances(
        ImageId=image, MinCount=1, MaxCount=1, SubnetId=app_subnet, InstanceType="t3.micro",
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "payments-1"},
                                                                 {"Key": "Application", "Value": "payments"}]}],
    )["Instances"][0]["InstanceId"]
    web = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, SubnetId=web_subnet)["Instances"][0]["InstanceId"]

    dx = boto3.client("directconnect", region_name=REGION)
    conn = dx.create_connection(location="EqLD5", bandwidth="10Gbps", connectionName="ld5-zayo",
                                tags=[{"key": "CircuitId", "value": "ZYO-1"}])["connectionId"]

    cfg = AwsConfig(enabled=True, regions=[REGION], accounts=[AwsAccount(name="test")])
    raw = collect(cfg)
    [account] = raw["accounts"]
    assert account["account_id"] == "123456789012"
    region = account["regions"][REGION]
    assert {v["VpcId"] for v in region["vpcs"]} >= {vpc}
    assert [c["connectionId"] for c in region["dx_connections"]] == [conn]
    # moto does not implement VIFs / DX gateways: recorded as coverage errors, not fatal
    assert any("describe_virtual_interfaces" in e["call"] for e in account["errors"])

    from blastradius.topology.build import map_applications
    from blastradius.topology.inventory import Inventory

    graph = Graph()
    warnings = add_to_graph(graph, raw, cfg)
    map_applications(graph, Inventory(), cfg)
    assert any("describe_virtual_interfaces" in w for w in warnings)

    ups = lambda key: {(e.src, e.kind, e.prefix) for e in graph.up(key)}
    assert (f"subnet:{app_subnet}", "dep", None) in ups(f"eni:{graph.parents(f'instance:{instance}')[0].id}")
    assert (f"rtb:{rtb}", "assoc", None) in ups(f"subnet:{app_subnet}")
    assert (f"tgwatt:{attachment}", "route", "10.100.0.0/16") in ups(f"rtb:{rtb}")
    assert (f"vgw:{vgw}", "route", "10.200.0.0/16") in ups(f"rtb:{rtb}")
    assert f"vpn:{vpn}" in {e.src for e in graph.up(f"vgw:{vgw}")}
    assert make_key("circuit", "BT-IL-1") in graph.upstream(f"vpn:{vpn}")
    assert (make_key("circuit", "ZYO-1"), "dep", None) in ups(f"dxcon:{conn}")
    assert ("dxloc:EqLD5", "dep", None) in ups(f"dxcon:{conn}")
    # the web subnet has no explicit association: it falls back to the VPC main route table
    assert graph.parents(f"subnet:{web_subnet}", "rtb")[0].attrs["main"] is True
    assert graph.nodes["cgw:" + cgw].ips == ["203.0.113.10"]
    assert graph.nodes[f"instance:{instance}"].apps == ["payments"]

    # losing the leased line under the VPN hits only 10.200/16 in the app subnet, not the web subnet
    report = what_if(graph, [make_key("circuit", "BT-IL-1")], OUTAGE)
    by_key = {n["key"]: n for n in report["nodes"]}
    assert by_key[f"subnet:{app_subnet}"]["prefixes"] == {"10.200.0.0/16": OUTAGE}
    assert f"subnet:{web_subnet}" not in by_key
    assert f"instance:{web}" not in by_key
    assert [a["name"] for a in report["apps"]] == ["payments"]


def test_session_failure_is_recorded():
    def broken(account):
        raise RuntimeError("no credentials")

    raw = collect(AwsConfig(enabled=True, regions=[REGION], accounts=[AwsAccount(name="x")]), session_factory=broken)
    assert raw["accounts"][0]["errors"][0]["error"] == "no credentials"


def test_eni_roles():
    assert eni_role({"Description": "ELB app/payments-alb/50dc6c495c0c9188"}) == ("ALB", "payments-alb", False)
    assert eni_role({"Description": "RDSNetworkInterface"})[0] == "RDS"
    assert eni_role({"Description": "AWS Lambda VPC ENI-my-fn-2c1f3e5a-1111-2222-3333-444455556666"})[:2] == ("Lambda", "my-fn")
    assert eni_role({"Description": "Interface for NAT Gateway nat-0123456789abcdef0"}) == ("NAT gateway", "nat-0123456789abcdef0", True)
    assert eni_role({"Attachment": {"InstanceId": "i-1"}}) == ("EC2", "i-1", False)
