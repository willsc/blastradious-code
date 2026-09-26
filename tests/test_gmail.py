"""Gmail source against a fake AuthorizedSession (no network)."""

import base64
import json
import re
from urllib.parse import urlparse

from conftest import NOW, demo_mail

from blastradius.sources.gmail import GmailSource


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeGmail:
    """Minimal Gmail REST emulation: messages.list (with paging), messages.get(raw), labels, modify."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.calls: list[tuple[str, str, dict]] = []
        self.labels = [{"id": "INBOX", "name": "INBOX"}]
        self.modified: list[tuple[str, list]] = []
        self.throttle_once = True

    def request(self, method, url, timeout=None, params=None, json=None):
        path = urlparse(url).path.split("/users/me", 1)[1]
        self.calls.append((method, path, params or json or {}))
        if path == "/messages" and method == "GET":
            if self.throttle_once:
                self.throttle_once = False
                return FakeResponse(429)
            ids = sorted(self.messages)
            start = int(params.get("pageToken") or 0)
            page = ids[start: start + 2]
            body = {"messages": [{"id": i, "threadId": "t-" + i} for i in page]}
            if start + 2 < len(ids):
                body["nextPageToken"] = str(start + 2)
            return FakeResponse(200, body)
        m = re.fullmatch(r"/messages/([^/]+)", path)
        if m and method == "GET":
            raw = self.messages[m.group(1)]
            return FakeResponse(200, {"id": m.group(1), "threadId": "t-" + m.group(1), "labelIds": ["INBOX"],
                                      "internalDate": str(1_790_000_000_000 + int(m.group(1)[-1]) * 1000),
                                      "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        if path == "/labels" and method == "GET":
            return FakeResponse(200, {"labels": self.labels})
        if path == "/labels" and method == "POST":
            label = {"id": "Label_1", "name": json["name"]}
            self.labels.append(label)
            return FakeResponse(200, label)
        m = re.fullmatch(r"/messages/([^/]+)/modify", path)
        if m:
            self.modified.append((m.group(1), json["addLabelIds"]))
            return FakeResponse(200, {})
        return FakeResponse(404, {"error": path})


def test_poll_pages_dedupes_and_labels(ctx, monkeypatch):
    monkeypatch.setattr("blastradius.sources.gmail.time.sleep", lambda s: None)
    ctx.cfg.gmail.enabled = True
    ctx.cfg.gmail.label_processed = "blastradius/processed"
    fake = FakeGmail({
        "m1": demo_mail("01-zayo-bcop.eml", NOW),
        "m2": demo_mail("02-colt-html.eml", NOW),
        "m3": demo_mail("09-zayo-newsletter.eml", NOW),
    })
    source = GmailSource(ctx.cfg, ctx.db, session=fake)
    stats = source.poll(ctx.pipeline)
    assert stats["new"] == 3 and stats["processed"] == 2 and stats["ignored"] == 1
    # one list per message type with a query; paging followed; first call retried after a 429
    list_calls = [p for m, path, p in fake.calls if path == "/messages"]
    assert "newer_than:14d" in list_calls[0]["q"]
    assert any(c.get("pageToken") == "2" for c in list_calls)
    assert sorted(m for m, _ in fake.modified) == ["m1", "m2"]  # only processed mails get the label
    assert fake.labels[-1]["name"] == "blastradius/processed"
    rows = ctx.db.query("SELECT source_id, thread_id, status FROM messages ORDER BY source_id")
    assert [(r["source_id"], r["thread_id"], r["status"]) for r in rows] == [
        ("m1", "t-m1", "processed"), ("m2", "t-m2", "processed"), ("m3", "t-m3", "ignored")]

    # second poll: incremental query with an after: watermark, nothing re-fetched
    fake.calls.clear()
    stats = source.poll(ctx.pipeline)
    assert stats["new"] == 0
    assert not any(re.fullmatch(r"/messages/m\d", path) for _, path, _ in fake.calls)
    queries = [p["q"] for m, path, p in fake.calls if path == "/messages"]
    assert queries and all(" after:" in q for q in queries)
