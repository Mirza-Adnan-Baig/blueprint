"""
The Traffic Cop: the FastAPI service that ties everything together.

  POST /ingest  file (+ optional source_id, chat_id) in, doc_id + schema out
  POST /query   question + doc_id (or chat_id) in, computed answer out

Flow for /query:
  1. Build a prompt containing ONLY the schema summary (not the data) + the
     question, ask the code model to write Python against the prepared
     DuckDB connection `con`, assigning its answer to `result`.
  2. Extract the code block, run it in execution_sandbox.
  3. On failure (no code block, or the code failed), show the model its
     code and the exact error once and let it retry.
  4. On success, ask the model to phrase `result` in plain language,
     without inventing any number that isn't in it.

Registry (storage/duckdb/registry.json): remembers which uploaded file
(`source_id`, Open WebUI's file id) became which doc_id, and which document
each chat is currently about (`chat_id`). So a file attached once is never
ingested twice, follow-up questions without a new attachment still know
their document, and both survive restarts of this service and of Open
WebUI.

Only one ingestion runs at a time. Two scanned PDFs at once would make
Ollama swap two 32B models in and out of memory for every page.

Every call to the code model uses keep_alive: -1, so it stays resident in
memory between questions. The vision model is never called from here,
only from smart_router during ingestion, which evicts it afterwards.

Endpoints are plain `def`, not `async def`: the work inside is blocking
(model calls, file parsing, the sandbox), and FastAPI runs plain `def`
endpoints in a thread pool so one long ingestion doesn't freeze /health
or other requests.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

import config
import data_layer
from data_layer import load_into_duckdb, schema_summary
from execution_sandbox import run_generated_code
from smart_router import ingest_file, warm_code_model

MAX_RESULT_CHARS = 20000

_ingest_lock = threading.Lock()
_registry_lock = threading.Lock()

logging.basicConfig(
    filename=str(config.LOG_FILE),
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pipeline")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await run_in_threadpool(warm_code_model)
    yield


app = FastAPI(title="Document Intelligence Pipeline", lifespan=_lifespan)

CODE_SYSTEM_PROMPT = """You write short Python that answers questions about a document's data.

Environment:
- `con` is an open, read-only DuckDB connection to this document's tables.
  DuckDB is a local, multithreaded SQL engine running on this machine.
- `pd` (pandas) and `np` (numpy) are already imported.

Rules:
- Prefer SQL through `con` for aggregations, counts, sums, grouping, joins,
  filtering, sorting and window functions, e.g.
      result = con.sql("SELECT part, SUM(qty) AS total FROM data GROUP BY part").df()
  Do the heavy work in SQL; only bring the small, final result into pandas.
- Do NOT load a whole large table into pandas just to aggregate it.
- Do NOT import duckdb or open any other connection -- use `con`.
- Do not read or write files. Do not print.
- Assign your final answer to a variable named exactly `result`: a number,
  string, list, dict, or a small pandas DataFrame.
- Use ONLY the table and column names given in the schema. Quote names
  with double quotes in SQL if they contain spaces or special characters.
- Output ONLY a single ```python code block, nothing else.
"""

ANSWER_SYSTEM_PROMPT = """You answer the user's question using ONLY the computed result you are given.
Do not recompute, round, or invent any number that isn't already in the result.
If the result is empty or looks wrong, say so plainly instead of guessing.
If the result says only the first rows are shown, say that the full result
has total_rows rows and that you are showing only part of it.
Answer in the same language the question was asked in.
"""


def _call_code_model(system: str, prompt: str) -> str:
    response = requests.post(
        f"{config.OLLAMA_HOST}/api/generate",
        json={
            "model": config.CODE_MODEL,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "keep_alive": -1,
            "options": {"num_ctx": config.NUM_CTX, "temperature": 0},
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json().get("response", "").strip()


def _extract_code(text: str) -> str | None:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _registry_path():
    return data_layer.DUCKDB_DIR / "registry.json"


def _load_registry() -> dict:
    path = _registry_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"sources": data.get("sources", {}), "chats": data.get("chats", {})}
        except (json.JSONDecodeError, OSError):
            logger.exception("registry.json unreadable, starting a fresh one")
    return {"sources": {}, "chats": {}}


def _save_registry(registry: dict) -> None:
    path = _registry_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=1), encoding="utf-8")
    tmp.replace(path)


def _remember(source_id: str | None, chat_id: str | None, doc_id: str) -> None:
    with _registry_lock:
        registry = _load_registry()
        if source_id:
            registry["sources"][source_id] = doc_id
        if chat_id:
            registry["chats"][chat_id] = doc_id
        _save_registry(registry)


def _known_doc(key: str, value: str | None) -> str | None:
    """doc_id for a source/chat, only if its database still exists (the
    cleanup job may have deleted it)."""
    if not value:
        return None
    with _registry_lock:
        doc_id = _load_registry()[key].get(value)
    if doc_id and data_layer.db_path(doc_id).exists():
        return doc_id
    return None


def _ingest_response(doc_id: str, **extra) -> dict:
    return {"doc_id": doc_id, "schema": schema_summary(doc_id), **extra}


@app.post("/ingest")
def ingest(
    file: UploadFile,
    source_id: str | None = Form(None),
    chat_id: str | None = Form(None),
):
    known = _known_doc("sources", source_id)
    if known:
        _remember(None, chat_id, known)
        return _ingest_response(known, cached=True)

    with _ingest_lock:
        # Another request may have ingested the same file while this one waited.
        known = _known_doc("sources", source_id)
        if known:
            _remember(None, chat_id, known)
            return _ingest_response(known, cached=True)

        doc_id = uuid.uuid4().hex[:12]
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (file.filename or "upload"))
        dest = config.STORAGE_DIR / f"{doc_id}_{safe_name}"

        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            result = ingest_file(dest)
            load_into_duckdb(doc_id, result)
        except Exception as e:
            logger.exception("ingest failed for %s", file.filename)
            dest.unlink(missing_ok=True)
            data_layer.db_path(doc_id).unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"could not process file: {e}") from e

    _remember(source_id, chat_id, doc_id)
    logger.info(
        "ingested doc_id=%s file=%s type=%s vision_pages=%s tables=%s",
        doc_id, file.filename, result.source_type, result.metadata.get("vision_pages", 0),
        len(result.tables),
    )
    return _ingest_response(
        doc_id,
        cached=False,
        source_type=result.source_type,
        metadata=result.metadata,
    )


def _result_text(result) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ... (result cut off here, it is longer)"
    return text


def _ask_for_code(prompt: str) -> tuple[str | None, str]:
    raw = _call_code_model(CODE_SYSTEM_PROMPT, prompt)
    return _extract_code(raw), raw


@app.post("/query")
def query(question: str, doc_id: str | None = None, chat_id: str | None = None):
    doc_id = doc_id or _known_doc("chats", chat_id)
    if not doc_id:
        raise HTTPException(status_code=404, detail="no document for this chat")
    try:
        schema = schema_summary(doc_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")

    code_prompt = f"Schema:\n{schema}\n\nQuestion: {question}"
    code, raw = _ask_for_code(code_prompt)

    if code is None:
        error = "Your reply did not contain a ```python code block."
        failed = raw
    else:
        sandbox_result = run_generated_code(doc_id, code)
        error = None if sandbox_result.success else sandbox_result.error
        failed = code

    if error:
        retry_prompt = (
            f"{code_prompt}\n\nYour previous reply:\n{failed}\n\n"
            f"failed with this error:\n{error}\n\n"
            f"Fix it and output only the corrected ```python code block."
        )
        code, _ = _ask_for_code(retry_prompt)
        if code is None:
            raise HTTPException(status_code=502, detail="model did not return a code block, twice")
        sandbox_result = run_generated_code(doc_id, code)

    if not sandbox_result.success:
        logger.error("query failed doc_id=%s question=%r error=%s", doc_id, question, sandbox_result.error)
        raise HTTPException(status_code=502, detail=f"code execution failed: {sandbox_result.error}")

    answer = _call_code_model(
        ANSWER_SYSTEM_PROMPT,
        f"Question: {question}\nComputed result (JSON): {_result_text(sandbox_result.result)}",
    )

    logger.info(
        "query doc_id=%s question=%r ok peak_mem=%.0fMB\ncode used:\n%s\nresult: %s",
        doc_id, question, sandbox_result.peak_memory_mb, code, _result_text(sandbox_result.result)[:2000],
    )

    return {"answer": answer, "result": sandbox_result.result, "code": code, "doc_id": doc_id}


@app.get("/health")
def health():
    return {"status": "ok"}
