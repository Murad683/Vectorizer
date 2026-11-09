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

## Automatic deploy

After a push to `main`, when the CI workflow is green, the **Deploy** workflow
(`.github/workflows/deploy.yml`) updates the server by itself; it can also be run by
hand (Actions → Deploy → Run workflow: `status`, `deploy` or `rollback`). A change that
touches only Markdown files is not deployed.

How it is kept safe:

- The workflow uses its own SSH key. On the server that key is in
  `~deploy/.ssh/authorized_keys2` with `restrict,command="/usr/local/bin/vectorizer-ci-deploy"`:
  it can run only that script, no shell, no port forwarding. The other keys on the server are
  not touched.
- `/usr/local/bin/vectorizer-ci-deploy` is a **root-owned copy** of `deploy/ci/vectorizer-ci-deploy.sh`
  (the deploy user and the CI key cannot change it). It accepts only `deploy <40-hex commit>`,
  `rollback` and `status`; a commit is accepted only if it is on `origin/main`; a lock stops two
  deploys at once.
- Before a deploy it takes a database dump (`/opt/vectorizer/backups/pre-deploy-*.dump`, newest 7
  kept), tags the running images `:rollback`, builds, and waits for `/api/health` and for the four
  containers to be healthy. If that fails it puts the previous commit and images back by itself
  and the workflow turns red (GitHub sends its usual e-mail). A schema migration is **not** undone
  by that; restore the pre-deploy dump by hand if one ever has to be.
- The workflow runs in the `production` environment (only `main` may use it), so its secrets are
  never available to pull-request branches.

One-time setup:

1. Key pair on your own computer (PowerShell); send ONLY the `.pub` line to whoever installs it:
   ```
   ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\vectorizer_ci" -N '""' -C github-actions-vectorizer
   Get-Content "$env:USERPROFILE\.ssh\vectorizer_ci.pub"
   ```
2. Server (root): install the script and the key line (never edit `authorized_keys`):
   ```
   install -m 755 -o root -g root /opt/vectorizer/deploy/ci/vectorizer-ci-deploy.sh /usr/local/bin/vectorizer-ci-deploy
   install -m 600 -o deploy -g deploy /dev/null /home/deploy/.ssh/authorized_keys2
   printf '%s
' 'restrict,command="/usr/local/bin/vectorizer-ci-deploy" <the .pub line>' >> /home/deploy/.ssh/authorized_keys2
   ```
3. GitHub → Settings → Environments → **production** (deployment branches: `main` only) with the secrets
   `DEPLOY_SSH_KEY` (the private key file), `DEPLOY_HOST` (server IP), `DEPLOY_USER` (`deploy`) and
   `DEPLOY_KNOWN_HOSTS` (the line printed by `ssh-keyscan -t ed25519 <IP>`; its fingerprint must
   match `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server).

Remove it again: delete `/home/deploy/.ssh/authorized_keys2` and
`/usr/local/bin/vectorizer-ci-deploy`, delete the four secrets, disable the Deploy workflow.
Manual `deploy/deploy.sh` keeps working.

Tests without a server: `bash deploy/ci/test-ci-deploy.sh` (also run by CI).

## Page mode

An uploaded image (PNG/JPG) is read as a whole page: the printed text becomes
editable text in reading order, the flowchart stays editable shapes, and every
other picture (graphs, pattern matrices, drawn options) is cut out of the image
and placed in the same spot. Still ONE Gemini request per image. Faint text
showing through the paper, handwriting and half lines at the image edge are
dropped by the prompt and by an ink-contrast check in the code.

- Switch off: `PAGE_MODE_ENABLED=false` in `/opt/vectorizer/.env`, then
  `docker compose -p vectorizer -f docker-compose.prod.yml up -d --no-build backend`.
- Images embedded in a DOCX/PDF keep the single-diagram behaviour.

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
  `/var/backups/vectorizer/FAILED` and writes a critical journal entry
  (`journalctl -t vectorizer-backup`). There is no push notification (no mail or
  chat channel on the host): somebody has to look, so check the two files below.
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
and `logger` in a temporary directory.

## Input file cleanup

Uploaded input files are only needed while a job waits or runs. A daily job deletes
them `INPUT_RETENTION_HOURS` (default 24) after the job is `completed` or `failed`.

- **Deleted:** input files of old finished jobs; uploads that have no job row at all and are
  older than the limit (crashed uploads); empty `data/jobs/<uuid>` directories whose job is
  not waiting or running.
- **Never touched:** files of `pending` / `processing` jobs (a failed attempt puts a job
  back to `pending`, so its input stays for the retry), result files (DOCX/PPTX), any
  directory that still holds files, and the database rows. A directory with files but no
  job row is only reported in the log.
- At most `INPUT_CLEANUP_MAX_PER_RUN` (default 100) things per run; the rest waits for the
  next run. Only `unlink` and `rmdir` are used, never a recursive delete.
- `RESULT_RETENTION_DAYS` is reserved (0 = off) and not used by any code: results are
  never deleted automatically.

Dry run (lists what would be deleted, deletes nothing; `--hours 0` ignores the age):

```bash
docker exec vectorizer-backend python -m app.cleanup_inputs --dry-run
docker exec vectorizer-backend python -m app.cleanup_inputs --dry-run --hours 0
```

Schedule (root-owned copies, like the backup):

```bash
install -m 755 -o root -g root deploy/cleanup/vectorizer-input-cleanup.sh      /usr/local/bin/
install -m 644 -o root -g root deploy/cleanup/vectorizer-input-cleanup.service /etc/systemd/system/
install -m 644 -o root -g root deploy/cleanup/vectorizer-input-cleanup.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vectorizer-input-cleanup.timer     # every day 04:15
```

Every deleted file is one journal line: `journalctl -t vectorizer-input-cleanup`
(`DELETED ...` / `WOULD DELETE ...` and a summary). A failed run shows in
`systemctl status vectorizer-input-cleanup.service`.

What to tell customers: *uploaded source files are deleted from the server 24 hours after
the job finishes; the finished DOCX/PPTX files and the job history stay; to convert a source
file again, upload it again.*

## Usage log and report

Every finished attempt of a job writes one row to `job_usage` (migration 0004): pages, Gemini
requests, tokens, estimated cost, duration and outcome (`completed`, `failed`, `budget`,
`retry`). Numbers only: no file names and no text. Rows are never deleted, also not when a job
or a user is deleted. A failure to write the row never fails the job.

Read-only report (one SELECT; cost is an estimate from list prices, compare it with the Google
billing page):

```bash
docker exec vectorizer-backend python -m app.usage_report --days 7
```

`GEMINI_PRICE_IN_PER_M` / `GEMINI_PRICE_OUT_PER_M` (USD per million tokens, defaults 0.25 and
1.50) only affect this estimate; change them in `.env` when the model changes. Every request
also logs `gemini_usage kind=... in=... out=... thoughts=...` (lost when the container is
recreated; the table is the permanent record).

## Scanned PDFs

Each PDF page is sorted before anything is sent to Gemini:

- **Scan** (one image covers at least `PDF_SCAN_COVERAGE`, default 0.80, even with a hidden OCR
  text layer; or fewer than `PDF_MIN_TEXT_CHARS` characters (20) plus an image or drawing): the
  page is rendered at `PDF_SCAN_DPI` (150) and read as a whole page (text, flowchart, figures),
  one request per page. In the DOCX it starts on its own page, in the PPTX on its own slide.
- **Blank** (no text, image or drawing): skipped, no request, logged as `Page N is blank`.
- **Text** (real text layer): copied directly, no request. The reading order is checked: one
  column, two clear columns (read column after column) are handled; a layout that is not clear
  (three columns, side notes) is read as a scan instead, logged as `layout not clear`.

The per-job request ceiling of a PDF is `max(MAX_VISION_CALLS_PER_JOB, pages * VISION_CALLS_PER_PAGE
+ VISION_CALLS_EXTRA)` (defaults 25, 2, 5: 50 pages -> 105). Images and DOCX keep
`MAX_VISION_CALLS_PER_JOB`. `PAGE_MODE_ENABLED=false` makes scans fall back to the old
one-image-one-flowchart reading. Check what a PDF cost with `python -m app.usage_report`.

## Page answer checks

The model's page answer is checked before it is used (jurnal lines in `docker logs`):

- **Two pages in one photo** (an open book): when a clear empty strip separates the blocks into a
  left and a right page, the left page is written first (the model answers the right page first).
- **Text blocks without text:** when at least 2 blocks and 8 % of the text blocks come back with
  no text, the page is asked once more and the better answer is kept; a still incomplete answer is
  logged as `page_incomplete empty=N of M` (numbers only). Count them with
  `docker logs vectorizer-backend | grep page_incomplete`.
- **Answer cut at the token ceiling** (`Gemini answer cut at the output token ceiling`): the model
  is looping, so the request is not repeated; with 3 or more detected symbols the old
  one-diagram prompt gets one try, otherwise the original image is embedded. The ceiling
  (`PAGE_MAX_OUTPUT_TOKENS`) now also covers that old prompt.
- Figures are cut with padding that stops at the neighbouring text; `^{2}` / `_{n}` written into
  a plain text run become real superscripts / subscripts.

## Page selection (scanned PDFs)

A PDF of 2+ pages with at least one scanned page does not start at once. It gets the status
`awaiting_selection` and the owner sees small pictures of the pages (scan / text / blank) to pick
from; everything is ticked except blank pages. `POST /jobs/{id}/start` records the choice and queues
the job; `GET /jobs/{id}/pages` returns the pictures.

- Chosen pages are read as before (scans by Gemini, text pages directly). The others stay in the
  result as plain full-page pictures (`keep_image`, rendered at `PDF_KEPT_PAGE_DPI`, 110, no Gemini
  request), or are left out when the owner turns that option off. Blank pages that were not chosen
  are always left out. The result is always in page order.
- Choosing no page needs a confirmation (everything then stays a picture).
- Text-layer PDFs, Word and images are not affected: they start at once.
- A waiting job counts as active (the 3-jobs allowance). Nobody picking within
  `SELECTION_EXPIRY_HOURS` (24) fails the job ("Səhifə seçimi vaxtında edilmədi...") in the nightly
  cleanup run, which then removes the file.
- The request ceiling and `job_usage.pages` follow the chosen pages.
- Switch: `PAGE_SELECTION_ENABLED=false` in `/opt/vectorizer/.env` (append, then recreate the backend)
  makes scanned PDFs start at once again. Jobs already waiting would have to be started or deleted.
- Migration 0005 adds `page_count`, `selected_pages`, `keep_unselected` to `jobs`.

## Upload list and merged images

The upload box first collects files in a list (remove, add more, pick several); "Başla" sends
them, each file as its own job. With 2+ PNG/JPEG images in the list the owner can choose "all in one
document" and put them in order: the images go to `POST /jobs/merge` in one request and become ONE
job (one Word, one PowerPoint, one page/slide per image).

- The server keeps the images unchanged in `uploads/<job_id>.zip` (stored, not compressed; members
  `0001.png`, `0002.jpg`, ...). The worker reads each member exactly like a single uploaded image,
  so quality and cost per page are the same as separate uploads (one Gemini request per image).
- Only `POST /jobs/merge` creates such a zip; `POST /jobs` still refuses `.zip`. Input cleanup,
  job delete and `job_usage` (`file_ext` is `zip`, `pages` is the number of images) work unchanged.
- It counts as one job for the active-jobs allowance and the daily count. The request ceiling is
  `max(MAX_VISION_CALLS_PER_JOB, images * VISION_CALLS_PER_PAGE + VISION_CALLS_EXTRA)`. There is no
  limit on the number of images yet (each image keeps the 30 MB limit; the Cloudflare tunnel caps a
  request at 100 MB): to be decided in the final limits review.
- The job name is "first image (+N şəkil)"; the download name is the same.
- No migration and no new setting. Rolling back the code leaves a queued merged job unreadable for
  the old worker (it fails after its retries).

## Figure cut

The vision model sometimes gives a figure box that covers only part of the figure (a matrix without
its last column, answer pictures without their letters). The cut used to take that box as it was (plus
a little padding), so the figure in the Word/PowerPoint was incomplete.

- `refine_figure_box` (`backend/app/core/assembly/figure_region.py`) now looks at the ink on the page
  image: starting from the model's box it takes in the parts of the figure that lie close by (gaps up
  to ~12% of the page width), and the letters ("A)") right next to a picture. It stops at the text
  blocks the model returned, never takes a line of handwriting further away, and never makes the box
  narrower than the model's. A box that is already right (growth under 2% of the page) is left as it is,
  and so is a result over 85% of the page or over 6 times the model's box. Any error keeps the model's box.
- When the model's box reaches into the text block above or below the figure, that edge is pulled
  back to the text (only through a strip without figure ink).
- Log lines: `figure box widened from [...] to [...]` when it changes a box; `figure_edge_ink share=...`
  when the cut still has ink on its border (the figure probably goes on). Count them in the backend
  log to see how often the model's box is too narrow.
- Switch: `FIGURE_REFINE_ENABLED=false` in `/opt/vectorizer/.env` (append, then recreate the backend)
  goes back to cutting the model's box as it is. No Gemini request is involved, no migration.

## Gap check (local test tool, not part of the server)

`backend/scripts/gap_check.py` looks for what a conversion loses. It runs on a developer
machine only. The code is in the repo (`backend/app/eval/`), the test pages and the recorded
answers are NOT: keep the corpus folder outside the repo (the tool refuses a folder inside it).

- `synth <corpus> [--photo] [--fake-runs]`: made-up pages with known text, plus a good and a
  broken model answer (a mistake that really happened: picture options returned as empty "A)").
- `capture <corpus> --runs 3 --yes`: sends every page to Gemini N times and saves each answer
  (`runs/runK/results.json`) and its usage. It prints the number of requests and a cost range
  and sends nothing without `--yes`. Gemini requests cost money: ask first.
- `check <corpus>`: offline and free. Plays the recorded answers back through the real pipeline
  and looks at the SOURCE image for gaps: ink no block explains, empty option letters, text boxes
  with almost no text, figures cut through, blocks out of order, text missing from the Word file,
  and (with `expected.txt`) expected lines that are missing. It writes `reports/<time>/index.html`.
- A run directory may also hold a bare model answer saved as `results.json`.
- Severity: `error` = content is missing; `review` = a person should look (a page header the
  model left out on purpose, a figure at the edge of the photo, ...). Very large ink areas (the
  table or cover around a photographed page) are treated as background.

## Saved answers and the automatic gap check

Every finished job that sent pages to the model leaves two things for us to learn from:

- The model's answer for each page, in `data/answers/<job id>.json` (`SAVE_PAGE_ANSWERS`). With
  the input file (`data/uploads/<job id>.<ext>`) it can be played back through the real pipeline
  for free: `backend/scripts/gap_check.py` (a corpus folder with `source.<ext>` and
  `runs/run1/results.json` holding this file).
- The automatic gap check (`AUTO_GAP_CHECK`, the detectors of `backend/app/eval/gaps.py`): how
  many pages were checked, how many errors (ink that no block explains, empty "A)" options, text
  missing from the Word file, ...) and pages to review, and the errors by kind. They go to the
  `job_quality` table (numbers only) and to one log line `gap_check job=... errors=...`.
  `job_usage.calls_by_kind` counts the Gemini requests by kind (`page`, `page_repeat`,
  `flowchart_rescue`, `topology_retry`).

Read them on the server, inside the backend container:

```
docker compose -p vectorizer -f docker-compose.prod.yml exec backend python -m app.quality_report --days 7
docker compose -p vectorizer -f docker-compose.prod.yml exec backend python -m app.usage_report --days 7
```

- Both run only after a job has COMPLETED and whatever goes wrong there is logged and never
  reaches the job. Switch either off with `SAVE_PAGE_ANSWERS=false` / `AUTO_GAP_CHECK=false` in
  `/opt/vectorizer/.env` (append, then recreate the backend).
- Retention: `INPUT_RETENTION_HOURS` (default 24; 720 = 30 days) applies to the input files and to
  `data/answers/*.json` (by the age of the file); the nightly `app.cleanup_inputs` removes them.
- **Deleting a job** (the "Sil" button) removes the Word/PowerPoint result and the job row, but NOT
  the input file and the saved answers: jobs an owner deletes are often the ones that came out
  badly. They leave by age, as above.
- Migration 0006 adds the `job_quality` table and `job_usage.calls_by_kind` (empty for older rows).
