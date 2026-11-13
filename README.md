# Codex Airlift

Move Codex coding chats and their Git work to your own GCP VM before travel,
then bring them back. Airlift provides Terraform, an unattended setup script,
two Codex skills, and an independent terminal chat picker. It uses the official
Codex terminal UI and public app-server protocol over IAP SSH.

This is an independent project, not an OpenAI or Google product. Migrated chats
open in **Airlift's terminal interface**. Official Codex Desktop registration is
not required. The optional native Desktop adapter remains experimental.

## Setup

Requires macOS/Linux, Python 3.10+, Git, SSH, Google Cloud CLI and Terraform 1.5+.
Authenticate GCP and application default credentials for your intended project,
and sign in to Codex locally. Review the Terraform allocation before applying.

```sh
git clone https://github.com/griffinclark/codex-airlift.git
cd codex-airlift
./scripts/setup-project.sh --project example-project --apply
```

For an existing Airlift deployment, run `./scripts/setup-project.sh` again.
It configures SSH, waits for bootstrap, installs your selected Codex credential
privately if needed, checks the original compute lease, installs both skills,
and installs the pinned local client and cloud worker. It requires no Desktop
Settings or project clicks. Existing credentials are preserved. The installer
and CLI release are pinned; Airlift does not silently update to `latest`.
Setup also writes an executable `Airlift.command` launcher in the private state
folder; double-click it on macOS to open the terminal chat picker.

The default deployment state is `~/.local/state/codex-airlift`, mode 700.
Use `--state-dir /private/path` consistently for a separate deployment. The
public repository receives no credentials, inventories, source repositories,
chat history, Terraform state, SSH keys or deployment-specific identifiers.

## Move chats and open the interface

Ask Codex to use `$codex-to-cloud` to move your open chats. The skill selects
coding chats through public chat tools, archives the sources to stop their
execution, and invokes a recoverable batch. Active work is interrupted; human
pauses and uncertain external actions are never automatically replayed.
Archived history and original checkouts remain retained.

```sh
./scripts/airlift open                  # interactive local/cloud chat picker
./scripts/airlift chats                 # private ownership catalog as JSON
./scripts/airlift chat cloud CHAT_UUID  # reopen one cloud chat
./scripts/airlift chat local CHAT_UUID  # reopen returned work
```

The vendor terminal UI provides history, prompts, tool execution and approval
controls. A newly restored folder can require its normal trust confirmation
when first opened. Disconnecting the terminal leaves cloud work executing in
its independent systemd runtime. Reconnect through Airlift, rather than reusing
the temporary socket address printed by the vendor UI.

Use `$codex-from-cloud` to return all or selected Airlift chats. Returned work
opens in a fresh local Airlift runtime and checkout, preserving original chat
and turn IDs. It does not overwrite your original repository or register chats
in official Codex Desktop. Imported chats wait for your next instruction.

For scripting, freeze a private JSON array of chat UUIDs and choose a stable
batch UUID. Reuse that exact UUID and selection after a failed connection:

```sh
./scripts/airlift batch --to cloud --inventory /private/selected.json --batch BATCH_UUID
./scripts/airlift batch --to local --inventory /private/selected.json --batch RETURN_UUID
```

For initial Desktop adoption, first run `stage-desktop --inventory FILE --batch
BATCH_UUID`, then archive through Codex's public archive tool, then run the batch
with `--desktop` and the same UUID. The skill does this automatically. Desktop
can remove managed checkouts during archive; the pre-archive capture preserves
their original index and saved files, and is compared against the retained Git
snapshot before use. The CLI refuses live
Desktop sources. `--desktop-binary` selects the source app's bundled CLI when
it is not at the default macOS location. It reads the source index only in
read-only mode. The coordinating chat moves last, after a durable checkpoint;
a queued finalizer is not reported as a completed transfer.

## What is preserved

Each transfer retains an immutable, hash-checked bundle of canonical JSONL
history and all referenced fork/segment prefixes. A self-contained execution
copy keeps model records, encrypted reasoning, tool records and turn IDs in
order; only session environment metadata and outer ordinals are adapted.
No live SQLite database is copied. The destination reconstructs its own index
using public `thread/resume`, and compares the complete paginated history
fingerprint before granting ownership. The rollout-path import API is
experimental; the tested transport pins Codex **0.167.1**. Do not assume another
release has identical history or permission schemas.

Git transport preserves committed HEAD, branch, staged and unstaged binary
changes, nonignored untracked files and common saved-output directories
(`artifacts`, `outputs`, `reports`). File hashes and modes are checked after
restoration. Submodules, special files, custom sandbox policies and oversized
packets fail closed for explicit reconciliation. Ignored dependencies, caches,
account-specific plugins, macOS applications and environment credentials need
their normal host setup; Airlift does not invent Linux equivalents.

Source ownership is suspended before archive. A timeout never authorizes a
second executor or a repeated model turn. Operation journals are atomic and
locked; immutable packets can be resent, while uncertain imports retain their
receipts for inspection. Original data is retained for recovery. Do not
manually resume an archived source while its destination owns the chat.

## Infrastructure and compute allowance

Terraform creates one Ubuntu VM, dedicated VPC/subnet, IAP-only SSH firewall,
and protected 100 GB persistent disk. The VM service account has no project
roles or OAuth scopes. App-server Unix sockets are owner-only; there is no
public application listener. Code and history survive compute shutdown.

The fixed shape is **one `e2-highmem-2` in `us-central1-a`** (2 vCPU, 16 GiB),
with no scaling, GPU, replica or machine-size switches. Its on-demand price
checked 2026-11-10 was $0.09039966/hour, approximately $67.26 per 31-day month.
A conservative $0.125/hour allocation reserves **$93 per 31-day calendar month**,
under the requested **$100/month compute allowance**.

GCP also enforces an absolute 90-day shutdown deadline. Reapply, reconnect and
restart do not extend the original lease. GCP rejects starts after expiry until
an explicitly authorized renewal; revalidate pricing before renewal.

This is a **bounded compute allocation, not a metered billing-dollar cutoff**.
It relies on the pinned VM remaining below the reserved rate. GCP does not offer
a persistent-VM billing spend cap. Disk, IPv4, network, model usage, taxes and
unrelated project services are separate charges. Retained disks keep billing
after compute stops. Billing alerts are not enforcement.

Sources: [VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[enforced VM time limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[spend-cap scope](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## Verification and recovery

Live acceptance has exercised a same-ID local→cloud→local chat round trip,
inherited model context, rendered terminal history, a real shell command that
finished after SSH disconnection, saved-output reopening, and exact staged/
unstaged/untracked Git readback. A segmented history import was also compared
against its full original turn fingerprint. Private receipts stay outside Git.
These fixture checks do not imply every plugin or project tool is portable.

If adopting a checkout that Desktop already removed without a prior capture,
recover its retained Git snapshot into a separate directory and pass
`--workspace` (one move) or `--workspace-map` (batch). A Desktop snapshot folds
uncommitted changes together; it cannot reconstruct the earlier staging split.
Keep that recovery provenance explicit. `--reconcile-workspace` permits readback
of an inspected failure only before any destination runtime exists; it never
clears an uncertain execution or replaces a different packet.

`doctor` verifies the deployed VM shape, original deadline, authentication and
pinned CLI/server versions. The opt-in `probe stop` tests GCP shutdown only on
an empty host. Never run infrastructure interruption probes on human work.
Inspect existing operation IDs and receipts before attempting recovery; do
not clear a failed journal or generate new IDs to conceal uncertainty.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash -n scripts/setup-project.sh scripts/airlift
terraform -chdir=infra/gcp fmt -check
terraform -chdir=infra/gcp init -backend=false -input=false
terraform -chdir=infra/gcp validate
```

CI performs no deployment and receives no cloud credentials. Decommission only
after returning work, then deliberately remove the VM/disk deletion protections.

## License

MIT.
