"""
Runs model-generated Python in a restricted subprocess.

Security model, stated plainly: this stops ACCIDENTS -- a hallucinated
`os.remove(...)`, an infinite loop, a runaway query -- not a determined
adversary. The model runs locally and is not attacker-controlled input in
the usual sense, so this is deliberately not a full OS-level sandbox
(no seccomp/container). Three layers:

  1. AST check before anything runs: only whitelisted imports allowed,
     and calls to file/process/network functions are rejected outright,
     even if the module that owns them was never imported directly
     (e.g. `pd.read_csv(...)` reaching the filesystem via pandas).
  2. A separate subprocess, not the API process -- a crash or hang in
     generated code cannot take down pipeline_api.py.
  3. A hard wall-clock timeout, enforced by killing the subprocess.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from data_layer import db_path

ALLOWED_IMPORTS = {"duckdb", "pandas", "numpy", "statistics", "math", "json", "datetime", "re"}

BLOCKED_CALL_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input",
    "read_csv", "read_excel", "read_json", "read_parquet", "read_sql",
    "to_csv", "to_excel", "to_json", "to_parquet", "to_sql",
    "system", "popen", "remove", "rmdir", "unlink", "rename", "chmod",
}

SANDBOX_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "30"))


@dataclass
class SandboxResult:
    success: bool
    result: object | None = None
    stdout: str = ""
    error: str | None = None


class UnsafeCodeError(Exception):
    pass


def validate_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCodeError(f"code does not parse: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise UnsafeCodeError(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise UnsafeCodeError(f"import not allowed: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in BLOCKED_CALL_NAMES:
                raise UnsafeCodeError(f"call not allowed: {name}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeCodeError(f"dunder access not allowed: {node.attr}")


_WRAPPER_TEMPLATE = """
import json
import duckdb
import pandas as pd
import numpy as np

con = duckdb.connect({db_path!r}, read_only=True)

{user_code}

try:
    if isinstance(result, pd.DataFrame):
        payload = result.to_dict(orient="records")
    else:
        payload = result
    print("__SANDBOX_RESULT_START__")
    print(json.dumps(payload, default=str))
    print("__SANDBOX_RESULT_END__")
except NameError:
    print("__SANDBOX_ERROR__: generated code never assigned a `result` variable")
"""


def run_generated_code(doc_id: str, code: str) -> SandboxResult:
    try:
        validate_code(code)
    except UnsafeCodeError as e:
        return SandboxResult(success=False, error=str(e))

    path = db_path(doc_id)
    if not path.exists():
        return SandboxResult(success=False, error=f"no ingested data for doc_id={doc_id}")

    script = _WRAPPER_TEMPLATE.format(db_path=str(path), user_code=code)

    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "run.py"
        script_path.write_text(script, encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                error=f"execution exceeded {SANDBOX_TIMEOUT_SECONDS}s and was killed",
            )

    if proc.returncode != 0:
        return SandboxResult(success=False, error=proc.stderr.strip()[-2000:], stdout=proc.stdout)

    stdout = proc.stdout
    if "__SANDBOX_RESULT_START__" not in stdout:
        return SandboxResult(success=False, error="no result produced", stdout=stdout)

    payload_text = stdout.split("__SANDBOX_RESULT_START__")[1].split("__SANDBOX_RESULT_END__")[0].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return SandboxResult(success=False, error="result was not JSON-serializable", stdout=stdout)

    return SandboxResult(success=True, result=payload, stdout=stdout)
