"""
Garbage collection: deletes ingested files (and their DuckDB databases)
older than GC_MAX_AGE_HOURS. Run on a schedule (README section 11) --
not imported by the service.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402


def run() -> int:
    cutoff = time.time() - config.GC_MAX_AGE_HOURS * 3600
    removed = 0

    for f in config.STORAGE_DIR.glob("*"):
        if f.name == ".gitkeep" or not f.is_file() or f.stat().st_mtime >= cutoff:
            continue
        doc_id = f.name.split("_", 1)[0]
        f.unlink()
        for db_file in config.DUCKDB_DIR.glob(f"{doc_id}.duckdb*"):
            db_file.unlink()
        removed += 1
        print(f"removed {f.name} (doc_id={doc_id})")

    print(f"gc_cleanup: removed {removed} file(s) older than {config.GC_MAX_AGE_HOURS:g}h")
    return removed


if __name__ == "__main__":
    run()
