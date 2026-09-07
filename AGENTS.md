# Instructions for AI agents

This repository is deployed in production. Before any deployment, server-side
diagnostic, database operation, webhook change, or incident response, read
`docs/deployment-vps.md` completely.

Production facts:

- server: `root@91.231.182.57` (`405877.vps.hostiko.network`);
- repository: `/root/secretary-bot`;
- public URL and Mini App: `https://bot.linkoid.net` and `/app/`;
- nginx proxies to `127.0.0.1:18080`;
- Docker Compose project: `secretary-bot`;
- `.env` is production-only, ignored by Git, and must never be printed or
  committed.

Safety rules:

- Never run `docker compose down --volumes`, delete Docker volumes, restore a
  dump, or rotate secrets unless the user explicitly authorizes replacement of
  production data.
- Normal updates use `docker compose up -d --build`; do not recreate data
  volumes.
- Do not expose `BOT_TOKEN`, `WEBHOOK_SECRET`, API keys,
  `MESSAGE_ENCRYPTION_KEY`, database passwords, Telegram IDs, or backup
  passphrases in commands, logs, diffs, or responses.
- Preserve `PUBLIC_BASE_URL=https://bot.linkoid.net` and
  `APP_BIND_PORT=18080` on this VPS.
- Do not run two copies of the bot against the same Telegram bot. Before a
  migration from another machine, stop the old app/worker.
- Diagnose before changing state. After a change, verify Compose health, local
  `/readyz`, public `/healthz` and `/readyz`, and Telegram webhook status.
