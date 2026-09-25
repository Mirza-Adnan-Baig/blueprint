# Local Document Intelligence Pipeline

Upload a large, messy business document (Excel, CSV, PDF — text or
scanned) and get calculated answers back, computed against the *actual*
data, not guessed from whatever chunk an LLM happened to see. Runs
entirely on one machine, no cloud calls, no API keys.

This README is the whole build guide, from an empty folder to a working
system. Follow it top to bottom the first time; after that, use it as a
reference. Every code file it describes already exists in this repo —
this document explains why each one is shaped the way it is, so you can
maintain and extend it rather than treat it as a black box.

**Target hardware:** Apple Silicon Mac (developed against an M2 Ultra, 64GB
unified memory). Everything here is native macOS — no Docker.

---

## 1. The problem this solves, and why the design looks like this

Feeding a whole document straight into a chat model's context breaks down
in two ways once documents get long: the model only reliably attends to
part of the context, and any question that requires aggregating across
*every* row ("how many total units", "which part appears most often")
fails silently — a wrong-but-confident answer, not an error.

Three design decisions follow directly from that, and from testing that
was actually done against this problem before writing this guide:

**No RAG (embeddings + vector search) for structured data.** Retrieval
returns the top-k most *similar* chunks, not all of them. For a counting
or sum question, missing even one relevant row gives you a wrong total —
and there's no way to know it happened. Published benchmarks on
corpus-wide aggregation questions confirm this isn't a tuning problem,
it's structural (the best-performing retrieval approach in one 2026
benchmark still only scored ~1.5/100 on aggregation questions). So: no
vector store for tabular data in this design. Full data goes into a real
database instead, and the model queries *all* of it.

**The model never sees the raw data.** It sees a schema — table names,
column names and types, row counts, a few sample rows — and writes SQL or
pandas code against it. The code runs against the complete dataset in a
sandboxed subprocess; only the computed result (a number, a small table)
comes back. This is what actually makes document size a non-issue: a
100-row sheet and a 100,000-row sheet produce the same size prompt,
because the model is reasoning about a schema, not scanning rows.

**No dependence on the model's native tool-calling.** Ollama's
tool-calling support is model- and version-dependent, and "thinking"
models in particular have had real, documented bugs where a tool call
leaks into visible text instead of being parsed. This design sidesteps
that class of bug entirely: the model is simply asked to write a Python
code block in its normal text output, which is then extracted with a
regex and validated/executed directly. There is no tool-call parser in
the critical path.

Everything below builds toward that: **File Doctor → DuckDB → sandboxed
code execution → plain-language answer.**

```
 Open WebUI (chat UI)
        |
        v
 pipe_function.py  (paste into Admin Panel -> Functions)
        |  POST /ingest (file)          POST /query (doc_id, question)
        v
 pipeline_api.py  (FastAPI, runs natively, not in a container)
        |                                       |
        v                                       v
 smart_router.py            data_layer.py (DuckDB)   execution_sandbox.py
 (Excel/CSV/PDF/OCR)   -->  (schema + full data)  --> (runs model's code,
                                     ^                  AST-validated,
                                     |                  timeout-enforced)
                              Ollama (native)
                       qwen2.5-coder:32b (writes the code, phrases the answer)
                       llava (describes scanned pages / images)
```

---

## 2. Repository layout

```
blueprint/
  README.md              <- this file
  pyproject.toml          <- uv-managed dependencies
  .env.example             <- copy to .env
  src/
    german.py               <- German number-format detection/conversion
    smart_router.py          <- File Doctor: any file -> tables + text
    data_layer.py             <- loads tables into DuckDB, builds schema summaries
    execution_sandbox.py      <- runs model-generated code safely
    pipeline_api.py            <- FastAPI service: /ingest, /query
  openwebui/
    pipe_function.py          <- paste into Open WebUI Admin Panel -> Functions
  scripts/
    setup_mac.sh               <- Phase I environment setup
    gc_cleanup.py               <- deletes old ingested files
  storage/
    ingested/                   <- uploaded files land here
    duckdb/                      <- one .duckdb file per ingested document
  logs/
    pipeline.log                 <- created at runtime
  tests/
    test_pipeline_offline.py      <- no-Ollama-required tests (already passing)
```

---

## 3. Phase I — Environment setup on the Mac

Run once, from Terminal, inside this repo folder:

```bash
chmod +x scripts/setup_mac.sh
./scripts/setup_mac.sh
```

What it does, and why, step by step:

1. **Removes any Docker-based Ollama container.** If Ollama has been
   running inside Docker Desktop, its VM has historically not had full
   Metal/GPU passthrough on Apple Silicon, and has its own separate RAM
   ceiling below your actual system RAM. Native Ollama has direct GPU
   access and, since Ollama v0.19, uses Apple's MLX backend instead of
   llama.cpp on Apple Silicon — a real speed improvement, not just a
   convenience.
2. **Installs Homebrew**, if not already present.
3. **Installs native Ollama** via `brew install ollama` and starts it as a
   background service (`brew services start ollama`), so it's always
   running, not something you launch manually each session.
4. **Installs `uv`** — this project is managed with `uv`, not `pip`
   directly. `uv sync` reads `pyproject.toml` and creates an isolated
   `.venv` with exact, reproducible versions.
5. **Pulls the two models this pipeline uses:**
   `qwen2.5-coder:32b` (writes the SQL/pandas code and phrases answers —
   a code-specialized model was chosen deliberately, since its job is
   literally to write correct Python/SQL) and `llava` (describes scanned
   pages and images that have no extractable text layer).
6. **Raises the context window default** via
   `OLLAMA_CONTEXT_LENGTH=32768`. This pipeline's prompts stay small (a
   schema summary, not raw data) so 32K is generous headroom, not a
   minimum requirement.
7. **Runs `uv sync`** to install this project's Python dependencies.
8. **Creates `.env`** from `.env.example` if it doesn't exist yet.

**Verify before moving on:**

```bash
ollama --version        # want 0.34.0 or newer — older versions have a
                         # documented bug where a thinking model's tool
                         # call leaks into plain text instead of parsing
ollama list              # should show qwen2.5-coder:32b and llava
ps aux | grep -i ollama  # confirm it's a native process, not `docker ...`
```

Also check free RAM headroom in Activity Monitor before running large
documents through this — qwen2.5-coder:32b at full precision plus llava
both loaded will use a meaningful chunk of the 64GB.

---

## 4. Phase II — The File Doctor (`src/smart_router.py`)

Takes any uploaded file and returns a standard shape (`IngestResult`):
named tables (pandas DataFrames) plus free-text blocks. Everything
downstream is agnostic to what the original file type was.

- **Excel/CSV** → `pandas`, one table per sheet.
- **Text-based PDF** → `pdfplumber`, tables extracted per page; pages
  with no detected table keep their raw text as a text block.
- **Scanned PDF / images** → a page is treated as scanned when it has
  fewer than 40 extracted characters. It's rendered to an image with
  PyMuPDF and sent to `llava` with a prompt that explicitly asks for
  every row of any table to be reproduced, not summarized.

Every table passes through `_clean_table`, which drops empty rows/columns
and applies German-aware numeric conversion (next section) — except for
columns that look like identifiers (leading zeros, long unique digit
strings — part numbers, IBANs, EANs), which are deliberately left as
strings so they're never accidentally summed.

## 5. German number handling (`src/german.py`)

German-formatted numbers use `.` as a thousands separator and `,` as the
decimal separator — the reverse of pandas' default parsing. Naively
parsing `"1.234"` gives `1.234` instead of `1234`; a column of
`1.234 / 2.500 / 750` sums to `753.734` instead of `4.484`. This module:

- Detects the format **per column**, not per file — different columns in
  the same sheet can be formatted differently, so this is never assumed.
- Converts only when it's confident (checks for an unambiguous decimal
  comma or decimal point in the sample); otherwise leaves the column
  alone rather than guessing.
- Flags identifier-looking columns (`looks_like_identifier`) so they're
  excluded from numeric conversion even if every value looks numeric.

## 6. Phase III — The data layer (`src/data_layer.py`)

Loads every table from an `IngestResult` into a DuckDB file
(`storage/duckdb/{doc_id}.duckdb`), one file per ingested document, plus
a `document_text` table for any free-text blocks (queryable with
`LIKE`/string functions — no embeddings involved).

`schema_summary(doc_id)` produces the only representation of the data
that ever reaches the model's prompt: table names, columns with types,
row counts, and up to 5 sample rows per table. This stays small
regardless of how large the underlying table actually is — that's the
whole point (see section 1).

## 7. Phase IV — The sandbox (`src/execution_sandbox.py`)

Runs the model's generated Python. Stated plainly: this stops
*accidents* — a hallucinated destructive call, an infinite loop, a
runaway query — not a determined adversary. The model runs locally and
isn't attacker-controlled input in the usual sense, so this is
deliberately not a full OS-level sandbox (no containers/seccomp). Three
real layers:

1. **AST validation before anything runs** — only
   `duckdb, pandas, numpy, statistics, math, json, datetime, re` may be
   imported, and calls to file/process functions (`open`, `read_csv`,
   `to_excel`, `os.system`, etc.) are rejected even if reached through an
   already-allowed module like pandas.
2. **A separate subprocess**, not the API process — a crash or hang in
   generated code can't take down `pipeline_api.py`.
3. **A hard timeout** (`SANDBOX_TIMEOUT_SECONDS`, default 30s in
   `.env.example`) enforced by killing the subprocess.

The generated code must assign its answer to a variable named `result`;
the sandbox serializes it to JSON and hands it back.

## 8. Phase V — The Traffic Cop (`src/pipeline_api.py`)

FastAPI service with two endpoints:

- **`POST /ingest`** (multipart file) → runs the File Doctor, loads the
  result into DuckDB, returns a `doc_id` and the schema summary.
- **`POST /query?doc_id=...&question=...`** →
  1. Sends the schema summary + question to `qwen2.5-coder:32b`, asking
     for a single Python code block that ends by assigning to `result`.
  2. Runs it through the sandbox.
  3. **On failure, retries once** with the error fed back to the model.
     This covers the known limitation of asking a model for
     code/structured output: generation can stop mid-way and produce
     something that doesn't parse or run. A validate-and-retry loop is a
     far smaller failure class than free-form hallucinated answers.
  4. On success, asks the model to phrase the computed `result` in plain
     language — explicitly instructed not to invent or recompute any
     number, only phrase the ones it was given.

Run it:

```bash
uv run uvicorn --app-dir src pipeline_api:app --host 0.0.0.0 --port 8080
```

Smoke-test it without Open WebUI:

```bash
curl -X POST http://localhost:8080/ingest -F "file=@/path/to/inventory.xlsx"
# -> {"doc_id": "a1b2c3d4e5f6", "source_type": "spreadsheet", ...}

curl -X POST "http://localhost:8080/query?doc_id=a1b2c3d4e5f6&question=How%20many%20units%20total%3F"
# -> {"answer": "...", "result": [...], "code": "..."}
```

## 9. Phase VI — Open WebUI integration

1. Confirm Open WebUI points at native Ollama: Admin Panel → Settings →
   Connections → `http://localhost:11434`.
2. Admin Panel → Functions → New Function → paste the entire contents of
   `openwebui/pipe_function.py`. **Only this one file goes into Open
   WebUI** — `pipeline_api.py` runs as its own separate process (start it
   with the `uv run uvicorn ...` command above, ideally as a background
   service — see the launchd example in section 10).
3. In the function's Valves, confirm `API_BASE` matches where
   `pipeline_api.py` is actually listening (default `http://localhost:8080`).
4. Select "Document Intelligence" as the active model/pipe in a new chat,
   attach a document, and ask a question.

Two things worth understanding about how this function is written:

- It does **not** rely on Open WebUI's or Ollama's native tool-calling at
  all — it intercepts the message directly and calls `/query` itself.
  The chat model you have selected in Open WebUI's own picker is
  irrelevant to the answer; `qwen2.5-coder:32b` is called directly by
  `pipeline_api.py`.
- It uses a **plain sync generator with a heartbeat thread**, not an
  async generator. Open WebUI has not reliably signaled stream
  completion on async generators during long-running calls — a sync
  generator with periodic keep-alive avoids the connection appearing to
  hang/disconnect during a slow Ollama prefill on a big prompt.

If Open WebUI's own built-in file handling is enabled for the model
you're using, it can intercept an uploaded file and inject its own
citation/RAG context *before* this Pipe function ever runs — check each
model's capabilities in Admin Panel → Models and disable any built-in
"file" / retrieval capability so this pipeline's `/ingest` path is the
only thing that sees uploaded files.

## 10. Phase VII — Maintenance

**Logging:** everything goes to `logs/pipeline.log` (ingest events, query
questions, and any failures) — check this first when something goes
wrong.

**Garbage collection:** `scripts/gc_cleanup.py` deletes files in
`storage/ingested/` (and their matching `.duckdb` file) older than
`GC_MAX_AGE_HOURS` (default 24). Schedule it with `launchd`:

Create `~/Library/LaunchAgents/com.docintel.gc.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.gc</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/blueprint/.venv/bin/python</string>
    <string>/path/to/blueprint/scripts/gc_cleanup.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/blueprint</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>/path/to/blueprint/logs/gc.log</string>
  <key>StandardErrorPath</key><string>/path/to/blueprint/logs/gc.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.docintel.gc.plist
```

You can run `pipeline_api.py` itself the same way (swap the
`ProgramArguments` for the `uvicorn` command) so it survives a reboot
without a Terminal window staying open.

---

## 11. Running the tests

```bash
uv run pytest tests/ -v
```

These cover German number parsing, CSV → DuckDB ingestion, schema
summary generation, and the sandbox's AST validation and execution —
all without needing Ollama running. They were run and pass as of writing
this guide. The model-dependent half of the system (`/query`'s actual
code generation) is exercised manually via the `curl` commands in
section 8, since it needs a real model call.

---

## 12. What this deliberately does not include yet

- **No embeddings/vector search** — only added if a real need for
  long *unstructured* text search shows up (a 500-page contract, not a
  spreadsheet). Not needed for the aggregation/counting use case this
  was built for.
- **No multi-document joins** — each upload gets its own DuckDB file;
  cross-document questions aren't handled yet.
- **No authentication** on the FastAPI service — it's assumed to run on
  `localhost`, reachable only from Open WebUI on the same machine.
