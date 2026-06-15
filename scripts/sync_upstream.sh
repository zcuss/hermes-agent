#!/usr/bin/env bash
set -euo pipefail

# Merge NousResearch/hermes-agent into zcuss/hermes-agent without flattening the
# fork-specific CockroachDB/cluster/chatv2 work. Run from the repo root:
#
#   scripts/sync_upstream.sh
#   scripts/sync_upstream.sh --push
#
# The script uses git's merge=ours driver for paths declared in .gitattributes.
# It stops on real conflicts so fork-specific integrations can be reviewed.

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/NousResearch/hermes-agent.git}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
LOCAL_BRANCH="${LOCAL_BRANCH:-main}"
PUSH=0
RUN_TESTS="${RUN_TESTS:-1}"

for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refuse: working tree dirty" >&2
  git status --short >&2
  exit 1
fi

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi
# Never accidentally push to NousResearch from this fork checkout.
git remote set-url --push "$UPSTREAM_REMOTE" DISABLED >/dev/null 2>&1 || true

# The merge driver referenced by .gitattributes must exist in repo-local config.
git config merge.ours.name "Keep zcuss fork version"
git config merge.ours.driver true

git fetch "$ORIGIN_REMOTE" "$LOCAL_BRANCH"
git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH" --tags --prune

git checkout "$LOCAL_BRANCH"
git merge --ff-only "$ORIGIN_REMOTE/$LOCAL_BRANCH" || {
  echo "local $LOCAL_BRANCH is not fast-forward from $ORIGIN_REMOTE/$LOCAL_BRANCH" >&2
  exit 1
}

BEFORE=$(git rev-parse HEAD)
UPSTREAM_SHA=$(git rev-parse "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")
BASE=$(git merge-base HEAD "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")

if [[ "$BASE" == "$UPSTREAM_SHA" ]]; then
  echo "already contains $UPSTREAM_REMOTE/$UPSTREAM_BRANCH ($UPSTREAM_SHA)"
  exit 0
fi

echo "syncing upstream range:"
git log --oneline --reverse "$BASE..$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" | sed 's/^/  /'

echo
set +e
git merge --no-ff --no-edit "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
MERGE_RC=$?
set -e

if [[ $MERGE_RC -ne 0 ]]; then
  echo
  echo "merge stopped on conflicts. protected zcuss-only paths are kept by merge=ours; review these:" >&2
  git diff --name-only --diff-filter=U >&2 || true
  echo >&2
  echo "after resolving: git add <files> && git commit" >&2
  exit $MERGE_RC
fi

echo
echo "merge ok: $BEFORE -> $(git rev-parse HEAD)"
echo "protected-path merge driver active for:"
git check-attr merge -- hermes_state.py hermes_db/config.py hermes_cluster/core.py tests/test_hermes_state.py | sed 's/^/  /'

if [[ "$RUN_TESTS" == "1" ]]; then
  if [[ -x .venv/bin/pytest ]]; then
    echo
    echo "running fork regression tests"
    .venv/bin/pytest tests/test_hermes_state.py tests/test_hermes_db_cluster.py tests/hermes_cli/test_dashboard_embedded_chat_default.py -q -o 'addopts='
  else
    echo "skip tests: .venv/bin/pytest missing" >&2
  fi
fi

if [[ $PUSH -eq 1 ]]; then
  git push "$ORIGIN_REMOTE" "$LOCAL_BRANCH"
else
  echo
  echo "not pushed. inspect, then run: git push $ORIGIN_REMOTE $LOCAL_BRANCH"
fi
