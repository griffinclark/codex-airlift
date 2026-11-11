---
name: codex-from-cloud
description: Return Codex chats and their Git work from a configured Codex Airlift SSH host to this computer. Use when bringing cloud-hosted coding work back after travel.
---

# Bring Codex work back to this computer

Run this from a local orchestration chat using the installed `codex-airlift` CLI
and Codex desktop app tools. Read the private deployment configuration and the
outbound transfer journal first. Cloud means the configured SSH host, not Codex
Cloud. Do not overwrite local Git work or copy a live Codex database.

1. Read current projects and chats from both hosts. Reconcile every unfinished
   outbound operation through its recorded operationId before planning returns.
   Default to the successfully moved outbound set still on that host; when the
   user requests every open cloud chat, include the current loaded cloud set too.
   Preserve human pauses, stopped turns, models, and permission settings.
2. Confirm this computer has matching saved projects, repository subdirectories,
   required credentials/tools, and the original worktrees. Inspect dirty local
   changes and divergent commits. Let native handoff reconcile Git state; stop
   on a conflict instead of stashing, resetting, or merging unrelated work.
3. Save the complete requested cloud inventory privately. Create a new return
   journal with `codex-airlift manifest plan --file /private/return.json --inventory /private/cloud.json --source HOST_ID --destination local --caller THIS_CHAT_ID`.
   For a precise outbound subset, filter the inventory to its verified IDs first.
   Keep both journals. Never replace the outbound evidence.
4. Move sequentially with `manifest claim`, `handoff_thread(destinationHostId="local")`,
   `manifest submit` using the returned operationId, and bounded
   `get_handoff_status(afterRevision=…, waitMs=30000..60000)` calls. If a submit is
   uncertain, inspect that operation and both hosts before a retry. A claimed or
   submitted row must be reconciled, never blindly resubmitted.
5. Read each returned chat and its Git work on this computer. Save a fresh local
   inventory and use `manifest verify --file … --thread ID --inventory …` only
   after the saved history and files reopen correctly. Mark confirmed failures
   with `manifest fail` and diagnose before continuing.
6. Report exact verified/failed/pending/excluded counts. Keep the VM and disk
   available if anything remains there. Once all requested work is verified local,
   stop the named VM when requested or already authorized; retain its persistent
   disk and journals. Never destroy private work as transfer cleanup.

The calling chat cannot move itself through the app tool. Checkpoint and use its
native footer last if it also needs to return. Do not relax the monthly budget,
renew the lease, or replay research/deployment side effects merely to bring a
conversation home.
