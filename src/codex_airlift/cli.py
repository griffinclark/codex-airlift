"""Operator commands; native desktop handoff stays with the supported Codex tool."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from .budget import make_lease, parse_time, validate_existing
from . import manifest
from .storage import locked, private_dir, read_json, write_json


def run(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, timeout=kwargs.pop("timeout", 600), **kwargs)


def capture(argv, **kwargs):
    return run(argv, capture_output=True, text=True, **kwargs).stdout.strip()


def source_root(args):
    root = Path(args.source_dir).expanduser().resolve() if args.source_dir else Path(__file__).resolve().parents[2]
    if not (root / "infra/gcp/main.tf").is_file():
        raise ValueError("Use an editable checkout installation or pass --source-dir pointing to the source repo")
    return root


def deployment(args):
    root = Path(args.state_dir).expanduser().resolve()
    return root, read_json(root / "deployment.json")


def ssh(args, command, **kwargs):
    root, config = deployment(args)
    return run(["ssh", "-F", root / "ssh_config", config["name"], command], **kwargs)


def ensure_config(root, lease):
    gcloud = shutil.which("gcloud")
    if not gcloud:
        raise ValueError("Install the Google Cloud CLI first")
    proxy = shlex.join([gcloud, "compute", "start-iap-tunnel", lease["name"], "22",
                        "--listen-on-stdin", "--project=" + lease["project"],
                        "--zone=" + lease["zone"], "--verbosity=error"])
    content = (f"Host {lease['name']}\n"
               "  HostName 127.0.0.1\n  User codex\n"
               f'  IdentityFile "{root / "id_ed25519"}"\n'
               f'  UserKnownHostsFile "{root / "known_hosts"}"\n'
               f"  HostKeyAlias airlift.{lease['project']}.{lease['name']}\n"
               "  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n"
               "  ServerAliveInterval 30\n  ServerAliveCountMax 3\n"
               "  ConnectTimeout 30\n"
               f"  ProxyCommand {proxy}\n")
    path = root / "ssh_config"
    path.write_text(content)
    path.chmod(0o600)


def deploy(args):
    root = private_dir(Path(args.state_dir).expanduser().resolve())
    requested = make_lease(args.project, args.name, args.monthly_budget, args.codex_version)
    with locked(root / "deployment.json"):
        path = root / "deployment.json"
        lease = validate_existing(read_json(path), requested, update_codex=args.update_codex) if path.exists() else requested
        write_json(path, lease)
        key = root / "id_ed25519"
        if not key.exists():
            run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "codex-airlift", "-f", key], stdout=subprocess.DEVNULL)
        ensure_config(root, lease)
        variables = {
            "project_id": lease["project"], "service_name": lease["name"],
            "ssh_public_key": (root / "id_ed25519.pub").read_text().strip(),
            "codex_version": lease["codex_version"],
            "monthly_compute_budget_usd": float(lease["compute_budget_usd"]),
            "budget_started_at": lease["budget_started_at"], "termination_time": lease["termination_time"],
        }
        write_json(root / "private.tfvars.json", variables)
        infra = source_root(args) / "infra/gcp"
        env = os.environ.copy()
        # Keep provider cache/backend metadata private and separate for every deployment.
        env["TF_DATA_DIR"] = str(private_dir(root / "terraform-data"))
        base = ["terraform", f"-chdir={infra}"]
        run(base + ["init", "-input=false", f"-backend-config=path={root / 'terraform.tfstate'}"], env=env)
        run(base + ["validate"], env=env)
        plan_path = root / "host.tfplan"
        run(base + ["plan", "-input=false", f"-var-file={root / 'private.tfvars.json'}", f"-out={plan_path}"], env=env)
        plan = json.loads(capture(base + ["show", "-json", plan_path], env=env))
        for change in plan.get("resource_changes", []):
            if "delete" in change["change"]["actions"]:
                raise ValueError("Plan would delete/replace a resource; reconcile data and budget before applying")
        if args.apply:
            run(base + ["apply", "-input=false", str(plan_path)], env=env, timeout=1800)
            output = json.loads(capture(base + ["output", "-json", "host"], env=env))
            write_json(root / "deployed.json", output)
        print(json.dumps({"applied": args.apply, "deployment": lease, "private_state": str(root)}, indent=2))


def configure_ssh(args):
    root, lease = deployment(args)
    ensure_config(root, lease)
    ssh_dir = Path.home() / ".ssh"
    private_dir(ssh_dir)
    config = ssh_dir / "config"
    include = f'Include "{root / "ssh_config"}"'
    with locked(config):
        old = config.read_text() if config.exists() else ""
        updated = merge_ssh_config(old, include, (root / "ssh_config").read_text(), lease["name"])
        fd, temporary = tempfile.mkstemp(prefix=".airlift-", dir=ssh_dir)
        with os.fdopen(fd, "w") as stream:
            stream.write(updated)
        os.replace(temporary, config)
    print(f"SSH alias configured: {lease['name']}")


def merge_ssh_config(old, include, block, name):
    begin, end = f"# BEGIN codex-airlift {name}", f"# END codex-airlift {name}"
    # Keep a concrete Host in the root config for clients that discover aliases
    # there instead of recursively parsing Include directives.
    old = "".join(line for line in old.splitlines(keepends=True) if line.rstrip("\r\n") != include)
    if begin in old:
        start = old.index(begin)
        finish = old.find(end, start)
        if finish < 0:
            raise ValueError("Incomplete owned SSH block; preserving the file")
        finish += len(end)
        if old[finish:finish + 1] == "\n":
            finish += 1
        old = old[:start] + old[finish:]
    if re.search(r"(?im)^Host\s+.*\b" + re.escape(name) + r"\b", old):
        raise ValueError("SSH alias already exists outside the owned block; preserving it")
    return begin + "\n" + block + end + "\n" + old


def authorize(args):
    # Stream one explicitly selected private credential over authenticated SSH.
    # Never include its contents in an argument, Terraform, logs, or this repo.
    program = """import json,os,sys,tempfile
from pathlib import Path
p=Path.home()/'.codex';p.mkdir(mode=0o700,exist_ok=True)
data=sys.stdin.buffer.read(1048577)
if len(data)>1048576: raise SystemExit('Credential exceeds size limit')
value=json.loads(data)
if not isinstance(value,dict) or not ('tokens' in value or 'OPENAI_API_KEY' in value): raise SystemExit('Unrecognized Codex credential format')
fd,name=tempfile.mkstemp(dir=p,prefix='.auth-airlift-')
try:
 with os.fdopen(fd,'wb') as f: f.write(data);f.flush();os.fsync(f.fileno())
 os.link(name,p/'auth.json')
finally: os.unlink(name)
print('Credential installed privately; existing credentials were not overwritten')
"""
    with Path(args.auth_file).expanduser().open("rb") as stream:
        ssh(args, "python3 -c " + shlex.quote(program), stdin=stream, timeout=90)


def bootstrap_daemon(args):
    # `daemon bootstrap` also starts a latest-channel updater. Airlift starts the
    # pinned daemon only; systemd owns restart/boot lifecycle on this host.
    ssh(args, "sudo -n systemctl start codex-airlift-daemon.service && codex app-server daemon start", timeout=120)


def doctor(args):
    root, lease = deployment(args)
    validate_existing(lease, lease)
    host = json.loads(capture(["gcloud", "compute", "instances", "describe", lease["name"],
                              "--project=" + lease["project"], "--zone=" + lease["zone"],
                              "--format=json(name,zone,machineType,status,scheduling,labels)", "--quiet"]))
    if host["machineType"].split("/")[-1] != lease["machine_type"]:
        raise ValueError("Live machine type differs from the budget-reviewed shape")
    actual = host.get("scheduling", {}).get("terminationTime", "")
    if not actual or parse_time(actual) != parse_time(lease["termination_time"]):
        raise ValueError("Live GCP shutdown deadline differs from the budget lease")
    print(json.dumps({"live_host": host, "monthly_compute_ceiling_usd": lease["maximum_monthly_compute_usd"]}, indent=2), flush=True)
    checked = ssh(args, "bash -s", input="""set -euo pipefail
test -f /var/lib/codex-airlift-ready
codex --version
codex login status
codex app-server daemon version
test -d ~/projects
""", text=True, capture_output=True, timeout=90)
    print(checked.stdout, end="")
    print(checked.stderr, end="", file=sys.stderr)
    lines = checked.stdout.strip().splitlines()
    server = json.loads(lines[-1])
    if "codex-cli " + lease["codex_version"] not in lines or server.get("appServerVersion") != lease["codex_version"]:
        raise ValueError("CLI/server version differs from the pinned deployment; reconcile before transferring chats")


def prepare_repo(args):
    local = Path(args.path).expanduser().resolve()
    top = Path(capture(["git", "-C", local, "rev-parse", "--show-toplevel"]))
    origin = capture(["git", "-C", top, "config", "--get", "remote.origin.url"])
    name = args.folder or top.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) or name in (".", ".."):
        raise ValueError("Invalid destination repository folder")
    root, lease = deployment(args)
    with tempfile.TemporaryDirectory(prefix="airlift-bundle-", dir=root) as temporary:
        bundle = Path(temporary) / "repository.bundle"
        run(["git", "-C", top, "bundle", "create", bundle, "--all"], timeout=600)
        remote_bundle = "/home/codex/.codex/airlift-" + os.urandom(12).hex() + ".bundle"
        run(["scp", "-F", root / "ssh_config", bundle, lease["name"] + ":" + remote_bundle], timeout=600)
        script = """set -euo pipefail
name=$1; origin=$2; bundle=$3
trap 'rm -f -- "$bundle"' EXIT
target="$HOME/projects/$name"
if test -d "$target/.git"; then
  test "$(git -C "$target" config --get remote.origin.url)" = "$origin"
  git -C "$target" fetch "$bundle" '+refs/heads/*:refs/remotes/airlift/*'
elif test -e "$target"; then
  echo 'Destination exists and is not a repository; preserving it' >&2; exit 1
else
  git clone "$bundle" "$target"
  git -C "$target" remote set-url origin "$origin"
fi
printf '%s\\n' "$target"
"""
        ssh(args, "bash -s -- " + shlex.join([name, origin, remote_bundle]), input=script, text=True, timeout=600)


def install_skills(args):
    root = source_root(args)
    target = Path(args.skills_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("codex-to-cloud", "codex-from-cloud"):
        destination = target / name
        source = root / "skills" / name
        if destination.exists() and (destination / "SKILL.md").read_bytes() != (source / "SKILL.md").read_bytes():
            raise ValueError(f"Existing skill differs: {destination}; preserving it")
        if not destination.exists():
            shutil.copytree(source, destination)
        print(str(destination))


def manifest_command(args):
    path = Path(args.file).expanduser()
    with locked(path):
        if args.action == "plan":
            if path.exists():
                raise ValueError("Manifest exists; reconcile it instead of overwriting")
            result = manifest.plan(read_json(args.inventory), args.source, args.destination,
                                   args.caller, args.include_unloaded)
        else:
            result = read_json(path)
            if args.action == "verify":
                result = manifest.verify_inventory(result, args.thread, read_json(args.inventory))
            elif args.action != "report":
                result = manifest.transition(result, args.thread, args.action, args.operation)
        if args.action != "report":
            write_json(path, result)
    print(json.dumps(result, indent=2))


def probe(args):
    from .probes import disconnect_probe, stop_probe
    root, lease = deployment(args)
    validate_existing(lease, lease)
    if args.kind == "stop":
        stop_probe(root, lease)
    else:
        if not args.remote_cwd and not args.reconcile:
            raise ValueError("Provide a task-owned --remote-cwd for the disconnect probe")
        disconnect_probe(root, lease, args.remote_cwd, reconcile=args.reconcile)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", default="~/.local/state/codex-airlift")
    p.add_argument("--source-dir")
    commands = p.add_subparsers(dest="command", required=True)
    d = commands.add_parser("deploy", help="Plan or apply one budget-bounded host")
    d.add_argument("--project", required=True)
    d.add_argument("--name", default="codex-airlift")
    d.add_argument("--monthly-budget", default="100")
    d.add_argument("--codex-version", required=True)
    d.add_argument("--update-codex", action="store_true", help="Explicitly update the pinned version without extending the compute lease")
    d.add_argument("--apply", action="store_true")
    d.set_defaults(func=deploy)
    for name, function in (("configure-ssh", configure_ssh), ("doctor", doctor), ("bootstrap-daemon", bootstrap_daemon)):
        commands.add_parser(name).set_defaults(func=function)
    d = commands.add_parser("authorize", help="Install explicitly selected Codex credentials over SSH")
    d.add_argument("--auth-file", required=True)
    d.set_defaults(func=authorize)
    d = commands.add_parser("prepare-repo", help="Transfer committed Git objects without cloud Git credentials")
    d.add_argument("path")
    d.add_argument("--folder")
    d.set_defaults(func=prepare_repo)
    d = commands.add_parser("install-skills")
    d.add_argument("--skills-dir", default=str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"))
    d.set_defaults(func=install_skills)
    d = commands.add_parser("manifest", help="Plan and reconcile a batch of native app handoffs")
    d.add_argument("action", choices=["plan", "claim", "submit", "verify", "fail", "report"])
    d.add_argument("--file", required=True)
    d.add_argument("--inventory")
    d.add_argument("--source", default="local")
    d.add_argument("--destination")
    d.add_argument("--caller", default="")
    d.add_argument("--include-unloaded", action="store_true")
    d.add_argument("--thread")
    d.add_argument("--operation")
    d.set_defaults(func=manifest_command)
    d = commands.add_parser("probe", help="Run an opt-in live verification; never use shutdown on a host with user chats")
    d.add_argument("kind", choices=["stop", "disconnect"])
    d.add_argument("--remote-cwd")
    d.add_argument("--reconcile", action="store_true", help="Read the recorded disconnect result without starting another turn")
    d.set_defaults(func=probe)
    return p


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        args.func(args)
    except (ValueError, OSError, EOFError, KeyError, subprocess.SubprocessError) as error:
        print(f"airlift: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
