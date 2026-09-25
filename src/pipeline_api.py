"""
The Traffic Cop: the FastAPI service that ties everything together.

  POST /ingest  -- file in, doc_id + schema summary out
  POST /query   -- doc_id + question in, computed answer out

Flow for /query:
  1. Build a prompt containing ONLY the schema summary (not the data) + the
     question, ask the code model to write Python against the prepared
     DuckDB connection `con`, assigning its answer to `result`.
  2. Extract the code block, run it in execution_sandbox.
  3. On failure, feed the error back to the model once and let it retry.
  4. On success, ask the model to phrase `result` in plain language,
     without inventing any number that isn't in it.

Every call to the code model uses keep_alive: -1, so it stays resident in
memory between questions. The vision model is never called from here --
only from smart_router during ingestion, which evicts it afterwards.

Endpoints are plain `def`, not `async def`: the work inside is blocking
(model calls, file parsing, the sandbox), and FastAPI runs plain `def`
endpoints in a thread pool so one long ingestion doesn't freeze /health
or other requests.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

import config
from data_layer import load_into_duckdb, schema_summary
from execution_sandbox import run_generated_code
from smart_router import ingest_file, warm_code_model

logging.basicConfig(
    filename=str(config.LOG_FILE),
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


@app.post("/ingest")
def ingest(file: UploadFile):
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
        raise HTTPException(status_code=422, detail=f"could not process file: {e}") from e

    logger.info(
        "ingested doc_id=%s file=%s type=%s vision_pages=%s",
        doc_id, file.filename, result.source_type, result.metadata.get("vision_pages", 0),
    )

    return {
        "doc_id": doc_id,
        "source_type": result.source_type,
        "metadata": result.metadata,
        "schema": schema_summary(doc_id),
    }


@app.post("/query")
def query(doc_id: str, question: str):
    try:
        schema = schema_summary(doc_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")

    code_prompt = f"Schema:\n{schema}\n\nQuestion: {question}"
    code = _extract_code(_call_code_model(CODE_SYSTEM_PROMPT, code_prompt))
    if code is None:
        raise HTTPException(status_code=502, detail="model did not return a code block")

    sandbox_result = run_generated_code(doc_id, code)

    if not sandbox_result.success:
        retry_prompt = (
            f"{code_prompt}\n\nYour previous code:\n```python\n{code}\n```\n"
            f"failed with this error:\n{sandbox_result.error}\n\n"
            f"Fix it and output the corrected code block."
        )
        code = _extract_code(_call_code_model(CODE_SYSTEM_PROMPT, retry_prompt))
        if code is None:
            raise HTTPException(status_code=502, detail="model did not return a code block on retry")
        sandbox_result = run_generated_code(doc_id, code)

    if not sandbox_result.success:
        logger.error("query failed doc_id=%s question=%r error=%s", doc_id, question, sandbox_result.error)
        raise HTTPException(status_code=502, detail=f"code execution failed: {sandbox_result.error}")

    answer = _call_code_model(
        ANSWER_SYSTEM_PROMPT,
        f"Question: {question}\nComputed result: {sandbox_result.result}",
    )

    logger.info(
        "query doc_id=%s question=%r ok peak_mem=%.0fMB",
        doc_id, question, sandbox_result.peak_memory_mb,
    )

    return {"answer": answer, "result": sandbox_result.result, "code": code}


@app.get("/health")
def health():
    return {"status": "ok"}
