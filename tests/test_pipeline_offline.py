"""
Offline tests -- no Ollama required. Model calls are replaced with fakes
that record what WOULD have been sent, so routing and memory-eviction
behaviour can be checked without a model. The real /query path (the model
actually writing code) is exercised manually -- README section 12.
"""

import pymupdf
import pandas as pd
import pytest

import data_layer
import smart_router
from execution_sandbox import UnsafeCodeError, run_generated_code, validate_code
from german import to_numeric_german_aware
from smart_router import ingest_file


class FakeOllama:
    """Stands in for requests.post inside smart_router; records every call."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": "| part | qty |\n|---|---|\n| A | 1 |"}

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


# --- German numbers -------------------------------------------------------

def test_german_number_parsing():
    result = to_numeric_german_aware(pd.Series(["1.234,56", "2.500,00", "750,00"]))
    assert list(result) == [1234.56, 2500.00, 750.00]


# --- Routing: structured formats never reach the vision model -------------

def test_csv_never_uses_vision(tmp_path, fake_ollama):
    csv_path = tmp_path / "inventory.csv"
    csv_path.write_text("part_id,quantity\n0012345,10\n", encoding="utf-8")
    result = ingest_file(csv_path)
    assert result.source_type == "spreadsheet"
    assert fake_ollama.calls == []


def test_excel_never_uses_vision(tmp_path, fake_ollama):
    xlsx_path = tmp_path / "inventory.xlsx"
    pd.DataFrame({"part": ["A", "B"], "qty": ["10", "20"]}).to_excel(xlsx_path, index=False)
    result = ingest_file(xlsx_path)
    assert result.source_type == "spreadsheet"
    assert fake_ollama.calls == []


def test_text_pdf_never_uses_vision(tmp_path, fake_ollama):
    pdf_path = tmp_path / "text.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "This page has a real text layer with plenty of characters on it.")
        doc.save(pdf_path)
    result = ingest_file(pdf_path)
    assert result.source_type == "pdf_text"
    assert fake_ollama.calls == []


# --- Scanned pages: vision used, then evicted, then code model re-pinned --

def test_scanned_pdf_uses_vision_then_evicts(tmp_path, fake_ollama):
    pdf_path = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        doc.save(pdf_path)

    result = ingest_file(pdf_path)

    assert result.source_type == "pdf_scanned"
    assert len(fake_ollama.vision_calls()) == 1

    evictions = [c for c in fake_ollama.calls if c.get("keep_alive") == 0]
    assert evictions == [{"model": smart_router.config.VISION_MODEL, "keep_alive": 0}]

    last_call = fake_ollama.calls[-1]
    assert last_call == {"model": smart_router.config.CODE_MODEL, "keep_alive": -1}


def test_vision_evicted_even_if_ingestion_fails(tmp_path, fake_ollama, monkeypatch):
    pdf_path = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(pdf_path)

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
])
def test_sandbox_rejects_unsafe_code(code):
    with pytest.raises(UnsafeCodeError):
        validate_code(code)


# --- Sandbox: runtime -------------------------------------------------------

def test_sandbox_runs_valid_sql(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_sum", "part_id,quantity\n0012345,10\n0012346,20\n")
    code = 'result = con.sql("SELECT SUM(quantity) AS total FROM data").df()'
    r = run_generated_code("doc_sum", code)
    assert r.success, r.error
    assert r.result[0]["total"] == 30


def test_sandbox_blocks_sql_file_access(tmp_path, isolated_db):
    _ingest_csv(tmp_path, "doc_ext", "part_id,quantity\n0012345,10\n")
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    code = f"result = con.sql(\"SELECT * FROM read_csv('{secret.as_posix()}')\").fetchall()"
    r = run_generated_code("doc_ext", code)
    assert not r.success


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
