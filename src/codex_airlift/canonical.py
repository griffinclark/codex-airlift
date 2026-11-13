"""Hash-checked canonical history bundles; never read or copy a live database."""
import hashlib
import json
from pathlib import Path
import re
import shutil

from .storage import private_dir, write_json

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_LINE_BYTES = 32 * 1024 * 1024
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
UUID_IN_NAME = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def digest(path, length=None):
    result = hashlib.sha256()
    remaining = length
    with Path(path).open("rb") as stream:
        while remaining is None or remaining:
            block = stream.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
            if not block:
                if remaining:
                    raise ValueError("Canonical file became shorter during export")
                break
            result.update(block)
            if remaining is not None:
                remaining -= len(block)
    return result.hexdigest()


def header(path):
    with Path(path).open("rb") as stream:
        line = stream.readline(MAX_LINE_BYTES + 1)
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise ValueError("Canonical session header is incomplete or too large")
    value = json.loads(line)
    if value.get("type") != "session_meta" or not UUID.fullmatch(value.get("payload", {}).get("id", "")):
        raise ValueError("Unrecognized canonical session header")
    return value


def prefix_boundary(path, length):
    """Validate complete JSONL bytes and return the first/last durable ordinal."""
    if not 0 < length <= MAX_FILE_BYTES:
        raise ValueError("Canonical file exceeds the per-file bound")
    offset, first, last = 0, None, None
    with Path(path).open("rb") as stream:
        while offset < length:
            line = stream.readline(min(MAX_LINE_BYTES + 1, length - offset))
            if not line or not line.endswith(b"\n") or len(line) > MAX_LINE_BYTES:
                raise ValueError("Canonical prefix does not end on a complete record")
            record = json.loads(line)
            ordinal = record.get("ordinal")
            if ordinal is not None:
                if not isinstance(ordinal, int) or ordinal < 0 or (last is not None and ordinal != last + 1):
                    raise ValueError("Canonical ordinals are not contiguous")
                if first is None:
                    first = ordinal
                last = ordinal
            offset += len(line)
    if offset != length:
        raise ValueError("Canonical prefix byte boundary differs")
    return first, last


class CanonicalExporter:
    def __init__(self, codex_home):
        self.home = Path(codex_home).resolve()
        self.roots = [self.home / "sessions", self.home / "archived_sessions"]
        self.paths = None

    def checked(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Canonical history must be a regular file")
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError("Canonical history path is outside this host's session directories")
        return resolved

    def candidates(self, thread_id):
        if not UUID.fullmatch(thread_id):
            raise ValueError("Invalid canonical history dependency ID")
        if self.paths is None:
            self.paths = [p for root in self.roots if root.exists() for p in root.rglob("*.jsonl")]
        for path in self.paths:
            identifiers = UUID_IN_NAME.findall(path.name)
            if identifiers and identifiers[-1] == thread_id and not path.is_symlink():
                path = self.checked(path)
                yield path

    def resolve_base(self, base):
        end, length = base["end_ordinal_exclusive"], base["end_byte_offset"]
        if not isinstance(end, int) or end <= 0 or not isinstance(length, int):
            raise ValueError("Unrecognized canonical history-base boundary")
        matches = []
        for path in self.candidates(base["thread_id"]):
            if path.stat().st_size < length:
                continue
            first = header(path).get("ordinal")
            if first is None or first >= end:
                continue
            try:
                _, last = prefix_boundary(path, length)
            except (ValueError, json.JSONDecodeError):
                continue
            if last == end - 1:
                matches.append((path, digest(path, length)))
        if not matches or len({sha for _, sha in matches}) != 1:
            raise ValueError("Canonical history base is missing or ambiguous; no partial history export")
        return min(matches, key=lambda item: str(item[0]))[0], length

    def export(self, current_path, destination):
        destination = private_dir(destination).resolve()
        current_path = self.checked(current_path)
        current = header(current_path)
        files, visiting, visited = {}, set(), set()
        total = 0

        def collect(path, length):
            nonlocal total
            node = (str(path), length)
            if node in visiting:
                raise ValueError("Canonical history dependency cycle")
            if node in visited:
                return
            visiting.add(node)
            meta = header(path)
            base = meta["payload"].get("history_base")
            if base:
                collect(*self.resolve_base(base))
            prefix_boundary(path, length)
            match = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", path.name)
            if not match:
                raise ValueError("Unrecognized canonical rollout filename")
            relative = Path("sessions", *match.groups(), path.name)
            sha = digest(path, length)
            if relative.as_posix() in files and files[relative.as_posix()]["sha256"] != sha:
                raise ValueError("Canonical history dependency filename collision")
            target = destination / relative
            private_dir(target.parent)
            if target.exists():
                raise ValueError("Export staging already contains history; reconcile its receipt")
            with path.open("rb") as source, target.open("xb") as output:
                remaining = length
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Canonical file changed during export")
                    output.write(block)
                    remaining -= len(block)
            target.chmod(0o600)
            if digest(target) != sha or digest(path, length) != sha:
                raise ValueError("Canonical history changed during export")
            total += length
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("Canonical bundle exceeds the size bound")
            files[relative.as_posix()] = {"path": relative.as_posix(), "size": length, "sha256": sha,
                                          "thread_id": meta["payload"]["id"]}
            visiting.remove(node)
            visited.add(node)

        collect(current_path, current_path.stat().st_size)
        current_relative = next(item["path"] for item in files.values()
                                if Path(item["path"]).name == current_path.name)
        result = {"schema": 1, "thread_id": current["payload"]["id"], "current": current_relative,
                  "files": list(files.values()), "bytes": total}
        write_json(destination / "canonical.json", result)
        return result


def portable_history(bundle):
    """Make a self-contained transport copy; keep every original lineage file.

    Only session headers and outer durable ordinals change. Model items,
    encrypted reasoning, tool records, turn IDs and their order stay intact.
    This avoids depending on another host's derived SQLite ancestry projection.
    """
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "canonical.json").read_text())
    meta = header(bundle / manifest["current"])
    output = bundle / "portable.jsonl"
    if output.exists():
        raise ValueError("Portable history already exists; reuse its manifest instead of regenerating")
    for item in manifest["files"]:
        source = bundle / item["path"]
        if source.is_symlink() or not source.resolve().is_relative_to(bundle):
            raise ValueError("Unsafe canonical bundle source")
        if source.stat().st_size != item["size"] or digest(source) != item["sha256"]:
            raise ValueError("Canonical lineage changed before transport materialization")
    meta["payload"].pop("history_base", None)
    # Ordinal bookmarks refer to the logical history, including old headers.
    bookmarks = {key: value for key, value in meta["payload"].items()
                 if key in ("subagent_history_start_ordinal", "forked_from_ordinal_exclusive")
                 and isinstance(value, int)}
    before = {key: 1 for key in bookmarks}
    for item in manifest["files"]:
        with (bundle / item["path"]).open() as source:
            for line in source:
                record = json.loads(line)
                if record["type"] != "session_meta":
                    for key, value in bookmarks.items():
                        if record.get("ordinal", 0) < value:
                            before[key] += 1
    meta["payload"].update(before)
    meta["ordinal"] = 0
    ordinal, content_hash = 1, hashlib.sha256()
    with output.open("xb") as target:
        target.write((json.dumps(meta, separators=(",", ":")) + "\n").encode())
        for item in manifest["files"]:
            with (bundle / item["path"]).open() as source:
                for line in source:
                    record = json.loads(line)
                    if record["type"] == "session_meta":
                        continue
                    record.pop("ordinal", None)
                    content_hash.update((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
                    record["ordinal"] = ordinal
                    ordinal += 1
                    target.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
    output.chmod(0o600)
    result = {"schema": 1, "thread_id": manifest["thread_id"], "original_current": manifest["current"],
              "path": "portable.jsonl", "size": output.stat().st_size, "sha256": digest(output),
              "content_sha256": content_hash.hexdigest(), "model_records": ordinal - 1,
              "source_files": len(manifest["files"])}
    write_json(bundle / "portable.json", result)
    return result


def import_portable(bundle, codex_home, cwd=None, workspace_root=None):
    """Import into a fresh task-owned runtime, retaining the chat's exact ID."""
    bundle, home = Path(bundle).resolve(), private_dir(codex_home).resolve()
    manifest = json.loads((bundle / "portable.json").read_text())
    source = bundle / "portable.jsonl"
    if manifest.get("schema") != 1 or source.is_symlink() or source.stat().st_size != manifest["size"]:
        raise ValueError("Unsupported portable history")
    if manifest["size"] > MAX_BUNDLE_BYTES or digest(source) != manifest["sha256"]:
        raise ValueError("Portable history hash or size differs")
    meta = header(source)
    if meta["payload"]["id"] != manifest["thread_id"] or meta["payload"].get("history_base"):
        raise ValueError("Portable history identity or ancestry differs")
    relative = Path(manifest["original_current"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "sessions":
        raise ValueError("Unsafe portable history path")
    target = home / relative
    if not target.parent.resolve().is_relative_to(home):
        raise ValueError("Unsafe portable destination")
    private_dir(target.parent)
    # Change only the environment header in this host's execution copy. Raw
    # lineage and the portable bundle stay immutable for readback and rollback.
    mapped = dict(meta)
    mapped["payload"] = dict(meta["payload"])
    if cwd is not None:
        mapped["payload"]["cwd"] = str(cwd)
        mapped["payload"]["runtime_workspace_roots"] = [str(workspace_root or cwd)]
    first = (json.dumps(mapped, separators=(",", ":")) + "\n").encode()
    if target.exists():
        if target.is_symlink() or header(target) != mapped:
            raise ValueError("Destination already has different history; use a fresh handoff runtime")
        with source.open("rb") as incoming, target.open("rb") as existing:
            incoming.readline(MAX_LINE_BYTES + 1)
            existing.readline(MAX_LINE_BYTES + 1)
            while True:
                block = incoming.read(1024 * 1024)
                if existing.read(1024 * 1024) != block:
                    raise ValueError("Destination already has different model records")
                if not block:
                    break
    else:
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            incoming.readline(MAX_LINE_BYTES + 1)
            outgoing.write(first)
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        target.chmod(0o600)
    return target, manifest


def import_canonical(bundle, codex_home):
    bundle, home = Path(bundle).resolve(), private_dir(codex_home).resolve()
    manifest = json.loads((bundle / "canonical.json").read_text())
    if manifest.get("schema") != 1 or not UUID.fullmatch(manifest.get("thread_id", "")):
        raise ValueError("Unsupported canonical bundle")
    total = 0
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "sessions":
            raise ValueError("Unsafe canonical import path")
        source, target = bundle / relative, home / relative
        if source.is_symlink() or not source.resolve().is_relative_to(bundle):
            raise ValueError("Unsafe canonical source")
        total += item["size"]
        if total > MAX_BUNDLE_BYTES or not 0 < item["size"] <= MAX_FILE_BYTES:
            raise ValueError("Canonical import exceeds the size bound")
        if source.stat().st_size != item["size"] or digest(source) != item["sha256"]:
            raise ValueError("Canonical bundle hash differs")
        if target.is_symlink() or not target.resolve().is_relative_to(home) or not target.parent.resolve().is_relative_to(home):
            raise ValueError("Unsafe canonical target")
        private_dir(target.parent)
        if target.exists():
            # An ancestry dependency may already have newer append-only records.
            if target.stat().st_size < item["size"] or digest(target, item["size"]) != item["sha256"]:
                raise ValueError("Destination canonical history conflicts; preserving both hosts")
            if item["path"] == manifest["current"] and target.stat().st_size != item["size"]:
                raise ValueError("Destination has newer current history; reconcile before importing")
        else:
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            target.chmod(0o600)
    current = home / manifest["current"]
    if not any(item["path"] == manifest["current"] for item in manifest["files"]):
        raise ValueError("Canonical bundle has no current history")
    if header(current)["payload"]["id"] != manifest["thread_id"]:
        raise ValueError("Canonical thread identity differs")
    return current, manifest
