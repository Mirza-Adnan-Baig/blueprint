# Local Document Intelligence Pipeline

Upload a large, messy business document (Excel, CSV, PDF — text or
scanned) and get calculated answers back, computed against the *actual*
data, not guessed from whatever part of it a language model happened to
read. Runs entirely on one machine, using only free, open-source and
open-weight software. **Documents, questions and answers never leave the
Mac** — no cloud AI services, no API keys. The Mac's internet connection
is used only to download and update software and models.

This README is the whole build guide. Every code file it describes
already exists in this repo — this document explains why each one is
shaped the way it is, so you can maintain and extend it rather than treat
it as a black box.

**Target hardware:** Apple Silicon Mac Studio, M2 Ultra, 64GB unified
memory. Everything runs natively on macOS — Ollama is never run in
Docker. Several design decisions below exist specifically because of
that 64GB limit; they're marked as such.

---

## 1. The problem this solves, and why the design looks like this

Feeding a whole document straight into a chat model's context breaks
down once documents get long: the model only reliably attends to part of
the context, and any question that aggregates across *every* row ("how
many total units", "which part appears most often") fails silently — a
confident wrong answer, not an error.

Three design decisions follow from that:

**No RAG (embeddings + vector search) for structured data.** Retrieval
returns the top-k most *similar* chunks, not all of them. For a count or
a sum, missing even one row gives a wrong total — and nothing tells you it
happened. Published benchmarks on corpus-wide aggregation questions show
this is structural, not a tuning problem. So the full data goes into a
real database, and every question is answered against *all* of it.

**The model never sees the raw data.** It sees a schema — table names,
column names and types, row counts, a few sample rows — and writes SQL or
Python against it. That code runs against the complete dataset in a
sandboxed subprocess; only the computed result comes back. A 100-row sheet
and a 100,000-row sheet produce the same size prompt.

**No dependence on the model's native tool-calling.** Ollama's
tool-calling support varies by model and version, and has had real,
documented bugs where a tool call leaks into visible text instead of
being parsed. Here the model simply writes a Python code block in its
normal text output, which is extracted with a regex, validated, and
executed. There is no tool-call parser in the critical path.

```
 Open WebUI (chat UI, reachable from office PCs)
        |
        v
 openwebui/pipe_function.py   (pasted into Admin Panel -> Functions)
        |   POST /ingest (file)             POST /query (doc_id, question)
        v
 src/pipeline_api.py   (FastAPI pipeline service, 127.0.0.1:8080)
        |                                              |
        v                                              v
 src/smart_router.py  -->  src/data_layer.py  -->  src/execution_sandbox.py
 File Doctor               DuckDB: full data,       runs the model's code:
 Excel/CSV: pandas         schema summary only      AST-checked, 8 GB memory
 text PDF: pdfplumber      goes to the model        cap, timeout, read-only
 scanned: vision model                              database connection
        |                                              ^
        v                                              |
 Ollama (native, 127.0.0.1:11434)
   qwen2.5vl:32b      -- scanned pages/images only, evicted after ingestion
   qwen2.5-coder:32b  -- writes the SQL/Python, phrases answers, stays loaded
```

---

## 2. Repository layout

```
blueprint/
  README.md                  <- this file: architecture + code walkthrough
  SETUP.md                    <- Mac environment setup, from unknown state
  pyproject.toml               <- uv-managed Python dependencies
  .env.example                  <- copy to .env
  src/
    config.py                    <- reads .env once; all settings live here
    german.py                     <- German number-format detection/conversion
    smart_router.py                <- File Doctor: routing + vision eviction
    data_layer.py                   <- tables into DuckDB, schema summaries
    execution_sandbox.py             <- runs model-generated code safely
    pipeline_api.py                   <- FastAPI service: /ingest, /query
  openwebui/
    pipe_function.py                   <- paste into Open WebUI Admin Panel -> Functions
  scripts/
    setup_mac.sh                        <- installs Ollama service, models, Python deps
    gc_cleanup.py                        <- deletes old ingested files
  storage/
    ingested/                             <- uploaded files
    duckdb/                                <- one .duckdb file per document
  logs/                                     <- pipeline.log, ollama.log
  tests/
    test_pipeline_offline.py                 <- runs without Ollama or network
```

---

## 3. Phase I — Environment setup

Follow **[`SETUP.md`](SETUP.md)** from top to bottom. It starts by
inspecting the machine without changing anything (is Ollama native or in
Docker? which version? is Open WebUI there?), then walks through every
installation step with the exact commands and expected output, runs
`scripts/setup_mac.sh`, configures Open WebUI so nothing leaves the Mac,
and ends with an end-to-end check of the whole system.

Come back here when its final checklist is complete.

---

## 4. Phase II — The File Doctor (`src/smart_router.py`)

Takes any uploaded file and returns a standard shape (`IngestResult`):
named tables (pandas DataFrames) plus free-text blocks. Everything
downstream is agnostic to what the original file type was.

### The routing rule: the vision model is a fallback, never a shortcut

| Input | Handled by | Vision model? |
|---|---|---|
| `.xlsx`, `.xls` | pandas | **Never** |
| `.csv` | pandas | **Never** |
| PDF page with a text layer | pdfplumber (tables + text) | **Never** |
| PDF page with fewer than 40 extractable characters (a scan) | `qwen2.5vl:32b` | Yes — that page only |
| `.png`, `.jpg`, `.tiff`, ... | `qwen2.5vl:32b` | Yes |

pandas and pdfplumber read the exact characters stored in the file. A
vision model reads numbers by *looking at pixels* — good, but never
exact. Sending a spreadsheet or text PDF through it would trade exact
values for approximate ones, so it's used only where there is no text to
read. A mixed PDF (some text pages, some scanned) is handled page by page.

The routing is visible at the top of `ingest_file()`: spreadsheets and
CSVs return before any vision code is reachable. Tests in
`tests/test_pipeline_offline.py` confirm that Excel, CSV and text PDFs
make zero calls to the vision model.

### Why `qwen2.5vl:32b` for scanned pages

Qwen2.5-VL uses *dynamic resolution*: it processes an image at close to
its native resolution instead of shrinking it to a fixed grid first. For
dense, multi-page inventory scans with small print, fixed-grid vision
models (like LLaVA) lose exactly the small digits that matter. Pages are
rendered at 200 dpi, and the prompt asks for every table row to be
transcribed exactly, not summarized. `temperature: 0` keeps the
transcription deterministic.

### Table cleaning

Every table passes through `_clean_table`: empty rows/columns are
dropped, and numeric columns get German-aware conversion (section 6) —
except columns that look like identifiers (leading zeros, long unique
digit strings: part numbers, EANs, IBANs), which stay text so they're
never summed.

---

## 5. Memory on 64GB: one large model at a time *(hardware-specific)*

Both models are ~32B parameters. Approximate footprints (check the real
numbers on your machine with `ollama ps`):

| | Weights | Context (KV cache) | Total |
|---|---|---|---|
| `qwen2.5-coder:32b` at 32K context | ~20 GB | ~8 GB | **~28 GB** |
| `qwen2.5vl:32b` at 16K context | ~21 GB | ~4 GB + vision encoder | **~26 GB** |

Together that's over 50 GB — before macOS itself, Open WebUI, the
pipeline, and up to 8 GB for the sandbox. On top of that, macOS by default
lets the GPU use only about three quarters of unified memory. Loading both
at once pushes the Mac into swap, which on a model this size looks like a
frozen machine.

So the rule is: **the code model stays resident; the vision model is
loaded only for scanned pages and evicted immediately afterwards.** Three
mechanisms, each backing up the previous one:

1. **Explicit eviction after ingestion** (`smart_router.py`,
   `VisionSession`). All vision calls for one document happen inside a
   `with VisionSession()` block. Between pages of the same document the
   vision model stays loaded (`keep_alive: "10m"`), so a 40-page scan
   doesn't reload a 21 GB model 40 times. When the block exits — whether
   ingestion succeeded **or failed halfway** — it sends:
   ```json
   POST /api/generate  {"model": "qwen2.5vl:32b", "keep_alive": 0}
   ```
   which unloads the vision model immediately.
2. **The code model is pinned and restored.** Right after the eviction,
   `warm_code_model()` sends `{"model": "qwen2.5-coder:32b",
   "keep_alive": -1}`, loading it back and pinning it, so the next
   question doesn't pay a load delay. Every normal call in
   `pipeline_api.py` also passes `keep_alive: -1`, and the service
   pre-loads the code model at startup.
3. **`OLLAMA_MAX_LOADED_MODELS=1`** (set in the Ollama service by
   `setup_mac.sh`). Even if an eviction call failed, Ollama itself will
   never hold two models at once — it unloads one before loading another.

*Why pin the code model per request instead of setting
`OLLAMA_KEEP_ALIVE=-1` globally:* a global `-1` would pin **every** model,
including the vision model if its eviction call ever failed. Pinning only
the code model, per request, keeps the rule precise. The global default
stays at `5m`.

What you should observe: after uploading a scanned PDF, `ollama ps` shows
only `qwen2.5-coder:32b`. Each eviction is logged in `logs/pipeline.log`,
and a failed eviction is logged as an error.

---

## 6. German number handling (`src/german.py`)

German-formatted numbers use `.` as the thousands separator and `,` as
the decimal separator — the reverse of pandas' default. Naively parsing
`"1.234"` gives `1.234` instead of `1234`; a column of `1.234 / 2.500 /
750` sums to `753.734` instead of `4.484`. This module:

- Detects the format **per column**, not per file.
- Converts only when confident (an unambiguous decimal comma or point in
  the sample); otherwise leaves the column alone rather than guessing.
- Skips identifier-looking columns (`looks_like_identifier`).

---

## 7. Phase III — The data layer (`src/data_layer.py`)

DuckDB is a **local, open-source Python library** — an embedded database
engine that runs inside the pipeline's own process, like SQLite. There is
no server, no account, no network connection. It's used here because it
is a multithreaded, columnar engine: aggregations, joins and window
functions use all of the M2 Ultra's CPU cores, where pandas mostly uses
one.

Each ingested document gets its own file, `storage/duckdb/{doc_id}.duckdb`,
with one table per extracted table plus a `document_text` table for
free-text blocks (queryable with `LIKE` and string functions — no
embeddings). Every connection is opened with extension auto-install and
auto-load switched off, so DuckDB can never try to download an extension.

`schema_summary(doc_id)` produces the only representation of the data
that reaches the model: table names, columns with types, row counts, and
up to 5 sample rows per table.

---

## 8. Phase IV — The sandbox (`src/execution_sandbox.py`)

Runs the model's generated Python. Stated plainly: this stops
*accidents* — a hallucinated destructive call, an infinite loop, a merge
that explodes memory — not a determined attacker. The model runs locally
and isn't hostile input in the usual sense. Five layers:

1. **AST check before anything runs.** Only `pandas, numpy, statistics,
   math, json, datetime, re, collections, itertools` may be imported.
   Calls to file/process functions (`open`, `read_csv`, `to_excel`,
   `system`, `connect`, ...) are rejected even when reached through an
   allowed module. The generated code can't use the `duckdb` module
   directly — only the prepared connection `con`.
2. **A locked-down DuckDB connection.** Read-only;
   `enable_external_access = false`, which blocks SQL such as
   `SELECT * FROM read_csv('/any/file')` from reaching the file system;
   extension downloads off; and DuckDB's own `memory_limit` (default
   6 GB) — past it, DuckDB spills to a temporary folder on disk instead
   of growing.
3. **A separate subprocess** — a crash or hang can't take down the
   service.
4. **A hard timeout** (`SANDBOX_TIMEOUT_SECONDS`, default 30).
5. **A hard 8 GB memory cap** (`SANDBOX_MEMORY_LIMIT_GB`). A watchdog in
   the service measures the subprocess's real resident memory every
   0.1 s (via `psutil`) and kills it the moment it crosses the limit. The
   model then gets the error back ("used more than 8.0 GB ... aggregate
   in SQL via `con` instead"), and usually fixes it on its retry.

**Why a watchdog and not `resource.setrlimit(RLIMIT_AS, ...)`:** the
kernel on macOS does not enforce `RLIMIT_AS`, and Python's `resource`
module has a known macOS bug where `setrlimit` raises `ValueError`
instead (CPython issue #78783). On the Mac Studio it would look like
protection while providing none. The watchdog measures actual memory use,
so it behaves the same on every OS — and the test suite proves it kills a
runaway allocation. For the same reason `resource` is deliberately
**not** importable by generated code: it's process control, not analysis.

The generated code must assign its answer to a variable named `result`;
the sandbox serializes it to JSON and hands it back.

---

## 9. Phase V — The pipeline service (`src/pipeline_api.py`)

A FastAPI service with two endpoints:

- **`POST /ingest`** (multipart file) → File Doctor → DuckDB → returns a
  `doc_id` and the schema summary.
- **`POST /query?doc_id=...&question=...`** →
  1. Sends the schema summary + question to `qwen2.5-coder:32b`.
  2. Runs the returned code block in the sandbox.
  3. **On failure, retries once**, showing the model its own code and the
     exact error. This covers the known weakness of generated code — it
     can stop midway or reference a wrong column.
  4. Asks the model to phrase the computed `result` in plain language,
     explicitly forbidden from inventing or recomputing any number.

### The code-writing prompt prefers DuckDB SQL over pandas

The system prompt tells the model to do aggregations, counts, sums,
grouping, joins, filtering, sorting and window functions **in SQL through
`con`** (e.g. `result = con.sql("SELECT ... GROUP BY ...").df()`), and to
bring only the small final result into pandas — never load a whole large
table into a DataFrame just to aggregate it. That keeps the heavy work in
DuckDB's multithreaded engine and keeps memory use low, which is also
what keeps generated code well inside the sandbox's 8 GB cap.

(`con` is used rather than `duckdb.sql(...)`: the module-level
`duckdb.sql` runs against an empty in-memory database, not the
document's tables.)

All model calls use `temperature: 0` and `keep_alive: -1` (section 5).
The endpoints are plain `def` functions, so FastAPI runs them in a thread
pool — a long ingestion doesn't block other requests.

### Run it by hand (for testing)
```bash
cd ~/blueprint
uv run uvicorn --app-dir src pipeline_api:app --host 127.0.0.1 --port 8080
```
Smoke-test without Open WebUI (in a second Terminal window):
```bash
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/ingest -F "file=@/path/to/inventory.xlsx"
curl -X POST "http://127.0.0.1:8080/query?doc_id=<doc_id>&question=How%20many%20units%20in%20total%3F"
```
`/ingest` returns a `doc_id`; `/query` returns `answer`, the raw
`result`, and the `code` the model wrote — read the code when an answer
looks wrong, it shows exactly what was computed.

### Run it as a background service (for daily use)
Copy this block into Terminal; it writes and starts a LaunchAgent that
starts the service at login and restarts it if it crashes:
```bash
cat > ~/Library/LaunchAgents/com.docintel.pipeline.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.pipeline</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/blueprint/.venv/bin/uvicorn</string>
    <string>--app-dir</string><string>src</string>
    <string>pipeline_api:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8080</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME/blueprint</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/blueprint/logs/service.log</string>
  <key>StandardErrorPath</key><string>$HOME/blueprint/logs/service.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.docintel.pipeline.plist
```
Restart after changing code or `.env`:
```bash
launchctl kickstart -k gui/$(id -u)/com.docintel.pipeline
```

---

## 10. Phase VI — Open WebUI integration

1. Admin Panel → Functions → **New Function** → paste the entire
   contents of `openwebui/pipe_function.py` → Save → switch it on.
   **Only this one file goes into Open WebUI.** The pipeline service
   runs separately (section 9).
2. Open the function's **Valves** and set `API_BASE`:
   - Open WebUI native: `http://localhost:8080` (the default)
   - Open WebUI in Docker: `http://host.docker.internal:8080` — inside a
     container, `localhost` is the container itself
3. Start a new chat, select **Document Intelligence** as the model,
   attach a document, ask a question.

How the function is written, and why:

- It does **not** use Open WebUI's or Ollama's native tool-calling. It
  intercepts the message and calls `/query` itself. The model picked in
  Open WebUI is irrelevant to the answer; the pipeline calls
  `qwen2.5-coder:32b` directly.
- It uses a **plain sync generator with a heartbeat thread**, not an
  async generator — Open WebUI has not reliably signaled completion on
  async generators during long calls, which looks like a hang or a
  dropped connection.

**Turn off Open WebUI's own file handling for this model.** Otherwise
Open WebUI may chunk the uploaded file and inject its own retrieval
context *before* the Pipe runs — the exact RAG behaviour this design
avoids. In Admin Panel → Models, open the Document Intelligence entry and
disable its built-in file/retrieval capability. If the setting doesn't
seem to take effect, reload the model list — Open WebUI caches it.

---

## 11. Phase VII — Maintenance

**Logs** — check these first when something goes wrong:
- `logs/pipeline.log` — ingestions (incl. how many pages went to the
  vision model), questions, failures, model evictions
- `logs/service.log` — the pipeline service's own output
- `logs/ollama.log` — Ollama itself

**Garbage collection:** `scripts/gc_cleanup.py` deletes uploaded files
and their `.duckdb` databases older than `GC_MAX_AGE_HOURS` (default 24).
Schedule it hourly:
```bash
cat > ~/Library/LaunchAgents/com.docintel.gc.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.gc</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/blueprint/.venv/bin/python</string>
    <string>$HOME/blueprint/scripts/gc_cleanup.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME/blueprint</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$HOME/blueprint/logs/gc.log</string>
  <key>StandardErrorPath</key><string>$HOME/blueprint/logs/gc.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.docintel.gc.plist
```

**Memory check:** `ollama ps` should normally show only
`qwen2.5-coder:32b`. If `qwen2.5vl:32b` is still listed minutes after an
ingestion finished, look for "FAILED to evict" in `logs/pipeline.log`.

---

## 12. Testing

```bash
uv run pytest tests/ -v
```
Runs without Ollama. Model calls are replaced by a
fake that records what would have been sent. Covered:

- German number parsing
- Routing: Excel, CSV and text PDFs make **zero** vision-model calls
- Scanned PDFs: the vision model is called, then evicted with
  `keep_alive: 0`, then the code model is re-pinned with `keep_alive: -1`
  — and eviction still happens when ingestion fails midway
- CSV → DuckDB → schema summary
- Sandbox: rejects `os`, `resource`, `duckdb`, `open()`, `pd.read_csv`;
  blocks SQL file access; kills a runaway memory allocation; kills an
  infinite loop; runs valid SQL correctly

The model-dependent half (`/query` actually generating code) is tested by
hand with the `curl` commands in section 9, with real documents.

---

## 13. What stays on the Mac

The internet connection is for downloading software and models (`brew`,
`uv`, `ollama pull`). Document processing never uses it:

| Component | Why your data stays local |
|---|---|
| Ollama | Models run on the Mac's own GPU; listens on `127.0.0.1` only |
| Pipeline service | Listens on `127.0.0.1` only; talks to nothing but Ollama on the same Mac |
| DuckDB | Local Python library inside the pipeline's own process; extension auto-install/auto-load off |
| Sandbox | No network modules importable; DuckDB file access outside the document's database off |
| Open WebUI | Cloud (OpenAI) connection and usage analytics off (SETUP.md section 5) |
| Homebrew | Analytics off; only used when installing/updating |

SETUP.md section 6 checks the listening addresses.

---

## 14. Deliberately not included yet

- **Embeddings / vector search** — only worth adding for long
  *unstructured* text (e.g. a 500-page contract). Not needed for
  counting and aggregation.
- **Cross-document questions** — each upload gets its own database.
- **Authentication on the pipeline service** — it only listens on
  `127.0.0.1`, so only programs on the Mac itself can reach it.
