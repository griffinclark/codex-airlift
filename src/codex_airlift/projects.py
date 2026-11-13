"""Idempotent public app-server project registration and desktop readback."""
import hashlib
import json
from pathlib import PurePosixPath

from .storage import locked, read_json, write_json


def project_key(lease, remote_path):
    path = PurePosixPath(remote_path)
    if (not remote_path.startswith("/home/codex/projects/") or str(path) != remote_path
            or ".." in path.parts or any(ord(c) < 32 for c in remote_path)):
        raise ValueError("Provide a normalized repository path under /home/codex/projects")
    identity = "\0".join((lease["project"], lease["name"], remote_path))
    return "airlift-project-" + hashlib.sha256(identity.encode()).hexdigest()


def list_projects(client):
    projects, cursor, seen = [], None, set()
    for _ in range(100):
        page = client.request("project/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
        projects.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            return projects
        if cursor in seen:
            raise ValueError("Project pagination did not advance")
        seen.add(cursor)
    raise ValueError("Project inventory exceeds the bounded registration scan")


def matches_root(project, remote_path):
    return project.get("roots") == [{"path": remote_path}]


def readback_project(client, project_id, remote_path):
    saved = client.request("project/read", {"projectId": project_id})["project"]
    if saved.get("id") != project_id or not matches_root(saved, remote_path):
        raise ValueError("Saved project readback does not match; setup remains incomplete")
    return saved


def register_project(client, journal, lease, remote_path, name):
    """Persist the operation key before create; a lost response reuses that key."""
    key = project_key(lease, remote_path)
    if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Provide a project name of 1–120 printable characters")
    with locked(journal):
        record = read_json(journal) if journal.exists() else {
            "schema": 1, "idempotency_key": key, "remote_path": remote_path,
            "name": name, "status": "prepared", "desktop_status": "unverified",
        }
        if (record["idempotency_key"], record["remote_path"], record["name"]) != (key, remote_path, name):
            raise ValueError("Project registration parameters differ from the existing receipt")
        # Never carry an old desktop verification into a new registration attempt.
        record["desktop_status"] = "unverified"
        record.pop("desktop_verified_at", None)
        record.pop("desktop_project_id", None)
        write_json(journal, record)
        matches = [p for p in list_projects(client) if matches_root(p, remote_path)]
        if len(matches) > 1:
            raise ValueError("Multiple projects have the requested root; no project was changed")
        if matches:
            project = matches[0]
            if record.get("project_id") and record["project_id"] != project["id"]:
                raise ValueError("The registered project identity changed; reconcile before continuing")
        elif record.get("project_id"):
            raise ValueError("The recorded project is missing or its roots changed; preserving user intent")
        else:
            record["status"] = "create-pending"
            write_json(journal, record)
            project = client.request("project/create", {
                "idempotencyKey": key, "name": name, "roots": [{"path": remote_path}],
            })["project"]
        if not isinstance(project.get("id"), str) or not project["id"] or not matches_root(project, remote_path):
            raise ValueError("Project response does not match the requested root")
        record.update(project_id=project["id"], status="readback-pending")
        write_json(journal, record)
        saved = readback_project(client, project["id"], remote_path)
        record.update(status="server-registered", project=saved)
        write_json(journal, record)
        return record


def unwrap_projects(value):
    if isinstance(value, dict) and value.get("isError"):
        raise ValueError("Desktop project inventory failed")
    if isinstance(value, dict) and isinstance(value.get("projects"), list):
        return value
    if isinstance(value, dict) and "structuredContent" in value:
        return unwrap_projects(value["structuredContent"])
    for block in value.get("content", []) if isinstance(value, dict) else []:
        if block.get("type") == "text":
            try:
                return unwrap_projects(json.loads(block["text"]))
            except (ValueError, TypeError):
                continue
    raise ValueError("Expected a fresh native Codex list_projects response")


def verify_desktop_project(record, inventory, host_id):
    if record.get("status") != "server-registered" or not host_id or host_id == "local":
        raise ValueError("A registered server project and explicit remote host ID are required")
    data = unwrap_projects(inventory)
    if data.get("unavailableHosts"):
        raise ValueError("Desktop project inventory contains unavailable hosts")
    matches = [p for p in data["projects"] if p.get("hostId") == host_id
               and p.get("path") == record["remote_path"] and p.get("isGitRepository") is True]
    if len(matches) != 1 or not matches[0].get("projectId"):
        raise ValueError("Desktop has not recognized the remote project; unattended setup is incomplete")
    return dict(record, desktop_status="registered", desktop_host_id=host_id,
                desktop_project_id=matches[0]["projectId"])
