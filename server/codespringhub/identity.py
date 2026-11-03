"""How the portal knows who is using it. This is the ONLY module that changes for HTTPS + SSO.

Every request carries `Authorization: Bearer <token>`. An auth provider turns the token
into a CSHL username, or refuses. Everything downstream just receives `user`.

Providers
---------
local  (now)   The portal runs on the cluster as you. At start-up it creates a secret link;
               only someone holding that link can use the portal, so other users on the
               same node can't use it as you even though they can reach localhost ports.
oidc   (later) CSHL single sign-on. The browser logs in at CSHL's identity provider, the
               token is verified against the provider's public keys, and the username comes
               from a claim (e.g. preferred_username). The same token check protects the
               HTTPS MCP endpoint that Claude Enterprise connects to.

Choose with CODESPRINGHUB_AUTH=local|oidc (default local).
"""

import getpass
import hmac
import os
import secrets

from . import config


class LocalLinkAuth:
    name = "local"

    def __init__(self, new_link: bool = False):
        self.user = getpass.getuser()
        path = config.VAR_DIR / "portal-token"
        if new_link or not path.exists():
            config.VAR_DIR.mkdir(parents=True, exist_ok=True)
            # 600: var/ is shared with BSR, but the link must be yours alone.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(32))
            os.chmod(path, 0o600)
        self.token = path.read_text().strip()

    def user_for(self, token: str | None) -> str | None:
        if token and hmac.compare_digest(token, self.token):
            return self.user
        return None


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
