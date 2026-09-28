# Local Document Intelligence Pipeline

Upload a large, messy business document (Excel, CSV, PDF, text or
scanned) and get calculated answers back, computed against the *actual*
data, not guessed from whatever part of it a language model happened to
read. Runs entirely on one machine, using only free, open-source and
open-weight software. **Documents, questions and answers never leave the
Mac.** There are no cloud AI services and no API keys. The Mac's internet
connection is used only to download and update software and models.

**To install it, follow [`SETUP.md`](SETUP.md).** It is written for
someone with no Terminal experience and goes from an unknown Mac to a
tested, working system. **To give colleagues access** from their own
computers, see [`ACCESS.md`](ACCESS.md).

This README explains how the system works and why each part is built the
way it is, so you can maintain and extend it. [`OPUS_FIXES.md`](OPUS_FIXES.md)
lists the problems found in a full code review and how each was fixed.

**Target hardware:** Apple Silicon Mac Studio, M2 Ultra, 64 GB unified
memory. Everything runs natively on macOS; Ollama is never run in Docker.
Several decisions below exist because of that 64 GB limit and are marked
as such.

---

## 1. The problem, and why the design looks like this

Feeding a whole document straight into a chat model breaks down once
documents get long: the model only reliably attends to part of what it's
given, and any question that aggregates across *every* row ("how many
units in total", "which part appears most often") fails silently with a
confident wrong answer, not an error.

Three decisions follow from that:

**No retrieval (RAG, embeddings, vector search) for tables.** Retrieval
returns the chunks most *similar* to the question, not all of them. For a
count or a sum, one missed row gives a wrong total, and nothing tells you
it happened. Published benchmarks on whole-document aggregation questions
show this is built into the method, not a tuning problem. So the full data
goes into a real database, and every question is answered against all of
it.

**The model never sees the raw data.** It sees a schema (table names,
column names and types, row counts, a few sample rows) and writes SQL or
Python against it. That code runs against the complete dataset in a
sandboxed subprocess, and only the computed result comes back. A 100-row
sheet and a 100,000-row sheet produce the same size prompt.

**No dependence on native tool-calling.** Ollama's tool-calling support
varies by model and version and has had real bugs where a tool call leaks
into visible text instead of being parsed. Here the model writes a Python
code block in its normal text answer, which is extracted, checked and run.
There is no tool-call parser anywhere in the path.

```
 Open WebUI (chat page, opened from office computers)
        |
        v
 openwebui/pipe_function.py   (pasted into Admin Panel -> Functions)
        |   POST /ingest (file, file id, chat id)   POST /query (question, chat id)
        v
 src/pipeline_api.py   (FastAPI service, 127.0.0.1:8080)
        |                                              |
        v                                              v
 src/smart_router.py  -->  src/data_layer.py  -->  src/execution_sandbox.py
 reads the file            DuckDB: full data,       runs the model's code:
 Excel/CSV: pandas         schema summary only      checked first, 8 GB memory
 PDF: pdfplumber           goes to the model        cap, timeout, read-only
 scans: vision model                                database, one at a time
        |                                              ^
        v                                              |
 Ollama (native, 127.0.0.1:11434)
   qwen2.5vl:32b      reads scanned pages and images, unloaded after each document
   qwen2.5-coder:32b  writes the SQL/Python and phrases answers, stays loaded
```

---

## 2. Repository layout

```
blueprint/
  README.md               this file: how it works and why
  SETUP.md                step-by-step installation, for non-technical readers
  ACCESS.md               giving colleagues access: link, accounts, fixed address
  OPUS_FIXES.md           review findings and what was fixed
  .python-version         the Python version uv installs (3.12)
  pyproject.toml          Python dependencies, managed with uv
  .env.example            settings template; setup copies it to .env
  src/
    config.py             reads .env once; every setting lives here
    german.py             German number formats (1.234,56)
    smart_router.py       reads files; routing, table joining, vision model handling
    data_layer.py         loads tables into DuckDB, builds the schema summary
    execution_sandbox.py  runs the model's code safely
    pipeline_api.py       FastAPI service: /ingest, /query, /health
  openwebui/
    pipe_function.py      pasted into Open WebUI, Admin Panel -> Functions
  scripts/
    setup_mac.sh          installs and starts everything (SETUP.md section 4)
    gc_cleanup.py         hourly cleanup of old uploads
  samples/
    lager_beispiel.csv    small test file with known answers (SETUP.md 6.4)
  storage/                uploads, one DuckDB file per document, registry.json
  logs/                   pipeline.log, service.log, ollama.log, gc.log
  tests/
    test_pipeline_offline.py   automatic tests, need neither Ollama nor Open WebUI
```

---

## 3. Setup

See [`SETUP.md`](SETUP.md). In short: `scripts/setup_mac.sh` installs
native Ollama as a background service with the right memory settings,
downloads both models, installs the Python packages with `uv`, runs the
tests, and starts the pipeline service and the cleanup job as background
services. Open WebUI and the Pipe function are then set up by hand,
following SETUP.md sections 5 and 6.

---

## 4. Reading files (`src/smart_router.py`)

Every uploaded file becomes the same shape (`IngestResult`): named tables
(pandas DataFrames) plus free-text blocks. Everything after this step
works the same whatever the original file type was.

### The routing rule: the vision model is a fallback, never a shortcut

| Input | Read by | Vision model? |
|---|---|---|
| `.xlsx`, `.xls` | pandas | **Never** |
| `.csv` | pandas | **Never** |
| PDF page with a table | pdfplumber | **Never**, however little text the page has |
| PDF page with normal text | pdfplumber | **Never** |
| PDF page with under 40 characters of text **and** an image on it (a scan) | `qwen2.5vl:32b` | Yes, that page only |
| `.png`, `.jpg`, `.tiff`, ... | `qwen2.5vl:32b` | Yes |

pandas and pdfplumber read the exact characters stored in the file. A
vision model reads numbers by *looking at pixels*: good, but never exact.
So it is only used where there is no text to read. A mixed PDF is handled
page by page. Tests confirm that Excel, CSV and text PDFs make zero calls
to the vision model.

### Excel keeps its own types
Excel files are read with `dtype=object`, then `infer_objects()`. Real
numbers stay exact numbers and dates stay dates. Without `dtype=object`,
pandas quietly turns text cells like "1.234" into the number 1.234 (US
reading) before any German handling can see them.

### CSV encoding and separator
The file is decoded as UTF-8 first, then Windows-1252 (what Excel on
Windows uses when saving "CSV"), then Latin-1. The separator is taken
from the header line: semicolon, tab, comma or pipe, whichever occurs
most.

### Tables that run over several PDF pages
pdfplumber finds one table per page. They are joined back together:

- The first table on a page continues the previous table if that table
  ended on the previous page and both have the same number of columns.
- If the continuation starts with the same header row, that row is
  dropped (repeated headers).
- If it starts with a data row, that row is kept as data (it is not
  mistaken for a header, which used to lose one row per page).
- A continuation that starts with a *different* header-like row is
  treated as a new table.
- Every row gets a `source_page` column with the page it came from.

Text on a table page that sits outside the table (a document header, a
totals line) is kept as a text block.

### Scanned pages and images
Pages are rendered at 200 dpi and sent to `qwen2.5vl:32b`, whose dynamic
resolution handles small print that fixed-grid vision models (like LLaVA)
lose. The prompt asks for every table row, exactly as printed, as a
markdown table. Those markdown tables are then turned into real tables
and go through the same joining as above, so scanned inventories can be
summed like any other. The full transcription is also kept as text.

### Table cleaning
Every table goes through `_clean_table`:
- blank header cells become `column_1`, `column_2`, ...; repeated header
  names get `_2`, `_3` (PDF tables with merged header cells produce both,
  and duplicate names used to crash the whole ingestion)
- empty rows and columns are dropped
- text columns that are really numbers are converted (section 6), except
  columns that look like identifiers (leading zeros, long unique digit
  strings: part numbers, EANs, IBANs), which stay text so they are never
  summed

---

## 5. Memory on 64 GB: one large model at a time *(hardware-specific)*

Approximate footprints (check the real numbers with `ollama ps`):

| | Weights | Context memory | Total |
|---|---|---|---|
| `qwen2.5-coder:32b` at 32K context | ~20 GB | ~8 GB | **~28 GB** |
| `qwen2.5vl:32b` at 16K context | ~21 GB | ~4 GB + vision encoder | **~26 GB** |

Together that's over 50 GB, before macOS, Open WebUI, the pipeline and up
to 8 GB for the sandbox. macOS also lets the graphics chip use only about
three quarters of unified memory by default. Both models at once pushes
the Mac into swap, which on models this size looks like a frozen machine.

So: **the code model stays loaded; the vision model is loaded only for
scanned pages and unloaded right after.** Four mechanisms, each backing up
the previous one:

1. **Unload after every document** (`VisionSession` in
   `smart_router.py`). All vision calls for one document happen inside a
   `with VisionSession()` block. Between pages the vision model stays
   loaded (`keep_alive: "10m"`), so a 40-page scan doesn't reload it 40
   times. When the block ends, whether reading succeeded **or failed
   halfway**, it sends
   `POST /api/generate {"model": "qwen2.5vl:32b", "keep_alive": 0}`.
2. **The code model is reloaded and pinned** right after, with
   `keep_alive: -1`. Every call in `pipeline_api.py` also uses
   `keep_alive: -1`, and the service loads the code model before it
   accepts its first request.
3. **One document at a time.** `/ingest` holds a lock, so two scans
   uploaded at once are read one after the other instead of forcing
   Ollama to swap models back and forth for every page.
4. **`OLLAMA_MAX_LOADED_MODELS=1`** in the Ollama service: even if an
   unload call failed, Ollama never holds two models at once.

The code model is pinned per request rather than with a global
`OLLAMA_KEEP_ALIVE=-1`, because a global setting would pin the vision
model too if its unload ever failed. Each unload is logged, and a failed
unload is logged as an error.

---

## 6. German numbers (`src/german.py`)

German documents write `1.234,56`: dot for thousands, comma for decimals,
the reverse of pandas' default. Read naively, "1.234" becomes 1.234
instead of 1234, and a column of 1.234 / 2.500 / 750 sums to 753.734
instead of 4484.

The format is decided **per column**, from the values that can only be
read one way:

| Evidence for German | Evidence for US |
|---|---|
| decimal comma: `12,5`, `1.234,56` | decimal point not followed by exactly 3 digits: `12.5`, `1,234.56` |
| more than one thousands dot: `1.234.567` | more than one thousands comma: `1,234,567` |

Values like `1.234` or `750` fit both. If a whole column is like that,
`NUMBER_FORMAT_DEFAULT` in `.env` decides; it is `german`, since these are
German documents. A column with evidence for both formats is left as text
rather than guessed. Currency signs, `EUR`, `%`, spaces and "Stk." are
removed before reading. Only text is parsed: values that are already
numbers (Excel cells stored as numbers) keep their exact value. A column
is only converted when at least 90% of its filled cells are numbers, so a
description column is never wiped out.

---

## 7. The data layer (`src/data_layer.py`)

DuckDB is a **local, open-source Python library**: a database engine that
runs inside the pipeline's own process, like SQLite. There is no server,
no account and no network connection. It is used because it is fast at
exactly this work: aggregations, joins and window functions use all of
the M2 Ultra's CPU cores, where pandas mostly uses one.

Each document gets its own file, `storage/duckdb/<doc_id>.duckdb`, with
one table per extracted table plus `document_text` for free text
(searchable with `LIKE` and string functions, no embeddings). Extension
auto-install and auto-load are off, so DuckDB never tries to download
anything.

`schema_summary(doc_id)` is the only view of the data the model gets:
table names, columns with types, row counts and up to 5 sample rows per
table.

---

## 8. The sandbox (`src/execution_sandbox.py`)

Runs the model's code. Stated plainly: this stops *accidents* (a made-up
destructive call, an endless loop, a merge that explodes memory), not a
determined attacker. The model runs locally and isn't hostile input in
the usual sense. Six layers:

1. **Code check before anything runs.** Only `pandas, numpy, statistics,
   math, json, datetime, re, collections, itertools` may be imported. Any
   call starting with `read_` is refused, and so is any call starting with
   `to_` except in-memory conversions (`to_dict`, `to_list`, `to_frame`,
   `to_numpy`, `to_df`, ...). numpy's file functions (`save`, `load`,
   `loadtxt`, `fromfile`, ...), `open`, `eval`, `exec`, `system` and
   `connect` are refused. The code cannot touch the `duckdb` module, only
   the prepared connection `con`.
2. **A locked-down database connection.** Read-only;
   `enable_external_access = false`, so SQL like
   `SELECT * FROM read_csv('/any/file')` is refused; no extension
   downloads; and DuckDB's own `memory_limit` (6 GB), beyond which it
   uses a temporary folder on disk instead of more memory.
3. **A separate process.** A crash or hang can't take down the service.
4. **A time limit** (`SANDBOX_TIMEOUT_SECONDS`, 30 s).
5. **An 8 GB memory limit** (`SANDBOX_MEMORY_LIMIT_GB`). A watchdog in
   the service measures the process's real memory every 0.1 s and kills
   it the moment it goes over. The model then gets that error back and
   usually fixes it on its retry.
6. **One calculation at a time** (`SANDBOX_MAX_CONCURRENT`, 1). The
   8 GB limit applies to each calculation; without this, four people
   asking at once could use 32 GB on top of the models.

**Why a watchdog and not `resource.setrlimit(RLIMIT_AS, ...)`:** macOS
does not enforce `RLIMIT_AS`, and Python's `setrlimit` has a known macOS
bug where it raises `ValueError` instead (CPython issue #78783). It would
look like protection on the Mac and provide none. For the same reason,
`resource` is not importable by generated code.

**Why "physical footprint" on macOS:** when memory is tight, macOS
compresses a process's memory, and compressed memory no longer counts as
resident (RSS). A runaway process could grow far past 8 GB while its RSS
stays low. On macOS the watchdog therefore reads the physical footprint
(what Activity Monitor shows as "Memory"), which includes compressed
memory. It checks its first reading against RSS and falls back to RSS if
the two disagree, rather than trust a wrong number.

**Results:** the code must put its answer in a variable named `result`.
Tables and pandas Series become lists of rows, with their labels (a
`groupby` result keeps its group names). numpy numbers become plain
numbers. Results longer than `MAX_RESULT_ROWS` (200) are cut, and say so
with the full row count, so the answer can mention that only part is
shown.

---

## 9. The pipeline service (`src/pipeline_api.py`)

- **`POST /ingest`** (a file, plus optional `source_id` and `chat_id`):
  reads the file, loads it into DuckDB, returns a `doc_id` and the schema
  summary. If this exact Open WebUI file (`source_id`) was read before,
  the stored result is returned at once (`"cached": true`).
- **`POST /query?question=...&chat_id=...`** (or `&doc_id=...`):
  1. Sends the schema summary and the question to `qwen2.5-coder:32b`.
  2. Runs the returned code block in the sandbox.
  3. **Retries once** if the reply had no code block, or the code failed,
     showing the model its reply and the exact error.
  4. Asks the model to phrase the computed result in plain language,
     forbidden from inventing or recalculating any number.
- **`GET /health`**: `{"status": "ok"}` when the service is up.

**Registry** (`storage/duckdb/registry.json`): remembers which Open
WebUI file became which document, and which document each chat is
about. So a file is never read twice, follow-up questions without a new
attachment still find their document, and both survive restarts. The
cleanup job removes entries whose data it deleted.

**The prompt prefers SQL.** The code model is told to do counting,
summing, grouping, joins, filtering, sorting and window functions in SQL
through `con` (for example
`result = con.sql("SELECT ... GROUP BY ...").df()`) and to bring only the
small final result into pandas. That keeps the work in DuckDB's fast
engine and memory use low. `con` is used rather than `duckdb.sql(...)`
because `duckdb.sql` runs against an empty in-memory database, not the
document's tables.

All model calls use `temperature: 0`. The endpoints are plain `def`
functions, which FastAPI runs in a thread pool, so a long ingestion
doesn't block `/health` or other requests.

`logs/pipeline.log` records every document read, every question, the
code that was run and its result. When an answer looks wrong, that is
where to see exactly what was calculated.

### Testing the service without Open WebUI
```bash
cd ~/blueprint
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/ingest -F "file=@samples/lager_beispiel.csv"
curl -X POST "http://127.0.0.1:8080/query?doc_id=<doc_id>&question=Wie%20viele%20St%C3%BCck%20insgesamt%3F"
```
`/query` returns `answer`, the raw `result` and the `code` the model wrote.

---

## 10. Open WebUI integration (`openwebui/pipe_function.py`)

Installation is in SETUP.md section 6. How the Pipe works, and why:

- **It reads `__files__` and `__chat_id__`.** Open WebUI removes `files`
  and `chat_id` from `body` before calling a Pipe (`main.py`,
  `middleware.py`) and passes them as these separate arguments
  (`functions.py`). The file entries have the shape the Open WebUI
  frontend builds: `{"type": "file", "file": {"id", "filename", "path",
  ...}, "id", "name", ...}`.
- **It is an `async def` that returns the finished answer as one
  string.** Open WebUI iterates a *sync* generator directly inside its
  event loop, so blocking network calls there froze Open WebUI for every
  user. Here, the blocking calls run in a worker thread
  (`asyncio.to_thread`). Returning a plain string (not an async
  generator) also avoids Open WebUI's async-generator completion problem
  (open-webui#20196).
- **A status line** above the answer updates every 15 seconds
  ("Dokument wird eingelesen", "Antwort wird berechnet (läuft seit 2
  Min.)"), so long scans never look frozen.
- **The real question is recovered** even when Open WebUI wraps it: with
  "File Context" on, Open WebUI puts its retrieval template and file
  excerpts in front of the question, ending with `</context>`; older
  versions use `<user_query>` tags; the native tool mode adds
  `<attached_files>`. All three are removed.
- **Open WebUI's background jobs** (chat title, tags, search queries,
  follow-up suggestions) are sent to whichever model the chat uses, this
  Pipe included. They get an instant empty reply instead of a
  calculation.
- **Timeouts** are long on purpose (1 hour to read a document, 30
  minutes per answer): a scan is read page by page, and the Pipe must
  not give up while the service is still working.
- It never uses Open WebUI's or Ollama's native tool-calling, and the
  model picked in the chat doesn't matter: the service calls
  `qwen2.5-coder:32b` itself.
- Status messages are German by default (`LANGUAGE` valve, `de` or `en`).
  Answers come in the language of the question.

This was checked end to end against a real Open WebUI (0.11): a German
Excel-style CSV uploaded through Open WebUI's own file API, the file entry
built exactly like the frontend builds it, and three questions (a total,
a maximum, and a follow-up without attachment) all answered correctly,
while Open WebUI kept answering other requests without delay.

---

## 11. Maintenance

- **Logs:** `logs/pipeline.log` first, then `logs/service.log` and
  `logs/ollama.log`. SETUP.md section 9 explains how to open them.
- **Cleanup:** `scripts/gc_cleanup.py` runs every hour (installed by the
  setup script) and deletes uploads and their databases older than
  `GC_MAX_AGE_HOURS` (24), then prunes the registry.
- **Memory check:** `ollama ps` should normally list only
  `qwen2.5-coder:32b`.
- **Restarting and updating:** SETUP.md section 9.

---

## 12. Tests

```bash
cd ~/blueprint
uv run pytest -v
```
They need neither Ollama nor Open WebUI: model calls are replaced by a
fake that records what would have been sent. The setup script runs them
too. Covered:

- German numbers: thousands dots, decimal commas, US format, currency and
  units, real Excel numbers and dates left untouched
- CSV from German Excel (Windows-1252, semicolons), CSV with a BOM
- blank and duplicate PDF header cells
- routing: Excel, CSV, text PDFs and short table pages make **no** vision
  calls
- tables across pages joined, repeated headers dropped, `source_page` set
- scans: vision called, markdown tables turned into real tables, vision
  model unloaded (also when reading fails halfway), code model re-pinned
- sandbox: every blocked import and file call, allowed conversions, SQL
  file access refused, Series results keep their labels, long results
  capped with a total, numpy results, memory limit, time limit, and the
  macOS memory reading's self-check
- service: a file is read only once, follow-up questions find the chat's
  document, unknown chat gives 404, a reply without code is retried, a
  failed upload leaves no files behind
- Pipe: the question is recovered from all Open WebUI wrappers, files and
  chat id are read from Open WebUI's real arguments, background jobs
  never reach the service, and a chat without a document gets a clear
  message

What the tests can't show, SETUP.md sections 6.4 and 7 check by hand: the
real models writing correct code, and the vision model being unloaded on
the real Mac.

---

## 13. What stays on the Mac

| Component | Why your data stays local |
|---|---|
| Ollama | Models run on the Mac's own chip; listens on `127.0.0.1` only |
| Pipeline service | Listens on `127.0.0.1` only; talks only to Ollama on the same Mac |
| DuckDB | Local library inside the pipeline process; no extension downloads |
| Sandbox | No network modules importable; no file access outside the document's database |
| Open WebUI | Cloud (OpenAI) connection and usage statistics off (SETUP.md section 5) |
| Homebrew | Statistics off; only used when installing or updating |

---

## 14. Not included yet

- **Embeddings / vector search:** only worth adding for long *unstructured*
  text (a 500-page contract). Not needed for counting and adding up.
- **Questions across several documents:** each upload gets its own
  database. If several files are attached in one chat, the last one is
  the one questions go to.
- **Login on the pipeline service:** it only listens on `127.0.0.1`, so
  only programs on the Mac itself can reach it.
