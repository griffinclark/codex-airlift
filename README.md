# Codex Airlift

Move open Codex coding chats to your own GCP SSH host before travel, then bring
them home. Airlift provisions a persistent host with Terraform and supplies two
Codex skills that coordinate **native Codex host handoff** with a recoverable
private journal. It does not replace Codex or copy its live session database.

This is an independent project, not an OpenAI or Google product.

## What runs where

- GCP runs a single Ubuntu VM, Codex, your repositories, and their tools.
- Codex's managed app-server daemon owns execution independently of a terminal.
- The desktop app's supported host-handoff tool moves conversation and Git state.
- `codex-to-cloud` and `codex-from-cloud` select chats, transfer sequentially,
  reconcile operation IDs, and verify destination readback.
- The CLI prepares infrastructure, repositories, and private transfer manifests.
  It does **not** offer an undocumented desktop RPC endpoint or a standalone
  command that pretends to migrate chats by copying JSONL files.

Read [OpenAI's SSH and handoff instructions](https://learn.chatgpt.com/docs/remote-connections).
An active turn is interrupted during native handoff. A chat cannot use the app
tool to hand itself off; move the orchestration chat with its native footer last.
Host tool availability, authentication, trusted hooks, browser sessions, and
local paths must be prepared separately. A successful transfer does not make a
Linux VM support macOS desktop apps.

## Install

Requires macOS/Linux, Python 3.10+, Git, Terraform 1.5+, Google Cloud CLI, SSH,
and a Codex desktop version with SSH host handoff. Use the same pinned Codex CLI
release on both hosts. Authenticate the Google Cloud CLI and its application
default credentials for the intended project.

The VM uses OpenAI's managed standalone installer, with its script digest and
CLI release pinned. The npm CLI alone cannot bootstrap the durable daemon.
If the upstream installer changes, review it and update the pinned digest;
do not bypass its checksum or silently install `latest`.

```sh
git clone https://github.com/griffinclark/codex-airlift.git
cd codex-airlift
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/codex-airlift install-skills
```

Put `.venv/bin` on PATH or use the absolute executable path in a Codex chat.
Source must remain available because the Terraform and skills ship in the repo.
For a noneditable install, pass `--source-dir /path/to/codex-airlift`.

## Deploy a standalone host

```sh
codex-airlift deploy --project example-project --monthly-budget 100 --codex-version 0.146.0
# Review the plan, then apply the same private deployment:
codex-airlift deploy --project example-project --monthly-budget 100 --codex-version 0.146.0 --apply
codex-airlift configure-ssh
ssh codex-airlift 'test -f /var/lib/codex-airlift-ready'
codex-airlift authorize --auth-file ~/.codex/auth.json
codex-airlift bootstrap-daemon
codex-airlift doctor
```

Startup package installation can take several minutes. Authorize only a trusted
host with your own credential. Credential contents are streamed over SSH, never
put in Terraform, command arguments, logs, or the repo; existing remote credentials
are preserved. You can instead sign in on the host using normal Codex login.

Terraform uses its own state, network, SSH key, and service account. Only the IAP
SSH source range can reach port 22. App-server has no public listener. The VM's
service account has no project roles or OAuth scopes. Persistent code and chats
remain on the protected 100 GB disk when compute stops.

State defaults to `~/.local/state/codex-airlift` with owner-only permissions.
For independent deployments, pass a different `--state-dir` to **every** command.
The SSH config include is added without replacing existing configuration.

In the desktop app, enable the named SSH host under Settings > Connections,
save a matching remote project, then invoke `$codex-to-cloud`. Return with
`$codex-from-cloud`. Open chats mean loaded active/idle/waiting Codex chats by
default. Archived chats and unloaded history are preserved unless explicitly
included. ChatGPT chats are outside the native handoff contract.

For private repositories without cloud Git credentials:

```sh
codex-airlift prepare-repo /path/to/project
```

This sends committed Git objects through IAP/SSH and preserves existing working
files. Native handoff subsequently transfers the selected chat's Git work. It
doesn't push private source to this public repository.

## Monthly compute limit

The deployment fixes **one `e2-highmem-2` (2 vCPU, 16 GiB) in `us-central1-a`**.
Its published on-demand price checked on 2026-11-10 is $0.09039966/hour, or about
$67.26 for a full 31-day month. Airlift reserves at a higher $0.125/hour ceiling:
744 hours costs **$93**, leaving headroom under a $100 calendar-month compute
allowance. Terraform exposes no machine-size, region, replica-count, GPU, or
autoscaling switch that can silently defeat this capacity bound.

Additionally, GCP enforces an absolute 90-day shutdown deadline. Reapplying or
restarting doesn't extend it; after expiry, GCP rejects a start until the lease is
explicitly renewed. Revalidate the published price before renewal. The source
and private deployment record retain the original anchor.

This is a **bounded compute allocation**, not a native billing-dollar cutoff.
It relies on the pinned VM remaining below the conservative rate ceiling. GCP
does not provide spend-cap enforcement for persistent VM compute. Disks, public
IPv4, network traffic, model usage, taxes, and unrelated project services are
outside this compute-only allowance and are billed separately. Disk retention
continues after the VM stops. Do not describe a billing alert as a hard cutoff.

Sources: [VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[enforced VM time limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[spend-cap scope](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## Recovery and verification

Each row moves through `pending → claimed → submitted → verified` in a private
atomic journal. A process failure after claim is uncertain, even without an
operation receipt: inspect both hosts before resubmitting. A recorded operation
must be reconciled by its original ID. Claiming another row is rejected while
one is unresolved. Verification requires the exact chat ID on the destination;
the skill also requires reopening its history and Git work. Failed rows are kept.

Before real bulk migration, verify on a disposable chat: transfer out, perform
work, disconnect the SSH client during execution, reconnect and reopen the saved
result, then transfer back. Separately test a shortened GCP deadline on an empty
host and restore the original deadline afterward. Keep private run IDs and
receipts outside this repository. Do not interrupt a host holding human work for
an infrastructure test.

The opt-in probes perform those two infrastructure checks and retain receipts:

```sh
# Refuses to run when the host has any non-archived chats.
codex-airlift probe stop
# Runs one real Codex turn, disconnects during a 90-second command, then reopens it.
codex-airlift probe disconnect --remote-cwd /home/codex/projects/disposable-test
```

The disconnect probe incurs model usage. An existing receipt blocks blind
resubmission; inspect its recorded thread before attempting another test.

The repository's automated suite covers budget bounds, immutable lease replay,
scope selection, uncertain submission, wrong-host rejection, and durable journal
readback. These checks alone are not proof of a live desktop migration.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
terraform -chdir=infra/gcp fmt -check
terraform -chdir=infra/gcp init -backend=false
terraform -chdir=infra/gcp validate
```

CI performs no deployment and receives no cloud credentials. The VM and disk are
protected against accidental deletion; decommission only after chats and files
are safely returned, then deliberately remove those protections in Terraform.

## License

MIT.
