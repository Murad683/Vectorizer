#!/bin/bash
# Started by systemd (OnFailure= of vectorizer-db-backup.service) when the
# nightly backup fails. Records the failure in two places that cannot be
# missed by whoever checks the server:
#   * a line appended to $DIR/FAILED
#   * a critical entry in the journal (journalctl -t vectorizer-backup)
#
# Installed as a root-owned copy in /usr/local/bin, like the backup script.
set -uo pipefail
umask 077

DIR="${VECTORIZER_BACKUP_DIR:-/var/backups/vectorizer}"

mkdir -p "$DIR"
msg="Vectorizer database backup FAILED on $(hostname) at $(date -u +%Y-%m-%dT%H:%M:%SZ). Details: journalctl -u vectorizer-db-backup.service"

echo "$msg" >>"$DIR/FAILED"
logger -t vectorizer-backup -p user.crit "$msg"
exit 0
