"""Bounded private transfer archives, without tar extraction side effects."""
from pathlib import Path
import hashlib
import shutil
import tarfile

from .storage import private_dir

MAX_PACKET_BYTES = 6 * 1024 * 1024 * 1024
MAX_MEMBERS = 120000


def pack(folder, destination):
    folder, destination = Path(folder).resolve(), Path(destination)
    total, count = 0, 0
    with tarfile.open(destination, "x") as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(folder):
                raise ValueError("Transfer packet contains a symlink")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Transfer packet contains a special file")
            total += path.stat().st_size
            count += 1
            if total > MAX_PACKET_BYTES or count > MAX_MEMBERS:
                raise ValueError("Transfer packet exceeds its bound")
            archive.add(path, arcname=path.relative_to(folder).as_posix(), recursive=False)
    destination.chmod(0o600)


def unpack(packet, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Packet destination exists; reconcile its operation")
    private_dir(destination)
    total, seen = 0, set()
    with tarfile.open(packet, "r:") as archive:
        for count, member in enumerate(archive):
            relative = Path(member.name)
            if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or member.name in seen):
                raise ValueError("Unsafe or duplicate transfer packet entry")
            seen.add(member.name)
            total += member.size
            if total > MAX_PACKET_BYTES or count >= MAX_MEMBERS or member.size < 0:
                raise ValueError("Transfer packet exceeds its bound")
            target = destination / relative
            if not target.parent.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Packet path escaped its destination")
            private_dir(target.parent)
            source = archive.extractfile(member)
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o600)
    return destination


def verify_contents(packet, folder):
    """Readback for a pre-runtime recovery; never trust a partial extraction."""
    folder = Path(folder).resolve()
    seen, total = set(), 0
    with tarfile.open(packet, "r:") as archive:
        for count, member in enumerate(archive):
            relative = Path(member.name)
            if not member.isfile() or relative.is_absolute() or ".." in relative.parts or member.name in seen:
                raise ValueError("Unsafe retained packet entry")
            seen.add(member.name)
            total += member.size
            if total > MAX_PACKET_BYTES or count >= MAX_MEMBERS:
                raise ValueError("Retained packet exceeds its bound")
            path = folder / relative
            if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size != member.size:
                raise ValueError("Retained packet extraction differs")
            incoming_hash, retained_hash = hashlib.sha256(), hashlib.sha256()
            with archive.extractfile(member) as source, path.open("rb") as retained:
                while block := source.read(1024 * 1024):
                    incoming_hash.update(block)
                while block := retained.read(1024 * 1024):
                    retained_hash.update(block)
            if incoming_hash.digest() != retained_hash.digest():
                raise ValueError("Retained packet extraction changed")
    if {p.relative_to(folder).as_posix() for p in folder.rglob("*") if not p.is_dir()} != seen:
        raise ValueError("Retained packet extraction has extra entries")
