import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from blastradius.parsing.timewin import extract_windows, parse_range, parse_value

UTC = dt.timezone.utc
REF = dt.datetime(2026, 9, 26, tzinfo=UTC)


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


def first(text, **kw):
    windows = extract_windows(text, reference=REF, **kw)
    assert windows, f"no window found in {text!r}"
    return windows[0].start.astimezone(UTC), windows[0].end.astimezone(UTC) if windows[0].end else None


@pytest.mark.parametrize(
    "text,kw,expected",
    [
        ("Start Date and Time: 2026-10-03 01:00 UTC\nEnd Date and Time: 2026-10-03 05:00 UTC", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Start: 03-Oct-2026 01:00 GMT\nEnd: 03-Oct-2026 05:00 GMT", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Window: 03/10/2026 22:00 - 04/10/2026 04:00 (UTC)", {"dayfirst": True},
         (utc(2026, 10, 3, 22), utc(2026, 10, 4, 4))),
        ("Maintenance Window: October 3, 2026 01:00 - 05:00 UTC", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Date: Saturday, 3 October 2026\nTime: 22:00 - 02:00 BST", {},
         (utc(2026, 10, 3, 21), utc(2026, 10, 4, 1))),
        ("Planned window 2026-10-03T01:00:00Z to 2026-10-03T05:00:00Z", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Start Time: 10/03/2026 01:00 AM EDT\nEnd Time: 10/03/2026 05:00 AM EDT", {},
         (utc(2026, 10, 3, 5), utc(2026, 10, 3, 9))),
        ("Start time: Wed, 07 Oct 2026 06:00:00 GMT\nEnd time: Wed, 07 Oct 2026 10:00:00 GMT", {},
         (utc(2026, 10, 7, 6), utc(2026, 10, 7, 10))),
        ("Planned Start (UTC): 03.10.2026 01:00\nPlanned End (UTC): 03.10.2026 05:00", {"dayfirst": True},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("The work will take place from 01:00 to 05:00 UTC on 3rd October 2026.", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Start Time | 2026-10-03 01:00:00 UTC\nEnd Time | 2026-10-03 05:00:00 UTC", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
        ("Start Date: 03/10/2026\nStart Time: 23:00\nEnd Date: 04/10/2026\nEnd Time: 03:00\nTimezone: UTC",
         {"dayfirst": True}, (utc(2026, 10, 3, 23), utc(2026, 10, 4, 3))),
        ("Start Date/Time:\n   2026-10-03 01:00 UTC\nEnd Date/Time:\n   2026-10-03 03:30 UTC", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 3, 30))),
        ("Scheduled: 2026-10-03 01:00 (UTC+01:00) - 2026-10-03 05:00 (UTC+01:00)", {},
         (utc(2026, 10, 3, 0), utc(2026, 10, 3, 4))),
        ("Window start: 2026-10-03T01:00:00-05:00\nWindow end: 2026-10-03T05:00:00-05:00", {},
         (utc(2026, 10, 3, 6), utc(2026, 10, 3, 10))),
        ("Start: 2026-10-03 01:00\nExpected duration: 4 hours", {},
         (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))),
    ],
)
def test_formats(text, kw, expected):
    assert first(text, **kw) == expected


def test_primary_and_backup_windows_are_ordered():
    text = (
        "Backup Window: 2026-Oct-10 23:00 UTC - 2026-Oct-11 03:00 UTC\n"
        "Primary Window: 2026-Oct-03 23:00 UTC - 2026-Oct-04 03:00 UTC\n"
    )
    windows = extract_windows(text, reference=REF)
    assert [w.backup for w in windows] == [False, True]
    assert windows[0].start == utc(2026, 10, 3, 23)
    assert windows[1].start == utc(2026, 10, 10, 23)


def test_iso_dates_ignore_dayfirst():
    assert parse_value("2026-10-03 01:00", dayfirst=True, reference=REF).value == utc(2026, 10, 3, 1)


def test_gmt_plus_offset_is_not_posix_inverted():
    value = parse_value("03 Oct 2026 01:00 GMT+2", reference=REF).value
    assert value.astimezone(UTC) == utc(2026, 10, 2, 23)


def test_range_crossing_midnight():
    start, end = parse_range("03 Oct 2026 22:00 - 02:00 UTC", reference=REF)
    assert (start, end) == (utc(2026, 10, 3, 22), utc(2026, 10, 4, 2))


def test_timezone_abbreviation_uses_dst_rules():
    value = parse_value("2026-10-03 01:00 CET", reference=REF).value
    assert value.utcoffset() == ZoneInfo("Europe/Paris").utcoffset(dt.datetime(2026, 10, 3, 1))


def test_forwarded_headers_are_not_windows():
    text = (
        "---------- Forwarded message ---------\n"
        "From: Zayo <mr@zayo.com>\nDate: Mon, 28 Sep 2026 10:22:31 +0000\nSubject: maintenance\nTo: noc@x.com\n\n"
        "Start: 2026-10-03 01:00 UTC\nEnd: 2026-10-03 05:00 UTC\n"
    )
    assert first(text) == (utc(2026, 10, 3, 1), utc(2026, 10, 3, 5))


def test_rejects_implausible_windows():
    assert extract_windows("Start: 2026-10-03 05:00 UTC\nEnd: 2026-10-03 01:00 UTC", reference=REF) == []
