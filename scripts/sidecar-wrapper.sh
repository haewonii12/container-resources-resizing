#!/bin/sh
# Legacy plain-sidecar mode (k8s < 1.29): run kubectl-ai until the agent writes $RS_DONE_FILE, then exit 0
# so the Job can complete. Native sidecars (default deploy/) do not need this.
DONE="${RS_DONE_FILE:-/shared/done}"
kubectl-ai "$@" &
pid=$!
trap 'kill "$pid" 2>/dev/null; exit 0' TERM INT
while [ ! -f "$DONE" ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid"; exit $?
  fi
  sleep 2
done
kill "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
exit 0
