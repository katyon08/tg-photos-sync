#!/usr/bin/env python3
"""
Build pHash index from Google Takeout ZIP archives.
Reads archives in streaming mode — no extraction to disk needed.

Usage:
  python build_index.py takeout-*.zip
  python build_index.py takeout-part1.zip takeout-part2.zip takeout-part3.zip
"""

import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import imagehash
from PIL import Image
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support optional

DB_PATH = Path(__file__).parent / "library_index.db"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tiff", ".bmp"}


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS library (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            phash    TEXT NOT NULL,
            archive  TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phash ON library(phash)")
    conn.commit()


def compute_phash(data: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        return str(imagehash.phash(img))
    except Exception:
        return None


def index_archive(archive_path: Path, conn: sqlite3.Connection):
    archive_name = archive_path.name
    print(f"\nProcessing: {archive_name} ({archive_path.stat().st_size // 1024**3} GB)")

    indexed = 0
    skipped = 0

    with zipfile.ZipFile(archive_path, "r") as zf:
        entries = [e for e in zf.infolist()
                   if Path(e.filename).suffix.lower() in IMAGE_EXTS
                   and not e.filename.startswith("__MACOSX")]
        total = len(entries)
        print(f"Found {total} images in archive")

        for i, entry in enumerate(entries, 1):
            if i % 500 == 0 or i == total:
                print(f"  {i}/{total} ({i*100//total}%)  indexed={indexed}", end="\r")

            filename = Path(entry.filename).name
            try:
                data = zf.read(entry.filename)
                phash = compute_phash(data)
                if phash is None:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO library (filename, phash, archive) VALUES (?, ?, ?)",
                    (filename, phash, archive_name),
                )
                indexed += 1
                if indexed % 1000 == 0:
                    conn.commit()
            except Exception as e:
                skipped += 1

    conn.commit()
    print(f"\n  Done: indexed={indexed} skipped={skipped}")
    return indexed


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_index.py takeout-*.zip")
        sys.exit(1)

    archives = [Path(a) for a in sys.argv[1:]]
    for a in archives:
        if not a.exists():
            print(f"ERROR: file not found: {a}")
            sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # Show existing count
    existing = conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]
    if existing:
        print(f"Existing index: {existing} photos already indexed")

    total_indexed = 0
    for archive in archives:
        total_indexed += index_archive(archive, conn)

    final_count = conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]
    conn.close()

    print(f"\n=== DONE ===")
    print(f"Total in index: {final_count} photos")
    print(f"DB saved to: {DB_PATH}")


if __name__ == "__main__":
    main()
