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
