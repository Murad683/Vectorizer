#!/bin/bash
# Nightly backup of the Vectorizer Postgres database to Object Storage.
#
# Installed as a root-owned copy in /usr/local/bin (NOT run from /opt/vectorizer:
# that directory belongs to the deploy user, and root must not execute a file
# the deploy user can change). Independent of the shared db-backup-all.sh, so a
# failure here cannot stop the other projects' backups and vice versa.
#
#   RETENTION_DRY_RUN=1 /usr/local/bin/vectorizer-db-backup.sh
#       does the real dump and upload, but only *shows* what the retention
#       step would delete (local and remote) instead of deleting it.
set -euo pipefail
umask 077

CONTAINER="${VECTORIZER_DB_CONTAINER:-vectorizer-db}"
BACKUP_DIR="${VECTORIZER_BACKUP_DIR:-/var/backups/vectorizer}"
REMOTE="${VECTORIZER_BACKUP_REMOTE:-contabo:vectorizer-db-backups}"
LOCAL_DAYS=7
REMOTE_DAYS=30
RETENTION_DRY_RUN="${RETENTION_DRY_RUN:-0}"

TS=$(date +%Y%m%d-%H%M%S)
NAME="vectorizer-${TS}.dump"
FILE="${BACKUP_DIR}/${NAME}"
PARTIAL="${FILE}.partial"
LISTING="${FILE}.list"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# Never leave half-written files that could be mistaken for a backup.
trap 'rm -f "$PARTIAL" "$LISTING"' EXIT

[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" = "true" ] \
  || fail "container $CONTAINER is not running"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# User, password and database name come from the container's own environment.
docker exec "$CONTAINER" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$PARTIAL"
[ -s "$PARTIAL" ] || fail "dump is empty"

# The dump must be readable by pg_restore and contain the core tables.
docker exec -i "$CONTAINER" pg_restore --list <"$PARTIAL" >"$LISTING" \
  || fail "pg_restore cannot read the dump"
grep -q "TABLE DATA public users" "$LISTING" || fail "dump has no users data"
grep -q "TABLE DATA public jobs" "$LISTING" || fail "dump has no jobs data"

mv "$PARTIAL" "$FILE"
echo "dump ok: $FILE ($(stat -c%s "$FILE") bytes)"

rclone copy "$FILE" "$REMOTE" --no-check-dest
rclone lsf "$REMOTE" --include "$NAME" | grep -qx "$NAME" \
  || fail "uploaded file $NAME not found in $REMOTE"
echo "uploaded and verified: $REMOTE/$NAME"

# Reached only when the dump is valid AND the upload is confirmed.
printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NAME" "$(stat -c%s "$FILE")" \
  >"${BACKUP_DIR}/LAST_SUCCESS.tmp"
mv "${BACKUP_DIR}/LAST_SUCCESS.tmp" "${BACKUP_DIR}/LAST_SUCCESS"

# Retention: local 7 days, remote 30 days. Both only touch vectorizer-*.dump.
# A retention problem is reported but does not turn a good backup into a failure.
if [ "$RETENTION_DRY_RUN" = "1" ]; then
  echo "retention DRY RUN: nothing is deleted"
  echo "local files older than ${LOCAL_DAYS} days that would be deleted:"
  find "$BACKUP_DIR" -maxdepth 1 -name 'vectorizer-*.dump' -mtime +"$LOCAL_DAYS" -print
  echo "remote files older than ${REMOTE_DAYS} days that would be deleted:"
  rclone delete "$REMOTE" --include "vectorizer-*.dump" --min-age "${REMOTE_DAYS}d" \
    --dry-run -v 2>&1 || echo "WARN: remote retention dry run failed"
else
  find "$BACKUP_DIR" -maxdepth 1 -name 'vectorizer-*.dump' -mtime +"$LOCAL_DAYS" -delete \
    || echo "WARN: local retention failed"
  rclone delete "$REMOTE" --include "vectorizer-*.dump" --min-age "${REMOTE_DAYS}d" \
    || echo "WARN: remote retention failed"
fi

echo "Vectorizer backup complete: $TS"
