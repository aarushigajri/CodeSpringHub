"""How the portal knows who is using it. This is the ONLY module that changes for HTTPS + SSO.

Every request carries `Authorization: Bearer <token>`. An auth provider turns the token
into a CSHL username, or refuses. Everything downstream just receives `user`.

Providers
---------
local  (now)   The portal runs on the cluster as you. At start-up it creates a secret link;
               only someone holding that link can use the portal, so other users on the
               same node can't use it as you even though they can reach localhost ports.
               Claude's open_portal tool hands out one-time tickets instead of that link, so
               the long-lived secret never enters a chat (see issue_ticket / redeem).
oidc   (later) CSHL single sign-on. The browser logs in at CSHL's identity provider, the
               token is verified against the provider's public keys, and the username comes
               from a claim (e.g. preferred_username). The same token check protects the
               HTTPS MCP endpoint that Claude Enterprise connects to.

Choose with CODESPRINGHUB_AUTH=local|oidc (default local).
"""

import getpass
import hmac
import os
import re
import secrets
import time

from . import config


class LocalLinkAuth:
    name = "local"

    def __init__(self, new_link: bool = False):
        self.user = getpass.getuser()
        # Each person's link lives in their own home folder (700/600): the Hub folder is
        # shared by BSR, and one person's link must never be readable by another.
        path = config.USER_DIR / "portal-token"
        if new_link or not path.exists():
            config.USER_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(config.USER_DIR, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(32))
            os.chmod(path, 0o600)
        self.token = path.read_text().strip()

    def user_for(self, token: str | None) -> str | None:
        if token and hmac.compare_digest(token, self.token):
            return self.user
        return None

    def redeem(self, ticket: str) -> str | None:
        """Swap a one-time ticket for the portal secret (kept by the browser, never by Claude)."""
        return self.token if redeem_ticket(ticket) else None


# ---- one-time tickets ---------------------------------------------------------------------
# A ticket is a random name for a file in the person's own ~/.codespringhub/portal-tickets/
# (700/600) holding its expiry time. Redeeming deletes the file, so each ticket works once.

TICKET = re.compile(r"[A-Za-z0-9_-]{43}")


def _ticket_dir():
    d = config.USER_DIR / "portal-tickets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(config.USER_DIR, 0o700)
    os.chmod(d, 0o700)
    return d


def issue_ticket(ttl_seconds: int | None = None) -> str:
    d = _ticket_dir()
    now = time.time()
    for old in d.iterdir():  # tidy up tickets nobody used
        try:
            if float(old.read_text()) < now:
                old.unlink()
        except (OSError, ValueError):
            pass
    ticket = secrets.token_urlsafe(32)
    fd = os.open(d / ticket, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(now + (ttl_seconds or config.OPEN_LINK_TTL_SECONDS)))
    return ticket


def redeem_ticket(ticket: str | None) -> bool:
    if not ticket or not TICKET.fullmatch(ticket):
        return False
    path = _ticket_dir() / ticket
    try:
        expires = float(path.read_text())
        path.unlink()  # whoever deletes it first wins; a second use finds nothing
    except (OSError, ValueError):
        return False
    return expires >= time.time()


class OIDCAuth:
    """Placeholder for CSHL SSO (Phase 2). Needs from IT: issuer URL, client ID, the claim
    that holds the CSHL username, and the redirect URL registered for the portal."""

    name = "oidc"

    def __init__(self):
        raise NotImplementedError("SSO login is planned for Phase 2; use CODESPRINGHUB_AUTH=local for now.")

    def user_for(self, token: str | None) -> str | None:  # pragma: no cover
        raise NotImplementedError


def get_auth(new_link: bool = False):
    kind = os.environ.get("CODESPRINGHUB_AUTH", "local")
    if kind == "local":
        return LocalLinkAuth(new_link)
    if kind == "oidc":
        return OIDCAuth()
    raise ValueError(f"Unknown CODESPRINGHUB_AUTH '{kind}'")
