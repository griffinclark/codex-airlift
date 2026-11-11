"""Small line-delimited client for the documented Codex app-server stdio protocol."""
import json
import base64
import hashlib
import os
import selectors
import struct
import subprocess
import time


class RpcClient:
    def __init__(self, command, websocket=False):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.sequence = 0
        self.events = []
        self.websocket = websocket
        if websocket:
            try:
                self._upgrade()
            except BaseException:
                self.close()
                raise

    def _write(self, data):
        remaining = memoryview(data)
        while remaining:
            written = self.process.stdin.write(remaining)
            if not written:
                raise EOFError("App-server input closed")
            remaining = remaining[written:]
        self.process.stdin.flush()

    def _take(self, count, deadline):
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Timed out waiting for app-server; reconcile before retrying a mutation")
            chunk = self.process.stdout.read(65536)
            if not chunk:
                raise EOFError("App-server transport closed; a mutation may still have committed")
            self.buffer += chunk
            if len(self.buffer) > 32 * 1024 * 1024:
                raise ValueError("App-server message exceeded 32 MiB")
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def _upgrade(self):
        # `codex app-server proxy` forwards the raw WebSocket-over-UDS stream;
        # unlike `app-server --stdio`, it is not a JSON-lines transport.
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                     "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                     f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        header = bytearray()
        deadline = time.monotonic() + 60
        while not header.endswith(b"\r\n\r\n"):
            header.extend(self._take(1, deadline))
            if len(header) > 65536:
                raise ValueError("WebSocket upgrade header too large")
        lines = header.decode("ascii").split("\r\n")
        fields = {key.lower().strip(): value.strip() for line in lines[1:] if ":" in line for key, value in [line.split(":", 1)]}
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if " 101 " not in lines[0] or fields.get("sec-websocket-accept", "") != expected:
            raise ValueError("App-server WebSocket upgrade was not accepted")

    def _frame(self, payload, opcode=1):
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size <= 65535:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", size)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        self._write(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _receive_frame(self, timeout):
        deadline = time.monotonic() + timeout
        result = bytearray()
        started = False
        while True:
            first, second = self._take(2, deadline)
            if first & 0x70 or second & 0x80:
                raise ValueError("Unsupported/reserved or masked server WebSocket frame")
            opcode, size = first & 15, second & 127
            if size == 126:
                size = struct.unpack("!H", self._take(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._take(8, deadline))[0]
            if size + len(result) > 32 * 1024 * 1024:
                raise ValueError("WebSocket message exceeded 32 MiB")
            body = self._take(size, deadline)
            if opcode == 8:
                raise EOFError("App-server closed its WebSocket")
            if opcode == 9:
                self._frame(body, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0 and not started) or (opcode == 1 and started):
                raise ValueError("Unexpected WebSocket data frame")
            started = True
            result.extend(body)
            if first & 0x80:
                return json.loads(result)

    def send(self, value):
        body = json.dumps(value).encode()
        if self.websocket:
            self._frame(body)
        else:
            self._write(body + b"\n")

    def receive(self, timeout=60):
        if self.websocket:
            return self._receive_frame(timeout)
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
