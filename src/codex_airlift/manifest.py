"""Durable workflow journal. The documented Codex app tool performs each move."""
from datetime import datetime, timezone
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def unwrap(value):
    import json
    if "structuredContent" in value:
        return unwrap(value["structuredContent"])
    if "threads" in value or "pinnedThreads" in value:
        return value
    for block in value.get("content", []):
        if block.get("type") == "text":
            try:
                return unwrap(json.loads(block["text"]))
            except (ValueError, TypeError):
                continue
    raise ValueError("Expected a complete list_threads response")


def threads(inventory):
    data = unwrap(inventory)
    if data.get("unavailableHosts") or data.get("unavailableSources"):
        raise ValueError("Inventory contains unavailable hosts/sources; reconcile before planning")
    found = {}
    for item in data.get("pinnedThreads", []) + data.get("threads", []):
        key = (item.get("hostId", "local"), item.get("id"))
        found[key] = item
    return list(found.values())


def plan(inventory, source, destination, caller, include_unloaded=False):
    if not isinstance(source, str) or not source or not isinstance(destination, str) or not destination:
        raise ValueError("Explicit source and destination host IDs are required")
    if source == destination:
        raise ValueError("Source and destination must differ")
    rows, excluded = [], []
    for item in threads(inventory):
        if item.get("kind", "codex") != "codex" or item.get("hostId", "local") != source:
            continue
        reason = None
        if item["id"] == caller:
            reason = "calling-chat-use-native-UI-last"
        elif item.get("archived"):
            reason = "archived"
        elif item.get("status") == "notLoaded" and not include_unloaded:
            reason = "unloaded-history"
        elif item.get("status") not in ("active", "idle", "notLoaded", "waitingForApproval", "waitingForInput"):
            reason = "unknown-status-preserved"
        if reason:
            excluded.append({"id": item["id"], "title": item.get("title"), "reason": reason})
            continue
        rows.append({"id": item["id"], "title": item.get("title"), "cwd": item.get("cwd"),
                     "original_status": item.get("status"), "state": "pending", "events": []})
    return {"schema": 1, "id": str(uuid.uuid4()), "created_at": now(), "source": source,
            "destination": destination, "threads": rows, "excluded": excluded}


def transition(manifest, thread_id, action, operation_id=None, observed_host=None):
    row = next((r for r in manifest["threads"] if r["id"] == thread_id), None)
    if row is None:
        raise ValueError("Thread is outside this manifest")
    state = row["state"]
    if action == "claim":
        if state != "pending":
            raise ValueError("Already claimed/submitted: reconcile the original operation; do not resubmit")
        if any(r["state"] in ("claimed", "submitted") for r in manifest["threads"]):
            raise ValueError("Another handoff needs reconciliation before continuing")
        row["state"] = "claimed"
    elif action == "submit":
        if state != "claimed" or not operation_id:
            raise ValueError("A claimed row and actual operation ID are required")
        row.update(state="submitted", operation_id=operation_id)
    elif action == "verify":
        if state not in ("claimed", "submitted", "verified") or observed_host != manifest["destination"]:
            raise ValueError("Destination-host readback is required")
        row.update(state="verified", observed_host=observed_host)
    elif action == "fail":
        if state not in ("claimed", "submitted"):
            raise ValueError("Only an attempted handoff can be marked failed")
        row["state"] = "failed"
    else:
        raise ValueError("Unknown transition")
    row["events"].append({"at": now(), "action": action, "operation_id": operation_id,
                          "observed_host": observed_host})
    return manifest


def verify_inventory(manifest, thread_id, inventory):
    matches = [t for t in threads(inventory) if t["id"] == thread_id and t.get("hostId") == manifest["destination"]]
    if len(matches) != 1:
        raise ValueError("Exact thread was not found on the destination")
    return transition(manifest, thread_id, "verify", observed_host=manifest["destination"])
