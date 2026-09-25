"""
The data layer: turns a File Doctor result into DuckDB tables and produces
the small schema summary that actually goes into the model's context.

This is deliberately not a vector store. Retrieval-by-embedding was
considered and dropped for structured data: top-k chunk retrieval can
silently omit rows, which breaks exactly the kind of question this system
exists to answer (counts, sums, "how many of X do we have"). Instead, the
full dataset is loaded into DuckDB and the model only ever sees the
SCHEMA -- table names, columns, types, a handful of sample rows, and row
counts -- never the data itself. It writes SQL/code against the schema;
that code runs against every row. See README.md, "Why DuckDB instead of a
vector store."
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

from smart_router import IngestResult

DUCKDB_DIR = Path(os.environ.get("DUCKDB_DIR", "./storage/duckdb"))
DUCKDB_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_ROWS = 5


def db_path(doc_id: str) -> Path:
    return DUCKDB_DIR / f"{doc_id}.duckdb"


def load_into_duckdb(doc_id: str, result: IngestResult) -> Path:
    """Register every table from an IngestResult into a per-document DuckDB file."""
    path = db_path(doc_id)
    con = duckdb.connect(str(path))

    try:
        con.execute("CREATE TABLE IF NOT EXISTS document_text (block_index INTEGER, content TEXT)")
        con.execute("DELETE FROM document_text")

        for name, df in result.tables.items():
            table_name = _safe_table_name(name)
            con.register("tmp_df", df)
            con.execute(f'CREATE OR REPLACE TABLE "{table_name}" AS SELECT * FROM tmp_df')
            con.unregister("tmp_df")

        if result.text_blocks:
            rows = [(i, block) for i, block in enumerate(result.text_blocks)]
            con.executemany("INSERT INTO document_text VALUES (?, ?)", rows)
    finally:
        con.close()

    return path


def schema_summary(doc_id: str) -> str:
    """Produce the compact, human/LLM-readable schema description used in prompts.

    This is the ONLY representation of the data that reaches the model's
    context window before it writes code -- table + column names, types,
    row counts, and a few sample rows. Deliberately small regardless of how
    large the underlying table is.
    """
    path = db_path(doc_id)
    if not path.exists():
        raise FileNotFoundError(f"No ingested data for doc_id={doc_id}")

    con = duckdb.connect(str(path), read_only=True)
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
                f"(columns: block_index INTEGER, content TEXT). Query with LIKE/string "
                f"functions for prose content that isn't tabular."
            )

        return "\n\n".join(parts) if parts else "(no tables found for this document)"
    finally:
        con.close()


def _safe_table_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return cleaned or "data"
