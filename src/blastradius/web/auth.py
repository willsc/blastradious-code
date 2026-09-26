"""Identity: none (single-user/dev), trusted proxy header (oauth2-proxy, IAP, ...), bearer API tokens."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from ..config import Config


@dataclass
class User:
    name: str
    admin: bool
    kind: str  # anonymous | header | token


def current_user(request: Request, cfg: Config) -> User:
    auth = cfg.web.auth
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        for name, expected in auth.api_tokens.items():
            if expected and hmac.compare_digest(str(expected), token):
                return User(f"token:{name}", True, "token")
        raise HTTPException(status_code=401, detail="invalid API token")
    if auth.mode == "none":
        return User("local", True, "anonymous")
    identity = request.headers.get(auth.header, "").strip().lower()
    if not identity:
        raise HTTPException(status_code=401, detail=f"missing identity header {auth.header}")
    return User(identity, identity in {a.lower() for a in auth.admins}, "header")


def check_origin(request: Request, user: User, cfg: Config) -> None:
    """CSRF guard for browser form posts: the Origin/Referer must be this site."""
    if user.kind == "token" or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        if cfg.web.auth.mode == "none":
            return  # curl / scripts against a local, unauthenticated instance
        raise HTTPException(status_code=403, detail="missing Origin header")
    allowed = {request.headers.get("host", ""), urlparse(cfg.base_url).netloc}
    if urlparse(source).netloc not in allowed:
        raise HTTPException(status_code=403, detail="cross-origin request refused")
