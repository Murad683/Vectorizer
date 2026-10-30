#!/bin/bash
# Started by systemd (OnFailure= of vectorizer-db-backup.service) when the
# nightly backup fails. Always records the failure; sends a Telegram message
# only if /etc/vectorizer-backup-notify.env exists (root-only, optional):
#
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
#
# Installed as a root-owned copy in /usr/local/bin, like the backup script.
set -uo pipefail
umask 077

DIR="${VECTORIZER_BACKUP_DIR:-/var/backups/vectorizer}"
CONF="${VECTORIZER_NOTIFY_CONF:-/etc/vectorizer-backup-notify.env}"

mkdir -p "$DIR"
msg="Vectorizer database backup FAILED on $(hostname) at $(date -u +%Y-%m-%dT%H:%M:%SZ). Details: journalctl -u vectorizer-db-backup.service"

echo "$msg" >>"$DIR/FAILED"
logger -t vectorizer-backup -p user.crit "$msg"

if [ -r "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    # The token goes through stdin (-K -) so it never shows up in `ps`.
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" \
      | curl -fsS -m 20 -K - -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=${msg}" \
        >/dev/null || echo "notify: sending the Telegram message failed" >&2
  fi
fi
exit 0
