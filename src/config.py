"""
Single place where settings are read. Loads .env before anything else reads
os.environ, so every module sees the same values regardless of import order.
Relative paths are resolved against the repo root, not the current working
directory, so the service behaves the same whether it's started from
Terminal or from a launchd job.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default)).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

CODE_MODEL = os.environ.get("CODE_MODEL", "qwen2.5-coder:32b")
NUM_CTX = int(os.environ.get("NUM_CTX", "32768"))

VISION_MODEL = os.environ.get("VISION_MODEL", "qwen2.5vl:32b")
VISION_NUM_CTX = int(os.environ.get("VISION_NUM_CTX", "16384"))
VISION_KEEP_ALIVE = os.environ.get("VISION_KEEP_ALIVE", "10m")

STORAGE_DIR = _path("STORAGE_DIR", "storage/ingested")
DUCKDB_DIR = _path("DUCKDB_DIR", "storage/duckdb")
LOG_FILE = _path("LOG_FILE", "logs/pipeline.log")

SANDBOX_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "30"))
SANDBOX_MEMORY_LIMIT_GB = float(os.environ.get("SANDBOX_MEMORY_LIMIT_GB", "8"))
SANDBOX_MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "1"))
DUCKDB_MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "6GB")

# How many result rows reach the answer-phrasing prompt (the full count is
# always reported alongside, so the model knows when it sees only a part).
MAX_RESULT_ROWS = int(os.environ.get("MAX_RESULT_ROWS", "200"))

# How to read a number like "1.234" when a column gives no other clue:
# "german" -> 1234 (thousands dot), "us" -> 1.234 (decimal point).
NUMBER_FORMAT_DEFAULT = os.environ.get("NUMBER_FORMAT_DEFAULT", "german").lower()

GC_MAX_AGE_HOURS = float(os.environ.get("GC_MAX_AGE_HOURS", "24"))

for _d in (STORAGE_DIR, DUCKDB_DIR, LOG_FILE.parent):
    _d.mkdir(parents=True, exist_ok=True)
