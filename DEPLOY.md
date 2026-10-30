# Deploy (shared VPS)

Vectorizer runs as an isolated docker compose project (`vectorizer`) in
`/opt/vectorizer`, next to other projects on the same server. It shares nothing
with them: own directory, network (`vectorizer_net`), volumes
(`vectorizer_pgdata`, `vectorizer_data`) and port.

```
Cloudflare -> cloudflared (own tunnel, 0.25 CPU / 128 MB) -> http://web:8080
                                                |- /      static React build
                                                `- /api/  -> backend:8000 (FastAPI, 2 CPU / 1.5 GB)
                                                                `- postgres:5432 (0.5 CPU / 384 MB)
```

- Only `127.0.0.1:4300` (web, for local health checks) is published on the host.
  The DB and the tunnel have no host port.
- The project has its own Cloudflare tunnel (`cloudflared` service). In Zero Trust
  create a tunnel and add a public hostname `<your-domain>` with service type
  `HTTP` and URL `web:8080` (the address inside `vectorizer_net`, not localhost).
- Secrets live only in `/opt/vectorizer/.env` (chmod 600). Template:
  `deploy/env.production.example`.
- The backend runs a single uvicorn process (the job worker is in-process);
  do not add workers. Migrations run automatically on start (`alembic upgrade head`).

## Deploy

```bash
bash /opt/vectorizer/deploy/deploy.sh                      # origin/main
BRANCH=deploy/contabo bash /opt/vectorizer/deploy/deploy.sh # another branch
```

`deploy.sh` does `git fetch` + `reset --hard`, tags the current backend image as
`vectorizer-backend:rollback`, runs `docker compose -p vectorizer up -d --build`
and checks `http://127.0.0.1:4300/api/health`. It never runs a global prune.

## Roll back

```bash
cd /opt/vectorizer
git reset --hard <previous-commit>
docker compose -p vectorizer -f docker-compose.prod.yml up -d --build
```

## Remove the project completely

Only the `vectorizer` project is affected; the volumes (data!) are deleted with `-v`.

```bash
cd /opt/vectorizer && docker compose -p vectorizer -f docker-compose.prod.yml down -v
```

## Abuse and cost limits

Enforced by the app and nginx; the numbers are defaults and can be changed in
`/opt/vectorizer/.env` (restart the backend afterwards).

| Limit | Where | Default |
|---|---|---|
| Login attempts | nginx, per visitor address (`CF-Connecting-IP`) | 10 per minute, burst 5, then HTTP 429 |
| `/api/docs`, `/api/redoc`, `/api/openapi.json` | nginx | blocked (404) |
| PDF pages | `MAX_PAGES_PER_FILE` | 50 |
| Jobs waiting or processing per user | `MAX_ACTIVE_JOBS_PER_USER` | 3 |
| Jobs per user per rolling 24 hours | `MAX_JOBS_PER_USER_PER_DAY` | 30 (deleting a job does not give it back) |
| Gemini requests per job, retries included | `MAX_VISION_CALLS_PER_JOB` | 25 (the job fails, no retry) |

Check the nginx rules on the server (the first requests answer 401, later ones
429; a different `CF-Connecting-IP` is counted separately):

```bash
for i in $(seq 1 9); do curl -s -o /dev/null -w "%{http_code} " -X POST \
  -H "CF-Connecting-IP: 203.0.113.10" -H "Content-Type: application/json" \
  -d '{"email":"x@x.x","password":"x"}' http://127.0.0.1:4300/api/auth/login; done; echo
curl -s -o /dev/null -w "other address: %{http_code}\n" -X POST \
  -H "CF-Connecting-IP: 203.0.113.11" -H "Content-Type: application/json" \
  -d '{"email":"x@x.x","password":"x"}' http://127.0.0.1:4300/api/auth/login
curl -s -o /dev/null -w "docs: %{http_code}\n" http://127.0.0.1:4300/api/docs
curl -s -o /dev/null -w "openapi: %{http_code}\n" http://127.0.0.1:4300/api/openapi.json
curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:4300/api/health
```

## Own Cloudflare tunnel

The `cloudflared` service reads `TUNNEL_TOKEN` from `/opt/vectorizer/.env`. The
compose file refuses to run without it, so **write the token before pulling a
version that contains the `cloudflared` service** (`git fetch/reset`), never after.
The backend container gets the variable blanked, so it never sees the token.

On the server (log in first, then run the lines one by one; the token is typed
silently):

```bash
ssh -i <key> root@<SERVER_IP>
grep -c '^TUNNEL_TOKEN=' /opt/vectorizer/.env     # must print 0
read -rsp 'Tunnel token: ' T; echo
printf '\nTUNNEL_TOKEN=%s\n' "$T" >> /opt/vectorizer/.env
unset T
awk -F= '/^TUNNEL_TOKEN=/ {print "TUNNEL_TOKEN set, length " length($2)}' /opt/vectorizer/.env
ls -l /opt/vectorizer/.env                       # -rw------- deploy deploy
```

Start only the tunnel, then check it:

```bash
docker compose -p vectorizer -f docker-compose.prod.yml up -d --no-build cloudflared
docker logs --tail 20 vectorizer-cloudflared      # "Registered tunnel connection"
docker inspect -f '{{.State.Health.Status}}' vectorizer-cloudflared
```

Remove it again: `docker compose -p vectorizer -f docker-compose.prod.yml rm -sf cloudflared`,
delete the `TUNNEL_TOKEN` line from `.env` and delete the tunnel in Zero Trust.

## Database backup

A separate nightly job backs up the Vectorizer database (independent of any other
backup job on the host, so a failure in one never stops the other).

- `deploy/backup/vectorizer-db-backup.sh` runs `pg_dump -Fc` inside the `vectorizer-db`
  container (credentials come from the container's own environment), checks that the
  dump is non-empty and readable by `pg_restore --list` and contains the `users` and
  `jobs` data, uploads it to the `vectorizer-db-backups` bucket (`rclone` remote
  `contabo:`), confirms the upload, and only then writes `LAST_SUCCESS`.
- Retention: 7 days locally (`/var/backups/vectorizer`), 30 days remotely. Both deletions
  only touch `vectorizer-*.dump` (the remote one via `--include "vectorizer-*.dump"`).
  `RETENTION_DRY_RUN=1` shows what would be deleted without deleting.
- Schedule: `vectorizer-db-backup.timer`, every day 03:30 (+ up to 2 minutes).
- **Install as root-owned copies** (never run it from `/opt/vectorizer`: that directory belongs
  to the deploy user, and root must not execute a file the deploy user can change):

```bash
install -m 755 -o root -g root deploy/backup/vectorizer-db-backup.sh        /usr/local/bin/
install -m 755 -o root -g root deploy/backup/vectorizer-db-backup-notify.sh /usr/local/bin/
install -m 644 -o root -g root deploy/backup/vectorizer-db-backup.service        /etc/systemd/system/
install -m 644 -o root -g root deploy/backup/vectorizer-db-backup-notify.service /etc/systemd/system/
install -m 644 -o root -g root deploy/backup/vectorizer-db-backup.timer          /etc/systemd/system/
systemctl daemon-reload
# first time: once by hand, retention only previewed
RETENTION_DRY_RUN=1 /usr/local/bin/vectorizer-db-backup.sh
systemctl enable --now vectorizer-db-backup.timer
```

### Knowing when a backup failed
- `OnFailure=` starts `vectorizer-db-backup-notify.service`, which appends a line to
  `/var/backups/vectorizer/FAILED`, writes to the journal (`logger -p user.crit`) and, if
  `/etc/vectorizer-backup-notify.env` exists (root-only, optional, with
  `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=`), sends a Telegram message.
- `LAST_SUCCESS` (one line: UTC time, file name, size) exists only after a verified upload.
  It also reveals runs that never happened (server off, timer disabled), which `OnFailure=`
  cannot see. Stale check (prints a warning when the last good backup is older than 26 hours):

```bash
find /var/backups/vectorizer/LAST_SUCCESS -mmin -1560 | grep -q . || echo "BACKUP IS STALE"
```

### Restore
```bash
# list the contents of a dump
docker exec -i vectorizer-db pg_restore --list < vectorizer-YYYYmmdd-HHMMSS.dump
# restore into a scratch database first, never straight over the live one
```
Download an older dump with `rclone copy contabo:vectorizer-db-backups/<file> .`.

### Not backed up
The Docker volume `vectorizer_data` (uploaded input files and generated DOCX/PPTX) and
`/opt/vectorizer/.env`. Losing the volume keeps the job rows in the database but removes
their files.

### Test the scripts offline
`bash deploy/backup/test-backup.sh` runs both scripts against stubbed `docker`, `rclone`
and `curl` in a temporary directory.
