#!/usr/bin/env python3
"""CodeSpringHub reconnect helper. Runs on the researcher's laptop, started by Claude Desktop.

Claude Desktop starts the Hub once, through SSH. If the laptop sleeps, changes network or the
VPN drops, that SSH link dies and Claude Desktop won't start it again until it is restarted.
This helper sits between Claude Desktop and SSH and, when the link dies, quietly starts SSH
again and repeats the opening handshake, so Claude can carry on.

It only ever runs the command it is given (the same `ssh ... codespringhub` command Claude
Desktop would otherwise run). It sends nothing anywhere else and stores nothing.

Usage (in claude_desktop_config.json):
    python3 ~/.codespringhub/connect.py -- ssh -T -o BatchMode=yes ... user@host /path/to/codespringhub

Standard library only, Python 3.8+.
"""

import json
import os
import subprocess
import sys
import threading
import time

RETRY_WAITS = [2, 5, 10, 20, 30, 60]  # seconds between reconnect attempts; the last repeats
DROPPED = ("The connection to CodeSpringHub on the cluster dropped (laptop asleep, Wi-Fi or VPN "
           "change). It is reconnecting by itself; please try again in a moment. If this keeps "
           "happening, check you're on CSHL Wi-Fi or the VPN.")


def log(msg):
    print(f"[codespringhub-connect] {msg}", file=sys.stderr, flush=True)


class Link:
    def __init__(self, command):
        self.command = command
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.proc = None
        self.ready = False          # handshake done on the current connection
        self.queue = []             # client messages waiting for the handshake
        self.pending = set()        # request ids sent to the Hub and not answered yet
        self.init_line = None       # the client's original initialize request
        self.initialized_line = None
        self.replay_id = None       # id of our replayed initialize, whose answer we swallow
        self.ever_connected = False
        self.client_gone = False
        self.reconnects = 0
        self.reconnecting = False   # a reconnect loop is running

    # ---- talking to Claude Desktop -----------------------------------------------------------
    def to_client(self, msg):
        line = msg if isinstance(msg, str) else json.dumps(msg)
        with self.out_lock:
            sys.stdout.write(line.rstrip("\n") + "\n")
            sys.stdout.flush()

    def fail(self, req_id):
        self.to_client({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": DROPPED}})

    # ---- talking to the Hub ------------------------------------------------------------------
    def _send(self, line):
        try:
            self.proc.stdin.write(line.rstrip("\n") + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def start(self):
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, text=True, bufsize=1)
        threading.Thread(target=self.read_hub, args=(self.proc,), daemon=True).start()
        if self.init_line is None:
            self.ready = True  # first connection: Claude Desktop does the handshake itself
            return
        # Reconnection: repeat Claude Desktop's original handshake under our own id.
        self.ready = False
        self.reconnects += 1
        init = json.loads(self.init_line)
        self.replay_id = f"codespringhub-reconnect-{self.reconnects}"
        init["id"] = self.replay_id
        self._send(json.dumps(init))

    def read_hub(self, proc):
        for line in proc.stdout:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.to_client(line)
                continue
            with self.lock:
                if self.replay_id is not None and msg.get("id") == self.replay_id:
                    self.replay_id = None
                    if "error" in msg:
                        log(f"reconnect handshake refused: {msg['error']}")
                        continue
                    if self.initialized_line:
                        self._send(self.initialized_line)
                    self.ready = True
                    log("reconnected")
                    waiting, self.queue = self.queue, []
                    for queued in waiting:
                        self._forward(queued)
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.pending.discard(json.dumps(msg["id"]))
                    if msg.get("result") is not None and self.init_line and not self.ever_connected:
                        self.ever_connected = True
            self.to_client(line)
        proc.wait()
        self.on_hub_exit(proc)

    def _forward(self, line):
        """Send one client message to the Hub (caller holds the lock)."""
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        is_request = "method" in msg and "id" in msg
        if self.proc is None or self.proc.poll() is not None:
            if is_request:
                self.fail(msg["id"])
            return
        if not self.ready:
            self.queue.append(line)
            return
        if is_request:
            self.pending.add(json.dumps(msg["id"]))
        if not self._send(line) and is_request:
            self.pending.discard(json.dumps(msg["id"]))
            self.fail(msg["id"])

    # ---- messages from Claude Desktop --------------------------------------------------------
    def from_client(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        method = msg.get("method")
        with self.lock:
            if method == "initialize" and self.init_line is None:
                self.init_line = line
            elif method == "notifications/initialized" and self.initialized_line is None:
                self.initialized_line = line
            self._forward(line)

    # ---- the link died -----------------------------------------------------------------------
    def on_hub_exit(self, proc):
        with self.lock:
            if proc is not self.proc or self.client_gone or self.reconnecting:
                return  # stale connection, shutting down, or the reconnect loop will notice
            code = proc.returncode
            if not self.ever_connected:
                # Never got going (no key, wrong host, ...): fail the way plain SSH would, so
                # Claude Desktop shows the error and its log has SSH's message.
                log(f"could not connect (ssh exited with {code})")
                sys.stdout.flush()
                os._exit(code or 1)
            log(f"connection dropped (ssh exited with {code}); reconnecting")
            for req_id in list(self.pending):
                self.fail(json.loads(req_id))
            self.pending.clear()
            for queued in self.queue:
                try:
                    q = json.loads(queued)
                    if "method" in q and "id" in q:
                        self.fail(q["id"])
                except ValueError:
                    pass
            self.queue = []
            self.ready = False
            self.proc = None
            self.reconnecting = True
        threading.Thread(target=self.reconnect, daemon=True).start()

    def reconnect(self):
        attempt = 0
        while not self.client_gone:
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            attempt += 1
            with self.lock:
                if self.client_gone:
                    return
                self.start()
                proc = self.proc
            # Give it a moment: if SSH dies straight away (still offline) try again later.
            time.sleep(1.5)
            with self.lock:
                if proc.poll() is None:
                    self.reconnecting = False  # alive; if it dies later, on_hub_exit starts over
                    return
                if self.proc is proc:
                    self.proc = None
            log(f"still can't reach the cluster (attempt {attempt}); will retry")


def main():
    args = sys.argv[1:]
    if "--" in args:
        args = args[args.index("--") + 1:]
    if not args:
        sys.exit("usage: connect.py -- ssh ... user@host /path/to/codespringhub")
    link = Link(args)
    with link.lock:
        link.start()
    for line in sys.stdin:
        if line.strip():
            link.from_client(line)
    # Claude Desktop closed: stop the Hub too.
    link.client_gone = True
    with link.lock:
        if link.proc and link.proc.poll() is None:
            try:
                link.proc.stdin.close()
                link.proc.wait(timeout=5)
            except Exception:
                link.proc.kill()


if __name__ == "__main__":
    main()
