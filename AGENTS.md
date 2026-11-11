# Codex Airlift

Public, provider-neutral source; private deployment state lives outside this repo.
Never commit credentials, chat inventories, manifests, SSH private keys, account
names, real project IDs, or Terraform state. Examples use fictional values.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`,
`terraform -chdir=infra/gcp fmt -check`, and Terraform init/validate before publishing.
Review executable code and public-content privacy before pushing. Use `main`
as the integration branch. CI deploys nothing and receives no cloud secrets.

Native Codex handoff owns chat and Git migration. Do not copy a live Codex
database, invent a desktop IPC endpoint, weaken permissions, or replay uncertain
handoffs. Reconcile submitted operations before a retry. Preserve human stops.
The current orchestration chat cannot hand itself off through the app tool.

Keep the budget anchor stable across reapply, VM rebuild, and reconnect.
Raising or resetting it requires explicit user authorization. No agent running
on the VM should receive infrastructure-administration credentials.
