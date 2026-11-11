"""Small line-delimited client for the documented Codex app-server stdio protocol."""
import json
import selectors
import subprocess
import time


class RpcClient:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.sequence = 0
        self.events = []

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout=60):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Timed out waiting for app-server; reconcile before retrying a mutation")
            chunk = self.process.stdout.read(65536)
            if not chunk:
                raise EOFError("App-server transport closed; a mutation may still have committed")
            self.buffer += chunk
            if len(self.buffer) > 32 * 1024 * 1024:
                raise ValueError("App-server message exceeded 32 MiB")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def request(self, method, params, timeout=60):
        self.sequence += 1
        identifier = self.sequence
        self.send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(max(0, deadline - time.monotonic()))
            if message.get("id") == identifier and ("result" in message or "error" in message):
                if "error" in message:
                    raise ValueError(f"{method}: {message['error']}")
                return message["result"]
            if "id" in message and "method" in message:
                raise ValueError("App-server requires an approval/input response; preserving it for a human")
            if len(self.events) >= 1000:
                self.events.pop(0)
            self.events.append(message)

    def initialize(self):
        result = self.request("initialize", {"clientInfo": {"name": "codex_airlift", "version": "0.1.0"},
                                             "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})
        return result

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process.stdin.close()
        self.process.stdout.close()

    def __enter__(self):
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *args):
        self.close()
