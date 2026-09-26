import datetime as dt

import yaml
from conftest import NOW, demo_mail, mail

from blastradius import collisions, demo, notices
from blastradius.config import load_config
from blastradius.context import build_context
from blastradius.demo.aws_raw import aws_raw
from blastradius.util import AT_RISK, DEGRADED, OUTAGE, iso, loads


def by_provider(ctx):
    out = {}
    for row in ctx.db.query("SELECT * FROM notices ORDER BY id"):
        out.setdefault(row["provider"], []).append(row)
    return out


def apps(row):
    return {a["name"]: a["severity"] for a in loads(row["apps"], [])}


def test_demo_blast_radius(seeded):
    rows = by_provider(seeded)
    [zayo] = rows["Zayo"]
    assert apps(zayo) == {"ledger": DEGRADED, "payments-api": DEGRADED, "reporting": DEGRADED}
    blast = loads(zayo["blast"])
    assert [a["key"] for a in blast["anchors"]] == ["circuit:ogyx123456zyo"]
    assert {d["key"] for d in blast["dropped"]} == {"site:ld5", "site:slough-dc", "dxloc:EqLD5"}
    dxgw = next(n for n in blast["nodes"] if n["type"] == "dxgw")
    assert dxgw["severity"] == DEGRADED and dxgw["alternates"] == ["dxvif:dxvif-fgtb2b02"]

    [bt] = rows["BT"]  # VPN underlay: only subnets routing 10.200/16 are hit, ledger is not
    assert apps(bt) == {"payments-api": OUTAGE, "reporting": OUTAGE}
    assert all(n["prefixes"] in (None, {"10.200.0.0/16": OUTAGE}) for n in loads(bt["blast"])["nodes"])

    [lumen] = rows["Lumen"]  # LAG minimumLinks=2: one member down takes the private VIF down
    assert apps(lumen) == {"data-warehouse": OUTAGE}
    lag = next(n for n in loads(lumen["blast"])["nodes"] if n["type"] == "dxlag")
    assert "minimumLinks=2" in lag["reasons"][0]

    [equinix] = rows["Equinix"]  # facility work, provider says no impact
    assert set(apps(equinix).values()) == {AT_RISK} and len(apps(equinix)) == 4

    dx_health = next(r for r in rows["AWS"] if r["maintenance_id"].startswith("arn:aws:health"))
    vpn_health = next(r for r in rows["AWS"] if r["maintenance_id"].startswith("AWS_VPN_REDUNDANCY_LOSS"))
    assert apps(dx_health) == {"ledger": DEGRADED, "payments-api": DEGRADED, "reporting": DEGRADED}
    assert apps(vpn_health) == {"payments-api": DEGRADED, "reporting": DEGRADED}
    assert [a["type"] for a in loads(vpn_health["blast"])["anchors"]] == ["vpntun"]  # tunnel, not whole VPN

    [gtt] = rows["GTT"]
    assert gtt["status"] == "CANCELLED" and gtt["mapped"] == 0
    assert loads(gtt["blast"])["unmapped"] == ["GTT-99887766"]
    assert seeded.db.scalar("SELECT count(*) FROM messages WHERE status = 'ignored'") == 1


def test_reschedule_merges_into_one_notice_and_creates_collision(seeded):
    [zayo] = by_provider(seeded)["Zayo"]
    assert zayo["version"] == 2 and zayo["sequence"] == 1
    kinds = [e["kind"] for e in notices.events_for(seeded.db, zayo["id"])]
    assert kinds == ["created", "rescheduled"]
    assert len(notices.messages_for(seeded.db, zayo["id"])) == 2
    [collision] = collisions.active(seeded.db)
    colt = by_provider(seeded)["Colt"][0]
    assert collision["notice_ids"] == sorted([zayo["id"], colt["id"]])
    assert collision["severity"] == OUTAGE
    assert {a["name"]: (a["from"], a["to"]) for a in collision["apps"]} == {
        "ledger": (DEGRADED, OUTAGE), "payments-api": (DEGRADED, OUTAGE), "reporting": (DEGRADED, OUTAGE)}
    # the overlap is exactly the intersection of the two windows
    assert (collision["start"], collision["end"]) == (max(zayo["start_at"], colt["start_at"]),
                                                      min(zayo["end_at"], colt["end_at"]))


def test_duplicate_and_stale_messages(ctx):
    first = ctx.pipeline.ingest(demo_mail("06-zayo-bcop-reschedule.eml", NOW), source="t", source_id="new")
    stale = ctx.pipeline.ingest(demo_mail("01-zayo-bcop.eml", NOW), source="t", source_id="old")
    again = ctx.pipeline.ingest(demo_mail("01-zayo-bcop.eml", NOW), source="t", source_id="old")
    assert first.notice_ids == stale.notice_ids and again.status == "duplicate"
    row = notices.get(ctx.db, first.notice_ids[0])
    # the older SEQUENCE:0 mail arriving late must not move the window back
    assert row["start_at"] == "2026-10-01T02:00:00Z" and row["sequence"] == 1
    roles = sorted(m["role"] for m in notices.messages_for(ctx.db, row["id"]))
    assert roles == ["initial", "stale"]


def test_reminder_mail_without_details_changes_nothing(ctx):
    ctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t")
    reminder = mail("REMINDER: Planned Engineering Works - Ref PEW-7788123",
                    "This is a reminder of the works referenced above.\nRef: PEW-7788123\n",
                    sender="service.notifications@bt.com")
    result = ctx.pipeline.ingest(reminder, source="t")
    row = notices.get(ctx.db, result.notice_ids[0])
    assert row["version"] == 1 and row["start_at"] == "2026-10-03T21:00:00Z"
    assert [e["kind"] for e in notices.events_for(ctx.db, row["id"])] == ["created", "reminder"]


def test_inventory_change_recorrelates_and_escalates(tmp_path, cfg_overrides):
    inventory = tmp_path / "inventory.yaml"
    data = yaml.safe_load(demo.INVENTORY.read_text())
    circuit = next(c for c in data["circuits"] if c["id"] == "C-884321")
    circuit["aliases"] = []  # the Colt mail uses this alias: without it the notice is unmapped
    inventory.write_text(yaml.safe_dump(data))
    ctx = build_context(load_config(None, cfg_overrides(inventory=str(inventory))))
    ctx.store.refresh(aws_raw=aws_raw(iso(NOW)))
    result = ctx.pipeline.ingest(demo_mail("02-colt-html.eml", NOW), source="t")
    row = notices.get(ctx.db, result.notice_ids[0])
    assert row["mapped"] == 0

    circuit["aliases"] = ["LON/SLO/LE-884321"]
    inventory.write_text(yaml.safe_dump(data))
    assert ctx.store.inventory_changed()
    ctx.store.refresh(collect_aws=False)  # reuses the cached AWS collection
    assert ctx.pipeline.recorrelate() == 1
    row = notices.get(ctx.db, row["id"])
    assert row["mapped"] == 1 and row["severity"] == DEGRADED and row["version"] == 2
    assert [e["kind"] for e in notices.events_for(ctx.db, row["id"])] == ["created", "impact_changed"]


def test_rebuild_replays_everything(seeded):
    before = [(r["key"], r["severity"], r["start_at"]) for r in seeded.db.query("SELECT * FROM notices ORDER BY key")]
    assert seeded.pipeline.rebuild() == 11
    after = [(r["key"], r["severity"], r["start_at"]) for r in seeded.db.query("SELECT * FROM notices ORDER BY key")]
    assert before == after


def test_phases():
    row = {"status": "CONFIRMED", "start_at": "2026-09-26T13:00:00Z", "end_at": "2026-09-26T16:00:00Z",
           "updated_at": "2026-09-26T12:00:00Z"}
    assert notices.phase(row, NOW) == "in_progress"
    assert notices.phase(row | {"status": "CANCELLED"}, NOW) == "cancelled"
    assert notices.phase(row | {"end_at": None}, NOW + dt.timedelta(days=5)) == "completed"  # stale open outage
    assert notices.phase(row | {"start_at": "2026-09-27T00:00:00Z", "end_at": None}, NOW) == "upcoming"


def test_unknown_circuit_at_known_site_does_not_blast_the_site(ctx):
    raw = mail("Planned maintenance notification PM-1", "Maintenance ID: PM-1\nAffected Circuit: GTT-11223344\n"
               "A-end: Equinix LD5, Slough\nStart Time (UTC): 2026-10-09 02:00\nEnd Time (UTC): 2026-10-09 04:00\n"
               "Impact: outage for up to 2 hours\n", sender="change.management@gtt.net")
    row = notices.get(ctx.db, ctx.pipeline.ingest(raw, source="t").notice_ids[0])
    blast = loads(row["blast"])
    assert row["mapped"] == 0 and blast["unmapped"] == ["GTT-11223344"]
    assert {d["key"] for d in blast["dropped"]} >= {"site:ld5"}


def test_facility_notice_without_circuits_anchors_the_site(ctx):
    raw = mail("Scheduled maintenance - LD5 cooling", "Location: Equinix LD5, Slough\n"
               "Start: 2026-10-09 02:00 UTC\nEnd: 2026-10-09 04:00 UTC\nImpact: equipment at risk\n",
               sender="servicedesk@equinix.com")
    row = notices.get(ctx.db, ctx.pipeline.ingest(raw, source="t").notice_ids[0])
    blast = loads(row["blast"])
    assert [a["key"] for a in blast["anchors"]] == ["dxloc:EqLD5"]
    assert row["severity"] == AT_RISK


def test_heuristic_follow_up_cannot_override_structured_window(ctx):
    first = ctx.pipeline.ingest(demo_mail("01-zayo-bcop.eml", NOW), source="t")
    row = notices.get(ctx.db, first.notice_ids[0])
    # a plain-text chaser whose free text the heuristic parser reads differently
    chaser = mail("Zayo Planned Maintenance - TTN-0003581234 - REMINDER",
                  "Maintenance Ticket #: TTN-0003581234\nPlease note the work window: Start: 2026-12-01 01:00 GMT\n"
                  "End: 2026-12-01 02:00 GMT\nCircuit Id: OGYX/123456//ZYO\n", sender="mr@zayo.com")
    result = ctx.pipeline.ingest(chaser, source="t")
    assert result.notice_ids == [row["id"]]
    after = notices.get(ctx.db, row["id"])
    assert (after["start_at"], after["parser"], after["version"]) == (row["start_at"], "bcop", 1)
    [event] = [e for e in notices.events_for(ctx.db, row["id"]) if e["kind"] == "reminder"]
    assert "kept start_at" in loads(event["detail"])["notes"][0]
    cancel = mail("CANCELLED - Zayo Planned Maintenance - TTN-0003581234", "Maintenance Ticket #: TTN-0003581234\n"
                  "This maintenance has been cancelled.", sender="mr@zayo.com")
    ctx.pipeline.ingest(cancel, source="t")
    assert notices.get(ctx.db, row["id"])["status"] == "CANCELLED"
