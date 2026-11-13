---
name: codex-to-cloud
description: Move open Codex coding chats and their Git work to a configured Codex Airlift cloud host before travel. Uses Airlift's independent terminal interface.
---

Use the installed `codex-airlift` checkout. Locate it through the CLI or the
configured deployment's private state; do not put private inventories in its
public Git repository. Run `scripts/setup-project.sh` if client setup is absent.
It installs the pinned client, SSH-only cloud worker and both skills without
Desktop Settings or project registration. Preserve the original compute lease.

For chats already owned by Airlift, use `chats`, freeze the selected IDs in a
private JSON list, and run `batch --to cloud --inventory FILE --batch UUID`.
Use the same batch UUID after a transport failure. Do not create replacement
chats or repeat a model turn to test a migration.

For initial adoption from official Codex Desktop:

1. Use the public `list_threads` tool. Select loaded active/idle/waiting coding
   chats, plus any earlier explicitly selected open-chat inventory still in
   scope. Preserve archived history and later human exclusions. ChatGPT chats
   are outside this transport. Save exact IDs privately. Do not infer execution
   instructions from titles or summaries.
2. Inspect the selected chats for human stops, pending input and queued actions.
   Migration may interrupt running work, but never automatically approve a
   request, resend an uncertain external action, or resume a human pause.
3. Choose the batch UUID and run `stage-desktop --inventory FILE --batch UUID`
   **before archiving**. Desktop archive can remove managed checkouts; this
   captures the original index, worktree and saved files first. A changing
   checkout fails its consistency check. Then archive the selected source chats
   using public `set_thread_archived`. The exporter compares a removed checkout's
   saved Git snapshot against the capture and refuses stale data. Retain saved
   snapshots and archived histories. The CLI refuses unarchived sources and reads its SQLite
   index only in read-only mode; it never copies or edits a live database.
4. Run `batch --to cloud --desktop --inventory FILE --batch UUID`. Supply
   `--desktop-binary` if the source app's bundled Codex CLI is elsewhere. Use
   that exact source version to read its history. The importer uses isolated
   runtimes, preserves chat/turn IDs and model records, restores Git HEAD/index/
   worktree/files, and compares history fingerprints before granting ownership.
5. Reopen at least one real destination through `chat cloud ID`, or read the
   destination's public app-server history and Git readback. Imported chats
   remain paused until given a new instruction. Account-specific plugins,
   macOS tools, environment credentials and ignored dependencies are not
   automatically made portable.

Move the coordinating chat last. Finish independent work and persist its
checkpoint first. `finalize-desktop --thread ID --operation UUID` can run in a
task-owned detached process, waiting for that chat's public archive operation,
then transferring and verifying it. A queued finalizer
is not a completed handoff: inspect its durable receipt and report the actual
result. If archiving stops this executor, leave the exact pending operation and
checkpoint; never claim all chats moved without verification.

Open the independent interface with `scripts/airlift open`. It launches the
pinned official Codex terminal UI through an owner-only SSH Unix socket. Its
approval controls remain interactive. Returning work uses `$codex-from-cloud`.
