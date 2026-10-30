#!/bin/bash
# Offline test of the backup scripts with stubbed docker, rclone and curl.
# Touches nothing outside a temporary directory:  bash deploy/backup/test-backup.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUBS="$TMP/stubs"
mkdir -p "$STUBS"

cat >"$STUBS/docker" <<'STUB'
#!/bin/bash
case "$1" in
  inspect) echo "${STUB_RUNNING:-true}" ;;
  exec)
    shift
    [ "$1" = "-i" ] && shift
    shift # container name
    if [ "$1" = "sh" ]; then # pg_dump
      [ "${STUB_MODE:-ok}" = "empty" ] || echo "PGDMP-fake-dump"
    elif [ "$1" = "pg_restore" ]; then
      cat >/dev/null
      case "${STUB_MODE:-ok}" in
        unreadable) exit 1 ;;
        badlist) echo "; no data tables" ;;
        *) printf '%s\n' "1; TABLE DATA public users v" "2; TABLE DATA public jobs v" ;;
      esac
    fi ;;
esac
STUB

cat >"$STUBS/rclone" <<'STUB'
#!/bin/bash
echo "rclone $*" >>"$STUB_LOG"
if [ "$1" = "lsf" ]; then
  [ -n "${STUB_LSF_EMPTY:-}" ] && exit 0
  # print the file name given to --include
  while [ $# -gt 0 ]; do [ "$1" = "--include" ] && echo "$2"; shift; done
fi
STUB

cat >"$STUBS/curl" <<'STUB'
#!/bin/bash
echo "curl $*" >>"$STUB_LOG"
cat >>"$STUB_LOG" # the config read from stdin (-K -)
STUB

cat >"$STUBS/logger" <<'STUB'
#!/bin/bash
echo "logger $*" >>"$STUB_LOG"
STUB
chmod +x "$STUBS"/*

PASS=0
FAIL=0
check() { # description, command...
  local desc="$1"
  shift
  if "$@"; then PASS=$((PASS + 1)); echo "ok   - $desc"; else FAIL=$((FAIL + 1)); echo "FAIL - $desc"; fi
}

run_backup() { # extra env assignments...; sets $RC, $DIR, $LOG
  DIR="$TMP/b$RANDOM"
  LOG="$TMP/log$RANDOM"
  : >"$LOG"
  mkdir -p "$DIR"
  env PATH="$STUBS:$PATH" STUB_LOG="$LOG" VECTORIZER_BACKUP_DIR="$DIR" \
    VECTORIZER_BACKUP_REMOTE="remote:bucket" "$@" bash "$HERE/vectorizer-db-backup.sh" \
    >"$LOG.out" 2>&1
  RC=$?
}

dumps() { ls "$DIR"/vectorizer-*.dump 2>/dev/null | wc -l; }
leftovers() { ls "$DIR"/*.partial "$DIR"/*.list 2>/dev/null | wc -l; }
logged() { grep -q -- "$1" "$LOG"; }

echo "== success"
run_backup STUB_MODE=ok
check "exit 0" test "$RC" -eq 0
check "one dump kept" test "$(dumps)" -eq 1
check "LAST_SUCCESS written" test -s "$DIR/LAST_SUCCESS"
check "no partial/list leftovers" test "$(leftovers)" -eq 0
check "uploaded once" test "$(grep -c '^rclone copy' "$LOG")" -eq 1
check "remote retention is filtered" logged 'rclone delete remote:bucket --include vectorizer-\*.dump --min-age 30d'
check "real run has no --dry-run" bash -c "! grep -q -- '--dry-run' '$LOG'"

echo "== failures stop before upload and before LAST_SUCCESS"
for mode in empty badlist unreadable; do
  run_backup STUB_MODE=$mode
  check "$mode: non-zero exit" test "$RC" -ne 0
  check "$mode: no dump kept" test "$(dumps)" -eq 0
  check "$mode: nothing uploaded" bash -c "! grep -q '^rclone copy' '$LOG'"
  check "$mode: no LAST_SUCCESS" test ! -e "$DIR/LAST_SUCCESS"
  check "$mode: no leftovers" test "$(leftovers)" -eq 0
done

run_backup STUB_RUNNING=false
check "container not running: non-zero exit" test "$RC" -ne 0
check "container not running: nothing uploaded" bash -c "! grep -q '^rclone copy' '$LOG'"

run_backup STUB_LSF_EMPTY=1
check "upload not confirmed: non-zero exit" test "$RC" -ne 0
check "upload not confirmed: no LAST_SUCCESS" test ! -e "$DIR/LAST_SUCCESS"

echo "== local retention only touches old vectorizer-*.dump files"
DIR="$TMP/keep"
mkdir -p "$DIR"
touch -d "10 days ago" "$DIR/vectorizer-old.dump" "$DIR/other-old.dump" "$DIR/notes.txt"
touch "$DIR/vectorizer-recent.dump"
LOG="$TMP/log-ret"
: >"$LOG"
env PATH="$STUBS:$PATH" STUB_LOG="$LOG" VECTORIZER_BACKUP_DIR="$DIR" \
  VECTORIZER_BACKUP_REMOTE="remote:bucket" bash "$HERE/vectorizer-db-backup.sh" >/dev/null 2>&1
check "old vectorizer dump deleted" test ! -e "$DIR/vectorizer-old.dump"
check "recent vectorizer dump kept" test -e "$DIR/vectorizer-recent.dump"
check "other old dump kept" test -e "$DIR/other-old.dump"
check "other file kept" test -e "$DIR/notes.txt"

echo "== dry run deletes nothing"
DIR="$TMP/dry"
mkdir -p "$DIR"
touch -d "10 days ago" "$DIR/vectorizer-old.dump"
LOG="$TMP/log-dry"
: >"$LOG"
env PATH="$STUBS:$PATH" STUB_LOG="$LOG" VECTORIZER_BACKUP_DIR="$DIR" \
  VECTORIZER_BACKUP_REMOTE="remote:bucket" RETENTION_DRY_RUN=1 \
  bash "$HERE/vectorizer-db-backup.sh" >"$LOG.out" 2>&1
check "old file still there" test -e "$DIR/vectorizer-old.dump"
check "dry run lists it" grep -q 'vectorizer-old.dump' "$LOG.out"
check "remote delete is --dry-run" logged '--dry-run'

echo "== failure notice"
DIR="$TMP/n1"
LOG="$TMP/log-n1"
: >"$LOG"
env PATH="$STUBS:$PATH" STUB_LOG="$LOG" VECTORIZER_BACKUP_DIR="$DIR" \
  VECTORIZER_NOTIFY_CONF="$TMP/missing.env" bash "$HERE/vectorizer-db-backup-notify.sh"
check "FAILED file written" test -s "$DIR/FAILED"
check "logged to the journal" logged '^logger'
check "no Telegram call without config" bash -c "! grep -q '^curl' '$LOG'"

DIR="$TMP/n2"
LOG="$TMP/log-n2"
: >"$LOG"
printf 'TELEGRAM_BOT_TOKEN=SECRET123\nTELEGRAM_CHAT_ID=42\n' >"$TMP/notify.env"
env PATH="$STUBS:$PATH" STUB_LOG="$LOG" VECTORIZER_BACKUP_DIR="$DIR" \
  VECTORIZER_NOTIFY_CONF="$TMP/notify.env" bash "$HERE/vectorizer-db-backup-notify.sh"
check "Telegram call made" logged '^curl'
check "token only on stdin, not in arguments" bash -c "grep '^curl' '$LOG' | grep -vq SECRET123"
check "token reached curl via stdin" logged 'bot SECRET123\|botSECRET123'

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
