#!/bin/bash
# Offline test of vectorizer-ci-deploy.sh with stubbed git, docker, curl, flock
# and sleep. Touches nothing outside a temporary directory:
#   bash deploy/ci/test-ci-deploy.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/vectorizer-ci-deploy.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUBS="$TMP/stubs"
APP="$TMP/app"
STATE="$TMP/state"
mkdir -p "$STUBS" "$APP/backups" "$STATE"
touch "$APP/.env"

NEW=1111111111111111111111111111111111111111
OLD=2222222222222222222222222222222222222222
OFFMAIN=3333333333333333333333333333333333333333

cat >"$STUBS/git" <<'STUB'
#!/bin/bash
echo "git $*" >>"$STUB_LOG"
case "$1" in
  fetch) ;;
  cat-file) [ "$3" = "9999999999999999999999999999999999999999^{commit}" ] && exit 1; exit 0 ;;
  merge-base) [ "$3" = "3333333333333333333333333333333333333333" ] && exit 1; exit 0 ;;
  rev-parse) cat "$STUB_STATE/head" ;;
  reset)
    echo "$3" >"$STUB_STATE/head"
    echo x >"$STUB_STATE/written-by-reset"
    # going back to the old commit brings the old (healthy) version back
    [ "$3" = "2222222222222222222222222222222222222222" ] && echo ok >"$STUB_STATE/health"
    ;;
  log) echo "abc1234 fake commit" ;;
esac
exit 0
STUB

cat >"$STUBS/docker" <<'STUB'
#!/bin/bash
echo "docker $*" >>"$STUB_LOG"
case "$1" in
  exec)
    shift
    [ "$1" = "-i" ] && shift
    shift # container
    if [ "$1" = "sh" ]; then echo "PGDMP-fake-dump"; fi
    if [ "$1" = "pg_restore" ]; then cat >/dev/null; echo "1; TABLE DATA public users v"; fi ;;
  inspect) echo "${STUB_CONTAINER:-healthy}" ;;
  image) exit 0 ;;
  tag) ;;
  compose)
    [ -n "${STUB_UP_FAIL:-}" ] && [[ "$*" == *"--build"* ]] && { echo "build failed" >&2; exit 1; }
    echo "compose ok" ;;
esac
exit 0
STUB

cat >"$STUBS/curl" <<'STUB'
#!/bin/bash
[ "$(cat "$STUB_STATE/health")" = ok ] && { echo '{"status":"ok"}'; exit 0; }
exit 22
STUB

cat >"$STUBS/flock" <<'STUB'
#!/bin/bash
[ -n "${STUB_FLOCK_FAIL:-}" ] && exit 1
exit 0
STUB

printf '#!/bin/bash\nexit 0\n' >"$STUBS/sleep"
chmod +x "$STUBS"/*

PASS=0
FAIL=0
ok() { echo "  ok: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# run <request> [VAR=value ...] -> sets RC and OUT
run() {
  local request="$1"
  shift
  : >"$TMP/log"
  OUT="$(env "$@" SSH_ORIGINAL_COMMAND="$request" PATH="$STUBS:$PATH" \
    STUB_LOG="$TMP/log" STUB_STATE="$STATE" VECTORIZER_APP_DIR="$APP" \
    VECTORIZER_HEALTH_TRIES=3 VECTORIZER_KEEP_DUMPS=3 bash "$SCRIPT" 2>&1)"
  RC=$?
}
reset_state() {
  echo "$OLD" >"$STATE/head"
  echo ok >"$STATE/health"
  rm -f "$APP"/backups/pre-deploy-*.dump "$APP"/backups/.prev_sha "$APP"/backups/.last_ok_sha
}
logged() { grep -q -- "$1" "$TMP/log"; }

echo "# requests that must be refused"
reset_state
for req in "" "ls" "bash" "deploy" "deploy abc" "deploy $NEW extra" "rollback now" "status all" "deploy ${NEW}; id" 'deploy $(id)'; do
  run "$req"
  if [ "$RC" -eq 2 ] && ! logged "docker" && ! logged "git"; then ok "refused: '$req'"; else bad "'$req' rc=$RC"; fi
done

echo "# commits that must be refused"
run "deploy 9999999999999999999999999999999999999999"
{ [ "$RC" -eq 1 ] && ! logged "reset"; } && ok "unknown commit" || bad "unknown commit rc=$RC"
run "deploy $OFFMAIN"
{ [ "$RC" -eq 1 ] && [[ "$OUT" == *"not on origin/main"* ]] && ! logged "reset"; } && ok "commit that is not on main" || bad "off-main rc=$RC"

echo "# happy path"
reset_state
run "deploy $NEW"
[ "$RC" -eq 0 ] && ok "exit 0" || bad "rc=$RC: $OUT"
ls "$APP"/backups/pre-deploy-*-1111111.dump >/dev/null 2>&1 && ok "dump taken before the change" || bad "no dump"
if [ "$(uname -o)" = "Msys" ]; then
  echo "  skip: file modes are not meaningful on this system"
else
  [ "$(stat -c%a "$(ls "$APP"/backups/pre-deploy-*.dump | head -1)")" = 600 ] && ok "dump is private (600)" || bad "dump mode"
fi
logged "docker tag vectorizer-backend:latest vectorizer-backend:rollback" && ok "rollback image tagged" || bad "no rollback tag"
logged "git reset --hard $NEW" && ok "reset to the new commit" || bad "no reset"
if [ "$(uname -o)" != "Msys" ]; then
  [ "$(stat -c%a "$STUB_STATE/written-by-reset")" = 644 ] && ok "files written by git stay readable (644)" || bad "git files mode"
fi
logged "up -d --build" && ok "built and started" || bad "no build"
[ "$(cat "$APP/backups/.prev_sha")" = "$OLD" ] && ok "previous commit recorded" || bad "prev_sha"
[ "$(cat "$APP/backups/.last_ok_sha")" = "$NEW" ] && ok "last good commit recorded" || bad "last_ok_sha"
grep -E "^docker .*prune" "$TMP/log" >/dev/null && bad "a docker prune ran" || ok "no docker prune anywhere"
# the dump must come before the reset
first_dump=$(grep -n "pg_dump" "$TMP/log" | head -1 | cut -d: -f1)
first_reset=$(grep -n "git reset" "$TMP/log" | head -1 | cut -d: -f1)
{ [ -n "$first_dump" ] && [ "$first_dump" -lt "$first_reset" ]; } && ok "dump before reset" || bad "order dump/reset"

echo "# nothing to do when it already runs and is healthy"
run "deploy $NEW"
{ [ "$RC" -eq 0 ] && ! logged "up -d" && ! logged "pg_dump"; } && ok "no rebuild, no dump" || bad "already-deployed rc=$RC"

echo "# new version unhealthy: automatic rollback"
reset_state
echo bad >"$STATE/health" # the new version never becomes healthy
run "deploy $NEW"
[ "$RC" -eq 1 ] && ok "exit 1" || bad "rc=$RC"
logged "git reset --hard $OLD" && ok "went back to the old commit" || bad "no reset back"
logged "up -d --no-build" && ok "restarted without building" || bad "no --no-build"
logged "docker tag vectorizer-backend:rollback vectorizer-backend:latest" && ok "old image restored" || bad "image not restored"
[[ "$OUT" == *"runs again"* ]] && ok "clear message" || bad "message: $OUT"
[ ! -e "$APP/backups/.last_ok_sha" ] && ok "failed deploy not recorded as good" || bad "last_ok_sha written"

echo "# build fails: automatic rollback"
reset_state
run "deploy $NEW" STUB_UP_FAIL=1
{ [ "$RC" -eq 1 ] && logged "git reset --hard $OLD"; } && ok "rolled back after a failed build" || bad "build failure rc=$RC"

echo "# backup fails: nothing is changed"
reset_state
run "deploy $NEW" STUB_CONTAINER=healthy
mv "$STUBS/docker" "$STUBS/docker.real"
cat >"$STUBS/docker" <<'STUB'
#!/bin/bash
echo "docker $*" >>"$STUB_LOG"
[ "$1" = "exec" ] && exit 1
exit 0
STUB
chmod +x "$STUBS/docker"
run "deploy $NEW"
{ [ "$RC" -eq 1 ] && ! logged "git reset" && [[ "$OUT" == *"nothing was changed"* ]]; } && ok "no change when the dump fails" || bad "dump failure rc=$RC"
mv "$STUBS/docker.real" "$STUBS/docker"

echo "# another deploy is running"
reset_state
run "deploy $NEW" STUB_FLOCK_FAIL=1
{ [ "$RC" -eq 1 ] && ! logged "git reset" && [[ "$OUT" == *"another deploy"* ]]; } && ok "lock respected" || bad "lock rc=$RC"

echo "# dump rotation keeps only the newest and only pre-deploy files"
reset_state
touch "$APP/backups/vectorizer-keep-me.dump" "$APP/backups/notes.txt"
for n in 1 2 3 4 5; do : >"$APP/backups/pre-deploy-2026010$n-000000-aaaaaaa.dump"; touch -d "2026-01-0$n" "$APP/backups/pre-deploy-2026010$n-000000-aaaaaaa.dump"; done
run "deploy $NEW"
left=$(ls "$APP"/backups/pre-deploy-*.dump | wc -l)
{ [ "$left" -eq 3 ] && [ -e "$APP/backups/vectorizer-keep-me.dump" ] && [ -e "$APP/backups/notes.txt" ]; } && ok "3 newest kept, other files untouched" || bad "rotation left=$left"

echo "# rollback request"
reset_state
echo "$OLD" >"$APP/backups/.prev_sha"
echo "$NEW" >"$STATE/head"
run "rollback"
{ [ "$RC" -eq 0 ] && logged "git reset --hard $OLD"; } && ok "rollback goes to the recorded commit" || bad "rollback rc=$RC: $OUT"
[ "$(cat "$APP/backups/.prev_sha")" = "$NEW" ] && ok "a second rollback returns" || bad "prev_sha after rollback"

echo "# status is read-only"
reset_state
run "status"
{ [ "$RC" -eq 0 ] && ! logged "reset" && ! logged "compose" && ! logged "pg_dump"; } && ok "status changes nothing" || bad "status rc=$RC"

echo
echo "passed: $PASS, failed: $FAIL"
[ "$FAIL" -eq 0 ]
