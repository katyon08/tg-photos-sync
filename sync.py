#!/usr/bin/env python3
"""Sync media from a Telegram dialog to a Google Photos album."""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from telethon.utils import get_extension

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

STATE_FILE  = BASE_DIR / "state.json"
TOKEN_FILE  = BASE_DIR / "token_google.json"
CREDS_FILE  = BASE_DIR / "credentials.json"
SESSION_FILE = str(BASE_DIR / "tg")
LOG_FILE    = BASE_DIR / "sync.log"

# ── Config ───────────────────────────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        sys.exit(f"ERROR: {key} is not set. Check your .env file.")
    return val

def _load_config():
    global TG_API_ID, TG_API_HASH, TG_PHONE, TG_DIALOG, ALBUM_NAME, NOTIFY_TOKEN, NOTIFY_CHAT
    TG_API_ID    = int(_require("TG_API_ID"))
    TG_API_HASH  = _require("TG_API_HASH")
    TG_PHONE     = _require("TG_PHONE")
    TG_DIALOG    = os.environ.get("TG_DIALOG", "@anna133456")
    ALBUM_NAME   = os.environ.get("ALBUM_NAME", "Telegram - Anna")
    NOTIFY_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN", "")
    NOTIFY_CHAT  = os.environ.get("NOTIFY_CHAT_ID", "")

TG_API_ID = TG_API_HASH = TG_PHONE = TG_DIALOG = ALBUM_NAME = NOTIFY_TOKEN = NOTIFY_CHAT = None

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/photoslibrary"]
PHOTOS_BASE   = "https://photoslibrary.googleapis.com/v1"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("tg_sync")


# ── Google Auth ───────────────────────────────────────────────────────────────

GOOGLE_AUTH_PORT = 8080


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
                sys.exit(f"ERROR: {CREDS_FILE} not found. Download OAuth2 Desktop credentials from Google Cloud Console.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            if headless:
                # Headless: local HTTP server on fixed port, use SSH tunnel:
                #   ssh -L 8080:localhost:8080 ubuntu@VM "python sync.py --auth-only"
                # Then open the printed URL in your local browser.
                creds = flow.run_local_server(port=GOOGLE_AUTH_PORT, open_browser=False)
            else:
                creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def _auth_headers(creds: Credentials) -> dict:
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return {"Authorization": f"Bearer {creds.token}"}


# ── Google Photos helpers ─────────────────────────────────────────────────────

def find_or_create_album(creds: Credentials, title: str) -> tuple[str, str]:
    """Return (albumId, productUrl). Creates album if not found."""
    params: dict = {"pageSize": 50}
    while True:
        r = requests.get(f"{PHOTOS_BASE}/albums", headers=_auth_headers(creds), params=params)
        r.raise_for_status()
        data = r.json()
        for album in data.get("albums", []):
            if album.get("title") == title:
                log.info("Found existing album: %s", title)
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
    log.info("Created new album: %s", title)
    return album["id"], album.get("productUrl", "")


def get_last_synced_id_from_album(creds: Credentials, album_id: str) -> int:
    """Scan album filenames (tg_MSGID_DATE.ext) to find max Telegram message ID."""
    max_id = 0
    body: dict = {"albumId": album_id, "pageSize": 100}
    while True:
        r = requests.post(
            f"{PHOTOS_BASE}/mediaItems:search",
            headers={**_auth_headers(creds), "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("mediaItems", []):
            fname = item.get("filename", "")
            # filename: tg_<msg_id>_<date>.<ext>
            if fname.startswith("tg_"):
                parts = fname.split("_")
                if len(parts) >= 2:
                    try:
                        max_id = max(max_id, int(parts[1]))
                    except ValueError:
                        pass
        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
    return max_id


def upload_to_album(creds: Credentials, album_id: str, file_path: Path, filename: str) -> bool:
    """Upload a single file and add it to the album. Returns True on success."""
    # Step 1: upload bytes → upload token
    r = requests.post(
        f"{PHOTOS_BASE}/uploads",
        headers={
            **_auth_headers(creds),
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-File-Name": filename,
            "X-Goog-Upload-Protocol": "raw",
        },
        data=file_path.read_bytes(),
        timeout=120,
    )
    r.raise_for_status()
    upload_token = r.text.strip()

    # Step 2: create media item in album
    r = requests.post(
        f"{PHOTOS_BASE}/mediaItems:batchCreate",
        headers={**_auth_headers(creds), "Content-Type": "application/json"},
        json={
            "albumId": album_id,
            "newMediaItems": [{
                "simpleMediaItem": {
                    "uploadToken": upload_token,
                    "fileName": filename,
                }
            }],
        },
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("newMediaItemResults", [])
    if results:
        status = results[0].get("status", {})
        if status.get("message") == "Success":
            return True
        log.warning("Upload status: %s", status)
    return False


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _is_syncable(msg) -> tuple[bool, str]:
    """Return (should_sync, media_type)."""
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


# ── Main sync ─────────────────────────────────────────────────────────────────

async def sync(headless: bool = True) -> tuple[int, int]:
    log.info("=== tg-photos-sync started ===")

    creds = get_google_creds(headless=headless)
    album_id, album_url = find_or_create_album(creds, ALBUM_NAME)

    state = load_state()
    if "last_message_id" in state:
        min_id = state["last_message_id"]
        log.info("Resuming from state.json: last_message_id=%d", min_id)
    else:
        # First run: scan album to find already-synced messages
        min_id = get_last_synced_id_from_album(creds, album_id)
        log.info("No state.json — scanned album, starting after message_id=%d", min_id)

    client = TelegramClient(SESSION_FILE, TG_API_ID, TG_API_HASH)
    await client.start(phone=TG_PHONE)

    entity = await client.get_entity(TG_DIALOG)
    log.info("Dialog resolved: %s", entity)

    uploaded = 0
    errors = 0
    last_id = min_id

    with tempfile.TemporaryDirectory() as tmpdir:
        async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
            should_sync, media_type = _is_syncable(msg)
            if not should_sync:
                last_id = max(last_id, msg.id)
                continue

            ext = get_extension(msg.media) or ".bin"
            filename = f"tg_{msg.id}_{msg.date.strftime('%Y%m%d_%H%M%S')}{ext}"
            tmp_path = Path(tmpdir) / filename

            log.info("Downloading %s msg_id=%d ...", media_type, msg.id)
            try:
                await client.download_media(msg, file=str(tmp_path))
            except Exception as e:
                log.error("Download error msg_id=%d: %s", msg.id, e)
                errors += 1
                last_id = max(last_id, msg.id)
                save_state({"last_message_id": last_id})
                continue

            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                log.warning("Empty download for msg_id=%d, skipping", msg.id)
                errors += 1
                last_id = max(last_id, msg.id)
                save_state({"last_message_id": last_id})
                continue

            try:
                ok = upload_to_album(creds, album_id, tmp_path, filename)
            except Exception as e:
                log.error("Upload error msg_id=%d: %s", msg.id, e)
                errors += 1
                last_id = max(last_id, msg.id)
                save_state({"last_message_id": last_id})
                continue

            if ok:
                uploaded += 1
                log.info("OK %s (%d bytes)", filename, tmp_path.stat().st_size)
            else:
                errors += 1
                log.warning("Upload failed for msg_id=%d", msg.id)

            last_id = max(last_id, msg.id)
            save_state({"last_message_id": last_id})

    await client.disconnect()
    save_state({"last_message_id": last_id})
    log.info("Done: uploaded=%d errors=%d last_message_id=%d", uploaded, errors, last_id)

    _notify(uploaded, errors, album_url)
    return uploaded, errors


# ── Notification ──────────────────────────────────────────────────────────────

def _notify(uploaded: int, errors: int, album_url: str) -> None:
    if not NOTIFY_TOKEN or not NOTIFY_CHAT:
        return
    status = "OK" if errors == 0 else f"WARNING: {errors} errors"
    text = (
        f"TG Photos Sync [{status}]\n"
        f"Загружено новых файлов: {uploaded}\n"
        f"Альбом: {album_url or ALBUM_NAME}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("Notification failed: %s", e)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Telegram media to Google Photos")
    parser.add_argument("--auth-only", action="store_true", help="Run auth flows and exit")
    parser.add_argument("--local-browser", action="store_true", help="Use local browser for Google OAuth (default: console/headless)")
    args = parser.parse_args()

    headless = not args.local_browser
    _load_config()

    if args.auth_only:
        get_google_creds(headless=headless)
        print("Google auth complete — token_google.json saved.")
        print("Now run without --auth-only to do Telegram auth + first sync.")
        sys.exit(0)

    asyncio.run(sync(headless=headless))
