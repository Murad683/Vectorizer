#!/bin/bash
# Forced command of the GitHub Actions deploy key (see DEPLOY.md, "Automatic deploy").
#
# sshd runs THIS script for every login of that key, whatever the client asks
# for; the client's wish arrives in SSH_ORIGINAL_COMMAND. Only three requests
# are accepted, everything else is refused:
#
#   deploy <40-hex commit>   back up the database, deploy that commit of main,
#                            check health, roll back by itself if it fails
#   rollback                 return to the version that ran before the last deploy
#   status                   show what runs (read-only)
#
# Installed as a ROOT-OWNED copy in /usr/local/bin (like the backup scripts), so
# neither the deploy user nor the CI key can change it.
# Touches only the "vectorizer" compose project; never prunes anything global.
set -uo pipefail
# Files git writes into the app folder must stay readable: the containers run
# as an unprivileged user and build from this folder (a 600 config.py once
# stopped the backend from starting). Only the dumps are made private.
umask 022

APP_DIR="${VECTORIZER_APP_DIR:-/opt/vectorizer}"
PROJECT="vectorizer"
COMPOSE_FILE="docker-compose.prod.yml"
BACKUP_DIR="${VECTORIZER_DEPLOY_BACKUP_DIR:-$APP_DIR/backups}"
HEALTH_URL="${VECTORIZER_HEALTH_URL:-http://127.0.0.1:4300/api/health}"
HEALTH_TRIES="${VECTORIZER_HEALTH_TRIES:-45}" # x 2 s
KEEP_DUMPS="${VECTORIZER_KEEP_DUMPS:-7}"
CONTAINERS=(vectorizer-db vectorizer-backend vectorizer-web vectorizer-cloudflared)
SERVICES=(backend web)

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }
die() {
  log "ERROR: $*"
  exit 1
}

compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }

is_healthy() {
  curl -sf -m 5 "$HEALTH_URL" >/dev/null || return 1
  local name
  for name in "${CONTAINERS[@]}"; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null)" = healthy ] || return 1
  done
}

wait_healthy() {
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    is_healthy && return 0
    sleep 2
  done
  return 1
}

# Database dump taken BEFORE anything changes. Only pre-deploy-*.dump files in
# this directory are ever rotated; nothing else is deleted.
backup_db() {
  local tag="$1" file
  mkdir -p "$BACKUP_DIR" || return 1
  file="$BACKUP_DIR/pre-deploy-$(date -u +%Y%m%d-%H%M%S)-${tag}.dump"
  if ! (umask 077 && docker exec vectorizer-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$file"); then
    rm -f "$file"
    return 1
  fi
  if [ ! -s "$file" ] ||
    ! docker exec -i vectorizer-db pg_restore --list <"$file" | grep -q 'TABLE DATA public users'; then
    log "the dump $file is empty or unreadable"
    rm -f "$file"
    return 1
  fi
  log "database dump: $file ($(stat -c%s "$file") bytes)"
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/pre-deploy-*.dump 2>/dev/null | tail -n +"$((KEEP_DUMPS + 1))" | while read -r old; do
    rm -f -- "$old"
  done
  LAST_DUMP="$file"
  return 0
}

tag_rollback() {
  local svc
  for svc in "${SERVICES[@]}"; do
    if docker image inspect "vectorizer-$svc:latest" >/dev/null 2>&1; then
      docker tag "vectorizer-$svc:latest" "vectorizer-$svc:rollback"
    fi
  done
}

# Back to a known commit with the images saved before the deploy.
restore_previous() {
  local prev="$1" svc
  log "rolling back to ${prev:0:7}"
  git reset --hard "$prev" >/dev/null || return 1
  for svc in "${SERVICES[@]}"; do
    if docker image inspect "vectorizer-$svc:rollback" >/dev/null 2>&1; then
      docker tag "vectorizer-$svc:rollback" "vectorizer-$svc:latest"
    fi
  done
  compose up -d --no-build >/dev/null 2>&1 || return 1
  wait_healthy
}

acquire_lock() {
  mkdir -p "$BACKUP_DIR" || die "cannot create $BACKUP_DIR"
  exec 9>"$BACKUP_DIR/.deploy.lock"
  flock -n 9 || die "another deploy is running"
}

on_main() {
  git cat-file -e "$1^{commit}" 2>/dev/null || die "commit $1 is unknown"
  git merge-base --is-ancestor "$1" origin/main || die "commit $1 is not on origin/main; refusing"
}

do_deploy() {
  local sha="$1" prev start rc tmp
  start=$(date +%s)
  cd "$APP_DIR" || die "no $APP_DIR"
  [ -f .env ] || die ".env is missing"
  acquire_lock
  git fetch --prune origin >/dev/null 2>&1 || die "git fetch failed"
  on_main "$sha"
  prev="$(git rev-parse HEAD)"
  if [ "$prev" = "$sha" ] && is_healthy; then
    log "already at ${sha:0:7} and healthy; nothing to do"
    return 0
  fi
  log "deploying ${sha:0:7} (running: ${prev:0:7})"
  backup_db "${sha:0:7}" || die "database backup failed; nothing was changed"
  tag_rollback
  echo "$prev" >"$BACKUP_DIR/.prev_sha"
  git reset --hard "$sha" >/dev/null || die "git reset failed"

  log "building and starting"
  tmp="$(mktemp)"
  compose up -d --build >"$tmp" 2>&1
  rc=$?
  tail -15 "$tmp"
  rm -f "$tmp"
  if [ "$rc" -ne 0 ] || ! wait_healthy; then
    log "the new version did not become healthy"
    if restore_previous "$prev"; then
      die "deploy of ${sha:0:7} failed and ${prev:0:7} runs again (dump kept: ${LAST_DUMP:-none})"
    fi
    die "deploy failed AND the rollback failed; restore by hand (dump: ${LAST_DUMP:-none})"
  fi
  echo "$sha" >"$BACKUP_DIR/.last_ok_sha"
  log "done: ${sha:0:7} is healthy ($(($(date +%s) - start)) s)"
}

do_rollback() {
  local prev current
  cd "$APP_DIR" || die "no $APP_DIR"
  acquire_lock
  git fetch --prune origin >/dev/null 2>&1 || die "git fetch failed"
  [ -s "$BACKUP_DIR/.prev_sha" ] || die "no earlier version is recorded"
  prev="$(cat "$BACKUP_DIR/.prev_sha")"
  [[ "$prev" =~ ^[0-9a-f]{40}$ ]] || die "the recorded version is not a commit id"
  on_main "$prev"
  current="$(git rev-parse HEAD)"
  backup_db "rollback" || die "database backup failed; nothing was changed"
  echo "$current" >"$BACKUP_DIR/.prev_sha"
  restore_previous "$prev" || die "rollback failed; restore by hand (dump: ${LAST_DUMP:-none})"
  log "done: ${prev:0:7} runs again"
}

do_status() {
  cd "$APP_DIR" || die "no $APP_DIR"
  log "commit: $(git log -1 --format='%h %s' 2>/dev/null)"
  log "last good deploy: $(cut -c1-7 "$BACKUP_DIR/.last_ok_sha" 2>/dev/null || echo none)"
  local name
  for name in "${CONTAINERS[@]}"; do
    log "$name: $(docker inspect -f '{{.State.Status}} / {{.State.Health.Status}}' "$name" 2>&1)"
  done
  if curl -sf -m 5 "$HEALTH_URL" >/dev/null; then log "health: ok"; else log "health: FAILED"; fi
}

request="${SSH_ORIGINAL_COMMAND:-}"
read -r verb arg extra <<<"$request"
if [ -n "${extra:-}" ]; then
  echo "refused: too many arguments" >&2
  exit 2
fi

case "$verb" in
  deploy)
    [[ "${arg:-}" =~ ^[0-9a-f]{40}$ ]] || {
      echo "refused: deploy needs a 40-character commit id" >&2
      exit 2
    }
    do_deploy "$arg"
    ;;
  rollback)
    [ -z "${arg:-}" ] || {
      echo "refused: rollback takes no argument" >&2
      exit 2
    }
    do_rollback
    ;;
  status)
    [ -z "${arg:-}" ] || {
      echo "refused: status takes no argument" >&2
      exit 2
    }
    do_status
    ;;
  *)
    echo "refused: this key may only run: deploy <commit> | rollback | status" >&2
    exit 2
    ;;
esac
