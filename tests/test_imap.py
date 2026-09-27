"""IMAP source against a fake imaplib connection (no network)."""

import imaplib

import pytest
from conftest import NOW, demo_mail

from blastradius.config import ConfigError, load_config
from blastradius.sources.imap import ImapError, ImapSource


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for ImapSource: login, select, UID SEARCH/FETCH/COPY, create."""

    def __init__(self, folders: dict[str, dict[int, bytes]], gmail: bool = False, password: str = "secret"):
        self.folders = folders
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.password = password
        self.literal = None
        self.selected = None
        self.readonly = None
        self.searches: list[tuple] = []
        self.fetched: list[int] = []
        self.copied: list[tuple[int, str]] = []
        self.created: list[str] = []
        self.logged_out = False

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.selected, self.readonly = mailbox.strip('"'), readonly
        return "OK", [str(len(self.folders[self.selected])).encode()]

    def response(self, code):
        return code, [b"7"]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "SEARCH":
            literal, self.literal = self.literal, None
            self.searches.append((self.selected, args, literal.decode() if literal else None))
            return "OK", [" ".join(str(u) for u in sorted(box)).encode()]
        if command == "FETCH" and args[1] == "(X-GM-MSGID X-GM-THRID)":
            uids = [int(u) for u in args[0].split(",")]
            return "OK", [f"{i} (X-GM-THRID {9000 + u} X-GM-MSGID {1000 + u} UID {u})".encode()
                          for i, u in enumerate(uids, 1)]
        if command == "FETCH":
            u = int(args[0])
            self.fetched.append(u)
            raw = box[u]
            meta = f'1 (UID {u} INTERNALDATE "26-Sep-2026 10:00:0{u % 10} +0000" BODY[] {{{len(raw)}}}'.encode()
            return "OK", [(meta, raw), b")"]
        if command == "COPY":
            target = args[1].strip('"')
            if target not in self.created:
                return "NO", [b"[TRYCREATE] No such mailbox"]
            self.copied.append((int(args[0]), target))
            return "OK", [b"copied"]
        raise AssertionError(f"unexpected UID {command}")

    def create(self, mailbox):
        self.created.append(mailbox.strip('"'))
        return "OK", [b"created"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


def _enable(ctx, **extra):
    imap = ctx.cfg.imap
    imap.enabled, imap.username, imap.password = True, "noc@example.com", "secret"
    for key, value in extra.items():
        setattr(imap, key, value)


def test_gmail_server_uses_queries_msgids_and_labels(ctx):
    _enable(ctx, processed_folder="blastradius/processed", password="abcd efgh ijkl mnop")
    fake = FakeIMAP({"INBOX": {
        1: demo_mail("01-zayo-bcop.eml", NOW),
        2: demo_mail("02-colt-html.eml", NOW),
        3: demo_mail("09-zayo-newsletter.eml", NOW),
    }}, gmail=True, password="abcdefghijklmnop")  # spaces in the app password are dropped for Gmail
    source = ImapSource(ctx.cfg, ctx.db, connect=lambda: fake)
    stats = source.poll(ctx.pipeline)

    assert stats["new"] == 3 and stats["processed"] == 2 and stats["ignored"] == 1
    assert fake.readonly and fake.logged_out
    # one X-GM-RAW search per message type with a gmail_query, sent as a literal
    queries = [q for _, args, q in fake.searches]
    assert len(queries) == len([t for t in ctx.cfg.message_types if t.gmail_query])
    assert all(args[-1] == "X-GM-RAW" for _, args, _ in fake.searches)
    assert all("newer_than:14d" in q for q in queries)
    # each message fetched once even though several queries matched it
    assert sorted(fake.fetched) == [1, 2, 3]
    assert fake.created == ["blastradius/processed"]
    assert sorted(u for u, _ in fake.copied) == [1, 2]  # only processed mail is labelled
    rows = ctx.db.query("SELECT source, source_id, thread_id, status FROM messages ORDER BY source_id")
    assert [tuple(r) for r in rows] == [
        ("imap", "1001", "9001", "processed"), ("imap", "1002", "9002", "processed"),
        ("imap", "1003", "9003", "ignored")]

    # second poll: after: watermark, nothing re-fetched
    fake.searches.clear()
    fake.fetched.clear()
    assert source.poll(ctx.pipeline)["new"] == 0
    assert not fake.fetched
    assert all(" after:" in q for _, _, q in fake.searches)


def test_generic_server_since_search_and_cap(ctx):
    _enable(ctx, host="mail.example.com", folders=["INBOX", "Carriers"], search='FROM "zayo.com"',
            max_messages_per_poll=2)
    fake = FakeIMAP({
        "INBOX": {5: demo_mail("01-zayo-bcop.eml", NOW), 6: demo_mail("02-colt-html.eml", NOW)},
        "Carriers": {5: demo_mail("03-bt-leased-line.eml", NOW)},
    })
    source = ImapSource(ctx.cfg, ctx.db, connect=lambda: fake)

    stats = source.poll(ctx.pipeline)
    assert stats["new"] == 2
    _, args, literal = fake.searches[0]
    assert args[0] == "SINCE" and args[2] == 'FROM "zayo.com"' and literal is None
    assert ctx.db.kv_get("imap.last_poll") is None  # capped: the watermark stays put

    stats = source.poll(ctx.pipeline)  # picks up the remaining mail in the second folder
    assert stats["new"] == 1
    assert ctx.db.kv_get("imap.last_poll") is not None
    ids = [r["source_id"] for r in ctx.db.query("SELECT source_id FROM messages ORDER BY id")]
    assert ids == ["INBOX:7:5", "INBOX:7:6", "Carriers:7:5"]


def test_login_failure_explains_app_passwords(ctx):
    _enable(ctx, password="wrong")
    source = ImapSource(ctx.cfg, ctx.db, connect=lambda: FakeIMAP({"INBOX": {}}))
    with pytest.raises(ImapError, match="app password"):
        source.poll(ctx.pipeline)


def test_config_requires_credentials(tmp_path):
    with pytest.raises(ConfigError, match="imap.username and imap.password"):
        load_config(None, {"database": str(tmp_path / "x.db"), "imap": {"enabled": True, "username": "a@b.c"}})
    cfg = load_config(None, {"imap": {"enabled": True, "username": "a@b.c", "password": "p", "poll_interval": "2m"}})
    assert cfg.imap.poll_interval == 120 and cfg.imap.folders == ["INBOX"]
