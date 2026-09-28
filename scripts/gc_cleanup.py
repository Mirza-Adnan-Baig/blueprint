"""
Garbage collection: deletes ingested files (and their DuckDB databases)
older than GC_MAX_AGE_HOURS. Run on a schedule (README section 11) --
not imported by the service.
"""

from __future__ import annotations

import json
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

    _prune_registry()
    print(f"gc_cleanup: removed {removed} file(s) older than {config.GC_MAX_AGE_HOURS:g}h")
    return removed


def _prune_registry() -> None:
    """Forget files and chats whose database was deleted, so they get
    ingested again next time instead of pointing at nothing."""
    path = config.DUCKDB_DIR / "registry.json"
    if not path.exists():
        return
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key in ("sources", "chats"):
        entries = registry.get(key, {})
        registry[key] = {
            k: doc_id for k, doc_id in entries.items()
            if (config.DUCKDB_DIR / f"{doc_id}.duckdb").exists()
        }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=1), encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    run()
