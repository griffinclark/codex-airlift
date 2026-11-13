"""Portable Git HEAD, index, worktree and nonignored files, with readback."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from .canonical import digest
from .storage import private_dir, write_json

MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 100000


def git(root, *arguments, text=False):
    result = subprocess.run(["git", "-C", str(root), *arguments], check=True,
                            capture_output=True, timeout=300)
    return result.stdout.decode().strip() if text else result.stdout


def safe_relative(value):
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or ".git" in relative.parts:
        raise ValueError("Unsafe workspace path")
    if any(ord(c) < 32 or ord(c) == 127 for c in str(relative)):
        raise ValueError("Workspace filenames contain unsupported control characters")
    return relative


def diff(root, staged=False, legacy=False):
    options = [] if legacy else ["--full-index", "--no-renames", "--no-color", "--unified=3",
                                 "--diff-algorithm=myers", "--src-prefix=a/", "--dst-prefix=b/"]
    config = [] if legacy else ["-c", "core.quotePath=true"]
    return git(root, *config, "diff", *(["--cached"] if staged else []),
               "--binary", "--no-ext-diff", "--no-textconv", *options)


def workspace_files(root):
    tracked = git(root, "ls-files", "--cached", "-z").split(b"\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    values = {os.fsdecode(p) for p in tracked + untracked if p}
    # Retain common saved-output directories even when repository ignores them.
    for name in ("artifacts", "outputs", "reports"):
        folder = root / name
        if folder.is_dir() and not folder.is_symlink():
            for path in folder.rglob("*"):
                relative = path.relative_to(root)
                if any(part in (".git", "node_modules", ".venv", ".terraform", "__pycache__")
                       or part.startswith(".env") for part in relative.parts):
                    continue
                if path.is_file() or path.is_symlink():
                    values.add(relative.as_posix())
    if len(values) > MAX_FILES:
        raise ValueError("Workspace exceeds the bounded file count")
    files, total = [], 0
    for name in sorted(values):
        relative = safe_relative(name)
        path = root / relative
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError("Workspace path traverses an external symlink")
        if path.is_symlink():
            files.append({"path": relative.as_posix(), "type": "symlink", "target": os.readlink(path)})
        elif not path.exists():
            files.append({"path": relative.as_posix(), "type": "missing"})
        elif path.is_file():
            size = path.stat().st_size
            total += size
            if total > MAX_WORKSPACE_BYTES:
                raise ValueError("Workspace exceeds the configured transfer bound")
            files.append({"path": relative.as_posix(), "type": "file", "size": size,
                          "mode": stat.S_IMODE(path.stat().st_mode) & 0o777, "sha256": digest(path)})
        else:
            raise ValueError("Nested repositories, submodules and special files require explicit migration support")
    return files


def state(root, legacy=False):
    root = Path(root).resolve()
    return {"head": git(root, "rev-parse", "HEAD", text=True),
            "staged_sha256": hashlib.sha256(diff(root, True, legacy)).hexdigest(),
            "unstaged_sha256": hashlib.sha256(diff(root, legacy=legacy)).hexdigest(),
            "files": workspace_files(root)}


def snapshot(cwd, destination):
    cwd = Path(cwd).resolve()
    root = Path(git(cwd, "rev-parse", "--show-toplevel", text=True)).resolve()
    destination = private_dir(destination).resolve()
    before = state(root)
    if any(line.startswith(b"160000 ") for line in git(root, "ls-files", "--stage").splitlines()):
        raise ValueError("Submodule worktrees are not supported by this transfer")
    git(root, "bundle", "create", str(destination / "repository.bundle"), "HEAD")
    for staged, name in ((True, "index.patch"), (False, "worktree.patch")):
        (destination / name).write_bytes(diff(root, staged))
    for item in before["files"]:
        if item["type"] != "file":
            continue
        target = destination / "files" / item["path"]
        private_dir(target.parent)
        shutil.copyfile(root / item["path"], target)
        target.chmod(0o600)
        if digest(target) != item["sha256"]:
            raise ValueError("Workspace changed during file capture")
    after = state(root)
    if before != after:
        raise ValueError("Workspace changed during capture; no inconsistent handoff")
    try:
        origin = git(root, "remote", "get-url", "origin", text=True)
    except subprocess.CalledProcessError:
        origin = ""
    manifest = {"schema": 2, "source_root": str(root), "relative_cwd": cwd.relative_to(root).as_posix(),
                "source_common_git_dir": git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", text=True),
                "origin": origin, "branch": git(root, "branch", "--show-current", text=True),
                "state": before,
                "bundle_sha256": digest(destination / "repository.bundle"),
                "index_patch_sha256": digest(destination / "index.patch"),
                "worktree_patch_sha256": digest(destination / "worktree.patch")}
    write_json(destination / "workspace.json", manifest)
    return manifest


def restore(bundle, destination):
    bundle, destination = Path(bundle).resolve(), Path(destination)
    manifest = json.loads((bundle / "workspace.json").read_text())
    if manifest.get("schema") not in (1, 2):
        raise ValueError("Unsupported workspace snapshot")
    if manifest["relative_cwd"] != ".":
        safe_relative(manifest["relative_cwd"])
    for name, key in (("repository.bundle", "bundle_sha256"), ("index.patch", "index_patch_sha256"),
                      ("worktree.patch", "worktree_patch_sha256")):
        if (bundle / name).is_symlink() or digest(bundle / name) != manifest[key]:
            raise ValueError("Workspace snapshot hash differs")
    if destination.exists():
        raise ValueError("Workspace destination already exists; reconcile the original import")
    private_dir(destination.parent)
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-checkout",
                    str(bundle / "repository.bundle"), str(destination)], check=True,
                   capture_output=True, timeout=300, env=env)
    git(destination, "config", "core.hooksPath", "/dev/null")
    git(destination, "checkout", "--detach", manifest["state"]["head"])
    if manifest["branch"]:
        git(destination, "check-ref-format", "--branch", manifest["branch"])
        git(destination, "checkout", "-b", manifest["branch"])
    if manifest["origin"]:
        git(destination, "remote", "set-url", "origin", manifest["origin"])
    for name, options in (("index.patch", ["--index"]), ("worktree.patch", [])):
        if (bundle / name).stat().st_size:
            git(destination, "apply", *options, "--binary", str(bundle / name))
    for item in manifest["state"]["files"]:
        relative = safe_relative(item["path"])
        target = destination / relative
        if not target.parent.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Workspace import would traverse an external symlink")
        if item["type"] == "missing":
            if target.is_file() or target.is_symlink():
                target.unlink()
            continue
        private_dir(target.parent) if not target.parent.exists() else None
        if target.is_symlink():
            target.unlink()
        if item["type"] == "symlink":
            if target.exists():
                target.unlink()
            target.symlink_to(item["target"])
        elif item["type"] == "file":
            source = bundle / "files" / relative
            if source.is_symlink() or not source.resolve().is_relative_to(bundle) or digest(source) != item["sha256"]:
                raise ValueError("Workspace file hash differs")
            shutil.copyfile(source, target)
            target.chmod(item["mode"] & 0o777)
        else:
            raise ValueError("Unsupported workspace entry")
    if state(destination, legacy=manifest["schema"] == 1) != manifest["state"]:
        raise ValueError("Restored HEAD, index, worktree or file hashes differ")
    # Checkout/import must not execute hooks. Once verified, normal future Git
    # operations can use the destination user's configured hooks and policies.
    git(destination, "config", "--unset", "core.hooksPath")
    return destination / manifest["relative_cwd"], manifest
