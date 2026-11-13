"""Capture Git before Desktop archive can clean up its managed worktree."""
import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid

from .git_snapshot import snapshot
from .storage import read_json, write_json
from .transfer import desktop_row, operation


def stage(store, batch_id, thread_ids, home):
    result = {}
    for tid in thread_ids:
        row = desktop_row(home, tid)
        if row["archived"]:
            raise ValueError("Stage live Git work before archiving its Desktop chat")
        op_id = str(uuid.uuid5(uuid.UUID(batch_id), tid))
        folder = operation(store, op_id)
        target = folder / "workspace-staged"
        if target.exists():
            raise ValueError("Git staging already exists; inspect it before another capture")
        value = snapshot(row["cwd"], target)
        result[tid] = {"operation_id": op_id, "workspace": str(target), "state": value["state"]}
    write_json(store.base / "staged" / (batch_id + ".json"), result)
    return result


def verify_archived_workspace(bundle):
    """Compare the cache against Desktop's retained public Git snapshot ref.

    This adapter is version-specific and fails closed if its Git artifact is
    absent. It never opens or modifies the desktop app's private registry.
    """
    bundle = Path(bundle)
    value = read_json(bundle / "workspace.json")
    common = value["source_common_git_dir"]
    ref = "refs/codex/snapshots/" + hashlib.sha1(value["source_root"].encode()).hexdigest()
    base = ["git", "--git-dir", common]
    commit = subprocess.check_output(base + ["rev-parse", ref], text=True).strip()
    parents = subprocess.check_output(base + ["show", "-s", "--format=%P", ref], text=True).split()
    if value["state"]["head"] not in [commit, *parents]:
        raise ValueError("Desktop archive HEAD differs from the staged workspace")
    expected = {f["path"]: f for f in value["state"]["files"]}
    seen = set()
    with tempfile.TemporaryFile() as archive_file:
        subprocess.run(base + ["archive", "--format=tar", ref], stdout=archive_file, check=True, timeout=300)
        archive_file.seek(0)
        with tarfile.open(fileobj=archive_file, mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                item = expected.get(member.name)
                if not item:
                    raise ValueError("Desktop archive contains files newer than the Git capture")
                seen.add(member.name)
                if member.issym():
                    if item["type"] != "symlink" or item["target"] != member.linkname:
                        raise ValueError("Desktop archive symlink differs from the Git capture")
                elif member.isfile() and item["type"] == "file":
                    sha = hashlib.sha256()
                    with archive.extractfile(member) as incoming:
                        while data := incoming.read(1024 * 1024):
                            sha.update(data)
                    if sha.hexdigest() != item["sha256"] or bool(member.mode & 0o111) != bool(item["mode"] & 0o111):
                        raise ValueError("Desktop archive file differs from the Git capture")
                else:
                    raise ValueError("Desktop archive entry differs from the Git capture")
    for name, item in expected.items():
        if item["type"] != "missing" and name not in seen and Path(name).parts[0] not in ("artifacts", "outputs", "reports"):
            raise ValueError("Desktop archive is missing a captured workspace file")
    return value
