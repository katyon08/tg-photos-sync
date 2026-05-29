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
from PIL import Image, ExifTags
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support optional

DB_PATH = Path(__file__).parent / "library_index.db"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tiff", ".bmp"}

# EXIF tag IDs
_TAG_DATE    = 36867  # DateTimeOriginal
_TAG_GPS     = 34853  # GPSInfo
_GPS_LAT     = 2
_GPS_LAT_REF = 1
_GPS_LON     = 4
_GPS_LON_REF = 3


def _dms_to_decimal(dms, ref: str) -> float | None:
    try:
        d, m, s = dms
        val = float(d) + float(m) / 60 + float(s) / 3600
        if ref in ("S", "W"):
            val = -val
        return round(val, 6)
    except Exception:
        return None


def extract_exif(img: Image.Image) -> dict:
    """Extract DateTimeOriginal and GPS from Pillow image."""
    result = {"exif_date": None, "gps_lat": None, "gps_lon": None,
              "width": img.width, "height": img.height}
    try:
        raw = img.getexif()
        if not raw:
            return result
        # Date
        date_val = raw.get(_TAG_DATE)
        if date_val:
            result["exif_date"] = str(date_val).strip()
        # GPS
        gps_data = raw.get_ifd(_TAG_GPS)
        if gps_data:
            lat = _dms_to_decimal(gps_data.get(_GPS_LAT), gps_data.get(_GPS_LAT_REF, "N"))
            lon = _dms_to_decimal(gps_data.get(_GPS_LON), gps_data.get(_GPS_LON_REF, "E"))
            result["gps_lat"] = lat
            result["gps_lon"] = lon
    except Exception:
        pass
    return result


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS library (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            filename  TEXT NOT NULL,
            phash     TEXT NOT NULL,
            archive   TEXT,
            exif_date TEXT,
            gps_lat   REAL,
            gps_lon   REAL,
            width     INTEGER,
            height    INTEGER,
            file_size INTEGER,
            avg_r     INTEGER,
            avg_g     INTEGER,
            avg_b     INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phash ON library(phash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date  ON library(exif_date)")
    conn.commit()


def avg_color(img: Image.Image) -> tuple[int, int, int]:
    """Compute average RGB by resizing to 1x1."""
    pixel = img.convert("RGB").resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    return pixel  # (r, g, b)


def compute_phash_and_meta(data: bytes) -> tuple[str | None, dict]:
    try:
        img = Image.open(io.BytesIO(data))
        rgb = img.convert("RGB")
        meta = extract_exif(img)
        meta["file_size"] = len(data)
        r, g, b = avg_color(rgb)
        meta["avg_r"] = r
        meta["avg_g"] = g
        meta["avg_b"] = b
        phash = str(imagehash.phash(rgb))
        return phash, meta
    except Exception:
        return None, {"exif_date": None, "gps_lat": None, "gps_lon": None,
                      "width": None, "height": None, "file_size": len(data),
                      "avg_r": None, "avg_g": None, "avg_b": None}


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
                phash, meta = compute_phash_and_meta(data)
                if phash is None:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO library
                       (filename, phash, archive, exif_date, gps_lat, gps_lon,
                        width, height, file_size, avg_r, avg_g, avg_b)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (filename, phash, archive_name,
                     meta["exif_date"], meta["gps_lat"], meta["gps_lon"],
                     meta["width"], meta["height"], meta["file_size"],
                     meta["avg_r"], meta["avg_g"], meta["avg_b"]),
                )
                indexed += 1
                if indexed % 1000 == 0:
                    conn.commit()
            except Exception:
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
