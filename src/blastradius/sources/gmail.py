"""Gmail API source (REST via google-auth's AuthorizedSession).

Auth: OAuth installed-app flow (personal Gmail or Workspace; `blastradius gmail-auth`) or a
Workspace service account with domain-wide delegation impersonating a shared NOC mailbox.
Polling uses each message type's Gmail search query plus an `after:` watermark; messages are
deduplicated by Gmail id in the database, so overlapping windows are harmless.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

from ..config import Config
from ..db import Database
from ..util import UTC, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/{user}"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"


class GmailError(RuntimeError):
    pass


def scopes(cfg: Config) -> list[str]:
    return [SCOPE_MODIFY] if cfg.gmail.label_processed else [SCOPE_READONLY]


def credentials(cfg: Config):
    g = cfg.gmail
    if g.auth == "service_account":
        from google.oauth2 import service_account

        path = cfg.path(g.service_account_file)
        if not path or not path.exists():
            raise GmailError(f"service account key not found: {path}")
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=scopes(cfg))
        return creds.with_subject(g.delegated_user) if g.delegated_user else creds
    from google.oauth2.credentials import Credentials

    token = cfg.path(g.token_file)
    if not token or not token.exists():
        raise GmailError(f"no OAuth token at {token}; run `blastradius gmail-auth` first")
    return Credentials.from_authorized_user_file(str(token), scopes(cfg))


def authorize(cfg: Config, port: int = 8765, open_browser: bool = False) -> Path:
    """Run the OAuth consent flow and store a refresh token. On a headless server, forward the
    port first: ssh -L 8765:localhost:8765 <server>."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_path = cfg.path(cfg.gmail.client_secrets)
    if not secrets_path or not secrets_path.exists():
        raise GmailError("gmail.client_secrets must point at the OAuth client JSON (Desktop app) from Google Cloud")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes(cfg))
    creds = flow.run_local_server(
        host="localhost", port=port, open_browser=open_browser,
        authorization_prompt_message=f"Open this URL in a browser (forward port {port} if remote):\n{{url}}\n",
        success_message="blastradius is authorised; you can close this tab.",
    )
    token = cfg.path(cfg.gmail.token_file)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    os.chmod(token, 0o600)
    return token


class GmailSource:
    def __init__(self, cfg: Config, db: Database, session=None):
        self.cfg = cfg
        self.db = db
        self._session = session
        self._label_id: str | None = None

    def session(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession

            self._session = AuthorizedSession(credentials(self.cfg))
        return self._session

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = API.format(user=quote(self.cfg.gmail.user_id, safe="@")) + path
        delay = 1.0
        for _ in range(6):
            resp = self.session().request(method, url, timeout=30, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning("gmail %s %s -> %s, retrying in %.0fs", method, path, resp.status_code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code == 403 and "insufficient" in resp.text.lower():
                raise GmailError(f"token lacks the required scope ({', '.join(scopes(self.cfg))}); re-run gmail-auth")
            if resp.status_code >= 400:
                raise GmailError(f"gmail {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise GmailError(f"gmail {method} {path}: gave up after retries")

    def list_ids(self, query: str, limit: int) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        token = None
        while len(out) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(out))}
            if token:
                params["pageToken"] = token
            data = self._request("GET", "/messages", params=params)
            out += [(m["id"], m.get("threadId")) for m in data.get("messages", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def fetch_raw(self, message_id: str) -> tuple[bytes, dict]:
        data = self._request("GET", f"/messages/{message_id}", params={"format": "raw"})
        raw = data["raw"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), {
            "thread_id": data.get("threadId"),
            "labels": data.get("labelIds", []),
            "internal_date": int(data.get("internalDate") or 0),
        }

    def query_for(self, base_query: str, since: dt.datetime | None) -> str:
        if since:
            return f"({base_query}) after:{int(since.timestamp())}"
        return f"({base_query}) newer_than:{self.cfg.gmail.backfill_days}d"

    def _ensure_label(self) -> str:
        if self._label_id:
            return self._label_id
        name = self.cfg.gmail.label_processed
        for label in self._request("GET", "/labels").get("labels", []):
            if label.get("name") == name:
                self._label_id = label["id"]
                return self._label_id
        created = self._request("POST", "/labels", json={"name": name, "labelListVisibility": "labelShow",
                                                         "messageListVisibility": "show"})
        self._label_id = created["id"]
        return self._label_id

    def label(self, message_id: str) -> None:
        self._request("POST", f"/messages/{message_id}/modify", json={"addLabelIds": [self._ensure_label()]})

    def poll(self, pipeline) -> dict:
        started = utcnow()
        last = parse_iso(self.db.kv_get("gmail.last_poll"))
        since = last - dt.timedelta(hours=self.cfg.gmail.overlap_hours) if last else None
        stats = {"listed": 0, "new": 0, "processed": 0, "ignored": 0, "duplicate": 0, "error": 0}
        pending: dict[str, tuple[str | None, str]] = {}
        for mtype in self.cfg.message_types:
            if not mtype.gmail_query:
                continue
            ids = self.list_ids(self.query_for(mtype.gmail_query, since), self.cfg.gmail.max_messages_per_poll)
            stats["listed"] += len(ids)
            for message_id, thread_id in ids:
                if message_id in pending:
                    continue
                if self.db.one("SELECT 1 FROM messages WHERE source = 'gmail' AND source_id = ?", (message_id,)):
                    continue
                pending[message_id] = (thread_id, mtype.name)
        fetched = []
        for message_id, (thread_id, type_name) in pending.items():
            raw, meta = self.fetch_raw(message_id)
            fetched.append((meta["internal_date"], message_id, thread_id or meta["thread_id"], type_name, raw))
        for internal_date, message_id, thread_id, type_name, raw in sorted(fetched):
            received = dt.datetime.fromtimestamp(internal_date / 1000, UTC) if internal_date else None
            result = pipeline.ingest(raw, source="gmail", source_id=message_id, thread_id=thread_id,
                                     received_at=received, preferred_type=type_name)
            stats["new"] += 1
            stats[result.status] = stats.get(result.status, 0) + 1
            if self.cfg.gmail.label_processed and result.status == "processed":
                try:
                    self.label(message_id)
                except GmailError as exc:
                    log.warning("could not label %s: %s", message_id, exc)
        self.db.kv_set("gmail.last_poll", iso(started))
        self.db.kv_set("gmail.last_stats", stats | {"at": iso(utcnow())})
        if stats["new"]:
            log.info("gmail poll: %s", stats)
        return stats
