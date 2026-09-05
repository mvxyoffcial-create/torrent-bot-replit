"""Telegram file vault bot.

Files are copied into a private Telegram channel and represented by a stable
MongoDB record. Telegram's expiring file URLs are never exposed to users.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import bencodepy
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events
from telethon.errors import RPCError

try:
    import libtorrent as lt  # type: ignore
except ImportError:
    lt = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-file-vault")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


API_ID = int_env("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "telegram_file_vault").strip()
BIN_CHANNEL_ID = int_env("BIN_CHANNEL_ID", 0)
ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_ID", "").split(",")
    if item.strip()
}
PORT = int_env("PORT", 8000)
MAX_FILE_SIZE = int_env("MAX_FILE_SIZE_BYTES", 4 * 1024 * 1024 * 1024)
PIECE_LENGTH = int_env("TORRENT_PIECE_LENGTH", 4 * 1024 * 1024)
TRACKERS = [
    item.strip()
    for item in os.getenv("TRACKER_URLS", "").split(",")
    if item.strip()
]
if not TRACKERS:
    TRACKERS = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://exodus.desync.com:6969/announce",
    ]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SEED_ENABLED = os.getenv("ENABLE_SEEDING", "true").lower() in {"1", "true", "yes"}
SEED_PATH = Path(os.getenv("SEED_PATH", "/data/seeds"))
SEED_MAX_ACTIVE = int_env("SEED_MAX_ACTIVE", 1)
SEED_PORT = int_env("SEED_PORT", 6881)


def validate_configuration() -> None:
    missing = []
    if API_ID <= 0:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not MONGODB_URI:
        missing.append("MONGODB_URI")
    if BIN_CHANNEL_ID == 0:
        missing.append("BIN_CHANNEL_ID")
    if not ADMIN_IDS:
        missing.append("ADMIN_ID")
    if missing:
        raise RuntimeError("Missing or invalid configuration: " + ", ".join(missing))
    if PIECE_LENGTH < 256 * 1024:
        raise RuntimeError("TORRENT_PIECE_LENGTH must be at least 262144 bytes")


def safe_filename(name: str | None) -> str:
    clean = (name or "telegram-file").replace("\x00", "").strip()
    clean = re.sub(r"[^\w.\- ()\[\]]+", "_", clean, flags=re.UNICODE)
    return clean[:240] or "telegram-file"


def format_size(size: int | None) -> str:
    if not size:
        return "unknown size"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def torrent_link(public_id: str) -> str:
    path = f"/torrents/{quote(public_id)}.torrent"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def torrent_links(public_id: str, magnet: str | None = None) -> str:
    lines = [f"Permanent torrent file:\n{torrent_link(public_id)}"]
    if magnet:
        lines.append(f"Permanent magnet:\n{magnet}")
    else:
        lines.append("Magnet: generating piece hashes in the background.")
    return "\n\n".join(lines)


def torrent_info(name: str, size: int, piece_hashes: bytes) -> dict[bytes, Any]:
    info: dict[bytes, Any] = {
        b"length": size,
        b"name": name.encode("utf-8", "replace"),
        b"piece length": PIECE_LENGTH,
        b"pieces": piece_hashes,
    }
    return info


def make_torrent_bytes(name: str, size: int, piece_hashes: bytes) -> tuple[bytes, str, str]:
    info = torrent_info(name, size, piece_hashes)
    info_encoded = bencodepy.encode(info)
    info_hash = hashlib.sha1(info_encoded).hexdigest()
    root: dict[bytes, Any] = {b"info": info}
    if TRACKERS:
        root[b"announce"] = TRACKERS[0].encode()
        root[b"announce-list"] = [[tracker.encode()] for tracker in TRACKERS]
    torrent_bytes = bencodepy.encode(root)
    magnet_parts = [
        f"magnet:?xt=urn:btih:{info_hash}",
        f"dn={quote(name)}",
        f"xl={size}",
    ]
    magnet_parts.extend(f"tr={quote(tracker, safe='')}" for tracker in TRACKERS)
    return torrent_bytes, info_hash, "&".join(magnet_parts)


async def hash_telegram_file(message: Any, size: int) -> bytes:
    """Create standard v1 torrent piece hashes without holding the file in RAM."""
    hashes = bytearray()
    piece_buffer = bytearray()
    completed = 0
    async for chunk in client.iter_download(
        message,
        request_size=min(512 * 1024, PIECE_LENGTH),
        chunk_size=min(512 * 1024, PIECE_LENGTH),
    ):
        piece_buffer.extend(chunk)
        while len(piece_buffer) >= PIECE_LENGTH:
            hashes.extend(hashlib.sha1(piece_buffer[:PIECE_LENGTH]).digest())
            del piece_buffer[:PIECE_LENGTH]
            completed += PIECE_LENGTH
            if completed and completed % (PIECE_LENGTH * 32) == 0:
                logger.info("Torrent hashing progress: %s/%s", format_size(completed), format_size(size))
    if piece_buffer:
        hashes.extend(hashlib.sha1(piece_buffer).digest())
    return bytes(hashes)


async def build_torrent(record: dict[str, Any]) -> None:
    public_id = record["public_id"]
    try:
        message = await client.get_messages(BIN_CHANNEL_ID, ids=record["channel_message_id"])
        if not message:
            raise RuntimeError("The forwarded channel message no longer exists")
        pieces = await hash_telegram_file(message, int(record["size"]))
        torrent_bytes, info_hash, magnet = make_torrent_bytes(
            record["file_name"], int(record["size"]), pieces
        )
        await files.update_one(
            {"public_id": public_id},
            {
                "$set": {
                    "torrent": torrent_bytes,
                    "info_hash": info_hash,
                    "magnet": magnet,
                    "torrent_status": "ready",
                }
            },
        )
        logger.info("Torrent ready for %s (%s)", public_id, info_hash)
        if SEED_ENABLED:
            await start_seed({**record, "torrent": torrent_bytes})
        if record.get("uploader_id"):
            try:
                await client.send_message(
                    int(record["uploader_id"]),
                    "Torrent metadata is ready.\n\n" + torrent_links(public_id, magnet),
                    link_preview=False,
                )
            except RPCError:
                logger.info("Could not notify uploader %s", record["uploader_id"])
    except Exception as exc:
        logger.exception("Torrent generation failed for %s", public_id)
        await files.update_one(
            {"public_id": public_id},
            {"$set": {"torrent_status": "failed", "torrent_error": str(exc)[:500]}},
        )


async def download_file_to_path(message: Any, destination: Path) -> None:
    """Download the channel copy to disk for libtorrent to verify and seed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        await client.download_media(message, file=str(temporary))
        if not temporary.exists() or temporary.stat().st_size != int(message.file.size):
            raise RuntimeError("Telegram download finished with an unexpected file size")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@dataclass
class SeedHandle:
    public_id: str
    handle: Any
    data_dir: Path


seed_session: Any = None
seed_handles: dict[str, SeedHandle] = {}
seed_tasks: dict[str, asyncio.Task[None]] = {}


def get_seed_session() -> Any:
    global seed_session
    if lt is None:
        raise RuntimeError("libtorrent is unavailable in this image")
    if seed_session is None:
        settings = {
            "listen_interfaces": f"0.0.0.0:{SEED_PORT}",
            "enable_dht": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
        }
        seed_session = lt.session(settings)
        logger.info("libtorrent seeder listening on port %s", SEED_PORT)
    return seed_session


async def seed_file(record: dict[str, Any]) -> None:
    public_id = record["public_id"]
    if lt is None:
        raise RuntimeError("libtorrent is not installed; use the supplied Dockerfile")
    if not record.get("torrent"):
        raise RuntimeError("Torrent metadata is not ready yet")
    if public_id in seed_handles:
        return
    if len(seed_handles) >= SEED_MAX_ACTIVE:
        raise RuntimeError(f"SEED_MAX_ACTIVE={SEED_MAX_ACTIVE} has been reached")

    channel_message = await client.get_messages(BIN_CHANNEL_ID, ids=record["channel_message_id"])
    if not channel_message or not channel_message.file:
        raise RuntimeError("The forwarded channel message no longer has a file")

    data_dir = SEED_PATH / public_id
    data_path = data_dir / record["file_name"]
    torrent_path = data_dir / f"{public_id}.torrent"
    data_dir.mkdir(parents=True, exist_ok=True)
    torrent_path.write_bytes(record["torrent"])
    if not data_path.exists() or data_path.stat().st_size != int(record["size"]):
        await download_file_to_path(channel_message, data_path)

    session = get_seed_session()
    torrent_meta = lt.torrent_info(str(torrent_path))
    # Do not use seed_mode: libtorrent must verify every piece before serving it.
    handle = session.add_torrent({"ti": torrent_meta, "save_path": str(data_dir)})
    seed_handles[public_id] = SeedHandle(public_id, handle, data_dir)
    await files.update_one(
        {"public_id": public_id},
        {
            "$set": {
                "seed_status": "checking",
                "seed_path": str(data_path),
                "seed_port": SEED_PORT,
            }
        },
    )

    for _ in range(600):
        if handle.is_seed():
            await files.update_one(
                {"public_id": public_id},
                {"$set": {"seed_status": "seeding"}},
            )
            logger.info("Verified and seeding %s", public_id)
            return
        await asyncio.sleep(1)
    raise RuntimeError("libtorrent piece verification timed out")


async def start_seed(record: dict[str, Any]) -> None:
    public_id = record["public_id"]
    if public_id in seed_handles or public_id in seed_tasks:
        return

    async def run() -> None:
        await files.update_one(
            {"public_id": public_id},
            {"$set": {"seed_status": "starting", "seed_error": None}},
        )
        try:
            await seed_file(record)
        except Exception as exc:
            logger.exception("Seeding failed for %s", public_id)
            seed_handles.pop(public_id, None)
            await files.update_one(
                {"public_id": public_id},
                {"$set": {"seed_status": "failed", "seed_error": str(exc)[:500]}},
            )
        finally:
            seed_tasks.pop(public_id, None)

    seed_tasks[public_id] = asyncio.create_task(run())


async def stop_seed(public_id: str, remove_data: bool = False) -> bool:
    seed = seed_handles.pop(public_id, None)
    task = seed_tasks.pop(public_id, None)
    if task and not task.done():
        task.cancel()
    if seed and seed_session is not None:
        seed_session.remove_torrent(seed.handle)
    if remove_data:
        shutil.rmtree(SEED_PATH / public_id, ignore_errors=True)
    changed = bool(seed or task)
    if changed:
        await files.update_one({"public_id": public_id}, {"$set": {"seed_status": "off"}})
    return changed


async def is_admin(event: events.NewMessage.Event) -> bool:
    sender = await event.get_sender()
    return bool(sender and getattr(sender, "id", None) in ADMIN_IDS)


async def send_help(event: events.NewMessage.Event) -> None:
    await event.respond(
        "Send me a file and I will store it permanently in the bin channel.\n\n"
        "Commands:\n"
        "/help — show this message\n"
        "/status ID — inspect a stored file\n"
        "/torrent ID — show its permanent torrent links\n"
        "/seed ID — start or resume real P2P seeding (admin)\n"
        "/unseed ID — stop seeding but keep the stored file (admin)\n"
        "/list — list recent files (admin)\n"
        "/stats — show vault stats (admin)\n"
        "/delete ID — remove a record and its channel copy (admin)\n\n"
        "Torrent links contain real piece hashes. A running seeder is required "
        "for peers to download the file."
    )


async def handle_file(event: events.NewMessage.Event) -> None:
    message = event.message
    if not message.file or not message.file.size:
        return
    size = int(message.file.size)
    if size > MAX_FILE_SIZE:
        await event.respond(
            f"This file is {format_size(size)}. The configured maximum is "
            f"{format_size(MAX_FILE_SIZE)}."
        )
        return
    try:
        forwarded = await message.forward_to(BIN_CHANNEL_ID)
        forwarded_id = int(forwarded.id)
        public_id = secrets.token_hex(10)
        record = {
            "public_id": public_id,
            "channel_message_id": forwarded_id,
            "uploader_id": int(event.sender_id) if event.sender_id else None,
            "file_name": safe_filename(message.file.name),
            "mime_type": message.file.mime_type or "application/octet-stream",
            "size": size,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "torrent_status": "pending",
            "seed_status": "off",
        }
        await files.insert_one(record)
        asyncio.create_task(build_torrent(record))
        await event.respond(
            "Stored permanently in the bin channel.\n\n"
            f"File ID: {public_id}\n"
            "Torrent metadata generation has started. I will send the permanent "
            "torrent and magnet links when it finishes.",
            link_preview=False,
        )
    except Exception:
        logger.exception("Could not store incoming file")
        await event.respond("I could not store that file. Check the bot's channel permissions and logs.")


async def command_status(event: events.NewMessage.Event) -> None:
    public_id = event.pattern_match.group(1).strip()
    record = await files.find_one({"public_id": public_id})
    if not record:
        await event.respond("File not found.")
        return
    await event.respond(
        f"{record['file_name']} · {format_size(record.get('size'))}\n"
        f"Stored: {record.get('created_at', 'unknown')}\n"
        f"Torrent: {record.get('torrent_status', 'pending')}\n\n"
        f"Seeding: {record.get('seed_status', 'off')}\n\n"
        + torrent_links(public_id, record.get("magnet")),
        link_preview=False,
    )


async def command_torrent(event: events.NewMessage.Event) -> None:
    public_id = event.pattern_match.group(1).strip()
    record = await files.find_one({"public_id": public_id})
    if not record:
        await event.respond("File not found.")
    elif record.get("torrent_status") != "ready":
        await event.respond(
            f"Torrent is not ready yet (status: {record.get('torrent_status', 'pending')}). "
            "Try again shortly."
        )
    else:
        await event.respond(torrent_links(public_id, record.get("magnet")), link_preview=False)


async def command_seed(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    public_id = event.pattern_match.group(1).strip()
    record = await files.find_one({"public_id": public_id})
    if not record:
        await event.respond("File not found.")
        return
    if not SEED_ENABLED:
        await event.respond("Seeding is disabled. Set ENABLE_SEEDING=true and redeploy.")
        return
    if record.get("torrent_status") != "ready":
        await event.respond("Wait until torrent metadata is ready, then retry.")
        return
    await start_seed(record)
    await event.respond(
        f"Seeding started for {public_id}. The bot will download and verify the "
        "bin-channel copy before serving it to BitTorrent peers."
    )


async def command_unseed(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    public_id = event.pattern_match.group(1).strip()
    if await stop_seed(public_id):
        await event.respond(f"Stopped seeding {public_id}. Local seed data was kept.")
    else:
        await event.respond(f"{public_id} is not currently seeding.")


async def command_stats(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    count = await files.count_documents({})
    ready = await files.count_documents({"torrent_status": "ready"})
    seeding = await files.count_documents({"seed_status": "seeding"})
    total = 0
    async for row in files.find({}, {"size": 1}):
        total += int(row.get("size") or 0)
    await event.respond(
        f"Files: {count}\nTorrent metadata ready: {ready}\nCurrently seeding: {seeding}\n"
        f"Stored logical size: {format_size(total)}"
    )


async def command_list(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    rows = []
    cursor = files.find({}).sort("created_at", -1).limit(15)
    async for row in cursor:
        rows.append(
            f"{row['public_id']} · {format_size(row.get('size'))} · {row['file_name']}"
        )
    await event.respond("\n".join(rows) if rows else "No files stored yet.")


async def command_delete(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    public_id = event.pattern_match.group(1).strip()
    record = await files.find_one({"public_id": public_id})
    if not record:
        await event.respond("File not found.")
        return
    await stop_seed(public_id, remove_data=True)
    await files.delete_one({"public_id": public_id})
    try:
        await client.delete_messages(BIN_CHANNEL_ID, record["channel_message_id"])
    except RPCError:
        logger.warning("Could not delete channel copy for %s", public_id)
    await event.respond(f"Deleted {public_id}. Its permanent links no longer work.")


client = TelegramClient("telegram-file-vault", API_ID, API_HASH)
client.add_event_handler(send_help, events.NewMessage(pattern=r"^/(?:start|help)$"))
client.add_event_handler(handle_file, events.NewMessage())
client.add_event_handler(command_status, events.NewMessage(pattern=r"^/status\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_torrent, events.NewMessage(pattern=r"^/torrent\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_seed, events.NewMessage(pattern=r"^/seed\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_unseed, events.NewMessage(pattern=r"^/unseed\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_stats, events.NewMessage(pattern=r"^/stats$"))
client.add_event_handler(command_list, events.NewMessage(pattern=r"^/list$"))
client.add_event_handler(command_delete, events.NewMessage(pattern=r"^/delete\s+([A-Za-z0-9_-]+)$"))

mongo_client: AsyncIOMotorClient[Any] | None = None
db: Any = None
files: Any = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global mongo_client, db, files
    validate_configuration()
    mongo_client = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=10_000)
    db = mongo_client[MONGODB_DATABASE]
    files = db.files
    await files.create_index("public_id", unique=True)
    await files.create_index("channel_message_id", unique=True)
    await mongo_client.admin.command("ping")
    await client.start(bot_token=BOT_TOKEN)
    if SEED_ENABLED:
        cursor = files.find({"torrent_status": "ready"}).sort("created_at", 1).limit(SEED_MAX_ACTIVE)
        async for record in cursor:
            await start_seed(record)
    logger.info("Bot and HTTP server are ready")
    yield
    await client.disconnect()
    mongo_client.close()


app = FastAPI(title="Telegram Torrent Link Generator", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "telegram-file-vault"})


@app.get("/torrents/{torrent_name}")
async def torrent_file(torrent_name: str) -> Response:
    public_id = torrent_name.removesuffix(".torrent")
    record = await files.find_one({"public_id": public_id})
    if not record:
        raise HTTPException(status_code=404, detail="Torrent not found")
    if record.get("torrent_status") != "ready" or not record.get("torrent"):
        raise HTTPException(status_code=202, detail="Torrent is still being generated")
    return Response(
        content=record["torrent"],
        media_type="application/x-bittorrent",
        headers={
            "Content-Disposition": f'attachment; filename="{public_id}.torrent"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)