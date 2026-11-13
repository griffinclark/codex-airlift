"""Recoverable canonical-history and Git transfers between Airlift runtimes."""
import json
import os
from pathlib import Path
import sqlite3
import shutil

from .canonical import CanonicalExporter, digest, import_portable, portable_history
from .git_snapshot import restore, snapshot, state
from .packet import pack, unpack, verify_contents
from .portable import history_fingerprint, read_turns
from .rpc import RpcClient
from .runtime import identifier
from .storage import locked, private_dir, read_json, write_json


def operation(store, operation_id):
    return private_dir(store.base / "operations" / identifier(operation_id))


def settings_from_row(row):
    modes = {"disabled": "danger-full-access", "danger-full-access": "danger-full-access",
             "read-only": "read-only", "workspace-write": "workspace-write"}
    policy = json.loads(row["sandbox_policy"])
    mode = modes.get(policy.get("type"))
    if not mode:
        raise ValueError("Source sandbox needs an explicit portable mapping")
    if any(k not in ("type", "writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp") for k in policy):
        raise ValueError("Unknown source sandbox fields need explicit reconciliation")
    if mode == "workspace-write" and (policy.get("writable_roots") or policy.get("network_access")
                                      or policy.get("exclude_tmpdir_env_var") or policy.get("exclude_slash_tmp")):
        raise ValueError("Custom workspace sandbox roots/network need reconciliation before adoption")
    approval = row["approval_mode"]
    if approval not in ("untrusted", "on-request", "never", "on-failure"):
        raise ValueError("Source approval policy needs an explicit portable mapping")
    return {"model": row["model"], "reasoningEffort": row["reasoning_effort"],
            "sandbox": mode, "approvalPolicy": approval}


def settings_from_response(value):
    policy = value["sandbox"]
    modes = {"dangerFullAccess": "danger-full-access", "readOnly": "read-only", "workspaceWrite": "workspace-write"}
    mode = modes.get(policy["type"])
    if not mode or policy.get("networkAccess") or policy.get("writableRoots") or policy.get("excludeSlashTmp") or policy.get("excludeTmpdirEnvVar"):
        raise ValueError("Current sandbox settings need explicit portable reconciliation")
    return {"model": value["model"], "reasoningEffort": value.get("reasoningEffort"),
            "sandbox": mode, "approvalPolicy": value["approvalPolicy"]}


def resume_parameters(thread_id, cwd, settings, path=None):
    value = {"threadId": thread_id, "cwd": str(cwd), "excludeTurns": True}
    value.update({key: settings[key] for key in ("model", "sandbox", "approvalPolicy") if settings.get(key) is not None})
    if settings.get("reasoningEffort") is not None:
        value["config"] = {"model_reasoning_effort": settings["reasoningEffort"]}
    if path is not None:
        value["path"] = str(path)
    return value


def verify_settings(effective, expected):
    actual = settings_from_response(effective)
    if any(actual[key] != value for key, value in expected.items() if value is not None):
        raise ValueError("Effective model, reasoning or permission settings differ from the source")
    return actual


def desktop_row(home, thread_id):
    identifier(thread_id)
    with sqlite3.connect((Path(home) / "state_5.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None:
        raise ValueError("Source chat is missing")
    return dict(row)


def build_export(store, op_id, thread_id, source_home, path, cwd, title, settings, turns, source,
                 staged_workspace=None, reconcile_workspace=False):
    folder = operation(store, op_id)
    receipt = folder / "export.json"
    if receipt.exists():
        result = read_json(receipt)
        if result["thread_id"] != thread_id or result["source"] != source:
            raise ValueError("Operation belongs to a different chat or source")
        if result["state"] == "exported":
            if digest(folder / "packet.tar") != result["packet_sha256"]:
                raise ValueError("Retained export packet changed")
            return result
        if not (reconcile_workspace and result["state"] == "preparing" and
                not (folder / "packet.tar").exists() and not (folder / "payload/workspace").exists()):
            raise ValueError("Incomplete source export; inspect it before using a new operation")
    write_json(receipt, {"state": "preparing", "thread_id": thread_id, "source": source})
    payload = private_dir(folder / "payload")
    if (payload / "history/portable.json").exists():
        history = read_json(payload / "history/portable.json")
    else:
        CanonicalExporter(source_home).export(path, payload / "history")
        history = portable_history(payload / "history")
    if staged_workspace:
        from .desktop_stage import verify_archived_workspace
        workspace = verify_archived_workspace(staged_workspace)
        shutil.copytree(staged_workspace, payload / "workspace")
    else:
        workspace = snapshot(cwd, payload / "workspace")
    metadata = {"schema": 2, "operation_id": op_id, "thread_id": thread_id, "source": source,
                "title": title, "settings": settings, "cwd": str(cwd),
                "history_fingerprint": history_fingerprint(turns), "turn_ids": [t["id"] for t in turns],
                "workspace_state": workspace["state"], "history": history}
    write_json(payload / "transfer.json", metadata)
    pack(payload, folder / "packet.tar")
    result = {"state": "exported", "thread_id": thread_id, "source": source,
              "operation_id": op_id, "packet": str(folder / "packet.tar"),
              "packet_sha256": digest(folder / "packet.tar"), "turn_ids": metadata["turn_ids"]}
    write_json(receipt, result)
    return result


def export_desktop(store, op_id, thread_id, home, binary, workspace_override=None, reconcile_workspace=False):
    """Adopt after Codex's public archive tool has stopped the source."""
    folder = operation(store, op_id)
    with locked(folder / "export.json"):
        if (folder / "export.json").exists():
            result = read_json(folder / "export.json")
            if result.get("state") == "exported" and result.get("thread_id") == thread_id:
                return result
            if not reconcile_workspace or result.get("state") != "preparing":
                raise ValueError("Reconcile the retained adoption operation")
        row = desktop_row(home, thread_id)
        if not row["archived"] or not Path(row["rollout_path"]).resolve().is_relative_to(Path(home).resolve() / "archived_sessions"):
            raise ValueError("Archive this chat with Codex's public archive tool before adopting it")
        settings = settings_from_row(row)
        with RpcClient([str(binary), "app-server", "--stdio"], env=dict(os.environ, CODEX_HOME=str(home))) as client:
            turns = read_turns(client, thread_id)
        cwd = workspace_override or row["cwd"]
        staged = folder / "workspace-staged" if not Path(cwd).is_dir() else None
        if staged and not staged.is_dir():
            raise ValueError("Desktop cleaned up this checkout; recover its saved Git snapshot or stage it before archive")
        return build_export(store, op_id, thread_id, home, row["rollout_path"], cwd,
                            row.get("name") or row["title"], settings, turns, "desktop",
                            staged_workspace=staged, reconcile_workspace=reconcile_workspace)


def export_runtime(store, op_id, thread_id):
    folder = operation(store, op_id)
    with locked(folder / "export.json"):
        if (folder / "export.json").exists():
            result = read_json(folder / "export.json")
            if result.get("state") == "exported" and result.get("thread_id") == thread_id:
                return result
            raise ValueError("Reconcile the retained source operation")
        record = store.catalog()["threads"].get(thread_id)
        if not record or (record.get("state") != "ready" and not (
                record.get("state") == "suspended" and record.get("outgoing_operation") == op_id)):
            raise ValueError("This host does not own the selected chat or outgoing operation")
        if record["state"] == "ready" and desktop_row(store.info(record["runtime_id"])["home"], thread_id)["archived"]:
            store.record(thread_id, dict(record, state="archived"))
            raise ValueError("The source was archived outside Airlift; preserving that human stop")
        already_archived = False
        if record["state"] == "suspended":
            already_archived = bool(desktop_row(store.info(record["runtime_id"])["home"], thread_id)["archived"])
            if not already_archived:
                raise ValueError("Source archive result is uncertain; reconcile without resubmitting")
        # Suspend before archive. An uncertain result cannot free the launcher
        # to resume a second source executor.
        store.record(thread_id, dict(record, state="suspended", outgoing_operation=op_id))
        with store.rpc(record["runtime_id"]) as client:
            queue = client.request("thread/queue/list", {"threadId": thread_id, "limit": 100})
            if queue.get("data") or queue.get("items") or queue.get("nextCursor"):
                store.record(thread_id, record)
                raise ValueError("Queued input needs a human decision before transfer")
            if not already_archived:
                effective = client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                record["settings"] = settings_from_response(effective)
                store.record(thread_id, dict(record, state="suspended", outgoing_operation=op_id))
                client.request("thread/archive", {"threadId": thread_id})
            info = client.request("thread/read", {"threadId": thread_id})["thread"]
            turns = read_turns(client, thread_id)
        row = desktop_row(store.info(record["runtime_id"])["home"], thread_id)
        if not row["archived"]:
            raise ValueError("Source archive did not become durable")
        return build_export(store, op_id, thread_id, store.info(record["runtime_id"])["home"],
                            row["rollout_path"], record["cwd"], info.get("name") or record["title"],
                            record["settings"], turns, "airlift")


def receive(store, op_id, packet, sha256, reconcile_workspace=False):
    folder = operation(store, op_id)
    receipt = folder / "import.json"
    with locked(receipt):
        recovering = False
        if receipt.exists():
            result = read_json(receipt)
            if result.get("packet_sha256") != sha256:
                raise ValueError("Operation packet differs from the original")
            if result["state"] == "verified":
                return result
            if not reconcile_workspace or result["state"] != "importing" or Path(store.info(op_id)["home"]).parent.exists():
                raise ValueError("Incomplete destination import; inspect retained state before retrying")
            # A narrowly scoped recovery: no runtime or resume request exists.
            # Read back the existing workspace before staging any execution.
            recovering = True
        if digest(packet) != sha256:
            raise ValueError("Transport packet hash differs")
        payload = folder / "incoming" if recovering else unpack(packet, folder / "incoming")
        if recovering:
            verify_contents(packet, payload)
        metadata = read_json(payload / "transfer.json")
        if metadata.get("schema") != 2 or metadata["operation_id"] != op_id:
            raise ValueError("Transfer identity differs")
        thread_id = identifier(metadata["thread_id"])
        previous = store.catalog()["threads"].get(thread_id)
        if previous and previous.get("state") != "suspended":
            raise ValueError("Destination already owns this chat; refusing a duplicate executor")
        write_json(receipt, {"state": "importing", "thread_id": thread_id, "packet_sha256": sha256})
        if recovering:
            workspace = read_json(payload / "workspace/workspace.json")
            if state(folder / "workspace", legacy=workspace["schema"] == 1) != workspace["state"]:
                raise ValueError("Retained workspace still differs; no execution was staged")
            cwd = folder / "workspace" / workspace["relative_cwd"]
        else:
            cwd, workspace = restore(payload / "workspace", folder / "workspace")
        if workspace["state"] != metadata["workspace_state"]:
            raise ValueError("Workspace metadata differs")
        runtime_id = op_id
        history_path, _ = import_portable(payload / "history", store.info(runtime_id)["home"],
                                         cwd=cwd, workspace_root=folder / "workspace")
        store.configure(runtime_id, metadata["settings"])
        store.ensure(runtime_id)
        with store.rpc(runtime_id) as client:
            params = resume_parameters(thread_id, cwd, metadata["settings"], history_path)
            resumed = client.request("thread/resume", params)
            if resumed["thread"]["id"] != thread_id:
                raise ValueError("Destination chat identity differs")
            verify_settings(resumed, metadata["settings"])
            client.request("thread/name/set", {"threadId": thread_id, "name": metadata["title"]})
            turns = read_turns(client, thread_id)
            if history_fingerprint(turns) != metadata["history_fingerprint"]:
                raise ValueError("Destination history differs from the archived source")
        if state(folder / "workspace", legacy=workspace["schema"] == 1) != workspace["state"]:
            raise ValueError("Destination Git readback differs")
        record = {"state": "ready", "thread_id": thread_id, "runtime_id": runtime_id,
                  "cwd": str(cwd), "title": metadata["title"], "settings": metadata["settings"],
                  "incoming_operation": op_id, "turn_ids": metadata["turn_ids"],
                  "history_fingerprint": metadata["history_fingerprint"]}
        store.record(thread_id, record)
        result = dict(record, state="verified", packet_sha256=sha256, operation_id=op_id)
        write_json(receipt, result)
        return result
