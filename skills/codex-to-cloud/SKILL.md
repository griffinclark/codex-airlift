---
name: codex-to-cloud
description: Move open Codex coding chats and their Git work to a configured Codex Airlift SSH host. Use for handing this machine's work to the cloud before travel.
---

# Hand Codex work to the cloud

Use the installed `codex-airlift` CLI and the documented Codex desktop app tools.
The cloud host is a self-managed SSH machine. Do not select Codex Cloud, copy
live session databases, or assume two hosts share credentials and local tools.

1. Read `~/.local/state/codex-airlift/deployment.json` (or the user-selected state
   directory), run `codex-airlift doctor`, and confirm the host's pinned machine,
   project, $100-or-lower monthly allowance, and unexpired price-validation lease.
   Do not reset the lease or increase the budget during a transfer.
2. Use `list_projects` and `list_threads` to identify the registered SSH host ID.
   If it is missing, enable the named SSH host in Settings > Connections and save
   matching projects. `codex-airlift configure-ssh` makes its alias discoverable.
   Prepare each repository with `codex-airlift prepare-repo /path/to/repository`.
   Save the same repository subdirectory on both hosts. Establish required tools,
   credentials, files, trusted hooks, and browser capabilities before moving work
   that depends on them; record concrete incompatibilities.
3. Freeze a private, complete inventory of the requested chats. Default "open"
   means loaded `active`, `idle`, or waiting Codex chats on this host. Include
   unloaded history only when requested; paginate history completely in that case.
   Preserve ChatGPT chats, archived chats, human stops, and other hosts. Explain
   the selected count and exclusions. Existing user authorization to move all
   selected chats also authorizes the handoff's interruption of their active turns.
4. Create a private journal:
   `codex-airlift manifest plan --file /private/batch.json --inventory /private/inventory.json --source local --destination HOST_ID --caller THIS_CHAT_ID`.
   Add `--include-unloaded` only for that requested scope. Never store these files
   in the public repo. Reuse an existing journal after interruption.
5. For each selected row, sequentially:
   - Run `manifest claim --file … --thread ID` before submitting.
   - Call `handoff_thread` with that ID and the exact destinationHostId. Preserve
     its model/settings and do not add a follow-up that overrides a human pause.
   - Save its operationId with `manifest submit --file … --thread ID --operation OP`.
   - Reconcile through `get_handoff_status`, using afterRevision and a bounded
     30–60 second wait. On uncertain submit/readback, inspect the same operation
     and destination before any retry. Never submit a second handoff blindly.
   - Read the destination chat and its Git state. Save a fresh destination
     `list_threads` inventory, then `manifest verify --file … --thread ID --inventory …`.
     If an operation fails, mark `manifest fail` and stop to diagnose that boundary.
6. Reopen the transferred chats, verify saved history and files, and report the
   verified, failed, pending, and excluded counts. A running operation is not a
   verified move. Keep the journal for the return trip. The calling chat cannot
   hand itself off with the app tool: use its native footer last, after checkpointing
   the exact journal, host, and remaining action. Do not invent a self-handoff API.

The supported desktop tools perform the actual move; the CLI prepares the host
and enforces journal transitions. Native browser/desktop interaction follows
the user's host coordination policy. A page load alone does not verify a move.
