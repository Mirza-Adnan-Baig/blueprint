"""
Garbage collection: deletes ingested files (and their DuckDB databases)
older than GC_MAX_AGE_HOURS. Run on a schedule (see README, "Scheduling
garbage collection" for the launchd setup) -- not imported by the API.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", "./storage/ingested"))
DUCKDB_DIR = Path(os.environ.get("DUCKDB_DIR", "./storage/duckdb"))
MAX_AGE_HOURS = float(os.environ.get("GC_MAX_AGE_HOURS", "24"))


def run() -> int:
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    removed = 0

    for f in STORAGE_DIR.glob("*"):
        if f.name == ".gitkeep":
            continue
        if f.is_file() and f.stat().st_mtime < cutoff:
            doc_id = f.name.split("_", 1)[0]
            f.unlink()
            db_file = DUCKDB_DIR / f"{doc_id}.duckdb"
            if db_file.exists():
                db_file.unlink()
            removed += 1
            print(f"removed {f.name} (and {db_file.name if db_file.exists() else 'no matching db'})")

    print(f"gc_cleanup: removed {removed} file(s) older than {MAX_AGE_HOURS}h")
    return removed


if __name__ == "__main__":
    run()
    sys.exit(0)
