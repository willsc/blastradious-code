"""Channel senders: Slack / Teams / generic webhook (HMAC-signed) / SMTP email / Amazon SNS."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
from email.message import EmailMessage
from urllib.parse import urlparse

import requests

from ..config import Config
from .render import Rendered


class DeliveryError(Exception):
    pass


def check_target(url: str, cfg: Config) -> None:
    """Webhook URLs are user-supplied: refuse internal targets (e.g. the EC2 metadata service)
    unless explicitly allowed, and enforce the optional host allow-list."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DeliveryError(f"unsupported URL {url!r}")
    host = parsed.hostname.lower()
    allow = cfg.notifications.webhook_allow
    if allow and not any(fnmatch.fnmatch(host, pattern.lower()) for pattern in allow):
        raise DeliveryError(f"host {host} is not in notifications.webhook_allow")
    if cfg.notifications.allow_private_targets:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DeliveryError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
                or address.is_multicast or address.is_unspecified):
            raise DeliveryError(f"{host} resolves to non-public address {address}; "
                                "set notifications.allow_private_targets to permit")


def _post(url: str, body: bytes, headers: dict, cfg: Config) -> str:
    check_target(url, cfg)
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=cfg.notifications.timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise DeliveryError(str(exc)) from exc
    if resp.status_code >= 300:
        raise DeliveryError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return f"HTTP {resp.status_code}"


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send(channel: dict, rendered: Rendered, cfg: Config, secret: str) -> str:
    ctype = channel.get("type")
    if ctype == "slack":
        body = json.dumps(rendered.slack).encode()
        return _post(channel["url"], body, {"Content-Type": "application/json"}, cfg)
    if ctype == "teams":
        body = json.dumps(rendered.teams).encode()
        return _post(channel["url"], body, {"Content-Type": "application/json"}, cfg)
    if ctype == "webhook":
        body = json.dumps(rendered.data, default=str).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "blastradius",
                   "X-BlastRadius-Event": str(rendered.data.get("event", "")),
                   "X-BlastRadius-Signature": signature(channel.get("secret") or secret, body)}
        headers.update({str(k): str(v) for k, v in (channel.get("headers") or {}).items()})
        return _post(channel["url"], body, headers, cfg)
    if ctype == "email":
        return _send_email(channel, rendered, cfg)
    if ctype == "sns":
        return _send_sns(channel, rendered)
    raise DeliveryError(f"unknown channel type {ctype!r}")


def _send_email(channel: dict, rendered: Rendered, cfg: Config) -> str:
    smtp_cfg = cfg.notifications.smtp
    if smtp_cfg is None:
        raise DeliveryError("notifications.smtp is not configured")
    msg = EmailMessage()
    msg["Subject"] = rendered.subject
    msg["From"] = smtp_cfg.sender
    msg["To"] = ", ".join(channel["to"])
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    try:
        if smtp_cfg.ssl:
            server = smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, timeout=smtp_cfg.timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=smtp_cfg.timeout)
        with server:
            if smtp_cfg.starttls and not smtp_cfg.ssl:
                server.starttls(context=ssl.create_default_context())
            if smtp_cfg.username:
                server.login(smtp_cfg.username, smtp_cfg.password or "")
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"SMTP: {exc}") from exc
    return f"sent to {len(channel['to'])} recipient(s)"


def _send_sns(channel: dict, rendered: Rendered) -> str:
    import boto3

    arn = channel["topic_arn"]
    region = channel.get("region") or arn.split(":")[3]
    data = rendered.data
    attributes = {"event": {"DataType": "String", "StringValue": str(data.get("event", ""))}}
    if data.get("severity"):
        attributes["severity"] = {"DataType": "String", "StringValue": str(data["severity"])}
    apps = [a["name"] for a in data.get("applications", [])]
    if apps:  # lets SNS subscribers use filter policies on application
        attributes["applications"] = {"DataType": "String.Array", "StringValue": json.dumps(apps)}
    provider = (data.get("notice") or {}).get("provider")
    if provider:
        attributes["provider"] = {"DataType": "String", "StringValue": str(provider)}
    subject = rendered.subject.encode("ascii", "ignore").decode()[:100] or "blastradius"
    try:
        resp = boto3.client("sns", region_name=region).publish(
            TopicArn=arn, Subject=subject, Message=json.dumps(data, default=str), MessageAttributes=attributes)
    except Exception as exc:
        raise DeliveryError(f"SNS: {exc}") from exc
    return f"MessageId {resp.get('MessageId')}"


def describe_target(channel: dict) -> str:
    ctype = channel.get("type")
    if ctype in ("slack", "teams", "webhook"):
        return f"{ctype}:{urlparse(channel.get('url', '')).hostname}"
    if ctype == "email":
        return "email:" + ",".join(channel.get("to", []))
    if ctype == "sns":
        return f"sns:{channel.get('topic_arn')}"
    return str(ctype)
