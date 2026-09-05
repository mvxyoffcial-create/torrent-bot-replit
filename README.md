# Telegram File Vault

A Telegram bot that copies incoming files into a private bin channel and
creates stable, permanent links backed by MongoDB. It uses Telethon/MTProto,
not the Bot API file-download endpoint, so it is suitable for large files up
to the transfer limit supported by the Telegram account and deployment.

## What it does

- Accepts files sent to the bot and forwards them to `BIN_CHANNEL_ID`.
- Stores only metadata and torrent bytes in MongoDB; the bin channel is the
  durable file store.
- Gives every file a stable public ID.
- Streams a permanent direct-download URL with HTTP Range support.
- Builds a standard single-file `.torrent` and permanent magnet link in the
  background. Piece hashes are stored in MongoDB, so the torrent metadata URL
  does not expire.
- Includes an optional libtorrent seeder. Seeding downloads a local copy, so
  it needs persistent disk at least as large as each actively seeded file.
- Restricts management commands to `ADMIN_ID`.

## Important limits

The links are permanent because the bot owns the route and MongoDB record;
they are not signed Telegram CDN URLs. A link remains valid while its MongoDB
record and forwarded channel message exist. `/delete` intentionally invalidates
it.

Set `PUBLIC_BASE_URL` to the Koyeb service URL if you want absolute links.
Without it, the bot returns stable relative paths.

Telegram and MTProto transfer limits depend on the account type and current
Telegram limits. The default maximum is 4 GiB, but it cannot raise a limit
imposed by Telegram. Do not promise 4 GiB transfers until your account has
successfully transferred a file of that size.

Koyeb's normal filesystem is ephemeral. The bin channel and MongoDB preserve
the logical file and links, while optional local torrent seeding stops after a
restart unless a persistent volume is configured.

## Telegram setup

1. Create a bot with BotFather and copy its token.
2. Get `API_ID` and `API_HASH` from `my.telegram.org`.
3. Create a private channel for storage.
4. Add the bot as an administrator with permission to post, read history, and
   delete messages.
5. Find the numeric channel ID and your numeric Telegram user ID.
6. Copy `.env.example` to `.env` for local use, or add the same variables as
   Koyeb secrets/environment variables.

Never commit `.env` or paste credentials into chat.

## Local run

```bash
cp .env.example .env
docker build -t telegram-file-vault .
docker run --env-file .env -p 8000:8000 telegram-file-vault
```

Open `/healthz` to check the HTTP service. Send a file to the bot, then use
the permanent links in its reply. The first magnet may take time to appear
because a 4 GiB file must be read once to calculate torrent piece hashes.

## Koyeb deployment

1. Create a MongoDB Atlas cluster and database user. Put the full connection
   URI in a Koyeb secret named `MONGODB_URI`.
2. Push this folder to a Git repository, or upload the supplied zip to a
   repository you control.
3. In Koyeb, create a Web Service from the repository and select **Dockerfile**.
4. Add every variable from `.env.example` as Koyeb environment variables.
   Keep `BOT_TOKEN`, `API_HASH`, and `MONGODB_URI` as secrets.
5. Set the service port to `8000` (or leave Koyeb's `PORT` injection enabled).
6. Deploy, copy the public Koyeb URL, and update `PUBLIC_BASE_URL` with it.
   Redeploy once so future replies contain absolute permanent links.
7. Check `https://YOUR_KOYEB_URL/healthz`.

For actual BitTorrent seeding, set `ENABLE_SEEDING=true`, configure a
persistent Koyeb volume mounted at `/data`, and expose the configured
BitTorrent listen range if your Koyeb plan/network supports it. Without
inbound peer connectivity, the direct link remains available but torrent
peer discovery/seeding will be limited.

## Admin commands

- `/stats` — count and logical storage size
- `/list` — recent file IDs
- `/status ID` — stable links and processing status
- `/torrent ID` — permanent torrent and magnet links
- `/seed ID` — start optional local seeding
- `/delete ID` — delete MongoDB record, channel copy, and local seed data