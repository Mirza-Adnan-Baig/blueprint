"""
The File Doctor.

Turns any uploaded file into a standard shape before anything else touches
it: a dict of named tables (pandas DataFrames) plus a list of free-text
blocks. Nothing downstream needs to know what the source file type was.

Routing (strict, and deliberately so):

  .xlsx / .xls / .csv        -> pandas only. NEVER sent to the vision model.
  .pdf page with a table     -> pdfplumber. NEVER sent to the vision model.
  .pdf page with text        -> pdfplumber. NEVER sent to the vision model.
  .pdf page that is a scan   -> vision model (little or no text layer, and
                                an image on the page)
  .png / .jpg / ...          -> vision model (there is no text layer to read)

A vision model reads numbers by looking at pixels; pandas and pdfplumber
read the exact characters stored in the file. So the vision model is only
a fallback for content that has no text layer at all. When it returns a
markdown table, that table is turned into a real table, so scanned
inventories can be summed and counted like any other.

Tables that continue over several PDF pages are joined back into one
table: a continuation page's first row is kept as data (not mistaken for
a header), and a header repeated at the top of each page is dropped. Every
PDF table row records the page it came from in a `source_page` column.

Memory: the code model and the vision model are both ~32B parameters and
do not fit in 64GB unified memory together with room to spare. Whenever
the vision model was used, it is explicitly evicted (keep_alive: 0) as
soon as ingestion finishes, success or failure, and the code model is
reloaded so the next question doesn't pay the load time.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pdfplumber
import pymupdf
import requests

import config
from german import convert_dataframe_numbers, looks_like_identifier

logger = logging.getLogger("pipeline")

MIN_TEXT_CHARS_PER_PAGE = 40
# A page with no text and no embedded image is only treated as a scan when
# it is drawn from many vector shapes (text converted to outlines).
MIN_VECTOR_OBJECTS_FOR_SCAN = 50

SPREADSHEET_SUFFIXES = {".xlsx", ".xls"}
CSV_SUFFIXES = {".csv"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

VISION_PROMPT = (
    "Transcribe this document page completely. If it contains a table, "
    "reproduce it as a markdown table with every row and every value exactly "
    "as printed. Do not round, summarize, or skip rows. Repeat the table's "
    "column headers as the first row of the markdown table. If it contains a "
    "chart or diagram, list the axes, labels, and every printed number. "
    "Keep numbers in the exact format shown on the page."
)

_NUMBER_LIKE = re.compile(r"^[-+]?[\d.,\s ]+(€|%|EUR)?$", re.IGNORECASE)
_MD_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


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
        logger.exception("FAILED to evict vision model %s, check `ollama ps`", config.VISION_MODEL)


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

    raise ValueError(f"Unsupported file type: {suffix or '(no extension)'}")


# --- Tables: shared cleaning ---------------------------------------------------

def _unique_columns(names: list) -> list[str]:
    """Blank headers get a position name, duplicates get a suffix. PDF tables
    with merged header cells produce exactly these, and duplicate column
    names break both pandas and DuckDB."""
    result: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(names):
        text = "" if raw is None else " ".join(str(raw).split())
        if text in ("", "None", "nan", "NaN"):
            text = f"column_{i + 1}"
        name, n = text, 1
        while name in seen:
            n += 1
            name = f"{text}_{n}"
        seen.add(name)
        result.append(name)
    return result


def _clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = _unique_columns(list(df.columns))
    for col in df.columns:
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(lambda v: np.nan if isinstance(v, str) and not v.strip() else v)
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

    numeric_targets = [c for c in df.columns if not looks_like_identifier(df[c])]
    return convert_dataframe_numbers(df, columns=numeric_targets)


# --- Spreadsheets and CSV -------------------------------------------------------

def _ingest_spreadsheet(path: Path) -> IngestResult:
    # Keep Excel's own types: real numbers stay exact numbers, dates stay
    # dates. dtype=object matters: without it pandas quietly turns text
    # cells like "1.234" into the number 1.234 (US reading) before German
    # parsing ever sees them. infer_objects() then restores number and date
    # columns without touching text.
    sheets = pd.read_excel(path, sheet_name=None, dtype=object)
    tables = {str(name): _clean_table(df.infer_objects()) for name, df in sheets.items()}
    tables = {name: df for name, df in tables.items() if not df.empty}
    return IngestResult(
        source_type="spreadsheet",
        tables=tables,
        metadata={"sheet_count": len(tables), "file_name": path.name, "vision_pages": 0},
    )


def _decode_text(raw: bytes) -> str:
    # UTF-8 first (with or without BOM). Excel on Windows saves "CSV" as
    # Windows-1252, which is where German umlauts would otherwise fail.
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _guess_delimiter(text: str) -> str:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    counts = {sep: first_line.count(sep) for sep in (";", "\t", ",", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def _ingest_csv(path: Path) -> IngestResult:
    text = _decode_text(path.read_bytes())
    df = pd.read_csv(io.StringIO(text), dtype=str, sep=_guess_delimiter(text), engine="python")
    return IngestResult(
        source_type="spreadsheet",
        tables={"data": _clean_table(df)},
        metadata={"sheet_count": 1, "file_name": path.name, "vision_pages": 0},
    )


# --- PDF ----------------------------------------------------------------------------

def _cell(value) -> str:
    return "" if value is None else " ".join(str(value).split())


def _looks_like_header(row: list[str]) -> bool:
    filled = [c for c in row if c]
    return bool(filled) and not any(_NUMBER_LIKE.match(c) for c in filled)


def _markdown_tables(text: str) -> list[list[list[str]]]:
    """Markdown tables from the vision model's answer, as lists of rows."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [c.strip().strip("*").strip() for c in stripped.strip("|").split("|")]
            if any(cells) and all(_MD_SEPARATOR_CELL.match(c) for c in cells if c):
                continue
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    return [t for t in tables if len(t) >= 2]


def _merge_page_tables(found: list[tuple[int, int, list[list]]]) -> dict[str, pd.DataFrame]:
    """Join tables that continue across pages.

    `found` holds (page_number, index_on_page, rows) in document order. The
    first table on a page continues the previous table when that table ended
    on the previous page and both have the same number of columns.
    """
    groups: list[dict] = []
    for page_no, index_on_page, raw_rows in found:
        rows = [[_cell(c) for c in r] for r in raw_rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue

        last = groups[-1] if groups else None
        width = len(rows[0])
        continues = (
            last is not None
            and index_on_page == 0
            and page_no == last["last_page"] + 1
            and width == len(last["header"])
        )
        if continues and rows[0] == last["header"]:
            rows = rows[1:]  # header repeated on the new page
        elif continues and _looks_like_header(rows[0]):
            continues = False  # a different table that happens to be as wide

        if continues:
            last["rows"].extend((page_no, r) for r in rows)
            last["last_page"] = page_no
            continue

        if _looks_like_header(rows[0]):
            header, body = rows[0], rows[1:]
        else:
            header, body = [""] * width, rows
        groups.append({"header": header, "rows": [(page_no, r) for r in body], "last_page": page_no})

    tables: dict[str, pd.DataFrame] = {}
    for group in groups:
        width = len(group["header"])
        data = [(r + [""] * width)[:width] + [page] for page, r in group["rows"]]
        if not data:
            continue
        df = pd.DataFrame(data, columns=_unique_columns(group["header"]) + ["source_page"])
        df = _clean_table(df)
        if not df.empty and len(df.columns) > 1:
            tables[f"table{len(tables) + 1}"] = df
    return tables


def _page_is_scan(page, text: str) -> bool:
    if len(text.strip()) >= MIN_TEXT_CHARS_PER_PAGE:
        return False
    if page.images:
        return True
    vector_objects = len(page.curves) + len(page.rects) + len(page.lines)
    return not page.chars and vector_objects >= MIN_VECTOR_OBJECTS_FOR_SCAN


def _ingest_pdf(path: Path, vision: VisionSession) -> IngestResult:
    found: list[tuple[int, int, list[list]]] = []
    text_blocks: list[str] = []
    page_count = 0

    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_no = i + 1
            page_count += 1
            text = page.extract_text() or ""
            page_tables = page.find_tables()

            if page_tables:
                # A table page is never a scan, however few characters it has.
                outside = page
                for index_on_page, table in enumerate(page_tables):
                    found.append((page_no, index_on_page, table.extract()))
                    outside = outside.outside_bbox(table.bbox)
                other_text = (outside.extract_text() or "").strip()
                if other_text:
                    text_blocks.append(f"[page {page_no}]\n{other_text}")
                continue

            if _page_is_scan(page, text):
                description = vision.describe(_render_pdf_page(path, i))
                text_blocks.append(f"[page {page_no}, read by vision model]\n{description}")
                for index_on_page, rows in enumerate(_markdown_tables(description)):
                    found.append((page_no, index_on_page, rows))
                continue

            if text.strip():
                text_blocks.append(f"[page {page_no}]\n{text}")

    return IngestResult(
        source_type="pdf_scanned" if vision.pages_described else "pdf_text",
        tables=_merge_page_tables(found),
        text_blocks=text_blocks,
        metadata={
            "page_count": page_count,
            "vision_pages": vision.pages_described,
            "file_name": path.name,
        },
    )


def _ingest_image(path: Path, vision: VisionSession) -> IngestResult:
    description = vision.describe(path.read_bytes())
    found = [(1, i, rows) for i, rows in enumerate(_markdown_tables(description))]
    return IngestResult(
        source_type="image",
        tables=_merge_page_tables(found),
        text_blocks=[description],
        metadata={"file_name": path.name, "vision_pages": 1},
    )


def _render_pdf_page(pdf_path: Path, page_index: int) -> bytes:
    # 200 dpi keeps small print legible; the vision model's dynamic
    # resolution uses the full image rather than downscaling to a fixed grid.
    with pymupdf.open(pdf_path) as doc:
        return doc.load_page(page_index).get_pixmap(dpi=200).tobytes("png")
