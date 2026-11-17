"""Opening the portal from Claude (the open_portal tool).

The Hub's MCP process runs as the person, on the login node their Claude Desktop SSHs to. It
starts their portal there if it isn't running (detached, so it outlives this chat) and returns a
one-time link. The link carries a ticket, not the portal secret, so the secret never enters the
chat; the portal swaps the ticket for the secret in the browser (identity.redeem_ticket).

The laptop reaches the portal through the -L forward that the Connect page's Claude Desktop setup
adds to the Hub's SSH login. Older setups don't have it, so the reply always includes the tunnel
command as well.
"""

import getpass
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from . import agent, config, editing, identity
from .errors import Forbidden, HubError


def state_file() -> Path:
    """Written by the running portal. Per login node, because home folders are shared."""
    return config.USER_DIR / f"portal-{socket.gethostname().split('.')[0]}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # also fails for other people's processes
        return True
    except (ProcessLookupError, PermissionError):
        return False


def running_portal() -> int | None:
    """This person's portal on this login node, if one is answering: its port."""
    try:
        state = json.loads(state_file().read_text())
        pid, port = int(state["pid"]), int(state["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _alive(pid):
        return None
    with socket.socket() as s:
        s.settimeout(2)
        return port if s.connect_ex(("127.0.0.1", port)) == 0 else None


def start_portal(wait_seconds: int = 180) -> int:
    launcher = config.HUB_HOME / "portal.sh"
    if not launcher.exists():
        raise HubError("The portal isn't installed here (portal.sh is missing). Ask BSR.")
    config.USER_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.USER_DIR, 0o700)
    log = config.USER_DIR / "portal-launch.log"
    with open(log, "w") as fh:
        subprocess.Popen(["setsid", "bash", str(launcher)], stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                         cwd=str(Path.home()), start_new_session=True)
    os.chmod(log, 0o600)
    deadline = time.time() + wait_seconds  # the first start may build the web app
    while time.time() < deadline:
        time.sleep(1)
        port = running_portal()
        if port:
            return port
    raise HubError(f"The portal didn't start within {wait_seconds} s. Its log is {log}.")


def open_portal(user: str) -> dict:
    if user != getpass.getuser():
        raise Forbidden("The portal can only be opened by the person whose account runs it.")
    port = running_portal()
    started = port is None
    if started:
        port = start_portal()
    ticket = identity.issue_ticket()
    minutes = config.OPEN_LINK_TTL_SECONDS // 60
    host = config.CONNECT_HOST or editing._laptop_hostname()
    tunnel = f"ssh -N -L {port}:localhost:{port} {user}@{host}"
    usual = editing.portal_port(user)
    from .runs import ports_forwarded  # runs imports this module
    needs_tunnel = not (port == usual and ports_forwarded())
    if not needs_tunnel:
        how = (f"Click the link (within {minutes} minutes; it works once). It opens straight away through the "
               "Connect page's Claude Desktop setup. If the page doesn't load, run the tunnel command in a "
               "terminal on your laptop (CSHL Wi-Fi or VPN), leave it open, and ask for a new link.")
    elif port != usual:
        how = (f"Your portal is on port {port} instead of your usual {usual} (something else was using it), so "
               "first run the tunnel command in a terminal on your laptop and leave it open, then click the link "
               f"within {minutes} minutes. It works once.")
    else:
        how = ("First run the tunnel command in a terminal on your laptop (CSHL Wi-Fi or VPN) and leave it open; "
               f"then click the link within {minutes} minutes. It works once.")
    link = f"http://localhost:{port}/#ticket={ticket}"
    return {
        "say": agent.link_say("CodeSpringHub", link, minutes, tunnel, needs_tunnel),
        "link": link,
        "needs_tunnel": needs_tunnel,
        "started_now": started,
        "expires_in_minutes": minutes,
        "how_to_open": how,
        "tunnel_command": tunnel,
        "note": "The portal keeps running on the cluster after this chat ends; ask for a new link any time.",
    }
