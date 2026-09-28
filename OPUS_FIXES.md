# Opus fixes

A full review of the repository, done before anything is installed on the
Mac Studio. Every file was read, the suspected problems were reproduced by
running the real code, and the Open WebUI behaviour was checked against
Open WebUI's own source code (version 0.11). This file lists what was
wrong, what it would have caused, and what was changed.

**Short version:** 15 problems were found. Four of them would have
stopped the system from working or produced wrong numbers the first day.
All 15 are fixed, 3 more problems found while testing the fixes are fixed
too, and the whole chain was tested end to end through a real Open WebUI.

---

## Critical: would have stopped it working, or given wrong numbers

### 1. Open WebUI never passed the uploaded file to the Pipe
**Where:** `openwebui/pipe_function.py`

**What was wrong:** the Pipe looked for the attached file and the chat id
inside `body`. Open WebUI removes both from `body` before it calls a Pipe
and hands them over separately (as `__files__` and `__chat_id__`).

**What would have happened:** every question, from every user, would
have been answered with "Please upload a document first". Had that been
worked around, all chats of all users would have shared one document
slot, so one person could have received answers about another person's
file.

**Fix:** the Pipe now reads `__files__` and `__chat_id__`, and reads the
file name from the place Open WebUI really puts it.

### 2. The Pipe froze Open WebUI for every user while it worked
**Where:** `openwebui/pipe_function.py`

**What was wrong:** the Pipe was a "sync generator". Open WebUI runs that
kind of function directly on its main loop, so while one document was
being read (minutes, for a scan), Open WebUI could not answer anyone
else. The "heartbeat" meant to keep the connection alive never actually
sent anything.

**Fix:** the Pipe now does its waiting in a background thread and returns
the finished answer at the end. A status line above the answer shows what
is happening and for how long. In the live test, Open WebUI answered other
requests in 0.01 seconds while a question was being processed.

### 3. German numbers with a thousands dot were read as decimals
**Where:** `src/german.py`

**What was wrong:** in a column like `1.234 / 2.500 / 750`, nothing tells
the program for certain whether the dot is a thousands separator or a
decimal point. In that case it gave up, and pandas then read "1.234" as
one point two three four.

**What would have happened:** the column added up to **753.734 instead of
4.484**. Any whole-number quantity over 1.000 ("1.234 Stück") would have
given a wrong total, with no error.

**Fix:** the column's format is decided from the values that can only be
read one way (a decimal comma, a second thousands dot, and so on). If a
whole column is ambiguous, it is read the German way, because these are
German documents (changeable with `NUMBER_FORMAT_DEFAULT` in `.env`).
Currency signs, "EUR", "%" and "Stk." are removed first. A small
programming mistake in the old check (`&` used where "and" was meant) is
gone with it.

### 4. Tables running over several PDF pages were split, and lost rows
**Where:** `src/smart_router.py`

**What was wrong:** each page's table was stored as a separate table, and
the first line of every table was taken as its header. On page 2 and
later, that first line is a real data row.

**What would have happened:** a 40-page inventory became 40 separate
tables, each continuation page lost one row, and the column names didn't
match between pages. A total over the document would have been wrong
without any warning, which is exactly what this project exists to
prevent. Also, the last page of a long table (only a few rows, so very
little text) was treated as a scan and sent to the vision model, which
took its rows out of the table.

**Fix:** tables that continue on the next page are joined back into one.
A header repeated on each page is removed, and a continuation's first row
is kept as data. Every row records its page in a `source_page` column. A
page that contains a table is never sent to the vision model.

---

## High: whole groups of real files would have failed

### 5. CSV files saved by German Excel failed to load
**Where:** `src/smart_router.py`

**What was wrong:** CSV files were read as UTF-8 only. Excel on Windows
saves "CSV" in a different character set (Windows-1252), so the first
"ö" or "ü" stopped the whole file.

**Fix:** UTF-8 is tried first, then Windows-1252, then Latin-1. The
separator (semicolon, comma, tab) is taken from the header line.

### 6. A PDF table with two empty header cells crashed the whole upload
**Where:** `src/smart_router.py`

**What was wrong:** merged header cells produce several empty column
names. Duplicate column names made the code fail with an error, and the
entire document was rejected.

**Fix:** empty headers become `column_1`, `column_2`, ...; repeated names
become `Menge_2`, `Menge_3`, ...

### 7. Tables in scanned documents could not be added up
**Where:** `src/smart_router.py`

**What was wrong:** the vision model returns a scanned table as a markdown
table, but it was only stored as plain text. The calculating model cannot
add up or count text reliably.

**Fix:** markdown tables from the vision model are turned into real tables
(and joined across pages like the others). The full transcription is
still kept as text as well.

### 8. The Pipe gave up long before the service had finished
**Where:** `openwebui/pipe_function.py`

**What was wrong:** the Pipe waited 2 minutes for a document and 3 minutes
for an answer. A scan can take minutes per page, and one answer can
involve three model calls.

**What would have happened:** a 10-page scan would always have ended in a
timeout while the service kept working in the background. Uploading
again would have repeated the same failure.

**Fix:** the Pipe now waits up to 1 hour for a document and 30 minutes for
an answer (both adjustable in its settings), and shows progress while it
waits.

---

## Medium

### 9. Some results reached the answer model incomplete
**Where:** `src/execution_sandbox.py`, `src/pipeline_api.py`

**What was wrong:** a pandas "Series" (what a "total per article" query
returns) was turned into shortened display text with "..." in it. Very
long results had no limit and could overflow what the model can read.

**Fix:** Series and tables become proper lists of rows with their labels.
Results over 200 rows are cut, and say so together with the full row
count, so the answer can mention that only part is shown.

### 10. The 8 GB memory limit applied to each question, not in total
**Where:** `src/execution_sandbox.py`

**What was wrong:** four people asking at the same time could have used
4 × 8 GB = 32 GB on top of the model, pushing the Mac into swap (which
looks like a frozen machine).

**Fix:** only one calculation runs at a time (`SANDBOX_MAX_CONCURRENT`),
others wait their turn. Also, only one document is read at a time, so two
scans can't force Ollama to swap models back and forth for every page.

### 11. Some file reading and writing got past the sandbox
**Where:** `src/execution_sandbox.py`

**What was wrong:** numpy's file functions (`np.save`, `np.loadtxt`, ...)
and several pandas file functions were not blocked.

**Fix:** all `read_...` calls and file-writing `to_...` calls are
refused, as are numpy's file functions. Harmless conversions like
`to_dict` still work.

### 12. The setup script carried on when the Ollama desktop app was running
**Where:** `scripts/setup_mac.sh`

**What was wrong:** the desktop app holds Ollama's port. The script only
printed a warning and continued. Its check then got its answer from the
desktop app, so it reported success while the memory settings
(`OLLAMA_MAX_LOADED_MODELS=1` and others) were never applied.

**Fix:** the script quits the desktop app, checks the port is free, and
stops with a clear message if something else holds it. After starting
Ollama, it checks that the program answering is really the new one.

### 13. Every follow-up question would have read the document again
**Where:** `openwebui/pipe_function.py`, `src/pipeline_api.py`

**What was wrong:** Open WebUI sends the chat's files along with later
messages too, and the Pipe read them again every time (minutes for a
scan). Which document a chat was about was only kept in memory and lost
on every restart.

**Fix:** the service keeps a small record (`registry.json`) of which Open
WebUI file became which document and which document each chat is about.
A file is read once; follow-up questions, even without the file attached
and even after a restart, go straight to the right document.

---

## Low

### 14. The memory limit could be fooled by macOS memory compression
**Where:** `src/execution_sandbox.py`

**What was wrong:** when memory gets tight, macOS compresses a program's
memory, and compressed memory isn't counted in the figure the watchdog
used. A runaway calculation could have grown well past 8 GB unnoticed.

**Fix:** on macOS, the watchdog reads the "physical footprint" (the
figure Activity Monitor shows), which includes compressed memory. It
checks that reading against the old figure first and falls back to the
old figure if they disagree, so a wrong reading can never kill normal
calculations. *This part can only be tested on the Mac itself; see the
last section.*

### 15. A reply without a code block was not retried
**Where:** `src/pipeline_api.py`

**What was wrong:** if the model answered in words instead of code, the
question failed at once; the retry only covered code that crashed.

**Fix:** a missing code block is retried too, with a note to the model
about what was missing.

---

## Found while testing the fixes

### 16. pandas turned Excel text cells into numbers the wrong way
Excel cells that hold "1.234" **as text** were silently turned into the
number 1.234 by pandas before the German handling could see them. Excel
files are now read keeping every cell's own type, so real numbers stay
exact, dates stay dates, and text goes through the German handling.

### 17. Open WebUI's background jobs ran full calculations
Open WebUI sends its own background jobs (making a chat title, search
queries, tag suggestions) to the model used in the chat, which is this
Pipe. Each one ran a full calculation. They now get an instant empty
reply.

### 18. With "File Context" on, the question arrived wrapped in Open WebUI's template
Open WebUI 0.11 puts its own search instructions and file excerpts in
front of the user's question. The Pipe passed all of that on as "the
question", and the answers came back with citation marks like "[1]". The
Pipe now takes only the text after Open WebUI's template. SETUP.md also
explains how to switch File Context off for this model.

---

## Other improvements made along the way

- The setup script now also installs and starts the pipeline service and
  the hourly cleanup as background services, and runs the tests, so there
  are fewer manual steps.
- The service loads the code model before it accepts requests and writes
  the exact calculation and result of every question to
  `logs/pipeline.log`, so a wrong answer can be traced.
- A failed upload no longer leaves files behind.
- A sample file (`samples/lager_beispiel.csv`) with four known answers,
  for the first test after setup (SETUP.md 6.4).
- SETUP.md was rewritten for someone without Terminal experience: how to
  use Terminal, every step with what to expect, making the function
  visible to non-admin staff (it's admin-only by default), a
  troubleshooting table and a final checklist.

---

## How this was checked

- **Automatic tests:** 49, all passing. Problems 1, 3 to 11, 13, 15, 16,
  17 and 18 each have a test that fails on the old code and passes on
  the new. Problem 2 (Open WebUI freezing) was checked in the live test
  below. Problem 12 (setup script) and the actual memory reading of
  problem 14 can only be checked on the Mac.
- **Live end-to-end test** through a real Open WebUI 0.11, a fresh test
  copy on a development PC, with the small `qwen2.5:7b` model standing in
  for the big one: a German Excel-style CSV (Windows-1252, semicolons,
  thousands dots) uploaded through Open WebUI, with the file entry built
  exactly like Open WebUI's own web page builds it. Results:
  - total quantity: **4484** (the old code gave 753.734)
  - largest item: correct
  - follow-up question with no file attached: correct
  - the same file sent again: not read a second time
  - Open WebUI stayed responsive during every question

## Still to check on the Mac Studio

These depend on the real hardware or the real big models and can't be
tested on a different PC:

1. The four sample questions in SETUP.md section 6.4 give the expected
   answers with `qwen2.5-coder:32b`.
2. After a scanned PDF, `ollama ps` shows only `qwen2.5-coder:32b`
   (SETUP.md section 7).
3. `logs/pipeline.log` does **not** contain "watchdog uses RSS" after
   the first question. If it does, the macOS memory reading (fix 14) fell
   back to the older method: still safe, just less strict.
4. A real multi-page inventory PDF from the office: the answer's total
   matches a total worked out by hand.
