# Codex Airlift

Public, provider-neutral source; private deployment state lives outside this repo.
Never commit credentials, chat inventories, manifests, SSH private keys, account
names, real project IDs, or Terraform state. Examples use fictional values.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`,
`terraform -chdir=infra/gcp fmt -check`, and Terraform init/validate before publishing.
Review executable code and public-content privacy before pushing. Use `main`
as the integration branch. CI deploys nothing and receives no cloud secrets.

The user approved an independent Airlift interface. Use the public app-server
protocol for its execution and project registry. Portable handoff may transfer
canonical rollout history only after the source is quiescent, with identity,
lineage, hashes, ownership, history and Git readback verified. Never copy a live
Codex database, invent a desktop IPC endpoint, weaken permissions, or replay an
uncertain operation. Preserve human stops and reconcile external side effects.
Native handoff is an optional adapter, not a prerequisite for the Airlift client.

Keep the budget anchor stable across reapply, VM rebuild, and reconnect.
Raising or resetting it requires explicit user authorization. No agent running
on the VM should receive infrastructure-administration credentials.
