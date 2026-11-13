"""Public-protocol reconstruction of hash-checked, quiescent chat history."""
import hashlib
import json


def read_turns(client, thread_id):
    result, cursor, seen = [], None, set()
    for _ in range(1000):
        page = client.request("thread/turns/list", {"threadId": thread_id, "limit": 100,
                               "itemsView": "summary", **({"cursor": cursor} if cursor else {})})
        result.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError("Turn pagination did not advance")
        seen.add(cursor)
    raise ValueError("Thread history exceeds the bounded verification scan")


def history_fingerprint(turns):
    visible = [{"id": turn["id"], "status": turn["status"], "items": turn.get("items", [])}
               for turn in turns]
    return hashlib.sha256(json.dumps(visible, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


