import datetime as dt

from conftest import demo_mail, mail

from blastradius import demo
from blastradius.classify import classify
from blastradius.config import load_config
from blastradius.mailparse import parse_mail
from blastradius.parsing import parse_message
from blastradius.parsing.generic import classify_impact, classify_status, clean_subject, extract_identifiers

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 26, 14, 30, tzinfo=UTC)


def _cfg(tmp_path):
    return load_config(None, demo.config_overrides(tmp_path / "x.db"))


def _parse(raw: bytes, cfg):
    m = parse_mail(raw)
    mtype = classify(m, cfg)
    assert mtype is not None, f"not classified: {m.subject}"
    return mtype, parse_message(m, mtype, cfg)


def test_bcop_calendar_is_authoritative(tmp_path):
    cfg = _cfg(tmp_path)
    mtype, [p] = _parse(demo_mail("01-zayo-bcop.eml", NOW), cfg)
    assert mtype.name == "carrier-maintenance"
    assert p.parser == "bcop" and p.structured
    assert p.provider == "Zayo"  # provider hint wins over X-MAINTNOTE-PROVIDER "zayo.com"
    assert p.maintenance_id == "TTN-0003581234"
    assert [c.id for c in p.circuits] == ["OGYX/123456//ZYO"]
    assert p.impact == "OUTAGE" and p.status == "CONFIRMED" and p.sequence == 0
    assert p.start == dt.datetime(2026, 9, 29, 1, tzinfo=UTC)
    assert p.end == dt.datetime(2026, 9, 29, 5, tzinfo=UTC)
    assert p.account == "ACME-UK-0042"


def test_multi_event_bcop_mail_yields_exclusive_notices(tmp_path):
    cfg = _cfg(tmp_path)
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:{i}\r\nDTSTART:2026100{i}T010000Z\r\nDTEND:2026100{i}T030000Z\r\n"
        f"X-MAINTNOTE-PROVIDER:Example\r\nX-MAINTNOTE-ACCOUNT:A\r\nX-MAINTNOTE-MAINTENANCE-ID:M-{i}\r\n"
        f"X-MAINTNOTE-OBJECT-ID:CKT-{i}\r\nX-MAINTNOTE-IMPACT:DEGRADED\r\nEND:VEVENT\r\n" for i in (1, 2))
    raw = mail("Planned maintenance x2", "BEGIN:VCALENDAR\r\nMETHOD:PUBLISH\r\n" + events + "END:VCALENDAR\r\n",
               content_type="text/calendar")
    _, parsed = _parse(raw, cfg)
    assert [p.maintenance_id for p in parsed] == ["M-1", "M-2"]
    assert all(p.exclusive_ids for p in parsed)
    assert [[c.id for c in p.circuits] for p in parsed] == [["CKT-1"], ["CKT-2"]]


def test_colt_html_table_with_dayfirst_hint(tmp_path):
    cfg = _cfg(tmp_path)
    _, [p] = _parse(demo_mail("02-colt-html.eml", NOW), cfg)
    assert p.parser == "generic"
    assert p.provider == "Colt"
    assert p.maintenance_id == "CRQ000012345678"
    assert [c.id for c in p.circuits] == ["LON/SLO/LE-884321"]
    assert p.start == dt.datetime(2026, 10, 1, 0, tzinfo=UTC)
    assert p.end == dt.datetime(2026, 10, 1, 4, tzinfo=UTC)
    assert p.impact == "OUTAGE"


def test_bt_plain_text_split_date_and_time(tmp_path):
    cfg = _cfg(tmp_path)
    _, [p] = _parse(demo_mail("03-bt-leased-line.eml", NOW), cfg)
    assert p.provider == "BT" and p.maintenance_id == "PEW-7788123"
    assert [c.id for c in p.circuits] == ["BT-IL-778899"]
    assert p.start == dt.datetime(2026, 10, 3, 21, tzinfo=UTC)  # 22:00 BST
    assert p.end == dt.datetime(2026, 10, 4, 1, tzinfo=UTC)
    assert "203.0.113.8/29" in p.identifiers["cidrs"]
    assert p.impact == "OUTAGE"


def test_aws_health_mail(tmp_path):
    cfg = _cfg(tmp_path)
    mtype, [p] = _parse(demo_mail("05-aws-health-dx.eml", NOW), cfg)
    assert mtype.name == "aws-health"
    assert p.parser == "aws_health" and p.provider == "AWS"
    assert p.declared_resources == ["dxcon-fgct2b02"]
    assert p.maintenance_id.startswith("arn:aws:health:eu-west-2::event/DIRECTCONNECT/")
    assert p.impact == "OUTAGE" and p.account == "111111111111"
    assert p.summary == "AWS Direct Connect scheduled maintenance"
    assert p.start == dt.datetime(2026, 10, 7, 6, tzinfo=UTC)


def test_unmapped_switch_hit_and_cancellation(tmp_path):
    cfg = _cfg(tmp_path)
    _, [p] = _parse(demo_mail("07-gtt-unmapped.eml", NOW), cfg)
    assert p.maintenance_id == "PM-556677" and p.impact == "DEGRADED"
    assert [c.id for c in p.circuits] == ["GTT-99887766"]
    _, [c] = _parse(demo_mail("10-gtt-cancelled.eml", NOW), cfg)
    assert c.status == "CANCELLED" and c.maintenance_id == "PM-556677"


def test_newsletter_is_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    assert classify(parse_mail(demo_mail("09-zayo-newsletter.eml", NOW)), cfg) is None


def test_plain_calendar_invite_supplies_window(tmp_path):
    cfg = _cfg(tmp_path)
    body = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Planned work\r\nDTSTART:20261010T220000Z\r\n"
            "DTEND:20261011T020000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    raw = mail("Planned maintenance on circuit ABC-12345", body, content_type="text/calendar")
    _, [p] = _parse(raw, cfg)
    assert p.parser == "generic+ical"
    assert p.start == dt.datetime(2026, 10, 10, 22, tzinfo=UTC)


def test_impact_wording():
    assert classify_impact("No impact to customer services is expected.", field=True) == "NO-IMPACT"
    assert classify_impact("Switch hit of less than 50ms") == "DEGRADED"
    assert classify_impact("Outage of up to 30 seconds during the switch") == "DEGRADED"
    assert classify_impact("Up to 30 minutes of downtime") == "OUTAGE"
    assert classify_impact("Your service will be down for approximately 4 hours") == "OUTAGE"
    assert classify_impact("Loss of redundancy on the protected path") == "REDUCED-REDUNDANCY"
    assert classify_impact("There is no outage expected; latency may increase") == "DEGRADED"
    assert classify_impact("Nothing relevant here") is None


def test_status_wording():
    assert classify_status("CANCELLED: planned work", "") == "CANCELLED"
    assert classify_status("Planned work - RESCHEDULED", "") == "RE-SCHEDULED"
    assert classify_status("Planned work", "The maintenance has been completed successfully.") == "COMPLETED"
    assert classify_status("Planned work", "This maintenance has been successfully completed.") == "COMPLETED"
    assert classify_status("Planned work", "We will notify you once the maintenance has been completed.") is None
    # boilerplate must not flip status
    assert classify_status("Planned work", "Maintenance may be cancelled or rescheduled at short notice.") is None


def test_subject_cleaning_and_identifiers():
    assert clean_subject("RE: FW: [EXTERNAL] Zayo maintenance") == "Zayo maintenance"
    assert clean_subject("[Scheduled change] AWS Direct Connect maintenance [AWS Account: 111111111111]") == \
        "AWS Direct Connect maintenance"
    ids = extract_identifiers("dxcon-fgabc123 vpn-0123456789abcdef0 i-0123abcd 10.0.0.1 2001:db8::1/64 "
                              "time 01:00:00 eu-west-2 account 123456789012 5f3e8a6c-2b1d-4c7e-9a0f-1b2c3d4e5f60")
    assert ids["aws_ids"] == ["dxcon-fgabc123", "vpn-0123456789abcdef0", "i-0123abcd"]
    assert ids["ips"] == ["10.0.0.1"] and ids["cidrs"] == ["2001:db8::/64"]
    assert ids["regions"] == ["eu-west-2"] and ids["accounts"] == ["123456789012"]
    assert ids["uuids"] == ["5f3e8a6c-2b1d-4c7e-9a0f-1b2c3d4e5f60"]
