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
     measures the subprocess's real memory use and kills it the moment it
     crosses the limit.

On top of that, only SANDBOX_MAX_CONCURRENT sandboxes (default 1) run at
the same time, so several people asking at once can't add up to more
memory than the Mac has. Later requests wait their turn.

Why a watchdog and not `resource.setrlimit(RLIMIT_AS, ...)`: on macOS the
kernel does not enforce RLIMIT_AS, and Python's `resource.setrlimit` has a
known macOS bug where it raises ValueError instead (CPython issue #78783).
It would look like protection on the Mac Studio while providing none.

Why "physical footprint" on macOS: when memory gets tight, macOS compresses
a process's pages, and compressed pages no longer count as resident (RSS).
A runaway process can then use far more than the limit while its RSS stays
low. The footprint (what Activity Monitor shows as "Memory") includes the
compressed part. It is read through libproc; the first reading is checked
against psutil's RSS, and if the two disagree the watchdog falls back to
RSS rather than trust a wrong number.
"""

from __future__ import annotations

import ast
import ctypes
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

import config
from data_layer import db_path

logger = logging.getLogger("pipeline")

# The wrapper already provides pd, np and `con`; these are the only extra
# modules generated code may import. Deliberately excludes `duckdb` (use
# `con`), and `resource`/`os`/`sys`/`subprocess` (process control).
ALLOWED_IMPORTS = {"pandas", "numpy", "statistics", "math", "json", "datetime", "re", "collections", "itertools"}

BLOCKED_CALL_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "breakpoint",
    "system", "popen", "remove", "rmdir", "unlink", "rename", "chmod",
    "connect", "install_extension", "load_extension",
    # numpy file access
    "save", "savez", "savez_compressed", "load", "loadtxt", "savetxt", "genfromtxt",
    "fromfile", "tofile", "memmap", "fromregex", "DataSource",
}

# Every pandas/numpy/DuckDB call starting with read_ reads a file, and most
# starting with to_ write one. These to_ calls only convert data in memory.
ALLOWED_TO_CALLS = {
    "to_dict", "to_list", "to_frame", "to_numpy", "to_string", "to_markdown", "to_records",
    "to_datetime", "to_numeric", "to_timedelta", "to_period", "to_timestamp", "to_pydatetime",
    "to_series", "to_flat_index", "to_df", "to_arrow_table",
}

BLOCKED_NAMES = {"duckdb", "__builtins__"}

POLL_INTERVAL_SECONDS = 0.1

_slots = threading.BoundedSemaphore(max(1, config.SANDBOX_MAX_CONCURRENT))


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
            if not name:
                continue
            if (
                name in BLOCKED_CALL_NAMES
                or name.startswith("read_")
                or (name.startswith("to_") and name not in ALLOWED_TO_CALLS)
            ):
                raise UnsafeCodeError(f"call not allowed: {name} (no file access; query through `con`)")
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

__MAX_ROWS = {max_rows}


def __table(frame):
    if not isinstance(frame.index, pd.RangeIndex):
        frame = frame.reset_index()
    total = len(frame)
    head = frame.head(__MAX_ROWS)
    try:
        rows = json.loads(head.to_json(orient="records", date_format="iso", force_ascii=False))
    except ValueError:
        rows = {{"columns": [str(c) for c in head.columns], "rows": head.astype(str).values.tolist()}}
    if total > __MAX_ROWS:
        return {{"first_rows": rows, "total_rows": total,
                "note": f"only the first {{__MAX_ROWS}} of {{total}} rows are shown"}}
    return rows


def __jsonable(value):
    if isinstance(value, pd.Series):
        value = value.to_frame(name=str(value.name) if value.name is not None else "value")
    if isinstance(value, pd.DataFrame):
        return __table(value)
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (list, tuple)) and len(value) > __MAX_ROWS:
        return {{"first_items": list(value[:__MAX_ROWS]), "total_items": len(value)}}
    return value


try:
    payload = __jsonable(result)
except NameError:
    raise SystemExit("generated code never assigned a `result` variable")

print("__SANDBOX_RESULT_START__")
print(json.dumps(payload, default=str, ensure_ascii=False))
print("__SANDBOX_RESULT_END__")
"""


class _RusageInfoV0(ctypes.Structure):
    # struct rusage_info_v0 from <sys/resource.h>
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


_libsystem = None
_footprint_trusted: bool | None = None


def _darwin_footprint(pid: int, rss: int) -> int | None:
    global _libsystem, _footprint_trusted
    if _footprint_trusted is False:
        return None
    try:
        if _libsystem is None:
            _libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            _libsystem.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            _libsystem.proc_pid_rusage.restype = ctypes.c_int
        info = _RusageInfoV0()
        if _libsystem.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
            return None
    except (OSError, AttributeError):
        _footprint_trusted = False
        logger.warning("macOS footprint unavailable, memory watchdog uses RSS")
        return None

    if _footprint_trusted is None:
        ratio = info.ri_resident_size / rss if rss else 0
        _footprint_trusted = 0.5 <= ratio <= 2.0
        if not _footprint_trusted:
            logger.warning("macOS footprint reading failed its self-check, memory watchdog uses RSS")
            return None
    return info.ri_phys_footprint


def _process_memory_bytes(p: psutil.Process) -> int:
    rss = p.memory_info().rss
    if sys.platform == "darwin":
        footprint = _darwin_footprint(p.pid, rss)
        if footprint is not None:
            return max(rss, footprint)
    return rss


def _tree_memory_bytes(proc: psutil.Process) -> int:
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        try:
            total += _process_memory_bytes(p)
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

    with _slots:
        return _run(path, code, timeout_seconds, memory_limit_bytes)


def _run(path: Path, code: str, timeout_seconds: float, memory_limit_bytes: int) -> SandboxResult:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script_path = tmp_path / "run.py"
        script_path.write_text(
            _WRAPPER_TEMPLATE.format(
                db_path=str(path),
                duckdb_memory_limit=config.DUCKDB_MEMORY_LIMIT,
                temp_dir=str(tmp_path / "spill"),
                user_code=code,
                max_rows=config.MAX_RESULT_ROWS,
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
                used = _tree_memory_bytes(proc)
                peak = max(peak, used)
                if used > memory_limit_bytes:
                    killed_reason = (
                        f"used more than {memory_limit_bytes / 1024**3:.1f} GB of memory and was killed; "
                        f"aggregate in SQL via `con` instead of loading everything into pandas"
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
