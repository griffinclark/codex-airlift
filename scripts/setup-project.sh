#!/usr/bin/env bash
# Register a repository automatically, and require native desktop readback.
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$source_dir/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  python3 -m venv "$source_dir/.venv"
fi
if ! "$python" -c 'import codex_airlift' >/dev/null 2>&1; then
  "$python" -m pip install -e "$source_dir"
fi
exec "$python" -m codex_airlift.cli --source-dir "$source_dir" setup-project "$@"
