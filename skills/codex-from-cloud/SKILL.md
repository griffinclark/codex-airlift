---
name: codex-from-cloud
description: Return Codex Airlift chats and their Git work from the cloud to this computer after travel, preserving history and saved results.
---

Use the configured `codex-airlift` checkout and its private deployment state.
Run `chats` to inspect cloud ownership, freeze the intended IDs in a private JSON
list, and run `batch --to local --inventory FILE --batch UUID`. Omit the inventory
only when the user explicitly requested all currently Airlift-owned cloud chats.
Reuse the original UUID to reconcile a failed connection; do not issue a new
operation or replay a model turn to resolve uncertainty.

The worker suspends source ownership before archiving execution. It transfers
canonical history and a Git snapshot over IAP SSH, restores a fresh local
runtime and isolated checkout, and compares chat ID, turn history and Git/file
hashes. Original cloud data and source checkouts remain retained. A failed or
uncertain operation stays incomplete; inspect its recorded receipt and both
hosts before changing ownership. Never bypass the duplicate-executor guard.

Preserve human pauses, queued decisions and external-action receipts. Do not
automatically approve requests or rerun interrupted commands. The source and
destination retain their histories; a new user instruction resumes work.

Verify `state=verified` for every selected ID, reopen a returned chat with
`chat local ID`, and check its saved artifacts. Use `open` for the terminal chat
picker. Returned chats live in Airlift's independent interface and isolated
working folders; this does not promise registration in official Codex Desktop.
Do not overwrite the user's original checkout or delete retained cloud data.
The VM's existing compute lease is unchanged by returning or reconnecting.
