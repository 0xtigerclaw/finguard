#!/bin/sh
# Give one agent its own copy of Finguard: a branch, a Python environment, a state folder and a port.
set -eu
name="${1:-}"
case "$name" in
  ""|*[!a-z0-9-]*) echo "Usage: scripts/new_worktree.sh <short-name>, using a-z, 0-9 and -" >&2; exit 2 ;;
esac
root="$(git rev-parse --show-toplevel)"
cd "$root"
path=".worktrees/$name"
[ -e "$path" ] && { echo "$path already exists." >&2; exit 1; }
git worktree add -q -b "agent/$name" "$path"
cd "$path"
uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python -e ".[dev]"
count=$(git -C "$root" worktree list | wc -l | tr -d ' ')
port=$((8790 + count))
printf 'FINGUARD_PORT=%s\nFINGUARD_STATE=%s/state\n' "$port" "$PWD" > .env.local
echo "Ready: $PWD"
echo "Branch: agent/$name  Port: $port"
