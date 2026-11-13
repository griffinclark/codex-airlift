"""Task-owned app-server runtimes and Unix-socket terminal clients."""
import os
from pathlib import Path
import subprocess
import time
import uuid

from .rpc import RpcClient
from .storage import locked, private_dir, read_json, write_json


def identifier(value):
    normalized = str(uuid.UUID(value))
    if normalized != value:
        raise ValueError("Use a canonical runtime UUID")
    return normalized


class RuntimeStore:
    def __init__(self, base, binary, auth_source, cloud=False):
        self.base = private_dir(base).resolve()
        self.binary = str(Path(binary).resolve())
        self.auth_source = Path(auth_source).resolve()
        self.cloud = cloud
        # macOS Unix socket paths are short; do not use its long TMPDIR path.
        self.sockets = private_dir((Path("/run/user") / str(os.getuid()) / "codex-airlift") if cloud else
                                   Path("/tmp") / ("codex-airlift-" + str(os.getuid())))

    def info(self, runtime_id):
        runtime_id = identifier(runtime_id)
        home = self.base / "runtimes" / runtime_id / "codex"
        return {"id": runtime_id, "home": str(home), "socket": str(self.sockets / (runtime_id + ".sock")),
                "binary": self.binary, "base": str(self.base)}

    def rpc(self, runtime_id):
        info = self.info(runtime_id)
        env = dict(os.environ, CODEX_HOME=info["home"])
        return RpcClient([self.binary, "app-server", "proxy", "--sock", info["socket"]],
                         websocket=True, env=env)

    def ensure(self, runtime_id):
        info = self.info(runtime_id)
        home = private_dir(info["home"])
        receipt = home.parent / "runtime.json"
        with locked(receipt):
            if not (home / "auth.json").exists():
                with self.auth_source.open("rb") as source:
                    fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as target:
                        target.write(source.read())
            config = home / "config.toml"
            if not config.exists():
                config.write_text('approval_policy = "on-request"\nsandbox_mode = "read-only"\n[analytics]\nenabled = false\n')
                config.chmod(0o600)
            try:
                with self.rpc(runtime_id):
                    return info
            except (OSError, ValueError, EOFError, TimeoutError):
                pass
            if self.cloud:
                subprocess.run(["systemctl", "--user", "enable", "--now", "codex-airlift-runtime@" + runtime_id + ".service"],
                               env=dict(os.environ, XDG_RUNTIME_DIR="/run/user/" + str(os.getuid())),
                               check=True, capture_output=True, timeout=90)
            else:
                if receipt.exists():
                    previous = read_json(receipt)
                    if previous.get("pid"):
                        try:
                            os.kill(previous["pid"], 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise ValueError("Owned runtime process is alive but unavailable; reconcile it before restarting")
                socket = Path(info["socket"])
                if socket.exists():
                    if not socket.is_socket() or socket.stat().st_uid != os.getuid():
                        raise ValueError("Unexpected runtime socket owner or type")
                    socket.unlink()
                log = os.open(home.parent / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    process = subprocess.Popen([self.binary, "app-server", "--listen", "unix://" + info["socket"]],
                                               env=dict(os.environ, CODEX_HOME=str(home)), stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=log, start_new_session=True)
                finally:
                    os.close(log)
                info["pid"] = process.pid
            write_json(receipt, info)
            until = time.monotonic() + 60
            while time.monotonic() < until:
                if Path(info["socket"]).exists():
                    try:
                        with self.rpc(runtime_id):
                            return info
                    except (OSError, ValueError, EOFError, TimeoutError):
                        pass
                time.sleep(0.5)
            raise ValueError("Runtime startup remains incomplete; inspect its owned log")

    def catalog(self):
        path = self.base / "catalog.json"
        return read_json(path) if path.exists() else {"schema": 2, "threads": {}}

    def record(self, thread_id, record):
        path = self.base / "catalog.json"
        with locked(path):
            value = self.catalog()
            value["threads"][thread_id] = record
            write_json(path, value)
        return record

    def owned(self, thread_id):
        value = self.catalog()["threads"].get(thread_id)
        if not value or value.get("state") != "ready":
            raise ValueError("This host does not own an executable copy of this chat")
        return value


def local_store(state_dir):
    state_dir = Path(state_dir).expanduser().resolve()
    binary = state_dir / "local-host/codex/packages/app-server-daemon/current/bin/codex"
    if not binary.is_file():
        raise ValueError("Run client installation before using the Airlift terminal interface")
    return RuntimeStore(state_dir / "client", binary, Path.home() / ".codex/auth.json")


def cloud_store():
    return RuntimeStore(Path.home() / ".codex/airlift/client", "/usr/local/bin/codex",
                        Path.home() / ".codex/auth.json", cloud=True)


def terminal_arguments(binary, socket, record):
    # Remote resume rejects permission overrides. These were restored and
    # checked through app-server at import; use the server's saved settings.
    return [str(binary), "resume", record["thread_id"], "--remote", "unix://" + str(socket),
            "--cd", record["cwd"], "--no-alt-screen"]
