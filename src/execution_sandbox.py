"""
Runs model-generated Python in a restricted subprocess.

Security model, stated plainly: this stops ACCIDENTS -- a hallucinated
destructive call, an infinite loop, a runaway merge that eats all memory --
not a determined adversary. The model runs locally and is not attacker-
controlled input in the usual sense, so this is not a full OS-level sandbox.
Five layers:

  1. AST check before anything runs: only a small set of imports is allowed,
     file/process calls are rejected, and the generated code cannot touch
     the `duckdb` module directly -- only the prepared connection `con`.
  2. DuckDB connection locked down: read-only, no access to files outside
     the document's own database (`enable_external_access = false`, which
     blocks SQL like `SELECT * FROM read_csv('/any/path')`), no extension
     downloads, and its own memory limit (it spills to a temp dir instead
     of growing past it).
  3. A separate subprocess -- a crash or hang cannot take down the API.
  4. A hard wall-clock timeout, enforced by killing the subprocess.
  5. A hard memory cap, enforced by a watchdog in THIS process that
     measures the subprocess's real resident memory and kills it the
     moment it crosses the limit.

Why a watchdog and not `resource.setrlimit(RLIMIT_AS, ...)`: on macOS the
kernel does not enforce RLIMIT_AS, and Python's `resource.setrlimit` has a
known macOS bug where it raises ValueError instead (CPython issue #78783).
It would look like protection on the Mac Studio while providing none. The
watchdog measures actual memory use, so it works the same on every OS.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

import config
from data_layer import db_path

# The wrapper already provides pd, np and `con`; these are the only extra
# modules generated code may import. Deliberately excludes `duckdb` (use
# `con`), and `resource`/`os`/`sys`/`subprocess` (process control).
ALLOWED_IMPORTS = {"pandas", "numpy", "statistics", "math", "json", "datetime", "re", "collections", "itertools"}

BLOCKED_CALL_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr",
    "read_csv", "read_excel", "read_json", "read_parquet", "read_sql", "read_pickle", "read_html",
    "to_csv", "to_excel", "to_json", "to_parquet", "to_sql", "to_pickle",
    "system", "popen", "remove", "rmdir", "unlink", "rename", "chmod",
    "connect", "install_extension", "load_extension",
}

BLOCKED_NAMES = {"duckdb", "__builtins__"}

POLL_INTERVAL_SECONDS = 0.1


@dataclass
class SandboxResult:
    success: bool
    result: object | None = None
    stdout: str = ""
    error: str | None = None
    peak_memory_mb: float = 0.0


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
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise UnsafeCodeError(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                raise UnsafeCodeError(f"import not allowed: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in BLOCKED_CALL_NAMES:
                raise UnsafeCodeError(f"call not allowed: {name}")
        elif isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise UnsafeCodeError(f"use `con` for queries, not `{node.id}` directly")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeCodeError(f"dunder access not allowed: {node.attr}")


_WRAPPER_TEMPLATE = """
import json
import duckdb
import pandas as pd
import numpy as np

con = duckdb.connect({db_path!r}, read_only=True, config={{
    "memory_limit": {duckdb_memory_limit!r},
    "temp_directory": {temp_dir!r},
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}})
con.execute("SET enable_external_access = false")
del duckdb

{user_code}

try:
    payload = result.to_dict(orient="records") if isinstance(result, pd.DataFrame) else result
except NameError:
    raise SystemExit("generated code never assigned a `result` variable")

print("__SANDBOX_RESULT_START__")
print(json.dumps(payload, default=str))
print("__SANDBOX_RESULT_END__")
"""


def _tree_rss_bytes(proc: psutil.Process) -> int:
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        try:
            total += p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def _kill_tree(proc: psutil.Process) -> None:
    for p in [*proc.children(recursive=True), proc]:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def run_generated_code(
    doc_id: str,
    code: str,
    timeout_seconds: float | None = None,
    memory_limit_gb: float | None = None,
) -> SandboxResult:
    timeout_seconds = timeout_seconds or config.SANDBOX_TIMEOUT_SECONDS
    memory_limit_bytes = int((memory_limit_gb or config.SANDBOX_MEMORY_LIMIT_GB) * 1024**3)

    try:
        validate_code(code)
    except UnsafeCodeError as e:
        return SandboxResult(success=False, error=str(e))

    path = db_path(doc_id)
    if not path.exists():
        return SandboxResult(success=False, error=f"no ingested data for doc_id={doc_id}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script_path = tmp_path / "run.py"
        script_path.write_text(
            _WRAPPER_TEMPLATE.format(
                db_path=str(path),
                duckdb_memory_limit=config.DUCKDB_MEMORY_LIMIT,
                temp_dir=str(tmp_path / "spill"),
                user_code=code,
            ),
            encoding="utf-8",
        )

        # Files, not pipes: a chatty script can't fill a pipe buffer and
        # deadlock while this process is busy polling memory.
        out_path, err_path = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
        with open(out_path, "w", encoding="utf-8") as out_f, open(err_path, "w", encoding="utf-8") as err_f:
            popen = subprocess.Popen([sys.executable, str(script_path)], cwd=tmp, stdout=out_f, stderr=err_f)
            proc = psutil.Process(popen.pid)

            started = time.monotonic()
            peak = 0
            killed_reason = None

            while popen.poll() is None:
                rss = _tree_rss_bytes(proc)
                peak = max(peak, rss)
                if rss > memory_limit_bytes:
                    killed_reason = (
                        f"used more than {memory_limit_bytes / 1024**3:.1f} GB of memory and was killed "
                        f"-- aggregate in SQL via `con` instead of loading everything into pandas"
                    )
                elif time.monotonic() - started > timeout_seconds:
                    killed_reason = f"execution exceeded {timeout_seconds:g}s and was killed"
                if killed_reason:
                    _kill_tree(proc)
                    popen.wait()
                    break
                time.sleep(POLL_INTERVAL_SECONDS)

        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")

    peak_mb = peak / 1024**2

    if killed_reason:
        return SandboxResult(success=False, error=killed_reason, stdout=stdout, peak_memory_mb=peak_mb)
    if popen.returncode != 0:
        return SandboxResult(success=False, error=stderr.strip()[-2000:], stdout=stdout, peak_memory_mb=peak_mb)
    if "__SANDBOX_RESULT_START__" not in stdout:
        return SandboxResult(success=False, error="no result produced", stdout=stdout, peak_memory_mb=peak_mb)

    payload_text = stdout.split("__SANDBOX_RESULT_START__")[1].split("__SANDBOX_RESULT_END__")[0].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return SandboxResult(success=False, error="result was not JSON-serializable", stdout=stdout, peak_memory_mb=peak_mb)

    return SandboxResult(success=True, result=payload, stdout=stdout, peak_memory_mb=peak_mb)
