# Telegram Torrent Link Generator

A Telegram bot that copies incoming files into a private bin channel and
creates stable, permanent torrent links backed by MongoDB. It is intentionally
torrent-only: it does not create or expose a direct file-download endpoint.
It uses Telethon/MTProto for large-file transfers and libtorrent to seed real
BitTorrent peers.

## What it does

- Accepts files sent to the bot and forwards them to `BIN_CHANNEL_ID`.
- Stores only metadata and torrent bytes in MongoDB; the bin channel is the
  durable file store.
- Gives every file a stable public ID.
- Builds a standard single-file `.torrent` and permanent magnet link in the
  background. Piece hashes are stored in MongoDB, so the torrent metadata URL
  does not expire.
- Downloads the bin-channel copy to persistent disk, verifies its real pieces,
  and announces it through public trackers and DHT so BitTorrent clients can
  download it peer-to-peer.
- Restricts management commands to `ADMIN_ID`.

## Important limits

The torrent and magnet links are permanent because the bot owns the route and
MongoDB record; they are not signed Telegram CDN URLs. A link remains valid
while its MongoDB record and forwarded channel message exist. `/delete`
intentionally invalidates it.

Set `PUBLIC_BASE_URL` to the Koyeb service URL if you want absolute links.
Without it, the bot returns stable relative paths.

Telegram and MTProto transfer limits depend on the account type and current
Telegram limits. The default maximum is 4 GiB, but it cannot raise a limit
imposed by Telegram. Do not promise 4 GiB transfers until your account has
successfully transferred a file of that size.

The bot does not provide direct file links. A real torrent still needs at
least one reachable seeder. Keep `ENABLE_SEEDING=true`, mount persistent disk
at `/data`, and allow the configured `SEED_PORT` through your hosting network.
The bot downloads one local copy per active seed, so `SEED_MAX_ACTIVE=1` is a
safe default for a 4 GB server.

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
the permanent torrent links in its reply. The first magnet may take time to
appear because a 4 GiB file must be read once to calculate torrent piece
hashes. After that, the seeder downloads the channel copy, verifies every
piece, and starts announcing it.

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
8. Attach persistent storage mounted at `/data`. Allow inbound TCP/UDP
   `SEED_PORT` if your Koyeb networking plan supports non-HTTP peer traffic.

The `/torrents/<id>.torrent` endpoint only serves torrent metadata. It is not
the data source; the libtorrent process is the real peer seeder. If the
hosting network cannot accept inbound BitTorrent traffic, trackers/DHT may
not find this seed reliably, even though the torrent file is valid.
## Admin commands

- `/stats` — count and logical storage size
- `/list` — recent file IDs
- `/status ID` — stable links and processing status
- `/torrent ID` — permanent torrent and magnet links
- `/seed ID` — start or resume real P2P seeding (admin)
- `/unseed ID` — stop seeding but keep local data (admin)
- `/delete ID` — delete MongoDB record and channel copy