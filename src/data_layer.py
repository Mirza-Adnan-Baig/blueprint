"""
The data layer: turns a File Doctor result into DuckDB tables and produces
the small schema summary that actually goes into the model's context.

DuckDB here is a local, open-source Python library -- an embedded database
engine running inside this process, like SQLite. Nothing is sent anywhere.
Extension auto-install is switched off so it can never try to download
anything, even by accident.

This is deliberately not a vector store. Top-k chunk retrieval can
silently omit rows, which breaks counts and sums. The full dataset is
loaded into DuckDB and the model only ever sees the SCHEMA -- table names,
columns, types, row counts, a few sample rows -- never the data itself.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

import config
from smart_router import IngestResult

SAMPLE_ROWS = 5

OFFLINE_DUCKDB_CONFIG = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}

DUCKDB_DIR = config.DUCKDB_DIR


def db_path(doc_id: str) -> Path:
    return DUCKDB_DIR / f"{doc_id}.duckdb"


def load_into_duckdb(doc_id: str, result: IngestResult) -> Path:
    """Register every table from an IngestResult into a per-document DuckDB file."""
    path = db_path(doc_id)
    con = duckdb.connect(str(path), config=OFFLINE_DUCKDB_CONFIG)

    try:
        con.execute("CREATE TABLE IF NOT EXISTS document_text (block_index INTEGER, content TEXT)")
        con.execute("DELETE FROM document_text")

        for name, df in result.tables.items():
            table_name = _safe_table_name(name)
            con.register("tmp_df", df)
            con.execute(f'CREATE OR REPLACE TABLE "{table_name}" AS SELECT * FROM tmp_df')
            con.unregister("tmp_df")

        if result.text_blocks:
            con.executemany(
                "INSERT INTO document_text VALUES (?, ?)",
                list(enumerate(result.text_blocks)),
            )
    finally:
        con.close()

    return path


def schema_summary(doc_id: str) -> str:
    """The ONLY representation of the data that reaches the model's prompt."""
    path = db_path(doc_id)
    if not path.exists():
        raise FileNotFoundError(f"No ingested data for doc_id={doc_id}")

    con = duckdb.connect(str(path), read_only=True, config=OFFLINE_DUCKDB_CONFIG)
    try:
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()

        parts: list[str] = []
        for (table_name,) in tables:
            if table_name == "document_text":
                continue

            columns = con.execute(f'DESCRIBE "{table_name}"').fetchdf()
            row_count = con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            sample = con.execute(f'SELECT * FROM "{table_name}" LIMIT {SAMPLE_ROWS}').fetchdf()

            col_lines = "\n".join(f"  - {r.column_name} ({r.column_type})" for r in columns.itertuples())
            parts.append(
                f'TABLE "{table_name}" ({row_count} rows)\n{col_lines}\n'
                f"sample rows:\n{sample.to_markdown(index=False)}"
            )

        text_count = con.execute("SELECT COUNT(*) FROM document_text").fetchone()[0]
        if text_count:
            parts.append(
                f'TABLE "document_text" ({text_count} rows) -- free-text blocks '
                f"(columns: block_index INTEGER, content TEXT). Query with LIKE / string "
                f"functions for prose content that isn't tabular."
            )

        return "\n\n".join(parts) if parts else "(no tables found for this document)"
    finally:
        con.close()


def _safe_table_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return cleaned or "data"
