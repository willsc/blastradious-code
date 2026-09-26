import datetime as dt
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import pytest
from conftest import NOW, demo_mail
from moto import mock_aws

from blastradius import notices
from blastradius.config import SmtpConfig, load_config
from blastradius.context import build_context
from blastradius.demo.aws_raw import aws_raw
from blastradius.notify import channels, subscriptions
from blastradius.notify.render import render_notice
from blastradius.topology.inventory import load_inventory
from blastradius.util import DEGRADED, OUTAGE, iso, loads, utcnow
from blastradius.worker import Worker


class Receiver:
    """Local HTTP endpoint recording POSTs; `fail` makes it answer 500."""

    def __init__(self):
        self.requests: list[dict] = []
        self.fail = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(500 if outer.fail else 200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


@pytest.fixture
def nctx(cfg_overrides, receiver):
    overrides = cfg_overrides(secret_key="s3cret", notifications={"allow_private_targets": True, "reminders": ["1h"]})
    overrides["subscriptions"] = [
        {"name": "payments", "filter": {"applications": ["payments-api"], "min_severity": "DEGRADED"},
         "channels": [{"type": "webhook", "url": receiver.url + "/hook", "secret": "hooksecret"},
                      {"type": "slack", "url": receiver.url + "/slack"}]},
        {"name": "noc", "filter": {"include_unmapped": True}, "events": ["new", "cancelled"],
         "channels": [{"type": "webhook", "url": receiver.url + "/noc"}]},
    ]
    ctx = build_context(load_config(None, overrides))
    ctx.store.refresh(aws_raw=aws_raw(iso(NOW)))
    subscriptions.sync_managed(ctx.db, ctx.cfg, load_inventory(ctx.cfg.path(ctx.cfg.inventory)))
    return ctx


def paths(receiver):
    return [r["path"] for r in receiver.requests]


def test_webhook_and_slack_delivery_signed_and_deduplicated(nctx, receiver):
    result = nctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t")
    assert sorted(paths(receiver)) == ["/hook", "/noc", "/slack"]
    hook = next(r for r in receiver.requests if r["path"] == "/hook")
    expected = "sha256=" + hmac.new(b"hooksecret", hook["body"], hashlib.sha256).hexdigest()
    assert hook["headers"]["X-BlastRadius-Signature"] == expected
    assert hook["headers"]["X-BlastRadius-Event"] == "new"
    body = json.loads(hook["body"])
    assert body["severity"] == "OUTAGE"
    assert {a["name"] for a in body["applications"]} == {"payments-api", "reporting"}
    slack = json.loads(next(r for r in receiver.requests if r["path"] == "/slack")["body"])
    assert slack["text"].startswith("[OUTAGE] New notice: BT PEW-7788123")
    assert slack["blocks"][0]["type"] == "header"
    # same event again (e.g. worker restart replaying) must not re-send
    assert nctx.notifier.notice_event(result.notice_ids[0], "new") == 0
    assert len(receiver.requests) == 3


def test_filters_respect_severity_and_unmapped(nctx, receiver):
    nctx.pipeline.ingest(demo_mail("07-gtt-unmapped.eml", NOW), source="t")
    assert paths(receiver) == ["/noc"]  # only the include_unmapped subscription hears about it
    nctx.pipeline.ingest(demo_mail("10-gtt-cancelled.eml", NOW), source="t")
    assert paths(receiver) == ["/noc", "/noc"]
    receiver.requests.clear()
    nctx.pipeline.ingest(demo_mail("04-lumen-lag-member.eml", NOW), source="t")  # data-warehouse only
    assert paths(receiver) == ["/noc"]


def test_reschedule_notifies_with_change_and_collision_event(nctx, receiver):
    nctx.pipeline.ingest(demo_mail("01-zayo-bcop.eml", NOW), source="t")
    nctx.pipeline.ingest(demo_mail("02-colt-html.eml", NOW), source="t")
    receiver.requests.clear()
    nctx.pipeline.ingest(demo_mail("06-zayo-bcop-reschedule.eml", NOW), source="t")
    hooks = [json.loads(r["body"]) for r in receiver.requests if r["path"] == "/hook"]
    assert [h["event"] for h in hooks] == ["rescheduled", "collision"]
    assert set(hooks[0]["changes"]) == {"start_at", "end_at"}
    assert hooks[1]["collision"]["severity"] == OUTAGE


def test_failed_delivery_is_retried(nctx, receiver):
    receiver.fail = True
    nctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t")
    rows = nctx.db.query("SELECT * FROM deliveries ORDER BY id")
    assert {r["status"] for r in rows} == {"retry"} and all(r["attempts"] == 1 for r in rows)
    receiver.fail = False
    receiver.requests.clear()
    assert nctx.notifier.retry_due() == 0  # not due yet
    nctx.db.execute("UPDATE deliveries SET next_attempt_at = ?", (iso(utcnow() - dt.timedelta(seconds=1)),))
    assert nctx.notifier.retry_due() == 3
    assert {r["status"] for r in nctx.db.query("SELECT status FROM deliveries")} == {"sent"}
    assert sorted(paths(receiver)) == ["/hook", "/noc", "/slack"]


def test_reminders_once_per_threshold_and_again_after_reschedule(nctx, receiver):
    result = nctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t")
    notice_id = result.notice_ids[0]
    start = notices.get(nctx.db, notice_id)["start_at"]
    worker = Worker(nctx)
    receiver.requests.clear()
    soon = dt.datetime.fromisoformat(start.replace("Z", "+00:00")) - dt.timedelta(minutes=30)
    assert worker.reminders(soon) == 2  # webhook + slack for the payments subscription
    assert worker.reminders(soon + dt.timedelta(minutes=5)) == 0
    assert worker.reminders(soon - dt.timedelta(hours=2)) == 0  # outside the 1h lead
    nctx.db.execute("UPDATE notices SET start_at = ? WHERE id = ?",
                    (iso(dt.datetime.fromisoformat(start.replace("Z", "+00:00")) + dt.timedelta(hours=1)), notice_id))
    assert worker.reminders(soon + dt.timedelta(hours=1)) == 2
    reminder = json.loads(next(r for r in receiver.requests if r["path"] == "/hook")["body"])
    assert reminder["event"] == "reminder"


def test_ssrf_guard(cfg_overrides):
    cfg = load_config(None, cfg_overrides())
    for url in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:9/x", "http://10.1.2.3/hook",
                "file:///etc/passwd"):
        with pytest.raises(channels.DeliveryError):
            channels.check_target(url, cfg)
    allow = load_config(None, cfg_overrides(notifications={"webhook_allow": ["hooks.slack.com"]}))
    with pytest.raises(channels.DeliveryError, match="webhook_allow"):
        channels.check_target("https://evil.example.com/x", allow)


def test_email_channel(nctx, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(channels.smtplib, "SMTP", FakeSMTP)
    nctx.cfg.notifications.smtp = SmtpConfig(host="smtp.example.com", sender="blastradius@example.com")
    result = nctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t", notify=False)
    row = notices.get(nctx.db, result.notice_ids[0])
    rendered = render_notice(nctx.cfg, notices.to_dict(row), loads(row["blast"]), "new")
    channels.send({"type": "email", "to": ["oncall@example.com"]}, rendered, nctx.cfg, "k")
    [msg] = sent
    assert msg["To"] == "oncall@example.com" and msg["Subject"].startswith("[OUTAGE]")
    assert msg.get_body(("html",)) is not None and "payments-api" in msg.get_body(("plain",)).get_content()


@mock_aws
def test_sns_channel_with_filterable_attributes(nctx, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    sns = boto3.client("sns", region_name="eu-west-2")
    sqs = boto3.client("sqs", region_name="eu-west-2")
    topic = sns.create_topic(Name="maintenance")["TopicArn"]
    queue_url = sqs.create_queue(QueueName="payments")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn,
                  Attributes={"FilterPolicy": json.dumps({"applications": ["payments-api"]})})
    result = nctx.pipeline.ingest(demo_mail("03-bt-leased-line.eml", NOW), source="t", notify=False)
    row = notices.get(nctx.db, result.notice_ids[0])
    rendered = render_notice(nctx.cfg, notices.to_dict(row), loads(row["blast"]), "new")
    assert channels.send({"type": "sns", "topic_arn": topic}, rendered, nctx.cfg, "k").startswith("MessageId")
    [message] = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)["Messages"]
    envelope = json.loads(message["Body"])
    assert json.loads(envelope["Message"])["severity"] == "OUTAGE"
    assert json.loads(envelope["MessageAttributes"]["applications"]["Value"]) == ["payments-api", "reporting"]


def test_subscription_validation_and_matching():
    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.validate({"name": "x", "filter": {"colour": "red"}})
    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.validate({"name": "x", "channels": [{"type": "slack", "url": "ftp://x"}]})
    sub = subscriptions.validate({"name": "x", "filter": {"applications": "a, b", "min_severity": "DEGRADED"}})
    assert sub["filter"]["applications"] == ["a", "b"] and sub["events"] == subscriptions.DEFAULT_EVENTS
    blast = {"mapped": True, "apps": [{"name": "a", "owners": ["team-x"]}],
             "nodes": [{"id": "vpc-1", "key": "vpc:vpc-1", "type": "vpc", "account": "111", "account_name": "prod",
                        "region": "eu-west-2"}]}
    notice = {"severity": DEGRADED, "provider": "Zayo", "circuits": [{"id": "OGYX/1//ZYO"}], "message_type": "m"}
    assert subscriptions.matches(sub["filter"], notice, blast)
    assert not subscriptions.matches(sub["filter"] | {"min_severity": "OUTAGE"}, notice, blast)
    assert subscriptions.matches({"owners": ["team-x"], "accounts": ["prod"], "regions": ["eu-west-2"],
                                  "resources": ["vpc-1"], "circuits": ["ogyx 1 zyo"], "providers": ["zayo"]},
                                 notice, blast)
    assert not subscriptions.matches({"providers": ["Colt"]}, notice, blast)
    assert not subscriptions.matches({}, notice | {"severity": 0}, {"mapped": False})
    assert subscriptions.matches({"include_unmapped": True}, notice, {"mapped": False})
