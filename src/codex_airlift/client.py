"""Airlift setup, recoverable SSH transport, and the vendor terminal UI."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
import unicodedata

from .canonical import digest
from .packet import pack
from .runtime import identifier, local_store, terminal_arguments
from .storage import locked, private_dir, read_json, write_json
from .transfer import export_desktop, export_runtime, receive

INSTALLER_SHA256 = "44ac252c1b9a68c02bdf0de8e1c6cbfde36fc1afa8684624962eae7d4c461e70"


def connection(state_dir):
    root = private_dir(Path(state_dir).expanduser().resolve())
    lease = read_json(root / "deployment.json")
    return root, lease, ["ssh", "-F", str(root / "ssh_config"), lease["name"]]


def remote(state_dir, action, **arguments):
    _, _, command = connection(state_dir)
    worker = ["python3", "-m", "codex_airlift.worker", action]
    for key, value in arguments.items():
        if isinstance(value, bool):
            if value:
                worker += ["--" + key.replace("_", "-")]
            continue
        worker += ["--" + key.replace("_", "-"), str(value)]
    script = 'PYTHONPATH="$HOME/.codex/airlift/software/current/src" ' + shlex.join(worker)
    result = subprocess.run(command + [script], capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise ValueError("Cloud worker failed; operation retained for reconciliation: " + result.stderr[-2500:])
    return json.loads(result.stdout)


def install_client(state_dir, source):
    root, lease, command = connection(state_dir)
    home = private_dir(root / "local-host/codex")
    binary = home / "packages/app-server-daemon/current/bin/codex"
    if not binary.exists():
        installer = root / "vendor-install.sh"
        if not installer.exists():
            with urllib.request.urlopen("https://chatgpt.com/codex/install.sh", timeout=60) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != INSTALLER_SHA256:
                raise ValueError("Installer changed; review it before updating its pinned hash")
            installer.write_bytes(data)
            installer.chmod(0o600)
        if digest(installer) != INSTALLER_SHA256:
            raise ValueError("Retained installer hash differs")
        subprocess.run(["sh", str(installer), "--release", lease["codex_version"]], check=True,
                       env=dict(os.environ, CODEX_HOME=str(home), CODEX_INSTALL_DAEMON_ONLY="1", CODEX_NON_INTERACTIVE="1"), timeout=600)
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != "codex-cli " + lease["codex_version"]:
        raise ValueError("Local isolated CLI differs from the pinned host version")
    with tempfile.TemporaryDirectory(prefix="client-install-", dir=root) as temporary:
        staging = private_dir(Path(temporary) / "package")
        package = staging / "src/codex_airlift"
        private_dir(package.parent)
        shutil.copytree(Path(source) / "src/codex_airlift", package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        archive = Path(temporary) / "client.tar"
        pack(staging, archive)
        sha = digest(archive)
        target = "/home/codex/.codex/airlift/client-package-" + sha + ".tar"
        subprocess.run(command + ["umask 077; mkdir -p ~/.codex/airlift/inbox ~/.codex/airlift/software/releases"], check=True, timeout=90)
        subprocess.run(["scp", "-F", str(root / "ssh_config"), str(archive), lease["name"] + ":" + target], check=True, timeout=600)
        # Bootstrap extraction is restricted to this content-addressed public
        # package. The normal packet module handles all private transfer data.
        script = '''import hashlib,os,sys,tarfile
from pathlib import Path
packet=Path(sys.argv[1]); sha=sys.argv[2]
assert hashlib.sha256(packet.read_bytes()).hexdigest()==sha
base=Path.home()/'.codex/airlift/software'; release=base/'releases'/sha
if not release.exists():
 release.mkdir(mode=0o700)
 with tarfile.open(packet,'r:') as archive:
  for member in archive:
   path=Path(member.name)
   assert member.isfile() and not path.is_absolute() and '..' not in path.parts
   target=release/path; target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   with archive.extractfile(member) as incoming,target.open('xb') as outgoing: outgoing.write(incoming.read())
   target.chmod(0o600)
link=base/'next'; link.unlink(missing_ok=True); link.symlink_to(release); link.replace(base/'current')
unit=Path.home()/'.config/systemd/user/codex-airlift-runtime@.service';unit.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
unit.write_text("""[Unit]
Description=Codex Airlift runtime %i
After=network-online.target
[Service]
Type=simple
Environment=CODEX_HOME=%h/.codex/airlift/client/runtimes/%i/codex
ExecStartPre=/usr/bin/mkdir -p -m 0700 %t/codex-airlift
ExecStart=/usr/local/bin/codex app-server --listen unix://%t/codex-airlift/%i.sock
Restart=on-failure
RestartSec=5
UMask=0077
TimeoutStopSec=120
[Install]
WantedBy=default.target
""")
print(sha)
'''
        subprocess.run(command + ["python3 -c " + shlex.quote(script) + " " + shlex.quote(target) + " " + sha], check=True, timeout=90)
        subprocess.run(command + ['XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user daemon-reload'], check=True, timeout=90)
    status = {"status": "ready", "interface": "airlift-terminal", "cli_version": lease["codex_version"],
              "package_sha256": sha, "cloud": remote(root, "catalog")}
    launcher = root / "Airlift.command"
    launcher.write_text("#!/bin/sh\n# Managed by Codex Airlift setup\nexec " +
                        shlex.join([str(Path(source).resolve() / ".venv/bin/python"), "-m", "codex_airlift.cli",
                                    "--state-dir", str(root), "open"]) + "\n")
    launcher.chmod(0o700)
    status["launcher"] = str(launcher)
    write_json(root / "client-setup.json", status)
    return status


def move(state_dir, thread_id, destination, operation_id, desktop=False, desktop_home=None, desktop_binary=None,
         reconcile_workspace=False, workspace_override=None):
    root, lease, command = connection(state_dir)
    identifier(thread_id)
    identifier(operation_id)
    store = local_store(root)
    journal = root / "client/moves" / (operation_id + ".json")
    with locked(journal):
        if journal.exists():
            old = read_json(journal)
            if old["thread_id"] != thread_id or old["destination"] != destination:
                raise ValueError("Operation belongs to a different move")
            if old["state"] == "verified":
                return old
        else:
            write_json(journal, {"state": "preparing", "thread_id": thread_id, "destination": destination})
        if destination == "cloud":
            exported = (export_desktop(store, operation_id, thread_id, desktop_home, desktop_binary,
                        workspace_override=workspace_override, reconcile_workspace=reconcile_workspace) if desktop
                        else export_runtime(store, operation_id, thread_id))
            write_json(journal, dict(exported, state="exported", destination=destination))
            target = "/home/codex/.codex/airlift/inbox/" + operation_id + ".tar"
            # Re-sending the same immutable packet is safe. The destination's
            # journal is checked before import and cannot repeat execution.
            subprocess.run(["scp", "-F", str(root / "ssh_config"), exported["packet"], lease["name"] + ":" + target],
                           check=True, timeout=1800)
            result = remote(root, "receive", operation=operation_id, sha256=exported["packet_sha256"],
                            reconcile_workspace=reconcile_workspace)
        else:
            exported = remote(root, "export", operation=operation_id, thread=thread_id)
            write_json(journal, dict(exported, state="exported", destination=destination))
            packet = root / "client/moves" / (operation_id + ".tar")
            subprocess.run(["scp", "-F", str(root / "ssh_config"), lease["name"] + ":" + exported["packet"], str(packet)],
                           check=True, timeout=1800)
            result = receive(store, operation_id, packet, exported["packet_sha256"], reconcile_workspace=reconcile_workspace)
        if result["state"] != "verified" or result["thread_id"] != thread_id:
            raise ValueError("Destination did not verify the selected chat")
        result = dict(result, destination=destination)
        write_json(journal, result)
        return result


def chat(state_dir, host, thread_id):
    root, lease, ssh = connection(state_dir)
    store = local_store(root)
    tunnel = None
    socket = None
    if host == "local":
        record = store.owned(thread_id)
        runtime = store.ensure(record["runtime_id"])
    else:
        info = remote(root, "info", thread=thread_id)
        record, runtime = info["record"], info["runtime"]
        socket = store.sockets / ("view-" + str(uuid.uuid4()) + ".sock")
        arguments = ssh[:-1] + ["-NT", "-o", "ExitOnForwardFailure=yes", "-L", str(socket) + ":" + runtime["socket"], ssh[-1]]
        tunnel = subprocess.Popen(arguments, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not socket.exists():
            if tunnel.poll() is not None or time.monotonic() >= deadline:
                if tunnel.poll() is None:
                    tunnel.terminate()
                    tunnel.wait(timeout=15)
                raise ValueError("Private SSH socket tunnel did not start")
            time.sleep(0.2)
        runtime["socket"] = str(socket)
    try:
        return subprocess.run(terminal_arguments(store.binary, runtime["socket"], record),
                              env=dict(os.environ, CODEX_HOME=str(root / "local-host/codex"))).returncode
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=15)
        if socket is not None:
            socket.unlink(missing_ok=True)


def cli_command(args):
    from .cli import source_root
    if args.command == "client-setup":
        result = install_client(args.state_dir, source_root(args))
    elif args.command == "stage-desktop":
        from .desktop_stage import stage
        result = stage(local_store(args.state_dir), args.batch, read_json(args.inventory), args.desktop_home)
    elif args.command == "finalize-desktop":
        from .transfer import desktop_row
        root, _, _ = connection(args.state_dir)
        journal = root / "client/finalizers" / (identifier(args.operation) + ".json")
        with locked(journal):
            if journal.exists() and read_json(journal).get("state") == "verified":
                print(json.dumps(read_json(journal), indent=2))
                return
            write_json(journal, {"state": "waiting-for-public-archive", "thread_id": args.thread,
                                 "operation_id": args.operation, "pid": os.getpid()})
            deadline = time.monotonic() + args.timeout
            while not desktop_row(args.desktop_home, args.thread)["archived"]:
                if time.monotonic() >= deadline:
                    write_json(journal, {"state": "incomplete", "thread_id": args.thread,
                                         "operation_id": args.operation, "reason": "Source remains live"})
                    raise ValueError("Source remains live; finalizer timed out without changing ownership")
                time.sleep(1)
            try:
                result = move(root, args.thread, "cloud", args.operation, desktop=True,
                              desktop_home=args.desktop_home, desktop_binary=args.desktop_binary)
            except Exception as error:
                write_json(journal, {"state": "incomplete", "thread_id": args.thread,
                                     "operation_id": args.operation, "reason": str(error)})
                raise
            write_json(journal, result)
    elif args.command == "move":
        result = move(args.state_dir, args.thread, args.to, args.operation,
                      desktop=args.desktop, desktop_home=args.desktop_home, desktop_binary=args.desktop_binary,
                      reconcile_workspace=args.reconcile_workspace, workspace_override=args.workspace)
    elif args.command == "batch":
        identifier(args.batch)
        root, _, _ = connection(args.state_dir)
        journal = root / "client/batches" / (args.batch + ".json")
        if args.inventory:
            inventory = read_json(Path(args.inventory).expanduser())
            rows = inventory if isinstance(inventory, list) else inventory.get("threads", []) + inventory.get("pinnedThreads", [])
            selected = [row if isinstance(row, str) else row["id"] for row in rows]
        elif journal.exists():
            selected = read_json(journal)["threads"]
        else:
            values = local_store(root).catalog() if args.to == "cloud" else remote(root, "catalog")
            selected = [tid for tid, row in values["threads"].items() if row["state"] == "ready"]
        if len(selected) != len(set(selected)) or not selected:
            raise ValueError("Select a nonempty set of unique chat IDs")
        for tid in selected:
            identifier(tid)
        with locked(journal):
            if journal.exists():
                result = read_json(journal)
                if result["threads"] != selected or result["destination"] != args.to:
                    raise ValueError("Batch selection changed; preserve and reconcile its original manifest")
            else:
                result = {"state": "running", "threads": selected, "destination": args.to, "results": {}}
                write_json(journal, result)
            for tid in selected:
                op = str(uuid.uuid5(uuid.UUID(args.batch), tid))
                try:
                    overrides = read_json(args.workspace_map) if args.workspace_map else {}
                    moved = move(root, tid, args.to, op, desktop=args.desktop,
                                 desktop_home=args.desktop_home, desktop_binary=args.desktop_binary,
                                 reconcile_workspace=args.reconcile_workspace, workspace_override=overrides.get(tid))
                except Exception as error:
                    result.update(state="incomplete", remaining=tid, error=str(error))
                    write_json(journal, result)
                    raise
                result["results"][tid] = moved
                result.update(state="running")
                result.pop("error", None)
                result.pop("remaining", None)
                write_json(journal, result)
                print(json.dumps({"verified": tid, "destination": args.to}), flush=True)
            result["state"] = "verified"
            write_json(journal, result)
    elif args.command == "chats":
        result = {"local": local_store(args.state_dir).catalog(), "cloud": remote(args.state_dir, "catalog")}
    elif args.command == "chat":
        raise SystemExit(chat(args.state_dir, args.host, args.thread))
    else:
        values = {"local": local_store(args.state_dir).catalog(), "cloud": remote(args.state_dir, "catalog")}
        choices = [(host, record) for host in ("cloud", "local") for record in values[host]["threads"].values()
                   if record["state"] == "ready"]
        if not choices:
            print("No Airlift chats yet. Use $codex-to-cloud in Codex to adopt your open chats.")
            return
        for index, (host, record) in enumerate(choices, 1):
            title = "".join(c for c in record["title"] if not unicodedata.category(c).startswith("C"))
            print(f"{index:3}  {host:5}  {title}  [{record['thread_id'][:8]}]")
        try:
            selected = int(input("Open chat number (0 to exit): "))
        except (EOFError, ValueError):
            return
        if selected == 0:
            return
        if not 1 <= selected <= len(choices):
            raise ValueError("Invalid chat number")
        host, record = choices[selected - 1]
        raise SystemExit(chat(args.state_dir, host, record["thread_id"]))
    print(json.dumps(result, indent=2))
