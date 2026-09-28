"""
Offline tests: no Ollama and no Open WebUI required. Model calls are
replaced with fakes that record what WOULD have been sent, so routing,
memory eviction and the service logic can be checked without a model.
The model actually writing code is checked by hand (SETUP.md section 7).
"""

import asyncio
import json

import numpy as np
import pandas as pd
import pymupdf
import pytest

import data_layer
import execution_sandbox
import smart_router
from execution_sandbox import UnsafeCodeError, run_generated_code, validate_code
from german import to_numeric_german_aware
from smart_router import ingest_file


class FakeOllama:
    """Stands in for requests.post inside smart_router; records every call."""

    def __init__(self, reply="| part | qty |\n|---|---|\n| A | 1 |\n| B | 2 |"):
        self.calls = []
        self.reply = reply

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        reply = self.reply

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": reply}

        return _Resp()

    def vision_calls(self):
        return [c for c in self.calls if c.get("images")]


@pytest.fixture
def fake_ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(smart_router.requests, "post", fake.post)
    return fake


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(data_layer, "DUCKDB_DIR", tmp_path)
    return tmp_path


def _ingest_csv(tmp_path, doc_id, content):
    csv_path = tmp_path / "inventory.csv"
    csv_path.write_text(content, encoding="utf-8")
    data_layer.load_into_duckdb(doc_id, ingest_file(csv_path))


def _scanned_pdf(path, pages=1):
    """PDF pages that hold only an image, like a scanner produces."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), 0)
    pix.clear_with(200)
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page().insert_image(pymupdf.Rect(50, 50, 300, 300), pixmap=pix)
        doc.save(path)


def _table_pdf(path, pages):
    """One ruled table per page; `pages` is a list of row lists."""
    with pymupdf.open() as doc:
        for rows in pages:
            page = doc.new_page()
            top = 60
            for n, (a, b) in enumerate(rows):
                page.insert_text((72, top + 16 + n * 22), a)
                page.insert_text((230, top + 16 + n * 22), b)
            bottom = top + len(rows) * 22
            for n in range(len(rows) + 1):
                page.draw_line((66, top + n * 22), (330, top + n * 22))
            for x in (66, 220, 330):
                page.draw_line((x, top), (x, bottom))
        doc.save(path)


# --- German numbers -------------------------------------------------------

def test_german_decimal_comma():
    result = to_numeric_german_aware(pd.Series(["1.234,56", "2.500,00", "750,00"]))
    assert list(result) == [1234.56, 2500.00, 750.00]


def test_german_thousands_without_decimals():
    # The bug: this column used to sum to 753.734.
    result = to_numeric_german_aware(pd.Series(["1.234", "2.500", "750"]))
    assert list(result) == [1234, 2500, 750]
    assert result.sum() == 4484


def test_us_format_still_recognised():
    result = to_numeric_german_aware(pd.Series(["1,234.50", "2,000.25", "12.5"]))
    assert list(result) == [1234.5, 2000.25, 12.5]


def test_currency_and_units_are_stripped():
    result = to_numeric_german_aware(pd.Series(["1.234,56 €", "12,00 EUR", "3 Stk."]))
    assert list(result) == [1234.56, 12.0, 3.0]


def test_real_numbers_are_never_reparsed():
    # Excel cells that already are numbers must keep their exact value.
    result = to_numeric_german_aware(pd.Series([1.125, 3.375, "2.500"], dtype=object))
    assert list(result) == [1.125, 3.375, 2500]


def test_excel_keeps_numbers_and_dates(tmp_path):
    xlsx = tmp_path / "lager.xlsx"
    pd.DataFrame({
        "Artikel": ["A", "B"],
        "Gewicht": [1.125, 3.375],
        "Menge (Text)": ["1.234", "2.500"],
        "Datum": pd.to_datetime(["2026-01-05", "2026-02-10"]),
    }).to_excel(xlsx, index=False)
    table = ingest_file(xlsx).tables["Sheet1"]
    assert list(table["Gewicht"]) == [1.125, 3.375]
    assert list(table["Menge (Text)"]) == [1234, 2500]
    assert pd.api.types.is_datetime64_any_dtype(table["Datum"])


# --- CSV ---------------------------------------------------------------------

def test_csv_saved_by_german_excel(tmp_path):
    csv = tmp_path / "lager.csv"
    csv.write_bytes("Artikel;Menge\nMöbelgriff;1.234\nSchlüssel;2.500\n".encode("cp1252"))
    table = ingest_file(csv).tables["data"]
    assert list(table["Artikel"]) == ["Möbelgriff", "Schlüssel"]
    assert list(table["Menge"]) == [1234, 2500]


def test_csv_with_bom_and_commas(tmp_path):
    csv = tmp_path / "list.csv"
    csv.write_bytes("﻿part,qty\nA,10\nB,20\n".encode("utf-8"))
    table = ingest_file(csv).tables["data"]
    assert list(table.columns) == ["part", "qty"]


# --- Table headers -------------------------------------------------------------

def test_blank_and_duplicate_headers_do_not_crash():
    df = pd.DataFrame([["A", "1", "x", "5"], ["B", "2", "y", "6"]], columns=[None, "Menge", None, "Menge"])
    cleaned = smart_router._clean_table(df)
    assert list(cleaned.columns) == ["column_1", "Menge", "column_3", "Menge_2"]


# --- Routing: structured formats never reach the vision model -------------

def test_csv_never_uses_vision(tmp_path, fake_ollama):
    csv_path = tmp_path / "inventory.csv"
    csv_path.write_text("part_id,quantity\n0012345,10\n", encoding="utf-8")
    assert ingest_file(csv_path).source_type == "spreadsheet"
    assert fake_ollama.calls == []


def test_excel_never_uses_vision(tmp_path, fake_ollama):
    xlsx_path = tmp_path / "inventory.xlsx"
    pd.DataFrame({"part": ["A", "B"], "qty": [10, 20]}).to_excel(xlsx_path, index=False)
    assert ingest_file(xlsx_path).source_type == "spreadsheet"
    assert fake_ollama.calls == []


def test_text_pdf_never_uses_vision(tmp_path, fake_ollama):
    pdf_path = tmp_path / "text.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "This page has a real text layer with plenty of characters on it.")
        doc.save(pdf_path)
    assert ingest_file(pdf_path).source_type == "pdf_text"
    assert fake_ollama.calls == []


def test_short_table_page_never_uses_vision(tmp_path, fake_ollama):
    # Last page of a long table: two rows, very little text. Still a table.
    pdf_path = tmp_path / "short.pdf"
    _table_pdf(pdf_path, [[("Artikel", "Menge"), ("C", "30")]])
    result = ingest_file(pdf_path)
    assert fake_ollama.calls == []
    assert list(result.tables["table1"]["Menge"]) == [30]


# --- Multi-page PDF tables ---------------------------------------------------------

def test_table_continuing_over_pages_is_joined(tmp_path, fake_ollama):
    pdf_path = tmp_path / "multi.pdf"
    _table_pdf(pdf_path, [
        [("Artikel", "Menge"), ("A", "10"), ("B", "20")],
        [("C", "30"), ("D", "40")],  # continuation: no header on page 2
    ])
    tables = ingest_file(pdf_path).tables
    assert list(tables) == ["table1"]
    table = tables["table1"]
    assert list(table["Artikel"]) == ["A", "B", "C", "D"]
    assert table["Menge"].sum() == 100
    assert list(table["source_page"]) == [1, 1, 2, 2]


def test_header_repeated_on_each_page_is_dropped(tmp_path, fake_ollama):
    pdf_path = tmp_path / "repeat.pdf"
    _table_pdf(pdf_path, [
        [("Artikel", "Menge"), ("A", "10")],
        [("Artikel", "Menge"), ("B", "20")],
    ])
    table = ingest_file(pdf_path).tables["table1"]
    assert list(table["Artikel"]) == ["A", "B"]


# --- Scanned pages: vision used, tables kept, then evicted ----------------------

def test_scanned_pdf_uses_vision_then_evicts(tmp_path, fake_ollama):
    pdf_path = tmp_path / "scan.pdf"
    _scanned_pdf(pdf_path)

    result = ingest_file(pdf_path)

    assert result.source_type == "pdf_scanned"
    assert len(fake_ollama.vision_calls()) == 1

    evictions = [c for c in fake_ollama.calls if c.get("keep_alive") == 0]
    assert evictions == [{"model": smart_router.config.VISION_MODEL, "keep_alive": 0}]
    assert fake_ollama.calls[-1] == {"model": smart_router.config.CODE_MODEL, "keep_alive": -1}


def test_scanned_table_becomes_a_real_table(tmp_path, fake_ollama):
    pdf_path = tmp_path / "scan.pdf"
    _scanned_pdf(pdf_path)
    table = ingest_file(pdf_path).tables["table1"]
    assert list(table["part"]) == ["A", "B"]
    assert table["qty"].sum() == 3


def test_vision_evicted_even_if_ingestion_fails(tmp_path, fake_ollama, monkeypatch):
    pdf_path = tmp_path / "scan.pdf"
    _scanned_pdf(pdf_path, pages=2)

    original = smart_router.VisionSession.describe

    def describe_then_fail(self, image_bytes):
        if self.pages_described >= 1:
            raise RuntimeError("simulated failure on page 2")
        return original(self, image_bytes)

    monkeypatch.setattr(smart_router.VisionSession, "describe", describe_then_fail)

    with pytest.raises(RuntimeError):
        ingest_file(pdf_path)

    assert any(c.get("keep_alive") == 0 for c in fake_ollama.calls)


# --- DuckDB -----------------------------------------------------------------

def test_csv_ingest_and_duckdb_roundtrip(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "testdoc", "part_id,quantity\n0012345,1.234\n0012346,2.500\n")
    summary = data_layer.schema_summary("testdoc")
    assert '"data"' in summary
    assert "quantity" in summary


# --- Sandbox: static checks -----------------------------------------------

@pytest.mark.parametrize("code", [
    "import os\nresult = 1",
    "import resource\nresult = 1",
    "import duckdb\nresult = 1",
    "result = duckdb.sql('SELECT 1').fetchall()",
    "f = open('x.txt', 'w')\nresult = 1",
    "result = pd.read_csv('/etc/passwd')",
    "result = pd.read_fwf('/etc/passwd')",
    "np.save('x.npy', np.ones(3))\nresult = 1",
    "result = np.loadtxt('/etc/passwd')",
    "con.sql('SELECT 1').df().to_feather('x')\nresult = 1",
])
def test_sandbox_rejects_unsafe_code(code):
    with pytest.raises(UnsafeCodeError):
        validate_code(code)


@pytest.mark.parametrize("code", [
    "result = con.sql('SELECT 1 AS x').df().to_dict(orient='records')",
    "result = pd.to_numeric(pd.Series(['1'])).to_list()",
    "result = con.sql('SELECT 1').to_df()",
])
def test_sandbox_allows_in_memory_conversions(code):
    validate_code(code)


# --- Sandbox: runtime -------------------------------------------------------

def test_sandbox_runs_valid_sql(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_sum", "part_id,quantity\n0012345,10\n0012346,20\n")
    r = run_generated_code("doc_sum", 'result = con.sql("SELECT SUM(quantity) AS total FROM data").df()')
    assert r.success, r.error
    assert r.result[0]["total"] == 30


def test_sandbox_series_result_keeps_its_labels(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_series", "part,qty\nA,1\nA,2\nB,5\n")
    code = 'result = con.sql("SELECT * FROM data").df().groupby("part")["qty"].sum()'
    r = run_generated_code("doc_series", code)
    assert r.success, r.error
    assert r.result == [{"part": "A", "qty": 3}, {"part": "B", "qty": 5}]


def test_sandbox_large_result_is_capped_and_says_so(tmp_path, isolated_db, monkeypatch):
    monkeypatch.setattr(execution_sandbox.config, "MAX_RESULT_ROWS", 10)
    rows = "\n".join(f"P{i},{i}" for i in range(50))
    _ingest_csv(tmp_path, "doc_big", "part,qty\n" + rows + "\n")
    r = run_generated_code("doc_big", 'result = con.sql("SELECT * FROM data").df()')
    assert r.success, r.error
    assert r.result["total_rows"] == 50
    assert len(r.result["first_rows"]) == 10


def test_sandbox_numpy_scalar_result(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_np", "part,qty\nA,1\nB,2\n")
    r = run_generated_code("doc_np", 'result = con.sql("SELECT * FROM data").df()["qty"].sum()')
    assert r.success, r.error
    assert r.result == 3


def test_sandbox_blocks_sql_file_access(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_ext", "part_id,quantity\n0012345,10\n")
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    code = f"result = con.sql(\"SELECT * FROM read_csv('{secret.as_posix()}')\").fetchall()"
    assert not run_generated_code("doc_ext", code).success


def test_sandbox_kills_runaway_memory(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_mem", "part_id,quantity\n0012345,10\n")
    code = (
        "chunks = []\n"
        "for _ in range(40):\n"
        "    chunks.append(np.ones(50 * 1024 * 1024 // 8))\n"
        "result = len(chunks)"
    )
    r = run_generated_code("doc_mem", code, memory_limit_gb=0.3)
    assert not r.success
    assert "memory" in r.error


def test_sandbox_kills_infinite_loop(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_loop", "part_id,quantity\n0012345,10\n")
    r = run_generated_code("doc_loop", "while True:\n    pass\nresult = 1", timeout_seconds=2)
    assert not r.success
    assert "exceeded" in r.error


def test_footprint_self_check_rejects_a_wrong_reading(monkeypatch):
    # If the macOS reading disagrees with RSS, the watchdog must not use it.
    monkeypatch.setattr(execution_sandbox, "_footprint_trusted", None)

    class FakeInfo:
        ri_resident_size = 10 * 1024**3
        ri_phys_footprint = 99 * 1024**3

    class FakeLib:
        class proc_pid_rusage:
            argtypes = restype = None

            def __new__(cls, pid, flavor, buffer):
                return 0

    monkeypatch.setattr(execution_sandbox, "_libsystem", FakeLib)
    monkeypatch.setattr(execution_sandbox, "_RusageInfoV0", FakeInfo)
    monkeypatch.setattr(execution_sandbox.ctypes, "byref", lambda x: x)
    assert execution_sandbox._darwin_footprint(1, rss=100 * 1024**2) is None
    assert execution_sandbox._footprint_trusted is False


# --- Pipeline service ----------------------------------------------------------

@pytest.fixture
def api(tmp_path, isolated_db, monkeypatch):
    import pipeline_api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(pipeline_api, "warm_code_model", lambda: None)
    monkeypatch.setattr(pipeline_api.config, "STORAGE_DIR", tmp_path)
    with TestClient(pipeline_api.app) as client:
        yield client, pipeline_api


def _upload(client, name="lager.csv", source_id="file-1", chat_id="chat-1"):
    body = "Artikel;Menge\nA;1.234\nB;2.500\n".encode("cp1252")
    return client.post(
        "/ingest",
        files={"file": (name, body)},
        data={"source_id": source_id, "chat_id": chat_id},
    )


def test_same_file_is_ingested_only_once(api):
    client, _ = api
    first = _upload(client).json()
    second = _upload(client, chat_id="chat-2").json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["doc_id"] == first["doc_id"]


def test_follow_up_question_finds_the_chats_document(api, monkeypatch):
    client, pipeline_api = api
    doc_id = _upload(client).json()["doc_id"]
    replies = iter(['```python\nresult = con.sql("SELECT SUM(Menge) AS s FROM data").df()\n```', "3734 Stück"])
    monkeypatch.setattr(pipeline_api, "_call_code_model", lambda system, prompt: next(replies))
    reply = client.post("/query", params={"question": "Wie viele?", "chat_id": "chat-1"}).json()
    assert reply["doc_id"] == doc_id
    assert reply["result"] == [{"s": 3734}]


def test_unknown_chat_gets_404(api):
    client, _ = api
    assert client.post("/query", params={"question": "x", "chat_id": "nobody"}).status_code == 404


def test_missing_code_block_is_retried(api, monkeypatch):
    client, pipeline_api = api
    doc_id = _upload(client).json()["doc_id"]
    replies = iter(["Sure, here is how I would do it.", "```python\nresult = 1\n```", "Eins."])
    prompts = []

    def fake(system, prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(pipeline_api, "_call_code_model", fake)
    reply = client.post("/query", params={"question": "x", "doc_id": doc_id}).json()
    assert reply["answer"] == "Eins."
    assert "did not contain" in prompts[1]


def test_failed_ingestion_leaves_nothing_behind(api, tmp_path):
    client, _ = api
    response = client.post("/ingest", files={"file": ("broken.xyz", b"???")}, data={"source_id": "f9"})
    assert response.status_code == 422
    assert not list(tmp_path.glob("*broken*"))


# --- Open WebUI Pipe -------------------------------------------------------------

def test_question_is_recovered_from_open_webui_wrappers():
    import pipe_function

    plain = {"messages": [{"role": "user", "content": "Wie viele Teile?"}]}
    tagged = {"messages": [{"role": "user", "content": '<attached_files>\n<file id="1"/>\n</attached_files>\n\nWie viele Teile?'}]}
    rag = {"messages": [{"role": "user", "content": "### Task: ...<source>...</source> <user_query>Wie viele Teile?</user_query>"}]}
    # Exact shape seen from Open WebUI 0.11 with File Context on:
    context = {"messages": [{"role": "user", "content": (
        '### Task:\nRespond to the user query using the provided context ...\n\n<context>\n'
        '<source id="1" name="lager.csv">Artikel;Menge: A;1</source>\n</context>\n\nWie viele Teile?'
    )}]}
    listed = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Wie viele Teile?"}]}]}
    for body in (plain, tagged, rag, context, listed):
        assert pipe_function.question_from(body) == "Wie viele Teile?"


def test_open_webui_background_jobs_never_run_a_calculation(monkeypatch):
    import pipe_function

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("background job reached the pipeline service")

    monkeypatch.setattr(pipe_function.requests, "post", must_not_be_called)
    body = {"messages": [{"role": "user", "content": "### Task: generate queries"}]}
    pipe = pipe_function.Pipe()
    assert asyncio.run(pipe.pipe(body, __task__="query_generation")) == '{"queries": []}'
    assert json.loads(asyncio.run(pipe.pipe(body, __task__="title_generation")))["title"]
    assert asyncio.run(pipe.pipe(body, __task__="emoji_generation")) == ""


def test_pipe_reads_files_and_chat_id_from_open_webui_arguments(tmp_path, monkeypatch):
    import pipe_function

    upload = tmp_path / "abc123_lager.csv"
    upload.write_text("Artikel;Menge\nA;1\n", encoding="utf-8")
    sent = []

    class FakeResponse:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_post(url, files=None, data=None, params=None, timeout=None):
        sent.append({"url": url, "data": data, "params": params, "timeout": timeout})
        if url.endswith("/ingest"):
            return FakeResponse({"doc_id": "d1"})
        return FakeResponse({"answer": "Ein Teil."})

    monkeypatch.setattr(pipe_function.requests, "post", fake_post)
    events = []

    async def emitter(event):
        events.append(event)

    files = [{"type": "file", "file": {"id": "abc123", "filename": "lager.csv", "path": str(upload)}}]
    body = {"messages": [{"role": "user", "content": "Wie viele?"}]}
    answer = asyncio.run(pipe_function.Pipe().pipe(
        body, __files__=files, __chat_id__="chat-9", __event_emitter__=emitter,
    ))

    assert answer == "Ein Teil."
    assert sent[0]["data"] == {"source_id": "abc123", "chat_id": "chat-9"}
    assert sent[1]["params"] == {"question": "Wie viele?", "doc_id": "d1", "chat_id": "chat-9"}
    assert sent[0]["timeout"] >= 3600
    assert events[-1]["data"]["done"] is True


def test_pipe_without_any_document_says_so(monkeypatch):
    import pipe_function

    class NotFound:
        status_code = 404

    monkeypatch.setattr(pipe_function.requests, "post", lambda *a, **k: NotFound())
    body = {"messages": [{"role": "user", "content": "Wie viele?"}]}
    answer = asyncio.run(pipe_function.Pipe().pipe(body, __files__=[], __chat_id__="c"))
    assert answer == pipe_function.TEXT["de"]["no_doc"]
