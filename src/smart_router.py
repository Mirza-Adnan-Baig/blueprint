"""
The File Doctor.

Turns any uploaded file into a standard shape before anything else touches
it: a dict of named tables (pandas DataFrames) plus a list of free-text
blocks (for prose/paragraphs that aren't tabular). Nothing downstream needs
to know whether the source was an Excel file, a text PDF, or a scanned PDF.

Routing:
  .xlsx / .xls / .csv   -> pandas, one table per sheet
  .pdf (text layer)      -> pdfplumber tables + text per page
  .pdf (scanned) / image -> rendered to an image, described by a vision
                            model (LLaVA via Ollama), result kept as text
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd
import pdfplumber
import requests

from german import convert_dataframe_numbers, looks_like_identifier

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
VISION_MODEL = os.environ.get("VISION_MODEL", "llava")

# A PDF page with less text than this is treated as a scan and routed to
# the vision model instead of pdfplumber's text/table extraction.
MIN_TEXT_CHARS_PER_PAGE = 40


@dataclass
class IngestResult:
    source_type: str  # "spreadsheet" | "pdf_text" | "pdf_scanned" | "image"
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    text_blocks: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def ingest_file(path: str | Path) -> IngestResult:
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xls"):
        return _ingest_spreadsheet(path)
    if suffix == ".csv":
        return _ingest_csv(path)
    if suffix == ".pdf":
        return _ingest_pdf(path)
    if suffix in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        return _ingest_image(path)

    raise ValueError(f"Unsupported file type: {suffix}")


def _clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]

    numeric_targets = [c for c in df.columns if not looks_like_identifier(df[c])]
    df = convert_dataframe_numbers(df, columns=numeric_targets)
    return df


def _ingest_spreadsheet(path: Path) -> IngestResult:
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    tables = {name: _clean_table(df) for name, df in sheets.items()}
    return IngestResult(
        source_type="spreadsheet",
        tables=tables,
        metadata={"sheet_count": len(tables), "file_name": path.name},
    )


def _ingest_csv(path: Path) -> IngestResult:
    df = pd.read_csv(path, dtype=str, sep=None, engine="python")
    table = _clean_table(df)
    return IngestResult(
        source_type="spreadsheet",
        tables={"data": table},
        metadata={"sheet_count": 1, "file_name": path.name},
    )


def _ingest_pdf(path: Path) -> IngestResult:
    tables: dict[str, pd.DataFrame] = {}
    text_blocks: list[str] = []
    scanned_pages = 0

    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""

            if len(page_text.strip()) < MIN_TEXT_CHARS_PER_PAGE:
                # Likely a scan -- fall back to the vision model for this page.
                scanned_pages += 1
                description = _describe_page_with_vision(path, i)
                text_blocks.append(f"[page {i + 1}, vision-described]\n{description}")
                continue

            page_tables = page.extract_tables()
            for t_idx, raw_table in enumerate(page_tables):
                if not raw_table or len(raw_table) < 2:
                    continue
                df = pd.DataFrame(raw_table[1:], columns=raw_table[0])
                df = _clean_table(df)
                if not df.empty:
                    tables[f"page{i + 1}_table{t_idx + 1}"] = df

            if not page_tables:
                text_blocks.append(f"[page {i + 1}]\n{page_text}")

    source_type = "pdf_scanned" if scanned_pages > 0 else "pdf_text"
    return IngestResult(
        source_type=source_type,
        tables=tables,
        text_blocks=text_blocks,
        metadata={"page_count": i + 1, "scanned_pages": scanned_pages, "file_name": path.name},
    )


def _ingest_image(path: Path) -> IngestResult:
    with open(path, "rb") as f:
        image_bytes = f.read()
    description = _describe_image_with_vision(image_bytes)
    return IngestResult(
        source_type="image",
        text_blocks=[description],
        metadata={"file_name": path.name},
    )


def _describe_page_with_vision(pdf_path: Path, page_index: int) -> str:
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_index)
    pix = page.get_pixmap(dpi=200)
    image_bytes = pix.tobytes("png")
    doc.close()
    return _describe_image_with_vision(image_bytes)


def _describe_image_with_vision(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "Describe this document page in full detail. If it contains a table, "
        "reproduce it as a markdown table with every row and value. If it "
        "contains a chart or diagram, describe the axes, values, and any "
        "labeled numbers precisely. Do not summarize or omit rows."
    )

    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": VISION_MODEL,
            "prompt": prompt,
            "images": [encoded],
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("response", "").strip()
