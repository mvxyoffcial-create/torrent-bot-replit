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
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import bencodepy
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events
from telethon.errors import RPCError

try:
    import libtorrent as lt  # type: ignore
except ImportError:  # Optional in local development; enabled in the Docker image.
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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SEED_ENABLED = os.getenv("ENABLE_SEEDING", "false").lower() in {"1", "true", "yes"}
SEED_PATH = Path(os.getenv("SEED_PATH", "/data/seeds"))
SEED_MAX_ACTIVE = int_env("SEED_MAX_ACTIVE", 1)


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


def link_for(kind: str, public_id: str) -> str:
    path = f"/{kind}/{quote(public_id)}"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def file_links(public_id: str, magnet: str | None = None) -> str:
    direct = link_for("files", public_id)
    torrent = link_for("torrents", f"{public_id}.torrent")
    lines = [
        f"Permanent direct link:\n{direct}",
        f"Permanent torrent file:\n{torrent}",
    ]
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


async def download_file_to_path(message: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        await client.download_media(message, file=str(temporary))
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
        if record.get("uploader_id"):
            try:
                await client.send_message(
                    int(record["uploader_id"]),
                    "Torrent metadata is ready.\n\n" + file_links(public_id, magnet),
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


@dataclass
class SeedHandle:
    public_id: str
    handle: Any
    data_path: Path


seed_handles: dict[str, SeedHandle] = {}
seed_tasks: dict[str, asyncio.Task[None]] = {}


async def seed_file(record: dict[str, Any]) -> None:
    public_id = record["public_id"]
    if lt is None:
        raise RuntimeError("libtorrent is not installed; use the supplied Dockerfile")
    if not record.get("torrent"):
        raise RuntimeError("Torrent metadata is not ready yet")
    if len(seed_handles) >= SEED_MAX_ACTIVE:
        raise RuntimeError(f"SEED_MAX_ACTIVE={SEED_MAX_ACTIVE} has been reached")

    data_dir = SEED_PATH / public_id
    data_path = data_dir / record["file_name"]
    torrent_path = data_dir / f"{public_id}.torrent"
    data_dir.mkdir(parents=True, exist_ok=True)
    torrent_path.write_bytes(record["torrent"])
    await download_file_to_path(
        await client.get_messages(BIN_CHANNEL_ID, ids=record["channel_message_id"]),
        data_path,
    )

    session = lt.session()
    settings = {
        "listen_interfaces": "0.0.0.0:6881-6891",
        "enable_dht": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        "alert_mask": lt.alert.category_t.error_notification,
    }
    session.apply_settings(settings)
    params = lt.torrent_info(str(torrent_path))
    handle = session.add_torrent({"ti": params, "save_path": str(data_dir)})
    seed_handles[public_id] = SeedHandle(public_id, handle, data_path)
    await files.update_one(
        {"public_id": public_id},
        {"$set": {"seed_status": "seeding", "seed_path": str(data_path)}},
    )
    logger.info("Seeding started for %s", public_id)


async def start_seed(record: dict[str, Any]) -> None:
    public_id = record["public_id"]
    if public_id in seed_handles or public_id in seed_tasks:
        return

    async def run() -> None:
        try:
            await seed_file(record)
        except Exception as exc:
            logger.exception("Seeding failed for %s", public_id)
            await files.update_one(
                {"public_id": public_id},
                {"$set": {"seed_status": "failed", "seed_error": str(exc)[:500]}},
            )
        finally:
            seed_tasks.pop(public_id, None)

    seed_tasks[public_id] = asyncio.create_task(run())


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
        "/seed ID — start a local BitTorrent seed (admin)\n"
        "/list — list recent files (admin)\n"
        "/stats — show vault stats (admin)\n"
        "/delete ID — remove a record and its channel copy (admin)\n\n"
        "Links are stable public IDs, not temporary Telegram URLs."
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
            "Stored permanently in the bin channel.\n\n" + file_links(public_id),
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
        f"Torrent: {record.get('torrent_status', 'pending')}\n"
        f"Seeding: {record.get('seed_status', 'off')}\n\n"
        + file_links(public_id, record.get("magnet")),
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
        await event.respond(file_links(public_id, record.get("magnet")), link_preview=False)


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
    await start_seed(record)
    await event.respond(
        f"Seeding queued for {public_id}. The bot will download a local copy first; "
        "this needs enough persistent disk for the file."
    )


async def command_stats(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        return
    count = await files.count_documents({})
    ready = await files.count_documents({"torrent_status": "ready"})
    total = 0
    async for row in files.find({}, {"size": 1}):
        total += int(row.get("size") or 0)
    await event.respond(
        f"Files: {count}\nTorrent metadata ready: {ready}\nStored logical size: {format_size(total)}"
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
    await files.delete_one({"public_id": public_id})
    try:
        await client.delete_messages(BIN_CHANNEL_ID, record["channel_message_id"])
    except RPCError:
        logger.warning("Could not delete channel copy for %s", public_id)
    shutil.rmtree(SEED_PATH / public_id, ignore_errors=True)
    seed_handles.pop(public_id, None)
    await event.respond(f"Deleted {public_id}. Its permanent links no longer work.")


client = TelegramClient("telegram-file-vault", API_ID, API_HASH)
client.add_event_handler(send_help, events.NewMessage(pattern=r"^/(?:start|help)$"))
client.add_event_handler(handle_file, events.NewMessage())
client.add_event_handler(command_status, events.NewMessage(pattern=r"^/status\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_torrent, events.NewMessage(pattern=r"^/torrent\s+([A-Za-z0-9_-]+)$"))
client.add_event_handler(command_seed, events.NewMessage(pattern=r"^/seed\s+([A-Za-z0-9_-]+)$"))
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
    logger.info("Bot and HTTP server are ready")
    yield
    await client.disconnect()
    mongo_client.close()


app = FastAPI(title="Telegram File Vault", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "telegram-file-vault"})


@app.get("/files/{public_id}")
async def direct_file(public_id: str, request: Request) -> Response:
    record = await files.find_one({"public_id": public_id})
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    message = await client.get_messages(BIN_CHANNEL_ID, ids=record["channel_message_id"])
    if not message:
        raise HTTPException(status_code=410, detail="Stored Telegram copy is unavailable")
    size = int(record["size"])
    start, end = 0, size - 1
    range_header = request.headers.get("range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="Invalid byte range")
        if match.group(1):
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
        else:
            suffix = int(match.group(2))
            start = max(0, size - suffix)
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
    length = end - start + 1

    async def body() -> AsyncIterator[bytes]:
        async for chunk in client.iter_download(
            message,
            offset=start,
            limit=length,
            request_size=min(512 * 1024, PIECE_LENGTH),
            chunk_size=min(512 * 1024, PIECE_LENGTH),
        ):
            yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(record['file_name'])}",
        "Content-Type": record.get("mime_type", "application/octet-stream"),
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(body(), status_code=206, headers=headers)
    return StreamingResponse(body(), headers=headers)


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