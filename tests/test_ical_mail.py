import datetime as dt
from zoneinfo import ZoneInfo

from blastradius.mailparse import html_to_text, organisation_from_domain, parse_mail
from blastradius.parsing import ical

UTC = dt.timezone.utc

CAL = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Custom/Zone\r\nBEGIN:STANDARD\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\n"
    "END:STANDARD\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:Line one\\, with comma\\nline two\r\n"
    "DTSTART;TZID=Europe/London:20261003T010000\r\nDTEND;TZID=\"Custom/Zone\":20261003T050000\r\n"
    "DTSTAMP:20260926T100000Z\r\nX-MAINTNOTE-OBJECT-ID;ALTREP=\"https://x/y:z\":CIRCUIT/1\r\n"
    "X-MAINTNOTE-OBJECT-ID:CIRCUIT-2\r\nDESCRIPTION:a very long line that is folded across\r\n  two physical lines\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_ical_parsing_folding_params_escapes_and_timezones():
    [cal] = ical.parse_ical(CAL)
    [event] = list(cal.walk("VEVENT"))
    assert event.text("SUMMARY") == "Line one, with comma\nline two"
    assert event.text("DESCRIPTION") == "a very long line that is folded across two physical lines"
    ids = event.get_all("X-MAINTNOTE-OBJECT-ID")
    assert [p.text() for p in ids] == ["CIRCUIT/1", "CIRCUIT-2"]
    assert ids[0].params["ALTREP"] == "https://x/y:z"
    start = ical.parse_datetime(event.get("DTSTART"), cal)
    assert start == dt.datetime(2026, 10, 3, 1, tzinfo=ZoneInfo("Europe/London"))
    end = ical.parse_datetime(event.get("DTEND"), cal)  # unknown TZID -> VTIMEZONE offset
    assert end.utcoffset() == dt.timedelta(hours=2)
    assert ical.parse_datetime(event.get("DTSTAMP")) == dt.datetime(2026, 9, 26, 10, tzinfo=UTC)


def test_windows_zone_names_resolve():
    prop = ical.Prop("DTSTART", {"TZID": "GMT Standard Time"}, "20261003T010000")
    assert ical.parse_datetime(prop).utcoffset() == dt.timedelta(hours=1)


def test_fold_and_escape_round_trip():
    line = "DESCRIPTION:" + ical.escape_text("x" * 200 + ", done; ok\nnext")
    folded = ical.fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    [cal] = ical.parse_ical("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + folded + "\r\nEND:VEVENT\r\nEND:VCALENDAR")
    assert next(cal.walk("VEVENT")).text("DESCRIPTION") == "x" * 200 + ", done; ok\nnext"


def test_html_tables_become_label_value_lines():
    html = """<html><head><style>td{color:red}</style></head><body><p>Hello&nbsp;there</p>
    <table><tr><td>Start Time</td><td>2026-10-03 01:00 UTC</td></tr>
    <tr><th>Circuit</th><th>Impact</th><th>Site</th></tr><tr><td>C-1</td><td>Outage</td><td>LD5</td></tr>
    <tr><td><table><tr><td>nested</td><td>value</td></tr></table></td><td>outer</td></tr></table>
    <script>ignore()</script></body></html>"""
    text = html_to_text(html)
    assert "Hello there" in text
    assert "Start Time: 2026-10-03 01:00 UTC" in text
    assert "C-1 | Outage | LD5" in text
    assert "nested: value" in text
    assert "ignore()" not in text and "color:red" not in text


def test_multipart_with_calendar_and_forwarded_message():
    inner = (
        "From: Zayo <mr@zayo.com>\r\nSubject: inner\r\nContent-Type: text/plain\r\n\r\nCircuit: X\r\n"
    )
    raw = (
        "From: Alice <alice@example.com>\r\nTo: noc@example.com\r\nSubject: Fwd: Zayo maintenance\r\n"
        "Date: Sat, 26 Sep 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=b1\r\n\r\n"
        "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nsee below\r\n"
        "--b1\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        "--b1\r\nContent-Type: message/rfc822\r\n\r\n" + inner + "\r\n"
        "--b1\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"a.pdf\"\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--b1--\r\n"
    ).encode()
    m = parse_mail(raw)
    assert m.sender == "alice@example.com"
    assert m.original_sender == "mr@zayo.com"
    assert m.senders == ["alice@example.com", "mr@zayo.com"]
    assert len(m.calendars) == 1
    assert m.attachments == [{"filename": "a.pdf", "content_type": "application/pdf", "size": 5}]
    assert m.date == dt.datetime(2026, 9, 26, 10, tzinfo=UTC)


def test_inline_forward_detection_and_html_only_body():
    raw = (
        b"From: Bob <bob@example.com>\r\nSubject: FW: planned works\r\nContent-Type: text/html\r\n\r\n"
        b"<p>FYI</p><p>-----Original Message-----<br>From: Colt Planned Works [mailto:PlannedWorks@colt.net]"
        b"<br>Subject: planned works</p><table><tr><td>Reference</td><td>CRQ1</td></tr></table>"
    )
    m = parse_mail(raw)
    assert m.original_sender == "plannedworks@colt.net"
    assert "Reference: CRQ1" in m.text


def test_organisation_names():
    assert organisation_from_domain("notifications.lumen.com") == "Lumen"
    assert organisation_from_domain("bt.co.uk") == "BT"
    assert organisation_from_domain("mail.gtt.net") == "GTT"
