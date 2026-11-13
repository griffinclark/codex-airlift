"""SSH-only cloud worker. No network listener or infrastructure credentials."""
import argparse
import json
import os
from pathlib import Path

from .runtime import cloud_store, identifier
from .transfer import export_runtime, receive


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("catalog", "info", "export", "receive"))
    parser.add_argument("--thread")
    parser.add_argument("--operation")
    parser.add_argument("--sha256")
    parser.add_argument("--reconcile-workspace", action="store_true")
    args = parser.parse_args()
    store = cloud_store()
    if args.action == "catalog":
        result = store.catalog()
    elif args.action == "info":
        record = store.owned(args.thread)
        result = {"record": record, "runtime": store.ensure(record["runtime_id"])}
    elif args.action == "export":
        result = export_runtime(store, args.operation, args.thread)
    else:
        op_id = identifier(args.operation)
        packet = Path.home() / ".codex/airlift/inbox" / (op_id + ".tar")
        result = receive(store, op_id, packet, args.sha256, reconcile_workspace=args.reconcile_workspace)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
