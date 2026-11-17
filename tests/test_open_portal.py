"""Opening the portal from Claude: one-time tickets, the open_portal tool, and the SSH forwards
that the Connect page's Claude Desktop setup adds so the link opens without a separate tunnel."""

import json
import os
import socket
import stat
import time

import pytest
from fastapi.testclient import TestClient

from codespringhub import config, editing, identity, portal_launch
from codespringhub.errors import Forbidden
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import USER


# ---- tickets ------------------------------------------------------------------------------

def test_a_ticket_works_once_and_is_private():
    t = identity.issue_ticket()
    f = config.USER_DIR / "portal-tickets" / t
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert identity.redeem_ticket(t) is True
    assert identity.redeem_ticket(t) is False and not f.exists()


def test_expired_unknown_and_malformed_tickets_are_refused():
    t = identity.issue_ticket(ttl_seconds=1)
    (config.USER_DIR / "portal-tickets" / t).write_text(str(time.time() - 5))
    assert identity.redeem_ticket(t) is False
    assert identity.redeem_ticket("A" * 43) is False
    for bad in ("", None, "../portal-token", "x" * 200, "a/b" + "c" * 40):
        assert identity.redeem_ticket(bad) is False


def test_issuing_tidies_up_expired_tickets():
    old = identity.issue_ticket()
    (config.USER_DIR / "portal-tickets" / old).write_text(str(time.time() - 5))
    identity.issue_ticket()
    assert not (config.USER_DIR / "portal-tickets" / old).exists()


def test_the_portal_swaps_a_ticket_for_its_secret_once(hub):
    auth = LocalLinkAuth(new_link=True)
    client = TestClient(create_app(auth))
    t = identity.issue_ticket()
    r = client.post("/api/redeem", json={"ticket": t})
    assert r.status_code == 200 and r.json() == {"token": auth.token}
    assert client.get("/api/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200
    again = client.post("/api/redeem", json={"ticket": t})
    assert again.status_code == 401 and "already used" in again.json()["detail"]
    assert "token" not in client.post("/api/redeem", json={"ticket": "nope"}).json()


# ---- finding and starting the portal ------------------------------------------------------

def test_a_running_portal_is_found_from_its_state_file():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        f = portal_launch.state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"pid": os.getpid(), "port": port}))
        assert portal_launch.running_portal() == port
        f.write_text(json.dumps({"pid": 2 ** 22 + 12345, "port": port}))  # no such process
        assert portal_launch.running_portal() is None
    f.write_text(json.dumps({"pid": os.getpid(), "port": port}))  # process alive, nothing listening
    assert portal_launch.running_portal() is None
    f.write_text("garbage")
    assert portal_launch.running_portal() is None


def _fake_portal(marker: str = "codespringhub-portal"):
    """A child process that listens on a port; `marker` in its command line makes it look like a portal."""
    import subprocess
    import sys
    code = ("import socket,sys,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
            "print(s.getsockname()[1], flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, marker], stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _record(pid: int, port: int, started: float):
    f = portal_launch.state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pid": pid, "port": port}))
    os.utime(f, (started, started))


def test_code_changes_after_the_portal_started_are_noticed(hub):
    code = config.HUB_HOME / "server" / "codespringhub" / "web.py"
    code.parent.mkdir(parents=True, exist_ok=True)
    code.write_text("# new route")
    assert portal_launch.code_changed_since(time.time() - 60)
    assert not portal_launch.code_changed_since(time.time() + 60)


def test_a_portal_older_than_the_code_is_stopped_so_a_fresh_one_starts(hub, monkeypatch):
    proc, port = _fake_portal()
    try:
        _record(proc.pid, port, time.time() - 3600)
        monkeypatch.setattr(portal_launch, "code_changed_since", lambda when: True)
        assert portal_launch.running_portal(restart_if_stale=False) == port
        assert portal_launch.running_portal() is None
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()


def test_an_up_to_date_portal_is_reused(hub, monkeypatch):
    proc, port = _fake_portal()
    try:
        _record(proc.pid, port, time.time())
        monkeypatch.setattr(portal_launch, "code_changed_since", lambda when: False)
        assert portal_launch.running_portal() == port and proc.poll() is None
    finally:
        proc.kill()


def test_a_reused_pid_that_isnt_a_portal_is_never_stopped(hub, monkeypatch):
    proc, port = _fake_portal(marker="something-else")
    try:
        _record(proc.pid, port, time.time() - 3600)
        monkeypatch.setattr(portal_launch, "code_changed_since", lambda when: True)
        assert portal_launch.running_portal() == port and proc.poll() is None
    finally:
        proc.kill()


def test_a_restarted_portal_keeps_its_port_while_old_connections_close():
    from codespringhub.web import _free_port
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()  # the server side closes first, so its end of the port sits in TIME_WAIT
        client.close()
    assert _free_port(port) == port


def test_open_portal_gives_a_one_time_link_not_the_secret(hub, monkeypatch):
    usual = editing.portal_port(USER)
    monkeypatch.setattr(portal_launch, "running_portal", lambda: usual)
    monkeypatch.setattr(portal_launch, "start_portal", lambda: pytest.fail("already running"))
    from codespringhub import runs
    monkeypatch.setattr(runs, "ports_forwarded", lambda: True)
    secret = LocalLinkAuth(new_link=True).token
    out = portal_launch.open_portal(USER)
    assert out["link"].startswith(f"http://localhost:{usual}/#ticket=") and secret not in json.dumps(out)
    assert not out["started_now"] and "straight away" in out["how_to_open"] and not out["needs_tunnel"]
    assert out["say"].startswith(f"**[Open CodeSpringHub]({out['link']})**")
    assert out["tunnel_command"].startswith(f"ssh -N -L {usual}:localhost:{usual} {USER}@")
    assert identity.redeem_ticket(out["link"].split("ticket=")[1]) is True


def test_open_portal_puts_the_tunnel_first_when_nothing_forwards_the_port(hub, monkeypatch):
    from codespringhub import runs
    monkeypatch.setattr(portal_launch, "running_portal", lambda: editing.portal_port(USER))
    monkeypatch.setattr(runs, "ports_forwarded", lambda: False)  # e.g. Claude Code on the cluster
    out = portal_launch.open_portal(USER)
    assert out["needs_tunnel"] and out["say"].startswith("First run this")
    assert out["say"].index(out["tunnel_command"]) < out["say"].index(out["link"])


def test_open_portal_starts_the_portal_when_needed_and_says_when_the_port_moved(hub, monkeypatch):
    monkeypatch.setattr(portal_launch, "running_portal", lambda: None)
    monkeypatch.setattr(portal_launch, "start_portal", lambda: 45999)
    out = portal_launch.open_portal(USER)
    assert out["started_now"] and out["link"].startswith("http://localhost:45999/#ticket=")
    if editing.portal_port(USER) != 45999:
        assert "instead of your usual" in out["how_to_open"]


def test_only_the_account_owner_can_open_their_portal():
    with pytest.raises(Forbidden):
        portal_launch.open_portal("someone-else")


def test_claude_can_call_open_portal_and_the_prompt_is_listed(hub, monkeypatch):
    import asyncio
    from mcp import Client
    from codespringhub.server import mcp
    from test_server import call
    monkeypatch.setattr(portal_launch, "running_portal", lambda: editing.portal_port(USER))
    out = call("open_portal")
    assert "#ticket=" in out["link"]

    async def prompts():
        async with Client(mcp) as c:
            return [p.name for p in (await c.list_prompts()).prompts]
    assert {"open_portal", "open_app"} <= set(asyncio.run(prompts()))


# ---- Claude Desktop's SSH login carries the ports ------------------------------------------

def test_connect_setup_forwards_the_portal_and_codespringapp_ports(hub):
    info = editing.connect_info(USER)
    args = info["claude_desktop"]["mcpServers"]["BSR Agent"]["args"]
    p, a = info["portal_port"], info["codespringapp_port"]
    assert p == editing.portal_port(USER) and a == editing.codespringapp_port(USER)
    forwards = [args[i + 1] for i, x in enumerate(args) if x == "-L"]
    assert forwards == [f"127.0.0.1:{p}:localhost:{p}", f"127.0.0.1:{a}:localhost:{a}"]
    assert "ExitOnForwardFailure=no" in args  # a busy laptop port must not break Claude's connection
    assert args[-2].startswith(f"{USER}@") and args[-1].endswith("env/bin/codespringhub")
    assert f"127.0.0.1:{p}:localhost:{p}" in info["setup_windows"]  # Windows: plain ssh.exe, same forwards
