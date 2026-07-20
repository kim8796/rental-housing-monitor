#!/usr/bin/env bash
set -euo pipefail

database_path=${1:?usage: persist_data_snapshot.sh DATABASE_PATH [REMOTE] [BRANCH]}
remote=${2:-origin}
branch=${3:-data}
remote_ref="refs/heads/$branch"

if [[ ! -f "$database_path" ]]; then
  printf 'database does not exist: %s\n' "$database_path" >&2
  exit 1
fi

old_sha=$(git ls-remote --heads "$remote" "$remote_ref" | awk '{print $1}')
database_blob=$(git hash-object -w "$database_path")
data_tree=$(printf '100644 blob %s\tannouncements.db\n' "$database_blob" | git mktree)
project_tree=$(printf '040000 tree %s\tdata\n' "$data_tree" | git mktree)
root_tree=$(printf '040000 tree %s\trental-housing-monitor\n' "$project_tree" | git mktree)
snapshot_commit=$(printf 'chore(data): update rental monitor snapshot\n' | git commit-tree "$root_tree")

if [[ -n "$old_sha" ]]; then
  git push \
    --force-with-lease="$remote_ref:$old_sha" \
    "$remote" \
    "$snapshot_commit:$remote_ref"
else
  git push "$remote" "$snapshot_commit:$remote_ref"
fi
