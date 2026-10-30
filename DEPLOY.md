# Deploy (shared VPS)

Vectorizer runs as an isolated docker compose project (`vectorizer`) in
`/opt/vectorizer`, next to other projects on the same server. It shares nothing
with them: own directory, network (`vectorizer_net`), volumes
(`vectorizer_pgdata`, `vectorizer_data`) and port.

```
reverse proxy / tunnel -> http://localhost:4300 -> web (nginx, 64 MB)
                                                |- /      static React build
                                                `- /api/  -> backend:8000 (FastAPI, 2 CPU / 1.5 GB)
                                                                `- postgres:5432 (0.5 CPU / 384 MB)
```

- Only `127.0.0.1:4300` is published on the host. The DB has no host port.
- Point your public hostname (`<your-domain>`) at `http://localhost:4300` on the server.
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
