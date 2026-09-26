"""Configuration: YAML file -> typed dataclasses, with ${ENV} interpolation.

Every section has defaults so `blastradius demo` runs without any file. Unknown keys
are reported (a typo in `circuit_tag_keys` should not silently disable correlation).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import parse_duration

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            env = os.environ.get(m.group(1))
            if env is None:
                if m.group(2) is not None:
                    return m.group(2)
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return env
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def _build(cls: type, data: Any, where: str) -> Any:
    """Instantiate dataclass `cls` from a mapping, recursing into nested dataclasses."""
    if data is None:
        return cls()
    if isinstance(data, cls):
        return data
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    known = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            log.warning("config %s: unknown key %r ignored", where, key)
            continue
        hook = getattr(cls, f"_coerce_{key}", None)
        kwargs[key] = hook(value, f"{where}.{key}") if hook else value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@dataclass
class MatchRule:
    """All given conditions must hold. `senders` entries are case-insensitive substrings
    of the sender address, or regexes when prefixed with 're:'."""

    senders: list[str] = field(default_factory=list)
    subject: str | None = None
    body: str | None = None
    not_subject: str | None = None
    has_calendar: bool | None = None


@dataclass
class MessageType:
    name: str = "unnamed"
    description: str = ""
    kind: str = "maintenance"  # maintenance | outage | advisory
    gmail_query: str | None = None
    match: MatchRule = field(default_factory=MatchRule)
    default_impact: str = "OUTAGE"
    parsers: list[str] = field(default_factory=lambda: ["bcop", "ntc", "aws_health", "generic"])

    @staticmethod
    def _coerce_match(value: Any, where: str) -> MatchRule:
        return _build(MatchRule, value, where)


@dataclass
class ProviderHint:
    name: str = ""
    senders: list[str] = field(default_factory=list)
    circuit_patterns: list[str] = field(default_factory=list)
    maintenance_id_patterns: list[str] = field(default_factory=list)
    dayfirst: bool = False
    timezone: str = "UTC"


@dataclass
class GmailConfig:
    enabled: bool = False
    auth: str = "oauth"  # oauth | service_account
    client_secrets: str | None = None
    token_file: str = "gmail-token.json"
    service_account_file: str | None = None
    delegated_user: str | None = None
    user_id: str = "me"
    poll_interval: int = 60
    backfill_days: int = 14
    overlap_hours: int = 48
    max_messages_per_poll: int = 200
    label_processed: str | None = None  # needs the gmail.modify scope

    _coerce_poll_interval = staticmethod(lambda v, w: parse_duration(v))


@dataclass
class DirectoryConfig:
    enabled: bool = False
    path: str = "inbox"
    poll_interval: int = 10

    _coerce_poll_interval = staticmethod(lambda v, w: parse_duration(v))


@dataclass
class AwsAccount:
    name: str = "default"
    role_arn: str | None = None
    external_id: str | None = None
    profile: str | None = None
    regions: list[str] | None = None


@dataclass
class AwsConfig:
    enabled: bool = False
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    accounts: list[AwsAccount] = field(default_factory=lambda: [AwsAccount()])
    refresh_interval: int = 6 * 3600
    app_tag_keys: list[str] = field(
        default_factory=lambda: ["Application", "application", "App", "app", "Service", "service"]
    )
    owner_tag_keys: list[str] = field(default_factory=lambda: ["Owner", "owner", "Team", "team"])
    circuit_tag_keys: list[str] = field(
        default_factory=lambda: ["CircuitId", "CircuitID", "circuit_id", "circuit-id", "Circuit", "circuit"]
    )
    provider_tag_keys: list[str] = field(default_factory=lambda: ["Carrier", "carrier", "Provider", "provider"])
    site_tag_keys: list[str] = field(default_factory=lambda: ["Site", "site"])
    collect_instances: bool = True
    collect_enis: bool = True

    _coerce_refresh_interval = staticmethod(lambda v, w: parse_duration(v))

    @staticmethod
    def _coerce_accounts(value: Any, where: str) -> list[AwsAccount]:
        return [_build(AwsAccount, v, f"{where}[{i}]") for i, v in enumerate(value or [])]


@dataclass
class SmtpConfig:
    host: str = "localhost"
    port: int = 587
    starttls: bool = True
    ssl: bool = False
    username: str | None = None
    password: str | None = None
    sender: str = "blastradius@localhost"
    timeout: int = 20


@dataclass
class NotificationsConfig:
    reminders: list[int] = field(default_factory=lambda: [86400, 3600])
    notify_on_start: bool = False
    default_min_severity: str = "AT_RISK"
    smtp: SmtpConfig | None = None
    # Host patterns webhooks may target (fnmatch). Empty = any public host.
    webhook_allow: list[str] = field(default_factory=list)
    allow_private_targets: bool = False
    timeout: int = 10
    max_attempts: int = 5

    @staticmethod
    def _coerce_reminders(value: Any, where: str) -> list[int]:
        return sorted({parse_duration(v) for v in value or []}, reverse=True)

    @staticmethod
    def _coerce_smtp(value: Any, where: str) -> SmtpConfig | None:
        return _build(SmtpConfig, value, where) if value else None


@dataclass
class AuthConfig:
    mode: str = "none"  # none | header
    header: str = "X-Forwarded-Email"
    admins: list[str] = field(default_factory=list)
    api_tokens: dict[str, str] = field(default_factory=dict)  # name -> token


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8470
    auth: AuthConfig = field(default_factory=AuthConfig)

    @staticmethod
    def _coerce_auth(value: Any, where: str) -> AuthConfig:
        return _build(AuthConfig, value, where)


@dataclass
class CorrelationConfig:
    min_identifier_length: int = 4
    min_name_length: int = 8
    match_names: bool = True
    collision_lookahead_days: int = 60
    open_outage_hours: int = 72
    recent_days: int = 7  # how long finished notices stay "recent" / re-correlated


@dataclass
class Config:
    database: str = "blastradius.db"
    base_url: str = "http://localhost:8470"
    display_timezone: str = "UTC"
    secret_key: str | None = None
    inventory: str | None = None
    web: WebConfig = field(default_factory=WebConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    directory: DirectoryConfig = field(default_factory=DirectoryConfig)
    aws: AwsConfig = field(default_factory=AwsConfig)
    message_types: list[MessageType] = field(default_factory=list)
    providers: list[ProviderHint] = field(default_factory=list)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    subscriptions: list[dict] = field(default_factory=list)
    config_dir: Path = field(default_factory=Path.cwd)

    _coerce_web = staticmethod(lambda v, w: _build(WebConfig, v, w))
    _coerce_gmail = staticmethod(lambda v, w: _build(GmailConfig, v, w))
    _coerce_directory = staticmethod(lambda v, w: _build(DirectoryConfig, v, w))
    _coerce_aws = staticmethod(lambda v, w: _build(AwsConfig, v, w))
    _coerce_notifications = staticmethod(lambda v, w: _build(NotificationsConfig, v, w))
    _coerce_correlation = staticmethod(lambda v, w: _build(CorrelationConfig, v, w))

    @staticmethod
    def _coerce_message_types(value: Any, where: str) -> list[MessageType]:
        return [_build(MessageType, v, f"{where}[{i}]") for i, v in enumerate(value or [])]

    @staticmethod
    def _coerce_providers(value: Any, where: str) -> list[ProviderHint]:
        return [_build(ProviderHint, v, f"{where}[{i}]") for i, v in enumerate(value or [])]

    def path(self, value: str | None) -> Path | None:
        """Resolve a path from the config relative to the config file's directory."""
        if not value:
            return None
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.config_dir / p)


DEFAULT_MESSAGE_TYPES: list[dict] = [
    {
        "name": "aws-health",
        "description": "AWS Health scheduled changes and issues (Direct Connect, VPN, EC2 retirement, ...)",
        "kind": "maintenance",
        "gmail_query": "from:(aws.com OR amazon.com OR amazonaws.com)",
        "match": {
            "senders": ["@aws.com", "@amazon.com", "@amazonaws.com"],
            "body": r"(?i)AWS Health|event type code|arn:aws:health|scheduled (maintenance|change)|"
                    r"AWS_[A-Z]+_[A-Z_]+",
        },
        "parsers": ["aws_health", "generic"],
        "default_impact": "OUTAGE",
    },
    {
        "name": "carrier-maintenance",
        "description": "Planned work notices from carriers / colos (BCOP iCalendar or free text)",
        "kind": "maintenance",
        "gmail_query": 'subject:(maintenance OR "planned work" OR "planned engineering" OR "scheduled work")',
        "match": {
            "subject": r"(?i)maint|planned (work|engineering|outage|event|activity)|scheduled (work|outage|activity)"
                       r"|change (notification|request)|network work|reschedul|cancel",
            "not_subject": r"(?i)newsletter|webinar|invoice|survey|unsubscribe",
        },
        "parsers": ["bcop", "ntc", "generic"],
        "default_impact": "OUTAGE",
    },
    {
        "name": "carrier-outage",
        "description": "Unplanned incident / fault notifications",
        "kind": "outage",
        "gmail_query": 'subject:(outage OR incident OR "service disruption" OR fault)',
        "match": {
            "subject": r"(?i)outage|incident|disruption|fault|service (issue|degradation)|unplanned|restored|resolved",
            "not_subject": r"(?i)planned|maintenance|newsletter|webinar",
        },
        "parsers": ["generic"],
        "default_impact": "OUTAGE",
    },
]


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    data: dict = {}
    config_dir = Path.cwd()
    if path:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
        config_dir = p.parent
    if overrides:
        data = _deep_merge(data, overrides)
    data = _interpolate(data)
    cfg: Config = _build(Config, data, "config")
    cfg.config_dir = config_dir
    if not cfg.message_types:
        cfg.message_types = Config._coerce_message_types(DEFAULT_MESSAGE_TYPES, "defaults")
    validate(cfg)
    return cfg


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(cfg: Config) -> None:
    known_parsers = {"bcop", "ntc", "aws_health", "generic"}
    names = set()
    for mt in cfg.message_types:
        if mt.name in names:
            raise ConfigError(f"duplicate message type {mt.name!r}")
        names.add(mt.name)
        unknown = set(mt.parsers) - known_parsers
        if unknown:
            raise ConfigError(f"message type {mt.name}: unknown parsers {sorted(unknown)}")
        for label, rx in (("subject", mt.match.subject), ("body", mt.match.body), ("not_subject", mt.match.not_subject)):
            if rx:
                try:
                    re.compile(rx)
                except re.error as exc:
                    raise ConfigError(f"message type {mt.name}: bad {label} regex: {exc}") from exc
        if mt.kind not in {"maintenance", "outage", "advisory"}:
            raise ConfigError(f"message type {mt.name}: kind must be maintenance|outage|advisory")
    for hint in cfg.providers:
        for rx in hint.circuit_patterns + hint.maintenance_id_patterns:
            try:
                re.compile(rx)
            except re.error as exc:
                raise ConfigError(f"provider {hint.name}: bad pattern {rx!r}: {exc}") from exc
    if cfg.web.auth.mode not in {"none", "header"}:
        raise ConfigError("web.auth.mode must be 'none' or 'header'")
    if cfg.gmail.enabled and cfg.gmail.auth not in {"oauth", "service_account"}:
        raise ConfigError("gmail.auth must be 'oauth' or 'service_account'")
    if cfg.gmail.label_processed and cfg.gmail.auth == "oauth":
        log.info("gmail.label_processed set: the OAuth token must include the gmail.modify scope")
