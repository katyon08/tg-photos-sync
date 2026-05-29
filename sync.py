#!/usr/bin/env python3
"""Sync incoming Telegram media to Google Photos with deduplication."""

import asyncio
import io
import json
import logging
import os
import random
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import imagehash
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from PIL import Image
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from telethon.utils import get_extension

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_FILE       = BASE_DIR / "photos.db"
LIB_INDEX_DB  = BASE_DIR / "library_index.db"
TOKEN_FILE    = BASE_DIR / "token_google.json"
CREDS_FILE    = BASE_DIR / "credentials.json"
SESSION_FILE  = str(BASE_DIR / "tg")
LOG_FILE      = BASE_DIR / "sync.log"
PENDING_URL   = BASE_DIR / "pending_auth_url.txt"

# ── Config ───────────────────────────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        sys.exit(f"ERROR: {key} not set in .env")
    return val

def _load_config():
    global TG_API_ID, TG_API_HASH, TG_PHONE, TG_DIALOG
    global ALBUM_NAME, NOTIFY_TOKEN, NOTIFY_CHAT
    TG_API_ID    = int(_require("TG_API_ID"))
    TG_API_HASH  = _require("TG_API_HASH")
    TG_PHONE     = _require("TG_PHONE")
    TG_DIALOG    = os.environ.get("TG_DIALOG", "@anna133456")
    ALBUM_NAME   = os.environ.get("ALBUM_NAME", "Telegram - Anna")
    NOTIFY_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
    NOTIFY_CHAT  = os.environ.get("NOTIFY_CHAT_ID", "")

TG_API_ID = TG_API_HASH = TG_PHONE = TG_DIALOG = None
ALBUM_NAME = NOTIFY_TOKEN = NOTIFY_CHAT = None

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
PHOTOS_BASE      = "https://photoslibrary.googleapis.com/v1"
GOOGLE_AUTH_PORT = 8080
PHASH_THRESHOLD  = 8   # max Hamming distance to consider photos identical
PROGRESS_INTERVAL = 3600  # report every hour (seconds)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("tg_sync")


# ── SQLite DB ─────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id   INTEGER PRIMARY KEY,
            sender_id    INTEGER,
            is_outgoing  INTEGER,
            message_date TEXT,
            filename     TEXT,
            file_size    INTEGER,
            phash        TEXT,
            google_id    TEXT,
            status       TEXT  -- 'uploaded', 'skipped_outgoing', 'skipped_duplicate', 'error'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phash ON messages(phash)")
    conn.commit()
    return conn


def already_processed(conn: sqlite3.Connection, message_id: int) -> bool:
    row = conn.execute(
        "SELECT status FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def save_record(conn: sqlite3.Connection, **kwargs):
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" * len(kwargs))
    conn.execute(
        f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()


def get_last_processed_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(message_id) FROM messages").fetchone()
    return row[0] or 0


# ── pHash helpers ─────────────────────────────────────────────────────────────

def compute_phash(data: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return str(imagehash.phash(img))
    except Exception:
        return None


COLOR_THRESHOLD = 30  # max RGB channel diff to bother with pHash


def _avg_color(data: bytes) -> tuple[int, int, int] | None:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((1, 1), Image.LANCZOS)
        return img.getpixel((0, 0))
    except Exception:
        return None


def _color_close(r: int, g: int, b: int, er, eg, eb) -> bool:
    if er is None:
        return True  # no color in DB → don't skip, fall through to pHash
    return (abs(r - er) <= COLOR_THRESHOLD and
            abs(g - eg) <= COLOR_THRESHOLD and
            abs(b - eb) <= COLOR_THRESHOLD)


def is_duplicate(phash_str: str, avg: tuple | None, conn: sqlite3.Connection) -> bool:
    """Check pHash (with avg color pre-filter) against own uploads and library index."""
    h = imagehash.hex_to_hash(phash_str)
    r, g, b = avg if avg else (0, 0, 0)

    # Check our own uploaded photos
    for (existing_hash,) in conn.execute(
        "SELECT phash FROM messages WHERE phash IS NOT NULL AND status='uploaded'"
    ):
        try:
            if (h - imagehash.hex_to_hash(existing_hash)) <= PHASH_THRESHOLD:
                return True
        except Exception:
            pass

    # Check against Google Takeout library index (with avg color pre-filter)
    if LIB_INDEX_DB.exists():
        lib_conn = sqlite3.connect(f"file:{LIB_INDEX_DB}?mode=ro", uri=True)
        for (existing_hash, er, eg, eb) in lib_conn.execute(
            "SELECT phash, avg_r, avg_g, avg_b FROM library"
        ):
            try:
                if not _color_close(r, g, b, er, eg, eb):
                    continue  # colours too different → skip pHash entirely
                if (h - imagehash.hex_to_hash(existing_hash)) <= PHASH_THRESHOLD:
                    lib_conn.close()
                    return True
            except Exception:
                pass
        lib_conn.close()

    return False


# ── EXIF date injection ───────────────────────────────────────────────────────

def inject_exif_date(data: bytes, dt: datetime, filename: str) -> bytes:
    """Inject DateTimeOriginal into JPEG/WebP EXIF. Returns original bytes if unsupported."""
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".webp"}:
        return data
    try:
        import piexif
        date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
        try:
            exif_dict = piexif.load(data)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode()
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str.encode()
        exif_bytes = piexif.dump(exif_dict)

        img = Image.open(io.BytesIO(data))
        out = io.BytesIO()
        img.save(out, format=img.format or "JPEG", exif=exif_bytes)
        return out.getvalue()
    except Exception as e:
        log.debug("EXIF inject failed for %s: %s", filename, e)
        return data


# ── Google Auth ───────────────────────────────────────────────────────────────

def _run_headless_auth(flow: InstalledAppFlow, port: int) -> Credentials:
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            result.update(urllib.parse.parse_qsl(p.query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Auth complete! You can close this tab.</h1>")
        def log_message(self, *_): pass

    flow.redirect_uri = f"http://localhost:{port}"
    auth_url, _ = flow.authorization_url(prompt="consent")
    PENDING_URL.write_text(auth_url + "\n")
    print("\n" + "=" * 60, flush=True)
    print("Open this URL in your browser:", flush=True)
    print(auth_url, flush=True)
    print("=" * 60, flush=True)
    print(f"Waiting on localhost:{port} ...", flush=True)

    server = HTTPServer(("localhost", port), _Handler)
    server.handle_request()
    PENDING_URL.unlink(missing_ok=True)

    if "error" in result:
        sys.exit(f"OAuth error: {result['error']}")
    if "code" not in result:
        sys.exit("No auth code received.")
    flow.fetch_token(code=result["code"])
    return flow.credentials


def get_google_creds(headless: bool = True) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            log.info("Google token refreshed.")
        else:
            if not CREDS_FILE.exists():
                sys.exit(f"ERROR: {CREDS_FILE} not found.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            creds = _run_headless_auth(flow, GOOGLE_AUTH_PORT) if headless else flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def _auth_headers(creds: Credentials) -> dict:
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return {"Authorization": f"Bearer {creds.token}"}


# ── Google Photos helpers ─────────────────────────────────────────────────────

def find_or_create_album(creds: Credentials, title: str) -> tuple[str, str]:
    params: dict = {"pageSize": 50}
    while True:
        r = requests.get(f"{PHOTOS_BASE}/albums", headers=_auth_headers(creds), params=params)
        r.raise_for_status()
        data = r.json()
        for album in data.get("albums", []):
            if album.get("title") == title:
                log.info("Found album: %s", title)
                return album["id"], album.get("productUrl", "")
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    r = requests.post(
        f"{PHOTOS_BASE}/albums",
        headers={**_auth_headers(creds), "Content-Type": "application/json"},
        json={"album": {"title": title}},
    )
    r.raise_for_status()
    album = r.json()
    log.info("Created album: %s", title)
    return album["id"], album.get("productUrl", "")


def upload_to_album(creds: Credentials, album_id: str, data: bytes, filename: str) -> str | None:
    """Upload bytes to Google Photos. Returns google media item ID or None."""
    r = requests.post(
        f"{PHOTOS_BASE}/uploads",
        headers={
            **_auth_headers(creds),
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-File-Name": filename,
            "X-Goog-Upload-Protocol": "raw",
        },
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    upload_token = r.text.strip()

    r = requests.post(
        f"{PHOTOS_BASE}/mediaItems:batchCreate",
        headers={**_auth_headers(creds), "Content-Type": "application/json"},
        json={
            "albumId": album_id,
            "newMediaItems": [{"simpleMediaItem": {"uploadToken": upload_token, "fileName": filename}}],
        },
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("newMediaItemResults", [])
    if results:
        status = results[0].get("status", {})
        media_item = results[0].get("mediaItem", {})
        if media_item.get("id"):
            return media_item["id"]
        if not status.get("code"):  # code 0 or absent = success
            return results[0].get("uploadToken", upload_token)
        log.warning("Upload status: %s", status)
    return None


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _is_syncable(msg) -> tuple[bool, str]:
    if isinstance(msg.media, MessageMediaPhoto):
        return True, "photo"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc and doc.mime_type:
            if doc.mime_type.startswith("image/"):
                return True, "image"
            if doc.mime_type.startswith("video/"):
                return True, "video"
    return False, ""


# ── Progress notification ─────────────────────────────────────────────────────

def send_progress(uploaded: int, skipped_dup: int, skipped_out: int,
                  errors: int, total_estimate: int,
                  current_date: str, sample_photos: list[bytes]):
    if not NOTIFY_TOKEN or not NOTIFY_CHAT:
        return
    processed = uploaded + skipped_dup + skipped_out + errors
    pct = int(processed * 100 / total_estimate) if total_estimate else 0
    text = (
        f"📸 Sync прогресс\n"
        f"Загружено: {uploaded} | Дубликаты: {skipped_dup} | Пропущено: {skipped_out}\n"
        f"Ошибок: {errors}\n"
        f"Обработано: {processed:,} / ~{total_estimate:,} ({pct}%)\n"
        f"Текущий период: {current_date}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT, "text": text},
            timeout=10,
        )
        for photo_data in sample_photos[:3]:
            requests.post(
                f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendPhoto",
                data={"chat_id": NOTIFY_CHAT},
                files={"photo": ("sample.jpg", photo_data, "image/jpeg")},
                timeout=30,
            )
    except Exception as e:
        log.warning("Progress notification failed: %s", e)


# ── Main sync ─────────────────────────────────────────────────────────────────

async def sync(headless: bool = True):
    log.info("=== tg-photos-sync started ===")

    creds = get_google_creds(headless=headless)
    album_id, album_url = find_or_create_album(creds, ALBUM_NAME)
    conn = open_db()

    min_id = get_last_processed_id(conn)
    log.info("Resuming after message_id=%d", min_id)

    client = TelegramClient(SESSION_FILE, TG_API_ID, TG_API_HASH)
    await client.start(phone=TG_PHONE)
    entity = await client.get_entity(TG_DIALOG)
    log.info("Dialog: %s", entity)

    # Estimate total for progress reporting
    total_msgs = (await client.get_messages(entity, limit=1))[0].id if True else 0
    total_estimate = max(total_msgs - min_id, 1)

    uploaded = skipped_dup = skipped_out = errors = 0
    last_progress_time = time.time()
    sample_buffer: list[bytes] = []  # random samples for progress report

    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
        if already_processed(conn, msg.id):
            continue

        should_sync, media_type = _is_syncable(msg)
        if not should_sync:
            continue

        # Skip outgoing messages (photos user sent)
        if msg.out:
            save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                        is_outgoing=1, message_date=str(msg.date),
                        status="skipped_outgoing")
            skipped_out += 1
            continue

        # Download media into memory
        log.info("Downloading %s msg_id=%d date=%s ...", media_type, msg.id, msg.date.date())
        try:
            buf = io.BytesIO()
            await client.download_media(msg, file=buf)
            data = buf.getvalue()
        except Exception as e:
            log.error("Download error msg_id=%d: %s", msg.id, e)
            save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                        is_outgoing=0, message_date=str(msg.date), status="error")
            errors += 1
            continue

        if not data:
            errors += 1
            continue

        # pHash deduplication (with avg color pre-filter)
        phash = None
        avg = None
        if media_type in ("photo", "image"):
            phash = compute_phash(data)
            avg = _avg_color(data)
            if phash and is_duplicate(phash, avg, conn):
                log.info("Duplicate skipped msg_id=%d", msg.id)
                save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                            is_outgoing=0, message_date=str(msg.date),
                            phash=phash, status="skipped_duplicate")
                skipped_dup += 1
                continue

        # Inject EXIF date
        ext = get_extension(msg.media) or ".bin"
        filename = f"tg_{msg.id}_{msg.date.strftime('%Y%m%d_%H%M%S')}{ext}"
        if media_type in ("photo", "image"):
            data = inject_exif_date(data, msg.date, filename)

        # Upload
        try:
            google_id = upload_to_album(creds, album_id, data, filename)
        except Exception as e:
            log.error("Upload error msg_id=%d: %s", msg.id, e)
            save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                        is_outgoing=0, message_date=str(msg.date),
                        phash=phash, status="error")
            errors += 1
            continue

        if google_id:
            save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                        is_outgoing=0, message_date=str(msg.date),
                        filename=filename, file_size=len(data),
                        phash=phash, google_id=google_id, status="uploaded")
            uploaded += 1
            log.info("OK %s", filename)

            # Keep random sample for progress report
            if media_type in ("photo", "image") and random.random() < 0.05:
                sample_buffer.append(data)
                if len(sample_buffer) > 20:
                    sample_buffer.pop(random.randint(0, len(sample_buffer) - 2))
        else:
            save_record(conn, message_id=msg.id, sender_id=msg.sender_id,
                        is_outgoing=0, message_date=str(msg.date),
                        phash=phash, status="error")
            errors += 1

        # Hourly progress report
        if time.time() - last_progress_time >= PROGRESS_INTERVAL:
            samples = random.sample(sample_buffer, min(3, len(sample_buffer)))
            send_progress(uploaded, skipped_dup, skipped_out, errors,
                          total_estimate, str(msg.date.date()), samples)
            last_progress_time = time.time()

    await client.disconnect()
    conn.close()

    log.info("Done: uploaded=%d dup=%d out=%d errors=%d",
             uploaded, skipped_dup, skipped_out, errors)

    # Final notification
    if NOTIFY_TOKEN and NOTIFY_CHAT:
        text = (
            f"✅ Sync завершён\n"
            f"Загружено: {uploaded}\n"
            f"Дубликаты пропущены: {skipped_dup}\n"
            f"Исходящие пропущены: {skipped_out}\n"
            f"Ошибок: {errors}\n"
            f"Альбом: {album_url}"
        )
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT, "text": text},
            timeout=10,
        )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-only", action="store_true")
    parser.add_argument("--local-browser", action="store_true")
    args = parser.parse_args()

    _load_config()
    headless = not args.local_browser

    if args.auth_only:
        get_google_creds(headless=headless)
        print("Google auth complete.")
        sys.exit(0)

    asyncio.run(sync(headless=headless))
