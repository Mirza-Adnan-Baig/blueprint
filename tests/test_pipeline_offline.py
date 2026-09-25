"""
Offline tests -- no Ollama required. Cover the parts that don't involve a
model call: German number parsing, file ingestion into DuckDB, schema
summaries, and the sandbox's ability to run a hand-written (not
model-generated) query end to end. The full /query path (which calls
Ollama to write the code) is exercised manually -- see README, "Testing
the running service."
"""

import os
import tempfile
from pathlib import Path

import pandas as pd

from data_layer import load_into_duckdb, schema_summary
from execution_sandbox import UnsafeCodeError, run_generated_code, validate_code
from german import to_numeric_german_aware
from smart_router import IngestResult, ingest_file


def test_german_number_parsing():
    series = pd.Series(["1.234,56", "2.500,00", "750,00"])
    result = to_numeric_german_aware(series)
    assert list(result) == [1234.56, 2500.00, 750.00]


def test_csv_ingest_and_duckdb_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_DIR", str(tmp_path))
    import importlib
    import data_layer
    importlib.reload(data_layer)

    csv_path = tmp_path / "inventory.csv"
    csv_path.write_text("part_id,quantity\n0012345,1.234\n0012346,2.500\n", encoding="utf-8")

    result = ingest_file(csv_path)
    assert result.source_type == "spreadsheet"

    data_layer.load_into_duckdb("testdoc", result)
    summary = data_layer.schema_summary("testdoc")
    assert "data" in summary
    assert "quantity" in summary


def test_sandbox_rejects_disallowed_import():
    try:
        validate_code("import os\nresult = 1")
        assert False, "should have raised"
    except UnsafeCodeError:
        pass


def test_sandbox_rejects_file_write_call():
    try:
        validate_code("f = open('x.txt', 'w')\nresult = 1")
        assert False, "should have raised"
    except UnsafeCodeError:
        pass


def test_sandbox_runs_valid_code(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_DIR", str(tmp_path))
    import importlib
    import data_layer
    importlib.reload(data_layer)

    csv_path = tmp_path / "inventory.csv"
    csv_path.write_text("part_id,quantity\n0012345,10\n0012346,20\n", encoding="utf-8")
    result = ingest_file(csv_path)
    data_layer.load_into_duckdb("testdoc2", result)

    code = 'result = con.execute("SELECT SUM(quantity) AS total FROM data").df().to_dict(orient="records")'
    sandbox_result = run_generated_code("testdoc2", code)

    assert sandbox_result.success, sandbox_result.error
    assert sandbox_result.result[0]["total"] == 30
