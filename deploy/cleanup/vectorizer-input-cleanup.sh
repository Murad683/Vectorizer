#!/bin/bash
# Daily cleanup of uploaded INPUT files (never result files) inside the
# vectorizer-backend container. Extra arguments go to the module, e.g.
#   vectorizer-input-cleanup.sh --dry-run
#   vectorizer-input-cleanup.sh --dry-run --hours 0
#
# Installed as a root-owned copy in /usr/local/bin (see DEPLOY.md). Output goes
# to the journal: journalctl -t vectorizer-input-cleanup
set -euo pipefail

CONTAINER="${VECTORIZER_BACKEND_CONTAINER:-vectorizer-backend}"

if [ -z "$(docker ps --filter "name=^${CONTAINER}$" --filter status=running -q)" ]; then
  echo "container ${CONTAINER} is not running; nothing cleaned" >&2
  exit 1
fi

exec docker exec "$CONTAINER" python -m app.cleanup_inputs "$@"
