"""Opt-in live checks, restricted to a configured, otherwise-empty host."""
from datetime import timedelta
import json
import os
import shlex
import subprocess
import time

from .budget import timestamp, utcnow
from .rpc import RpcClient
from .storage import write_json


def remote_rpc(root, lease):
    return RpcClient(["ssh", "-F", str(root / "ssh_config"), lease["name"], "codex app-server proxy"], websocket=True)


def gcloud(lease, operation, *arguments, check=True):
    result = subprocess.run(["gcloud", "compute", "instances", operation, lease["name"],
                           "--project=" + lease["project"], "--zone=" + lease["zone"],
                           "--quiet", *arguments], capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise ValueError(f"GCP {operation} failed: {result.stderr[-3000:]}")
    return result


def stop_probe(root, lease):
    if (root / "stop-probe.json").exists():
        raise ValueError("A shutdown probe receipt exists; reconcile it before another live test")
    with remote_rpc(root, lease) as client:
        existing = client.request("thread/list", {"limit": 1})
        if existing.get("data"):
            raise ValueError("Shutdown test requires a host with no non-archived chats")
    receipt = {"started_at": timestamp(utcnow()), "original_deadline": lease["termination_time"],
               "checks": []}
    path = root / "stop-probe.json"
    write_json(path, receipt)
    try:
        # GCP requires TERMINATED before a schedule update. Calculate the short
        # deadline after this preparation, so shutdown latency cannot consume it.
        gcloud(lease, "stop")
        receipt["test_deadline"] = timestamp(utcnow() + timedelta(seconds=120))
        write_json(path, receipt)
        gcloud(lease, "set-scheduling", "--termination-time=" + receipt["test_deadline"],
               "--instance-termination-action=STOP")
        gcloud(lease, "start")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = gcloud(lease, "describe", "--format=value(status)")
            if result.stdout.strip() == "TERMINATED":
                receipt["checks"].append("GCP automatically stopped the VM")
                break
            time.sleep(10)
        else:
            raise ValueError("GCP did not stop the VM before the probe timeout")
        attempted = gcloud(lease, "start", check=False)
        if attempted.returncode == 0:
            gcloud(lease, "stop")
            raise ValueError("VM unexpectedly restarted after its absolute deadline")
        # Require the expected cause, not an unrelated permission/network failure.
        error = attempted.stderr.lower()
        if "termination" not in error or not any(term in error for term in ("past", "future")):
            raise ValueError("Restart failed for an unverified reason: " + attempted.stderr[-1500:])
        receipt["restart_rejection"] = attempted.stderr
        receipt["checks"].append("GCP rejected restart after the expired deadline")
    finally:
        current = gcloud(lease, "describe", "--format=value(status)").stdout.strip()
        if current != "TERMINATED":
            gcloud(lease, "stop")
        gcloud(lease, "set-scheduling", "--termination-time=" + lease["termination_time"],
               "--instance-termination-action=STOP")
        receipt["restored_deadline"] = lease["termination_time"]
        write_json(path, receipt)
    gcloud(lease, "start")
    receipt["checks"].append("Restored the original deadline and started the same VM")
    receipt["completed_at"] = timestamp(utcnow())
    write_json(path, receipt)
    print(json.dumps(receipt, indent=2))


def disconnect_probe(root, lease, cwd):
    if (root / "disconnect-probe.json").exists():
        raise ValueError("A disconnect probe receipt exists; inspect that thread before another live test")
    if not cwd.startswith("/home/codex/projects/") or ".." in cwd.split("/"):
        raise ValueError("Probe must use a task-owned repository under /home/codex/projects")
    marker = ".airlift-probe-" + os.urandom(12).hex()
    client = remote_rpc(root, lease)
    try:
        client.initialize()
        started = client.request("thread/start", {"cwd": cwd, "sandbox": "workspace-write",
                                                  "approvalPolicy": "on-request", "historyMode": "paginated"})
        thread_id = started["thread"]["id"]
        receipt = {"thread_id": thread_id, "cwd": cwd, "marker": marker, "started_at": timestamp(utcnow())}
        write_json(root / "disconnect-probe.json", receipt)
        command = f"python3 -c \"import time; time.sleep(90); open('{marker}', 'w').write('airlift-complete')\""
        prompt = ("This is a disposable execution persistence check. Run exactly this one terminal command, "
                  "with no backgrounding, then read its file and report the contents. Do not perform any other work: " + command)
        turn = client.request("turn/start", {"threadId": thread_id,
                                             "input": [{"type": "text", "text": prompt}]})
        receipt["turn_id"] = turn["turn"]["id"]
        # Disconnect only after real execution begins, not merely after enqueueing.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            event = client.receive(deadline - time.monotonic())
            if "id" in event and "method" in event:
                raise ValueError("Probe awaits human approval; no response was fabricated")
            item = event.get("params", {}).get("item", {})
            if event.get("method") == "item/started" and item.get("type") == "commandExecution":
                receipt["disconnected_at"] = timestamp(utcnow())
                write_json(root / "disconnect-probe.json", receipt)
                break
        else:
            raise ValueError("No real command execution observed; refusing to claim a disconnect test")
    finally:
        client.close()
    # Deterministic I/O only. The invoking coding chat can do other work while this
    # command runs in a yielded terminal; no model polling or extra agent is used.
    time.sleep(110)
    with remote_rpc(root, lease) as reopened:
        saved = reopened.request("thread/read", {"threadId": thread_id})
        receipt["reopened_at"] = timestamp(utcnow())
        receipt["thread_readback"] = saved
        write_json(root / "disconnect-probe.json", receipt)
        until = time.monotonic() + 120
        while True:
            turns = reopened.request("thread/turns/list", {"threadId": thread_id, "limit": 10, "itemsView": "full"})
            receipt["turns_readback"] = turns
            write_json(root / "disconnect-probe.json", receipt)
            target = next((t for t in turns.get("data", []) if t["id"] == receipt["turn_id"]), None)
            if target and target.get("status") == "completed":
                break
            if target and target.get("status") in ("failed", "interrupted"):
                raise ValueError("Detached turn did not complete; inspect the retained thread receipt")
            if time.monotonic() >= until:
                raise ValueError("Detached turn remains unresolved; preserve and inspect the same thread")
            time.sleep(10)
    body = subprocess.run(["ssh", "-F", str(root / "ssh_config"), lease["name"],
                           "cat -- " + shlex.quote(cwd + "/" + marker)], check=True, capture_output=True, text=True, timeout=60).stdout
    if body != "airlift-complete":
        raise ValueError("Detached execution did not produce the expected saved result")
    receipt["saved_result"] = body
    write_json(root / "disconnect-probe.json", receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k not in ("thread_readback", "turns_readback")}, indent=2))
