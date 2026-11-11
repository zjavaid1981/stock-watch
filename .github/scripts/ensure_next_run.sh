#!/usr/bin/env bash
# Make sure ANOTHER run of the stock_manual workflow is queued or running, and start one if not.
#
# Used at the end of every loop run (and by the fallback "relaunch" job).  It does not trust that
# starting a run worked: it checks afterwards that the new run really exists and did not die with a
# startup failure, and retries if it did.
#
# Needs: gh CLI, GH_TOKEN with "actions: write", and the GITHUB_* variables Actions provides.
set -u

WORKFLOW="stock_manual.yml"
ATTEMPTS="${ATTEMPTS:-6}"
WAIT="${WAIT:-45}"                     # seconds to let GitHub register a new run before checking it

other_runs() {                         # runs of this workflow, other than this one, not yet finished
  gh run list --repo "$GITHUB_REPOSITORY" --workflow "$WORKFLOW" --limit 20 \
    --json databaseId,status \
    --jq "[.[] | select(.databaseId != ${GITHUB_RUN_ID:-0}) | select(.status != \"completed\")] | length" 2>/dev/null
}

for i in $(seq 1 "$ATTEMPTS"); do
  n="$(other_runs)"
  if [ "${n:-0}" -gt 0 ]; then
    echo "[ensure] another run is queued or running ($n): the chain continues."
    exit 0
  fi
  echo "[ensure] attempt $i/$ATTEMPTS: no next run found; starting one"
  gh workflow run "$WORKFLOW" --repo "$GITHUB_REPOSITORY" --ref "${GITHUB_REF_NAME:-main}" \
    || echo "[ensure] dispatch request failed"
  sleep "$WAIT"
done

n="$(other_runs)"
if [ "${n:-0}" -gt 0 ]; then
  echo "[ensure] next run is in place."
  exit 0
fi
echo "[ensure] could not confirm a next run; the 2-hourly safety-net schedule will try again."
exit 1
