"""
The Traffic Cop: the FastAPI service that ties everything together.

  POST /ingest  -- file in, doc_id + schema summary out
  POST /query   -- doc_id + question in, computed answer out

Flow for /query:
  1. Build a prompt containing ONLY the schema summary (not the data) + the
     question, ask the code model to write Python against the already-open
     DuckDB connection `con`, assigning its answer to `result`.
  2. Extract the code block, run it in execution_sandbox (separate process,
     timeout, import/call whitelist).
  3. On failure, feed the error back to the model once and let it retry
     (handles truncated/invalid code -- see README, "Known limitation of
     structured output").
  4. On success, ask the model to phrase `result` as a plain-language
     answer. It is told explicitly not to invent numbers -- only phrase
     the ones it was given.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile

from data_layer import load_into_duckdb, schema_summary
from execution_sandbox import run_generated_code
from smart_router import ingest_file

load_dotenv()

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
CODE_MODEL = os.environ.get("CODE_MODEL", "qwen2.5-coder:32b")
NUM_CTX = int(os.environ.get("NUM_CTX", "32768"))
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", "./storage/ingested"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = Path(os.environ.get("LOG_FILE", "./logs/pipeline.log"))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pipeline")

app = FastAPI(title="Document Intelligence Pipeline")

CODE_SYSTEM_PROMPT = """You write short Python that answers questions about a document's data.

Rules:
- A DuckDB connection is already open as `con` (read-only). Query it with
  con.execute("SELECT ...").df() or con.sql("...").
- Use pandas (pd) and numpy (np) for anything SQL doesn't cover cleanly.
- Do not import anything else. Do not read or write files. Do not print.
- Assign your final answer to a variable named exactly `result`. It should
  be a plain number, string, list, or a pandas DataFrame -- whichever fits
  the question.
- Base everything on the ACTUAL table names and columns given in the
  schema. Never invent a column or table name.
- Output ONLY a single ```python code block, nothing else.
"""

ANSWER_SYSTEM_PROMPT = """You answer the user's question using ONLY the computed result you are given.
Do not recompute, round, or invent any number that isn't already in the result.
If the result is empty or looks wrong, say so plainly instead of guessing.
Answer in the same language the question was asked in.
"""


def _call_ollama(system: str, prompt: str, model: str = CODE_MODEL) -> str:
    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": NUM_CTX},
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json().get("response", "").strip()


def _extract_code(text: str) -> str | None:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else None


@app.post("/ingest")
async def ingest(file: UploadFile):
    doc_id = uuid.uuid4().hex[:12]
    dest = STORAGE_DIR / f"{doc_id}_{file.filename}"

    with open(dest, "wb") as f:
        f.write(await file.read())

    try:
        result = ingest_file(dest)
        load_into_duckdb(doc_id, result)
    except Exception as e:
        logger.exception("ingest failed for %s", file.filename)
        raise HTTPException(status_code=422, detail=f"could not process file: {e}") from e

    summary = schema_summary(doc_id)
    logger.info("ingested doc_id=%s file=%s type=%s", doc_id, file.filename, result.source_type)

    return {
        "doc_id": doc_id,
        "source_type": result.source_type,
        "metadata": result.metadata,
        "schema": summary,
    }


@app.post("/query")
async def query(doc_id: str, question: str):
    try:
        schema = schema_summary(doc_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")

    code_prompt = f"Schema:\n{schema}\n\nQuestion: {question}"
    raw = _call_ollama(CODE_SYSTEM_PROMPT, code_prompt)
    code = _extract_code(raw)

    if code is None:
        raise HTTPException(status_code=502, detail="model did not return a code block")

    sandbox_result = run_generated_code(doc_id, code)

    if not sandbox_result.success:
        # One retry, with the error fed back -- covers truncated/invalid
        # code, the known limitation of grammar-constrained generation.
        retry_prompt = (
            f"{code_prompt}\n\nYour previous code failed with this error:\n"
            f"{sandbox_result.error}\n\nFix it and output the corrected code block."
        )
        raw = _call_ollama(CODE_SYSTEM_PROMPT, retry_prompt)
        code = _extract_code(raw)
        if code is None:
            raise HTTPException(status_code=502, detail="model did not return a code block on retry")
        sandbox_result = run_generated_code(doc_id, code)

    if not sandbox_result.success:
        logger.error("query failed doc_id=%s question=%r error=%s", doc_id, question, sandbox_result.error)
        raise HTTPException(status_code=502, detail=f"code execution failed: {sandbox_result.error}")

    answer_prompt = f"Question: {question}\nComputed result: {sandbox_result.result}"
    answer = _call_ollama(ANSWER_SYSTEM_PROMPT, answer_prompt)

    logger.info("query doc_id=%s question=%r ok", doc_id, question)

    return {
        "answer": answer,
        "result": sandbox_result.result,
        "code": code,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
