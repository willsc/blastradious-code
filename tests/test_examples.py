"""The shipped example config and inventory must stay valid."""

from pathlib import Path

from blastradius.config import load_config
from blastradius.notify import subscriptions
from blastradius.topology.build import build_graph
from blastradius.topology.inventory import load_inventory

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_and_inventory_load(monkeypatch):
    cfg = load_config(ROOT / "config.example.yaml")
    assert [t.name for t in cfg.message_types] == ["aws-health", "carrier-maintenance", "carrier-outage"]
    assert cfg.gmail.poll_interval == 60 and cfg.aws.refresh_interval == 6 * 3600
    assert cfg.notifications.reminders == [86400, 3600]
    for sub in cfg.subscriptions:
        subscriptions.validate(sub)
    inventory = load_inventory(ROOT / "inventory.example.yaml")
    graph, warnings = build_graph(None, inventory, cfg.aws)
    assert not [w for w in warnings if "unknown site" in w]
    assert "circuit:ogyx123456zyo" in graph.nodes and "app:payments-api" in graph.nodes
