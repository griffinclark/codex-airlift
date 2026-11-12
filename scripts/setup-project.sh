#!/usr/bin/env bash
# Prepare a repository for the supported desktop SSH-project selection.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || ${1:-} == --help ]]; then
  printf '%s\n' 'Usage: scripts/setup-project.sh LOCAL_REPOSITORY [PRIVATE_STATE_DIRECTORY]'
  printf '%s\n' 'Prepares SSH and Git; the final saved-project selection is performed in the desktop app.'
  if [[ ${1:-} == --help ]]; then exit 0; else exit 2; fi
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$source_dir/.venv/bin/python"
state_dir=${2:-"$HOME/.local/state/codex-airlift"}
if [[ ! -x "$python" ]]; then
  printf '%s\n' 'Install this checkout first: python3 -m venv .venv && .venv/bin/pip install -e .' >&2
  exit 1
fi
repository=$(git -C "$1" rev-parse --show-toplevel)
airlift=("$python" -m codex_airlift.cli --source-dir "$source_dir" --state-dir "$state_dir")

"${airlift[@]}" configure-ssh
"${airlift[@]}" doctor
"${airlift[@]}" prepare-repo "$repository"
"${airlift[@]}" install-skills

printf '%s\n' 'Host and repository preparation passed.'
printf '%s\n' 'Desktop project registration is still required: Settings > Connections > SSH > folder-plus.'
printf '%s\n' 'Choose the remote repository path printed above. Then verify it with the Codex list_projects tool.'
printf '%s\n' 'This script does not save a desktop project or hand off chats.'
