"""
The File Doctor.

Turns any uploaded file into a standard shape before anything else touches
it: a dict of named tables (pandas DataFrames) plus a list of free-text
blocks. Nothing downstream needs to know what the source file type was.

Routing (strict, and deliberately so):

  .xlsx / .xls / .csv   -> pandas only. NEVER sent to the vision model.
  .pdf, page has text   -> pdfplumber only. NEVER sent to the vision model.
  .pdf, page is scanned -> vision model (fewer than MIN_TEXT_CHARS_PER_PAGE
                           extractable characters on that page)
  .png / .jpg / ...     -> vision model (there is no text layer to read)

A vision model reads numbers by looking at pixels; pandas and pdfplumber
read the exact characters stored in the file. Routing a spreadsheet or a
text PDF through a vision model would trade exact values for approximate
ones, so the vision model is a fallback only for content that has no
text layer at all.

Memory: the code model and the vision model are both ~32B parameters and
do not fit in 64GB unified memory together with room to spare. Whenever
the vision model was used, it is explicitly evicted (keep_alive: 0) as
soon as ingestion finishes -- success or failure -- and the code model is
reloaded so the next question doesn't pay the load time.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import pandas as pd
import pdfplumber
import requests

import config
from german import convert_dataframe_numbers, looks_like_identifier

logger = logging.getLogger("pipeline")

MIN_TEXT_CHARS_PER_PAGE = 40

SPREADSHEET_SUFFIXES = {".xlsx", ".xls"}
CSV_SUFFIXES = {".csv"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

VISION_PROMPT = (
    "Transcribe this document page completely. If it contains a table, "
    "reproduce it as a markdown table with every row and every value exactly "
    "as printed -- do not round, summarize, or skip rows. If it contains a "
    "chart or diagram, list the axes, labels, and every printed number. "
    "Keep numbers in the exact format shown on the page."
)


@dataclass
class IngestResult:
    source_type: str  # "spreadsheet" | "pdf_text" | "pdf_scanned" | "image"
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    text_blocks: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class VisionSession:
    """Tracks whether the vision model was loaded during one ingestion, and
    evicts it afterwards. Used as a context manager so eviction runs even
    when ingestion raises halfway through a document."""

    def __init__(self):
        self.pages_described = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.pages_described:
            evict_vision_model()
            warm_code_model()
        return False

    def describe(self, image_bytes: bytes) -> str:
        self.pages_described += 1
        response = requests.post(
            f"{config.OLLAMA_HOST}/api/generate",
            json={
                "model": config.VISION_MODEL,
                "prompt": VISION_PROMPT,
                "images": [base64.b64encode(image_bytes).decode("ascii")],
                "stream": False,
                # Stay loaded between pages of the same document; evicted
                # explicitly in __exit__ once the whole document is done.
                "keep_alive": config.VISION_KEEP_ALIVE,
                "options": {"num_ctx": config.VISION_NUM_CTX, "temperature": 0},
            },
            timeout=600,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()


def evict_vision_model() -> None:
    """Unload the vision model from memory right now (keep_alive: 0)."""
    try:
        requests.post(
            f"{config.OLLAMA_HOST}/api/generate",
            json={"model": config.VISION_MODEL, "keep_alive": 0},
            timeout=60,
        ).raise_for_status()
        logger.info("evicted vision model %s", config.VISION_MODEL)
    except requests.RequestException:
        logger.exception("FAILED to evict vision model %s -- check `ollama ps`", config.VISION_MODEL)


def warm_code_model() -> None:
    """Load the code model and pin it in memory (keep_alive: -1)."""
    try:
        requests.post(
            f"{config.OLLAMA_HOST}/api/generate",
            json={"model": config.CODE_MODEL, "keep_alive": -1},
            timeout=600,
        ).raise_for_status()
        logger.info("code model %s loaded and pinned", config.CODE_MODEL)
    except requests.RequestException:
        logger.exception("could not pre-load code model %s", config.CODE_MODEL)


def ingest_file(path: str | Path) -> IngestResult:
    path = Path(path)
    suffix = path.suffix.lower()

    # Structured formats: exact values from the file itself, no model involved.
    if suffix in SPREADSHEET_SUFFIXES:
        return _ingest_spreadsheet(path)
    if suffix in CSV_SUFFIXES:
        return _ingest_csv(path)

    # Formats that MAY need the vision model (only for pages without text).
    if suffix in PDF_SUFFIXES:
        with VisionSession() as vision:
            return _ingest_pdf(path, vision)
    if suffix in IMAGE_SUFFIXES:
        with VisionSession() as vision:
            return _ingest_image(path, vision)

    raise ValueError(f"Unsupported file type: {suffix}")


def _clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]

    numeric_targets = [c for c in df.columns if not looks_like_identifier(df[c])]
    return convert_dataframe_numbers(df, columns=numeric_targets)


def _ingest_spreadsheet(path: Path) -> IngestResult:
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    tables = {name: _clean_table(df) for name, df in sheets.items()}
    return IngestResult(
        source_type="spreadsheet",
        tables=tables,
        metadata={"sheet_count": len(tables), "file_name": path.name, "vision_pages": 0},
    )


def _ingest_csv(path: Path) -> IngestResult:
    df = pd.read_csv(path, dtype=str, sep=None, engine="python")
    return IngestResult(
        source_type="spreadsheet",
        tables={"data": _clean_table(df)},
        metadata={"sheet_count": 1, "file_name": path.name, "vision_pages": 0},
    )


def _ingest_pdf(path: Path, vision: VisionSession) -> IngestResult:
    tables: dict[str, pd.DataFrame] = {}
    text_blocks: list[str] = []
    page_count = 0

    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_count += 1
            page_text = page.extract_text() or ""

            if len(page_text.strip()) < MIN_TEXT_CHARS_PER_PAGE:
                description = vision.describe(_render_pdf_page(path, i))
                text_blocks.append(f"[page {i + 1}, read by vision model]\n{description}")
                continue

            page_tables = page.extract_tables()
            for t_idx, raw_table in enumerate(page_tables):
                if not raw_table or len(raw_table) < 2:
                    continue
                df = _clean_table(pd.DataFrame(raw_table[1:], columns=raw_table[0]))
                if not df.empty:
                    tables[f"page{i + 1}_table{t_idx + 1}"] = df

            if not page_tables:
                text_blocks.append(f"[page {i + 1}]\n{page_text}")

    return IngestResult(
        source_type="pdf_scanned" if vision.pages_described else "pdf_text",
        tables=tables,
        text_blocks=text_blocks,
        metadata={
            "page_count": page_count,
            "vision_pages": vision.pages_described,
            "file_name": path.name,
        },
    )


def _ingest_image(path: Path, vision: VisionSession) -> IngestResult:
    description = vision.describe(path.read_bytes())
    return IngestResult(
        source_type="image",
        text_blocks=[description],
        metadata={"file_name": path.name, "vision_pages": 1},
    )


def _render_pdf_page(pdf_path: Path, page_index: int) -> bytes:
    # 200 dpi keeps small print legible; the vision model's dynamic
    # resolution uses the full image rather than downscaling to a fixed grid.
    with pymupdf.open(pdf_path) as doc:
        return doc.load_page(page_index).get_pixmap(dpi=200).tobytes("png")
