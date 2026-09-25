# Production deployment and operations runbook

Last verified: 2026-09-07 (Europe/Kyiv). This file contains operational facts,
not secrets. Values from `.env` must never be pasted into logs, commits, issues,
or agent responses.

## Production topology

| Component | Production value |
|---|---|
| VPS | `root@91.231.182.57` |
| Hostname | `405877.vps.hostiko.network` |
| Repository | `/root/secretary-bot` |
| Git branch | `main` |
| Compose project | `secretary-bot` (derived from the directory name) |
| Public base URL | `https://bot.linkoid.net` |
| Mini App | `https://bot.linkoid.net/app/` |
| Telegram webhook | `https://bot.linkoid.net/telegram/webhook` |
| Host application socket | `127.0.0.1:18080` |
| Container application socket | `0.0.0.0:8000` |
| nginx site | `/etc/nginx/sites-available/bot.linkoid.net` |
| TLS | Certbot-managed certificate for `bot.linkoid.net` |

Request path:

```text
Telegram/browser -> HTTPS bot.linkoid.net -> nginx
                 -> 127.0.0.1:18080 -> app container :8000
                 -> PostgreSQL/Redis on the private Compose network
```

nginx already owns ports 80/443 and proxies this subdomain to port 18080. Do not
publish the app, PostgreSQL, or Redis to a public interface. Compose binds the app
through `APP_BIND_PORT`; production `.env` must contain:

```dotenv
PUBLIC_BASE_URL=https://bot.linkoid.net
APP_BIND_PORT=18080
```

The repository's `.env.example` uses port 8000 for local development. Production
`.env` is ignored by Git and has mode `0600`.

## Services and persistent state

`docker-compose.yml` defines:

- `postgres`: PostgreSQL 16, volume `secretary-bot_postgres-data`;
- `redis`: Redis 7 with AOF, volume `secretary-bot_redis-data`;
- `migrate`: one-shot `alembic upgrade head`, expected to exit with code 0;
- `app`: FastAPI/aiogram application and background workers.

The `migrate` container exiting successfully is normal. PostgreSQL stores the
durable domain state and encrypted retained message bodies. Redis stores leases,
deduplication state, and scheduling data. `MESSAGE_ENCRYPTION_KEY` is outside the
database in `.env`; losing or changing it makes retained ciphertext unreadable.

## Safe routine deployment

Run from `/root/secretary-bot`:

```bash
git status --short --branch
git fetch origin
git pull --ff-only
docker compose up -d --build
docker compose ps -a
docker compose logs --no-color --tail=100 migrate app
curl --fail http://127.0.0.1:18080/readyz
curl --fail https://bot.linkoid.net/healthz
curl --fail https://bot.linkoid.net/readyz
```

Do not use `docker compose down --volumes` for an update. `up -d --build`
preserves both data volumes. Review migrations and make a backup before any
schema change that is not demonstrably backward compatible.

Register or repair the Telegram webhook after changing `PUBLIC_BASE_URL`, the
token, webhook secret, or host:

```bash
docker compose exec -T app secretary-set-webhook
```

Expected URL is `https://bot.linkoid.net/telegram/webhook`, with zero pending
updates under normal conditions. The command deliberately keeps pending updates.

## Health and logs

Quick status:

```bash
cd /root/secretary-bot
docker compose ps -a
curl --fail http://127.0.0.1:18080/readyz
curl --fail https://bot.linkoid.net/healthz
curl --fail https://bot.linkoid.net/readyz
```

Logs:

```bash
docker compose logs --no-color --tail=200 app
docker compose logs --no-color --tail=200 migrate
docker compose logs --no-color --tail=100 postgres redis
docker compose logs --no-color --since=30m app
docker compose logs --no-color --tail=200 -f app
```

Do not publish full logs before checking them for Telegram identifiers and user
content. The application is designed not to log message bodies, but incident
output still needs review.

`/healthz` confirms that the process and its background tasks are alive.
`/readyz` additionally checks dependencies and is the stronger deployment gate.
An exited `migrate` container is healthy only when its exit code is 0.

Useful non-secret database checks:

```bash
docker compose exec -T postgres psql -U secretary -d secretary -Atc \
  "SELECT version_num FROM alembic_version"
docker compose exec -T redis redis-cli ping
docker compose exec -T redis redis-cli dbsize
```

## Common incidents

### Public URL returns 502

First check the application and port binding:

```bash
docker compose ps -a
curl --fail http://127.0.0.1:18080/readyz
docker compose logs --no-color --tail=200 app migrate
```

If the local request fails, repair the Compose stack. If it succeeds but public
HTTPS fails, inspect nginx and its error log without changing unrelated sites:

```bash
nginx -t
systemctl status nginx --no-pager
journalctl -u nginx --since "30 minutes ago" --no-pager
```

The nginx upstream and `APP_BIND_PORT` must both be 18080.

### Mini App opens an obsolete `trycloudflare.com` URL

The production app itself does not use Cloudflare Tunnel. An old URL can remain
in a per-chat Telegram menu button created before migration. Sending `/start`
updates the current authorized user's menu button from `PUBLIC_BASE_URL`.

For all active users, use a controlled script through the running app to call
Telegram `setChatMenuButton`; do not print user IDs or the bot token. Preserve
the global default menu unless product requirements explicitly change it. After
repair, verify each button with `getChatMenuButton` and verify `/app/` returns
HTTP 200.

### Webhook is not delivering updates

```bash
docker compose exec -T app secretary-set-webhook
docker compose logs --no-color --since=30m app
curl --fail https://bot.linkoid.net/readyz
```

Check that Telegram reports the expected URL, zero or decreasing pending updates,
and no recent delivery error. Never use `drop_pending_updates=true` during a
routine repair.

### Migration fails

Do not repeatedly restart the stack blindly. Read `migrate` logs, confirm the
current revision in `alembic_version`, inspect the pending migration, and restore
from a verified backup if the migration partially changed data. PostgreSQL DDL
migrations are expected to be transactional, but each migration must still be
reviewed.

## Rollback: new-contact setup rule (migration `20260924_0026`)

Commit `eca5a8e` makes the bot stay silent to a contact until the owner saves
its card in the Mini App or picks a rule in Manage Bot. Existing contacts were
marked as reviewed by the migration. The previous release is `84294b1`
(Alembic `20260917_0025`).

Pre-deploy backup on the VPS (custom format, mode `0600`, 26 tables):

```text
tmp/secretary-bot-pre-20260924_0026-20260924T175847Z.dump
SHA-256: 081c8bf6e061b5e3ccf57d1c2c0d0de8b0f296e6762b80dc2818ef10b08deedb
```

### Level 1: switch the rule off (preferred, no code or schema change)

The bot immediately answers new contacts by the general rules again, sends no
setup notices, and the Mini App stops marking contacts as not configured. The
change only touches `.env`; do not print the file.

```bash
cd /root/secretary-bot
grep -q '^REQUIRE_CONTACT_SETUP=' .env \
  && sed -i 's/^REQUIRE_CONTACT_SETUP=.*/REQUIRE_CONTACT_SETUP=false/' .env \
  || echo 'REQUIRE_CONTACT_SETUP=false' >> .env
docker compose up -d app
docker compose exec -T app printenv REQUIRE_CONTACT_SETUP
curl --fail http://127.0.0.1:18080/readyz
curl --fail https://bot.linkoid.net/readyz
```

To turn it back on, set the value to `true` and run the same commands. Contacts
that first wrote while the rule was off have no review mark and would become
silent (each owner gets one notice). To keep answering them, mark them before
switching the rule on:

```bash
docker compose exec -T postgres psql -U secretary -d secretary -c \
  "UPDATE contact_activity SET configured_at = now() WHERE configured_at IS NULL"
```

### Level 2: full code and schema rollback

Use only if Level 1 is not enough. The downgrade must run while the new image,
which still contains the migration, is present. It drops the two new
`contact_activity` columns and deletes `skipped_unconfigured` log rows; all
other data stays. No dump restore is needed.

```bash
cd /root/secretary-bot
docker compose stop app
docker compose run --rm migrate alembic downgrade 20260917_0025
git checkout 84294b1          # emergency: detached HEAD on the previous release
docker compose up -d --build
docker compose exec -T postgres psql -U secretary -d secretary -Atc \
  "SELECT version_num FROM alembic_version"   # expect 20260917_0025
```

Then run the health checks and verify the webhook. Afterwards revert the
feature commit locally, push `main`, and return the server to the branch with
`git checkout main && git pull --ff-only`, so it does not stay detached.

Restoring the dump replaces production data and requires explicit authorization
(see `AGENTS.md`); it is not part of this rollback.

## Trial deploy: branch `feature/minimal-mini-app`

The redesigned Mini App runs on production from its feature branch so the
owner can test it before merging. No migrations. Besides the static files it
adds `GET /api/v1/contacts/stats` and makes a 00:00–00:00 schedule window
mean the whole day. The previous release is `main` at `c5470ee`.

Deploy:

```bash
cd /root/secretary-bot
git fetch origin
git switch --track origin/feature/minimal-mini-app   # later: git pull --ff-only
docker compose up -d --build
```

Roll back to `main`. On `main` a 00:00–00:00 window covers nothing, so first
check whether "Цілодобово" was saved while the branch ran, and turn such
windows into 00:00–23:59 so the bot keeps answering after the rollback:

```bash
docker compose exec -T postgres psql -U secretary -d secretary -Atc \
  "SELECT 'schedules', count(*) FROM schedules WHERE time_from = '00:00' AND time_to = '00:00'
   UNION ALL SELECT 'contact_windows', count(*) FROM contact_windows
   WHERE time_from = '00:00' AND time_to = '00:00'"
# only if a count is not zero:
docker compose exec -T postgres psql -U secretary -d secretary -c \
  "UPDATE schedules SET time_to = '23:59' WHERE time_from = '00:00' AND time_to = '00:00';
   UPDATE contact_windows SET time_to = '23:59' WHERE time_from = '00:00' AND time_to = '00:00'"
```

Then switch the code:

```bash
cd /root/secretary-bot
git switch main
git pull --ff-only
docker compose up -d --build
```

Run the health checks and verify the webhook after either step. Telegram may
keep the old assets for a moment; the page references versioned asset URLs,
so reopening the Mini App picks up the switch.

## Backup and transfer snapshot

The initial VPS deployment was restored from the encrypted full-transfer package:

```text
tmp/secretary-bot-transfer-20260907T093353Z.tar.gz.enc
SHA-256: 81634305b6479b4835806fca581a2f0f3b803ed30a60cc5725d9b1b2b0447856
Source revision: 97bb6753f4fbb380febe20c8c20addd06bb02a68
Encryption: OpenSSL AES-256-CBC, PBKDF2, 200000 iterations
```

The encrypted file is ignored by Git and has mode `0600`. Its passphrase is not
stored in the repository. The package contains `.env`, a PostgreSQL custom dump,
a Redis snapshot, checksums, `RESTORE.md`, and a guarded `restore.sh`.

Verify the encrypted artifact before using it:

```bash
sha256sum tmp/secretary-bot-transfer-20260907T093353Z.tar.gz.enc
```

Decrypt interactively so the passphrase is not present in the process list or
shell history:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in tmp/secretary-bot-transfer-20260907T093353Z.tar.gz.enc \
  -out tmp/secretary-bot-transfer-20260907T093353Z.tar.gz
```

Before extracting, list the archive and reject absolute paths or `..` traversal.
Extract into a private staging directory, verify `SHA256SUMS`, and read both
`RESTORE.md` and `restore.sh` completely. Remove decrypted staging artifacts as
soon as verification/restore finishes.

The packaged restore command intentionally runs `docker compose down --volumes`
and replaces PostgreSQL and Redis data. It is destructive and requires explicit
authorization:

```bash
CONFIRM_REPLACE_DATA=YES sh PATH_TO_EXTRACTED_PACKAGE/restore.sh
```

Before a machine-to-machine migration, stop the old bot so two worker sets cannot
send duplicate replies. After restore, set the stable production URL and port in
`.env`, run `docker compose up -d --build`, configure the webhook, and execute all
health checks in this runbook.

## Verified baseline after the 2026-09-07 migration

- source revision: `97bb6753f4fbb380febe20c8c20addd06bb02a68`;
- Alembic revision: `20260905_0024`;
- PostgreSQL restore completed without errors;
- Redis restored 100 keys at import time;
- `postgres`, `redis`, and `app` were healthy;
- `migrate` exited 0;
- local and public readiness passed;
- Telegram webhook resolved to `91.231.182.57`, with zero pending updates;
- stale per-chat Mini App buttons were updated for all active users;
- no pending `reply_jobs` or `notification_jobs` remained after migration.

Counts above are a migration record, not invariants; they will naturally change
in production.
