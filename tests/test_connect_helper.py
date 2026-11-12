"""The laptop reconnect helper (laptop/connect.py), tested against a fake Hub we can kill."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "laptop" / "connect.py"

FAKE_HUB = r'''
import json, os, sys, time
open(sys.argv[1], "a").write(f"{os.getpid()}\n")
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "slow":
        time.sleep(60)
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"pid": os.getpid(), "method": msg["method"]}}), flush=True)
'''


class Helper:
    def __init__(self, tmp_path, command=None):
        self.pids = tmp_path / "pids"
        fake = tmp_path / "fake_hub.py"
        fake.write_text(FAKE_HUB)
        command = command or [sys.executable, str(fake), str(self.pids)]
        self.proc = subprocess.Popen([sys.executable, str(HELPER), "--", *command], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.out = queue.Queue()
        threading.Thread(target=lambda: [self.out.put(json.loads(l)) for l in self.proc.stdout], daemon=True).start()

    def send(self, msg):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=10):
        return self.out.get(timeout=timeout)

    def call(self, i, method="tools/call"):
        self.send({"id": i, "method": method, "params": {}})
        return self.recv()

    def hub_pids(self):
        return [int(p) for p in self.pids.read_text().split()] if self.pids.exists() else []

    def kill_hub(self):
        os.kill(self.hub_pids()[-1], signal.SIGKILL)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def helper(tmp_path):
    h = Helper(tmp_path)
    assert h.call(0, "initialize")["result"]["method"] == "initialize"
    h.send({"method": "notifications/initialized"})
    yield h
    if h.proc.poll() is None:
        h.close()


def test_passes_messages_through(helper):
    r = helper.call(1)
    assert r["id"] == 1 and r["result"]["pid"] == helper.hub_pids()[0]


def test_reconnects_after_the_link_dies_and_claude_carries_on(helper):
    first = helper.call(1)["result"]["pid"]
    helper.kill_hub()
    deadline, i = time.time() + 20, 100
    while time.time() < deadline:
        i += 1
        r = helper.call(i)
        assert r["id"] == i  # never the helper's own replayed handshake
        if "result" in r:
            break
        assert "reconnecting" in r["error"]["message"]
        time.sleep(0.5)
    else:
        pytest.fail("never reconnected")
    assert r["result"]["pid"] != first and len(helper.hub_pids()) == 2


def test_a_request_in_flight_when_the_link_dies_gets_an_answer(helper):
    helper.send({"id": 7, "method": "slow", "params": {}})
    time.sleep(0.5)
    helper.kill_hub()
    r = helper.recv()
    assert r["id"] == 7 and "dropped" in r["error"]["message"]


def test_closing_claude_stops_the_hub(helper):
    helper.call(1)
    pid = helper.hub_pids()[-1]
    helper.close()
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_first_connection_that_fails_is_reported_like_plain_ssh(tmp_path):
    h = Helper(tmp_path, command=[sys.executable, "-c", "import sys; sys.stderr.write('Permission denied\\n'); sys.exit(255)"])
    h.send({"id": 0, "method": "initialize", "params": {}})
    assert h.proc.wait(timeout=10) == 255
    assert "Permission denied" in h.proc.stderr.read()
