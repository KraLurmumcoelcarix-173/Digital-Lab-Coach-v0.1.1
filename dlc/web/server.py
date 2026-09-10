"""
FastAPI app for the Digital Lab Coach local web UI.

Run with: `uv run python -m dlc.web.server`.

Endpoints:
  GET  /                       index.html
  GET  /static/...             JS, CSS, images
  POST /api/circuit            multipart upload of one OR MORE .dig

  GET  /api/health             readiness probe
  GET  /api/config/jar         current Digital.jar path 
  POST /api/config/jar         set Digital.jar path
  GET  /api/config/jar/browse  open the native file picker on the server 

  POST /api/tests              run per-row tests
  POST /api/llm/explain        Layer 2 conceptual summary
  POST /api/llm/grade          Layer 2 summary credibility grade
"""
from collections import OrderedDict
from pathlib import Path
import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dlc.facts.extractor import extract_facts
from dlc.llm import client as llm_client
from dlc.llm.explain import explain_circuit
from dlc.llm.grade import grade_summary

from dlc.analyzer import check_all_l1_deep
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.testing.config import (
    get_configured_jar,
    set_digital_jar_path,
    prompt_for_jar_path,
)
from dlc.testing.results import parse_cli_output, parse_cli_output_verbose
from dlc.testing.runner import (
    find_digital_jar, per_file_run_fast, per_row_run, per_row_run_auto,
    per_row_run_iter, run_digital_cli,
)

from dlc.web.component_kb import library_for_inventory
from dlc.parser.pin_geometry import inverted_input_names

from dlc.testing.spec import extract_test_specs
from dlc.sim.simulator import RowReplay, simulate_sequential
from dlc.telemetry.sink import log_events
from dlc.web.graph_export import circuit_summary, to_cytoscape


STATIC_DIR = Path(__file__).parent / "static"

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    _telemetry_boot()
    yield


app = FastAPI(title="Digital Lab Coach", version="0.3.1",
              lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from dlc.web import l3_routes
app.include_router(l3_routes.router)

_SESSIONS: dict[str, dict] = {}

SESSION_TTL_SECONDS = float(os.environ.get("DLC_SESSION_TTL", 12 * 3600))
JOB_TTL_SECONDS = float(os.environ.get("DLC_JOB_TTL", 24 * 3600))


def _gc_sessions(now: float | None = None) -> int:
    """
    Drop sessions idle for longer than SESSION_TTL_SECONDS and remove
    their upload temp dirs. Returns how many sessions were collected.
    """
    now = time.time() if now is None else now
    dead = [
        sid for sid, s in _SESSIONS.items()
        if now - s.get("last_used", now) > SESSION_TTL_SECONDS
    ]
    for sid in dead:
        tmp_dir = _SESSIONS[sid].get("tmp_dir")
        del _SESSIONS[sid]
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return len(dead)


def _gc_jobs(now: float | None = None) -> int:
    """Drop per-row job records older than JOB_TTL_SECONDS."""
    now = time.time() if now is None else now
    with _JOBS_LOCK:
        dead = [
            jid for jid, j in _JOBS.items()
            if now - j.get("created_at", now) > JOB_TTL_SECONDS
        ]
        for jid in dead:
            del _JOBS[jid]
    return len(dead)


class JarRequest(BaseModel):
    path: str


class TestsRequest(BaseModel):
    session_id: str
    filename: str
    timeout: float = 30.0
    mode: str = "per_row"

class TestsAllRequest(BaseModel):
    session_id: str
    timeout: float = 60.0

class SimulateRequest(BaseModel):
    session_id: str
    filename: str
    spec_index: int = 0
    row_index: int = 0

class SubcircuitRequest(BaseModel):
    session_id: str
    filename: str
    spec_index: int = 0
    row_index: int = 0
    path: list[int] = []


class ApiKeyRequest(BaseModel):
    provider: str = "anthropic"
    key: str


class TelemetryRequest(BaseModel):
    session_id: str | None = None
    events: list[dict] = []

class LlmExplainRequest(BaseModel):
    session_id: str
    filename: str
    student_goal: str | None = None
    test_summary: str | None = None
    model: str | None = None

class LlmGradeRequest(BaseModel):
    session_id: str
    filename: str
    summary_text: str
    student_goal: str | None = None
    test_summary: str | None = None
    grader_model: str | None = None

import threading

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}

@app.get("/api/config/jar")
def get_jar() -> dict:
    p = find_digital_jar()
    configured = get_configured_jar()
    return {
        "path": p,
        "configured": configured,
        "exists": bool(p and Path(p).exists()),
    }


@app.post("/api/config/jar")
def set_jar(req: JarRequest) -> dict:
    try:
        set_digital_jar_path(req.path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "path": req.path}


@app.get("/api/config/jar/browse")
def browse_jar() -> dict:
    try:
        path = prompt_for_jar_path()
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not path:
        return {"ok": False, "reason": "cancelled or no tkinter available"}
    return {"ok": True, "path": path}

_APP_ROOT = Path(__file__).resolve().parents[2]


def _tutor_marker() -> Path:
    env = os.environ.get("DLC_TUTOR_MARKER")
    return Path(env) if env else _APP_ROOT / ".dlc_tutor_done"


@app.get("/api/tutorial/state")
def tutorial_state() -> dict:
    return {"seen": _tutor_marker().exists()}


@app.post("/api/tutorial/seen")
def tutorial_seen() -> dict:
    try:
        _tutor_marker().write_text(time.strftime("%Y-%m-%d %H:%M:%S"),
                                   encoding="utf-8")
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


_TUTOR_BENCH = _APP_ROOT / "data" / "sample_circuits" / "30_bug_benchmark"
_TUTOR_DEMOS = {
    1: ("tutor_demo.dig",
        _TUTOR_BENCH / "bug3_wrong_cin" / "Wrong_cin.dig"),
    2: ("tutor_demo2.dig",
        _TUTOR_BENCH / "bug4_missing_pipeline" / "Missing_pipeline.dig"),
}


@app.get("/api/tutorial/demo")
def tutorial_demo(which: int = 1) -> dict:
    name, path = _TUTOR_DEMOS.get(which, _TUTOR_DEMOS[1])
    try:
        return {"ok": True, "filename": name,
                "content": path.read_text(encoding="utf-8")}
    except OSError as exc:
        return {"ok": False, "filename": None, "content": None,
                "error": f"demo circuit unavailable: {exc}"}


@app.post("/api/circuit")
async def circuit(files: list[UploadFile] = File(...)) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="No files received.")

    tmp_dir = Path(tempfile.mkdtemp(prefix="dlc-"))
    saved: list[tuple[str, Path]] = []
    for f in files:
        if not f.filename or not f.filename.endswith(".dig"):
            continue
        name = Path(f.filename).name
        path = tmp_dir / name
        with open(path, "wb") as out:
            out.write(await f.read())
        saved.append((name, path))

    if not saved:
        raise HTTPException(
            status_code=400, detail="Please upload at least one .dig file."
        )
    
    _gc_sessions()
    _gc_jobs()

    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = {
        "tmp_dir": str(tmp_dir),
        "files": [{"name": n, "path": str(p)} for n, p in saved],
        "last_used": time.time(),
    }

    results: list[dict] = []
    for name, path in saved:
        try:
            c = parse_dig_file(str(path))
            nl = build_netlist(c)
            g = build_signal_graph(c, nl)
            try:
                issues_payload = check_all_l1_deep(c).to_dict()["issues"]
                issues_error = None
            except Exception as exc:
                issues_payload = []
                issues_error = f"{type(exc).__name__}: {exc}"
            try:
                from dlc.analyzer.test_io_coverage import (
                    check_test_io_coverage)
                from dlc.testing.inject import (
                    prepare_injected_run, cleanup_injected)
                from dlc.testing.spec import extract_test_specs
                inj_temp, _ = prepare_injected_run(str(path), name)
                try:
                    spec_circuit = (parse_dig_file(inj_temp)
                                    if inj_temp else c)
                    specs = extract_test_specs(spec_circuit)
                finally:
                    cleanup_injected(inj_temp)
                issues_payload.extend(
                    i.to_dict()
                    for i in check_test_io_coverage(c, specs))
            except Exception:
                pass
            try:
                from dlc.l3.official_store import get_runtime_payload
                if get_runtime_payload(name, "rom"):
                    for iss in issues_payload:
                        if iss.get("kind") != "empty_rom" or iss.get("scope"):
                            continue
                        iss["message"] += (
                            " NOTE: this lab registers the official "
                            "contents of this ROM. Enter them before "
                            "submitting — the Layer 3 debugger refuses "
                            "to run until the ROM matches.")
            except Exception:
                pass
            results.append({
                "filename": name,
                "graph": to_cytoscape(c, nl, g),
                "summary": circuit_summary(c, nl),
                "issues": issues_payload,
                "issues_error": issues_error,
                "error": None,
            })
        except Exception as exc:
            results.append({
                "filename": name,
                "graph": None,
                "summary": None,
                "issues": [],
                "issues_error": None,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {"session_id": session_id, "files": results}

def _resolve_target(session_id: str, filename: str) -> dict:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session["last_used"] = time.time()
    target = next(
        (f for f in session["files"] if f["name"] == filename), None
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"File {filename!r} not in session"
        )
    return target


_MUTE_THRESHOLD = 3


def _l1_error_block(circuit, target: dict | None = None) -> str | None:
    try:
        issues = check_all_l1_deep(circuit)
        n_err = len(issues.errors())
    except Exception:
        return None
    if n_err == 0:
        return None
    if (target is not None
            and target.get("last_all_passed") is True
            and len(issues.issues) <= _MUTE_THRESHOLD):
        return None
    plural = "s" if n_err != 1 else ""
    return (
        f"Blocked: {n_err} Layer 1 structural error{plural} unresolved "
        f"(see the Dashboard issues panel). Fix them first — test "
        f"results on a broken circuit are unreliable."
    )


def _prepare_injection(target: dict):
    """
    Gradescope-style injection for one run (see dlc/testing/inject.py):
    returns (run_path, run_circuit_or_None, notes, temp_path). The caller
    runs the jar on run_path and MUST call cleanup_injected(temp_path)
    when done. run_circuit is the reparsed injected circuit (None when
    nothing was injected — keep using the original).
    """
    from dlc.testing.inject import prepare_injected_run, cleanup_injected
    name = target.get("name") or os.path.basename(target["path"])
    temp_path, notes = prepare_injected_run(target["path"], name)
    if not temp_path:
        return target["path"], None, [], None
    try:
        return temp_path, parse_dig_file(temp_path), notes, temp_path
    except Exception:
        cleanup_injected(temp_path)
        return target["path"], None, [], None


def _run_general(target: dict, timeout: float) -> dict:
    from dlc.testing.inject import cleanup_injected
    try:
        circuit = parse_dig_file(target["path"])
    except Exception as exc:
        return {
            "ok": False, "warning": f"Parse failed: {exc}",
            "mode": "general", "specs": [], "all_passed": None,
        }
    run_path, inj_circuit, inj_notes, inj_temp = _prepare_injection(target)
    try:
        specs = extract_test_specs(inj_circuit or circuit)
        if not specs:
            return {
                "ok": True, "warning": None, "mode": "general",
                "specs": [], "all_passed": None, "injected": inj_notes,
            }
        blocked = _l1_error_block(circuit, target)
        if blocked:
            return {
                "ok": False, "warning": blocked,
                "mode": "general", "specs": [], "all_passed": None,
                "injected": inj_notes,
            }
        jar_path = find_digital_jar()
        if jar_path is None:
            return {
                "ok": False, "mode": "general",
                "warning": "Digital.jar not configured. Open the jar picker.",
                "specs": [], "all_passed": None, "injected": inj_notes,
            }
        code, output = run_digital_cli(run_path, jar_path, timeout=timeout)
        if code < 0:
            msg = {
                -1: "Digital CLI timed out",
                -2: "java not on PATH",
            }.get(code, f"Runner error: {output}")
            return {
                "ok": False, "warning": msg,
                "mode": "general", "specs": [], "all_passed": None,
                "injected": inj_notes,
            }
    finally:
        cleanup_injected(inj_temp)
    run = parse_cli_output(output)
    by_name = run.by_name()
    spec_payloads = []
    any_failed = False
    for spec in specs:
        tc = by_name.get(spec.name)
        if tc is None and len(run.testcases) == 1:
            tc = run.testcases[0]
        if tc is None:
            spec_payloads.append({
                "name": spec.name,
                "status": "error",
                "pass_pct": None,
                "fail_pct": None,
                "row_count": len(spec.rows),
            })
            any_failed = True
            continue
        if tc.status == "passed":
            pass_pct = 100
            fail_pct = 0
        else:
            fail_pct = tc.fail_pct or 0
            pass_pct = 100 - fail_pct
            any_failed = True
        spec_payloads.append({
            "name": spec.name,
            "status": tc.status,
            "pass_pct": pass_pct,
            "fail_pct": fail_pct,
            "row_count": len(spec.rows),
        })
    target["last_all_passed"] = not any_failed
    return {
        "ok": True, "warning": None, "mode": "general",
        "specs": spec_payloads, "all_passed": not any_failed,
        "injected": inj_notes,
    }


def _run_per_row_job(job_id: str, target: dict, timeout: float) -> None:
    def write(updates: dict) -> None:
        with _JOBS_LOCK:
            _JOBS[job_id].update(updates)

    from dlc.testing.inject import cleanup_injected
    try:
        circuit = parse_dig_file(target["path"])
    except Exception as exc:
        write({
            "ok": False, "finished": True,
            "warning": f"Could not parse circuit for testing: {exc}",
            "all_passed": None,
        })
        return
    run_path, inj_circuit, inj_notes, inj_temp = _prepare_injection(target)
    if inj_notes:
        write({"injected": inj_notes})
    specs = extract_test_specs(inj_circuit or circuit)
    if not specs:
        cleanup_injected(inj_temp)
        write({
            "ok": True, "finished": True, "warning": None,
            "all_passed": None,
        })
        return
    blocked = _l1_error_block(circuit, target)
    if blocked:
        cleanup_injected(inj_temp)
        write({
            "ok": False, "finished": True,
            "warning": blocked, "all_passed": None,
        })
        return
    jar_path = find_digital_jar()
    if jar_path is None:
        cleanup_injected(inj_temp)
        write({
            "ok": False, "finished": True,
            "warning": (
                "Digital.jar not configured. Open the jar picker from "
                "the toolbar to select it."
            ),
            "all_passed": None,
        })
        return

    total = sum(len(s.rows) for s in specs)
    with _JOBS_LOCK:
        _JOBS[job_id]["total_rows"] = total
        _JOBS[job_id]["specs"] = [
            {"name": s.name, "headers": s.headers, "rows": []}
            for s in specs
        ]

    any_failed = False
    any_runner_error = False
    done = 0

    def _row_payload(spec, row_result) -> dict:
        rows_by_idx = {row.line_index: row for row in spec.rows}
        row = rows_by_idx.get(row_result.row_index)
        return {
            "index": row_result.row_index,
            "raw": row.raw if row else "",
            "status": row_result.status,
            "error_message": row_result.error_message,
            "mismatches": row_result.mismatches,
        }

    try:
        fast_results, fallback = per_file_run_fast(
            specs, run_path, jar_path=jar_path,
            timeout=max(timeout, 60.0),
        )
    except Exception as exc:
        cleanup_injected(inj_temp)
        write({
            "ok": False, "finished": True,
            "warning": f"Test runner crashed: {type(exc).__name__}: {exc}",
            "all_passed": None,
        })
        return

    fallback_names = {s.name for s in fallback}
    for spec_idx, spec in enumerate(specs):
        if spec.name in fallback_names:
            continue
        for row_result in fast_results.get(spec.name, []):
            payload = _row_payload(spec, row_result)
            if row_result.status == "failed":
                any_failed = True
            if row_result.status == "error":
                any_runner_error = True
            done += 1
            with _JOBS_LOCK:
                _JOBS[job_id]["specs"][spec_idx]["rows"].append(payload)
                _JOBS[job_id]["done_rows"] = done

    for spec_idx, spec in enumerate(specs):
        if spec.name not in fallback_names:
            continue
        try:
            for row_result in per_row_run_iter(
                spec, run_path, jar_path=jar_path, timeout=timeout,
            ):
                payload = _row_payload(spec, row_result)
                if row_result.status == "failed":
                    any_failed = True
                if row_result.status == "error":
                    any_runner_error = True
                done += 1
                with _JOBS_LOCK:
                    _JOBS[job_id]["specs"][spec_idx]["rows"].append(payload)
                    _JOBS[job_id]["done_rows"] = done
        except Exception as exc:
            cleanup_injected(inj_temp)
            write({
                "ok": False, "finished": True,
                "warning": f"Test runner crashed: {type(exc).__name__}: {exc}",
                "all_passed": None,
            })
            return

    cleanup_injected(inj_temp)
    target["last_all_passed"] = (not any_failed) and (not any_runner_error)
    write({
        "ok": True, "finished": True,
        "warning": (
            "One or more rows could not be run (see status=error)."
            if any_runner_error else None
        ),
        "all_passed": (not any_failed) and (not any_runner_error),
    })

@app.post("/api/tests/all")
def tests_all(req: TestsAllRequest) -> dict:
    session = _SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    jar_path = find_digital_jar()
    files_payload: list[dict] = []
    n_with_tests = n_passed = n_failed = n_error = n_blocked = 0

    for f in session["files"]:
        entry = {
            "filename": f["name"], "ok": True, "warning": None,
            "mode": "general", "status": "no_tests",
            "specs": [], "all_passed": None,
        }
        files_payload.append(entry)
        try:
            circuit = parse_dig_file(f["path"])
        except Exception as exc:
            entry.update(ok=False, status="parse_error",
                         warning=f"Parse failed: {exc}")
            n_error += 1
            continue
        run_path, inj_circuit, inj_notes, inj_temp = _prepare_injection(f)
        if inj_notes:
            entry["injected"] = inj_notes
        try:
            specs = [
                s for s in extract_test_specs(inj_circuit or circuit)
                if s.rows
            ]
            if not specs:
                continue
            n_with_tests += 1
            blocked = _l1_error_block(circuit, f)
            if blocked:
                entry.update(ok=False, status="blocked", warning=blocked)
                n_blocked += 1
                continue
            if jar_path is None:
                entry.update(ok=False, status="error",
                             warning="Digital.jar not configured. Open the jar picker.")
                n_error += 1
                continue
            code, output = run_digital_cli(
                run_path, jar_path, timeout=req.timeout, verbose=True,
            )
        finally:
            from dlc.testing.inject import cleanup_injected as _ci
            _ci(inj_temp)
        if code < 0:
            msg = {-1: "Digital CLI timed out", -2: "java not on PATH"}.get(
                code, f"Runner error: {output}")
            entry.update(ok=False, status="error", warning=msg)
            n_error += 1
            continue
        sections = parse_cli_output_verbose(
            output, known_names={s.name for s in specs},
        )
        any_failed = any_error = False
        for spec in specs:
            sec = sections.get(spec.name)
            if sec is None and len(specs) == 1 and len(sections) == 1:
                sec = next(iter(sections.values()))
            if sec is None or sec.status == "error":
                any_error = True
                entry["specs"].append({
                    "name": spec.name, "status": "error",
                    "pass_pct": None, "fail_pct": None,
                    "row_count": len(spec.rows), "failing_rows": None,
                    "error_message": sec.error_message if sec else None,
                })
                continue
            if sec.status == "passed":
                entry["specs"].append({
                    "name": spec.name, "status": "passed",
                    "pass_pct": 100, "fail_pct": 0,
                    "row_count": len(spec.rows), "failing_rows": 0,
                })
                continue
            any_failed = True
            fail_pct = sec.fail_pct or 0
            failing_rows = (
                sum(1 for x in sec.row_failed if x) if sec.table_ok else None
            )
            entry["specs"].append({
                "name": spec.name, "status": "failed",
                "pass_pct": 100 - fail_pct, "fail_pct": fail_pct,
                "row_count": len(spec.rows), "failing_rows": failing_rows,
            })
        if any_error:
            entry.update(status="error", all_passed=None)
            n_error += 1
        elif any_failed:
            entry.update(status="failed", all_passed=False)
            f["last_all_passed"] = False
            n_failed += 1
        else:
            entry.update(status="passed", all_passed=True)
            f["last_all_passed"] = True
            n_passed += 1

    return {
        "ok": True,
        "files": files_payload,
        "summary": {
            "total_files": len(files_payload),
            "files_with_tests": n_with_tests,
            "passed": n_passed,
            "failed": n_failed,
            "errors": n_error,
            "blocked": n_blocked,
        },
        "all_passed": (n_with_tests > 0 and n_passed == n_with_tests),
    }


@app.post("/api/tests/start")
def tests_start(req: TestsRequest) -> dict:
    target = _resolve_target(req.session_id, req.filename)

    if req.mode == "general":
        return _run_general(target, req.timeout)

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "ok": True, "finished": False,
            "warning": None, "all_passed": None,
            "done_rows": 0, "total_rows": 0,
            "specs": [], "mode": "per_row",
            "created_at": time.time(),
        }
    threading.Thread(
        target=_run_per_row_job,
        args=(job_id, target, req.timeout),
        daemon=True,
    ).start()
    return {"job_id": job_id, "mode": "per_row"}


@app.get("/api/tests/progress/{job_id}")
def tests_progress(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "ok": job["ok"],
            "finished": job["finished"],
            "warning": job["warning"],
            "all_passed": job["all_passed"],
            "done_rows": job["done_rows"],
            "total_rows": job["total_rows"],
            "specs": job["specs"],
            "mode": job.get("mode", "per_row"),
        }

@app.post("/api/tests")
def run_tests(req: TestsRequest) -> dict:
    session = _SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    target = next(
        (f for f in session["files"] if f["name"] == req.filename), None
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"File {req.filename!r} not in session"
        )

    from dlc.testing.inject import cleanup_injected
    try:
        circuit = parse_dig_file(target["path"])
    except Exception as exc:
        return {
            "ok": False,
            "warning": f"Could not parse circuit for testing: {exc}",
            "all_passed": None,
            "specs": [],
        }

    run_path, inj_circuit, inj_notes, inj_temp = _prepare_injection(target)

    specs = extract_test_specs(inj_circuit or circuit)
    if not specs:
        cleanup_injected(inj_temp)
        return {
            "ok": True,
            "warning": None,
            "all_passed": None,
            "specs": [],
            "injected": inj_notes,
        }

    blocked = _l1_error_block(circuit, target)
    if blocked:
        cleanup_injected(inj_temp)
        return {
            "ok": False,
            "warning": blocked,
            "all_passed": None,
            "specs": [],
            "injected": inj_notes,
        }

    jar_path = find_digital_jar()
    if jar_path is None:
        cleanup_injected(inj_temp)
        return {
            "ok": False,
            "warning": (
                "Digital.jar not configured. Open the jar picker from "
                "the toolbar to select it."
            ),
            "all_passed": None,
            "specs": [],
            "injected": inj_notes,
        }

    spec_payloads: list[dict] = []
    any_failed = False
    any_runner_error = False

    try:
        for spec in specs:
            try:
                row_results = per_row_run_auto(
                    spec, run_path, jar_path=jar_path, timeout=req.timeout,
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "warning": f"Test runner crashed: {type(exc).__name__}: {exc}",
                    "all_passed": None,
                    "specs": [],
                    "injected": inj_notes,
                }
            rows_by_idx = {row.line_index: row for row in spec.rows}
            row_payload: list[dict] = []
            for r in row_results:
                row = rows_by_idx.get(r.row_index)
                row_payload.append({
                    "index": r.row_index,
                    "raw": row.raw if row else "",
                    "status": r.status,
                    "error_message": r.error_message,
                    "mismatches": r.mismatches,
                })
                if r.status == "failed":
                    any_failed = True
                if r.status == "error":
                    any_runner_error = True

            spec_payloads.append({
                "name": spec.name,
                "headers": spec.headers,
                "rows": row_payload,
            })
    finally:
        cleanup_injected(inj_temp)

    target["last_all_passed"] = (not any_failed) and (not any_runner_error)
    return {
        "ok": True,
        "warning": (
            "One or more rows could not be run (see status=error)."
            if any_runner_error else None
        ),
        "all_passed": (not any_failed) and (not any_runner_error),
        "specs": spec_payloads,
        "injected": inj_notes,
    }

def _node_reactions(circuit, netlist, res) -> dict:
    from collections import defaultdict
    from dlc.web.shape_svg import react_svg
    from dlc.web.graph_export import _family

    comp_pins: dict[int, list] = defaultdict(list)
    for net in netlist.nets:
        for p in net.pins:
            comp_pins[p.component_index].append(
                (p.pin_name, p.direction, net.net_id))

    nv = res.net_values
    out: dict[str, str] = {}
    for idx, comp in enumerate(circuit.components):
        name = comp.element_name
        if name == "Seven-Seg":
            segs = {pn: bool(nv[nid]) for pn, d, nid in comp_pins.get(idx, [])
                    if d == "in" and nid in nv}
            if segs:
                svg = react_svg(comp, _family(name), {"segments": segs})
                if svg:
                    out[str(idx)] = svg
        elif name in ("Multiplexer", "Decoder"):
            sel = next((nv[nid] for pn, d, nid in comp_pins.get(idx, [])
                        if pn == "sel" and nid in nv), None)
            if sel is not None:
                svg = react_svg(comp, _family(name), {"sel": int(sel)})
                if svg:
                    out[str(idx)] = svg
        elif name == "Register":
            q = next(((nv[nid], res.net_bits.get(nid, 1))
                      for pn, d, nid in comp_pins.get(idx, [])
                      if pn == "Q" and nid in nv), None)
            if q is not None:
                qv, qbits = q
                vt = f"0x{qv:X}" if qbits > 1 else str(qv)
                svg = react_svg(comp, _family(name), {"value": vt})
                if svg:
                    out[str(idx)] = svg
    return out


def _output_ok(found, exp_val, width) -> bool | None:
    if found is None:
        return None
    if width:
        m = (1 << width) - 1
        return (found & m) == (exp_val & m)
    return found == exp_val


def _fmt_output(v, width, signed_hint) -> str:
    if v is None:
        return ""
    if not width or width <= 1:
        return str(v)
    m = (1 << width) - 1
    u = v & m
    if signed_hint and (u >> (width - 1)) & 1:
        return str(u - (1 << width))
    return f"0x{u:X}"



_ROW_REPLAYS: "OrderedDict[tuple, dict]" = OrderedDict()
_ROW_REPLAYS_MAX = 8
_ROW_REPLAYS_LOCK = threading.Lock()


def _tree_digest(path: str, circuit) -> str:
    paths = [path]
    seen: set[str] = set()

    def walk(c):
        for ref in c.subcircuits:
            p = ref.resolved_path
            if p and p not in seen and ref.child_circuit is not None:
                seen.add(p)
                paths.append(p)
                walk(ref.child_circuit)
    walk(circuit)
    h = hashlib.sha1()
    for p in paths:
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(p.encode())
    return h.hexdigest()


def _row_replay(path: str, spec_index: int) -> dict:
    circuit = parse_dig_file(path)
    digest = _tree_digest(path, circuit)
    key = (path, spec_index)
    with _ROW_REPLAYS_LOCK:
        hit = _ROW_REPLAYS.get(key)
        if hit is not None and hit["digest"] == digest:
            _ROW_REPLAYS.move_to_end(key)
            return hit
    netlist = build_netlist(circuit)
    graph = build_signal_graph(circuit, netlist)
    specs = extract_test_specs(circuit)
    spec = specs[spec_index] if 0 <= spec_index < len(specs) else None
    entry = {
        "digest": digest, "circuit": circuit, "netlist": netlist,
        "graph": graph, "specs": specs, "spec": spec,
        "replay": RowReplay(circuit, netlist, graph, spec) if spec else None,
        "lock": threading.Lock(),
    }
    with _ROW_REPLAYS_LOCK:
        _ROW_REPLAYS[key] = entry
        _ROW_REPLAYS.move_to_end(key)
        while len(_ROW_REPLAYS) > _ROW_REPLAYS_MAX:
            _ROW_REPLAYS.popitem(last=False)
    return entry


def _view_path(target: dict) -> tuple[str, str | None]:
    from dlc.testing.inject import prepare_injected_run
    name = target.get("name") or os.path.basename(target["path"])
    temp, _notes = prepare_injected_run(target["path"], name)
    return (temp or target["path"]), temp


@app.post("/api/simulate")
def simulate_row(req: SimulateRequest) -> dict:
    from dlc.testing.inject import cleanup_injected
    target = _resolve_target(req.session_id, req.filename)
    view_path, inj_temp = _view_path(target)
    try:
        entry = _row_replay(view_path, req.spec_index)
    except Exception as exc:
        return {"ok": False, "warning": f"Could not parse circuit: {exc}",
                "net_values": {}, "outputs": [], "unresolved_nets": []}
    finally:
        cleanup_injected(inj_temp)
    circuit, netlist = entry["circuit"], entry["netlist"]
    specs, spec = entry["specs"], entry["spec"]
    if not specs or spec is None:
        return {"ok": False, "warning": "No such testcase in this circuit.",
                "net_values": {}, "outputs": [], "unresolved_nets": []}

    try:
        with entry["lock"]:
            res = entry["replay"].upto(req.row_index)
    except Exception as exc:
        return {"ok": False,
                "warning": f"Evaluator error: {type(exc).__name__}: {exc}",
                "net_values": {}, "outputs": [], "unresolved_nets": []}

    net_values = {
        str(nid): {
            "value": val,
            "bits": res.net_bits.get(nid, 1),
            "hex": format(val, "X"),
        }
        for nid, val in res.net_values.items()
    }

    row = next(
        (r for r in spec.rows if r.line_index == req.row_index and not r.is_malformed),
        None,
    )
    expected: dict[str, dict] = {}
    if row is not None:
        from dlc.testing.spec import match_variables_to_io
        bindings = match_variables_to_io(spec.headers, circuit)
        for col, header in enumerate(spec.headers):
            b = bindings.get(header)
            if b and b.role == "output" and col < len(row.values):
                tok = row.values[col]
                if tok.kind == "int" and tok.value is not None:
                    expected[header] = {
                        "val": tok.value, "raw": tok.raw, "width": b.bit_width,
                    }

    outputs = []
    for label, e in expected.items():
        found = res.output_values.get(label)
        width, exp_val = e["width"], e["val"]
        signed = exp_val < 0
        outputs.append({
            "label": label,
            "expected": _fmt_output(exp_val, width, signed),
            "found": _fmt_output(found, width, signed) if found is not None else None,
            "ok": _output_ok(found, exp_val, width),
        })

    return {
        "ok": True,
        "warning": None,
        "row_index": req.row_index,
        "spec_index": req.spec_index,
        "net_values": net_values,
        "unresolved_nets": sorted(res.unresolved_nets),
        "outputs": outputs,
        "notes": res.notes,
        "node_svgs": _node_reactions(circuit, netlist, res),
    }

def _child_at(circuit, comp_idx: int):
    if comp_idx < 0 or comp_idx >= len(circuit.components):
        return None
    comp = circuit.components[comp_idx]
    for sub in circuit.subcircuits:
        if sub.parent_component is comp:
            return sub.child_circuit
    return None


@app.post("/api/subcircuit")
def subcircuit_row(req: SubcircuitRequest) -> dict:
    from dlc.testing.inject import cleanup_injected
    target = _resolve_target(req.session_id, req.filename)
    view_path, inj_temp = _view_path(target)
    try:
        circuit = parse_dig_file(view_path)
        netlist = build_netlist(circuit)
        graph = build_signal_graph(circuit, netlist)
    except Exception as exc:
        return {"ok": False, "warning": f"Could not parse circuit: {exc}"}
    finally:
        cleanup_injected(inj_temp)

    if not req.path:
        return {"ok": False, "warning": "No subcircuit path given."}

    node = circuit
    crumbs: list[str] = []
    for comp_idx in req.path:
        child = _child_at(node, comp_idx)
        if child is None:
            return {"ok": False,
                    "warning": "That component is not a resolvable subcircuit."}
        comp = node.components[comp_idx]
        crumbs.append(comp.label or comp.element_name)
        node = child
    child_circuit = node

    specs = extract_test_specs(circuit)
    if not specs or req.spec_index >= len(specs):
        return {"ok": False, "warning": "No such testcase in this circuit."}
    spec = specs[req.spec_index]

    box: dict = {}
    try:
        simulate_sequential(circuit, netlist, graph, spec, req.row_index,
                            capture_path=tuple(req.path), capture_box=box)
    except Exception as exc:
        return {"ok": False,
                "warning": f"Evaluator error: {type(exc).__name__}: {exc}"}

    res = box.get("result")
    child_nl = getattr(child_circuit, "_sim_nl", None) or build_netlist(child_circuit)
    child_g = (getattr(child_circuit, "_sim_g", None)
               or build_signal_graph(child_circuit, child_nl))
    graph_json = to_cytoscape(child_circuit, child_nl, child_g)

    if res is None:
        return {"ok": True, "graph": graph_json, "net_values": {},
                "unresolved_nets": [], "node_svgs": {},
                "breadcrumb": crumbs, "depth": len(req.path),
                "note": "No live values reached this subcircuit for this row."}

    net_values = {
        str(nid): {"value": val, "bits": res.net_bits.get(nid, 1),
                   "hex": format(val, "X")}
        for nid, val in res.net_values.items()
    }
    return {
        "ok": True,
        "graph": graph_json,
        "net_values": net_values,
        "unresolved_nets": sorted(res.unresolved_nets),
        "node_svgs": _node_reactions(child_circuit, child_nl, res),
        "breadcrumb": crumbs,
        "depth": len(req.path),
    }

_last_ship_attempt = 0.0


def _ship_soon() -> None:
    global _last_ship_attempt
    now = time.time()
    if now - _last_ship_attempt < 20:
        return
    _last_ship_attempt = now

    def _run():
        try:
            from dlc.telemetry.ship import ship_all
            ship_all()
        except Exception:
            pass
    import threading
    threading.Thread(target=_run, daemon=True).start()


@app.post("/api/telemetry")
def telemetry(req: TelemetryRequest) -> dict:
    try:
        stored = log_events(req.session_id, req.events)
    except Exception as exc:
        return {"ok": False, "stored": 0,
                "warning": f"{type(exc).__name__}: {exc}"}
    _ship_soon()
    return {"ok": True, "stored": stored}


@app.get("/api/machine")
def machine_info() -> dict:
    from dlc.llm.client import _proxy_config
    from dlc.telemetry.machine import machine_identity
    ident = machine_identity()
    url, _tok = _proxy_config()
    return {**ident, "proxy_configured": bool(url)}


def _telemetry_boot() -> None:
    try:
        from dlc.telemetry.machine import machine_identity
        ident = machine_identity()
        log_events(None, [{
            "kind": "app_start",
            "install_id": ident["install_id"],
            "issued": ident["issued"],
            "id_source": ident["source"],
            "version": os.environ.get("DLC_VERSION", "0.1.0"),
        }])
    except Exception:
        pass
    _ship_soon()



class ProxyConfigRequest(BaseModel):
    url: str | None = None
    token: str | None = None


def _verify_course_server(url: str, token: str | None) -> str:
    """Ping the proxy with the saved token: an empty events batch is a
    free authenticated request. Returns accepted / bad_token / unreachable."""
    try:
        import httpx
        from dlc.telemetry.machine import install_id
        r = httpx.post(f"{url}/v1/events",
                       json={"install_id": install_id(), "events": []},
                       headers={"X-DLC-Token": token or ""}, timeout=5.0)
        if r.status_code == 401:
            return "bad_token"
        return "accepted" if r.status_code // 100 == 2 else "unreachable"
    except Exception:
        return "unreachable"


@app.get("/api/config/proxy")
def get_proxy_config(verify: bool = False) -> dict:
    from dlc.llm.client import _proxy_config
    url, token = _proxy_config()
    out = {"configured": bool(url), "url": url,
           "token_set": bool(token)}
    if verify and url:
        out["verify"] = _verify_course_server(url, token)
    return out


@app.post("/api/config/proxy")
def set_proxy_config(req: ProxyConfigRequest) -> dict:
    from dlc.llm.client import _load_config, _save_config
    cfg = _load_config()
    url = (req.url or "").strip()
    if url:
        cfg["proxy_url"] = url.rstrip("/")
        if req.token is not None:
            cfg["proxy_token"] = req.token.strip()
    else:
        cfg.pop("proxy_url", None)
        cfg.pop("proxy_token", None)
    _save_config(cfg)
    out = {"ok": True, "configured": bool(url)}
    if url:
        out["verify"] = _verify_course_server(
            cfg.get("proxy_url", ""), cfg.get("proxy_token"))
    return out


_PROVIDERS = ["anthropic", "openai"]


@app.get("/api/config/api_key")
def get_api_key_status(provider: str | None = None) -> dict:
    per_provider = {p: llm_client.has_api_key(p) for p in _PROVIDERS}
    if provider is None:
        return {
            "configured": per_provider["anthropic"],
            "providers": per_provider,
        }
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    return {"provider": provider, "configured": per_provider[provider]}


@app.post("/api/config/api_key")
def set_api_key_endpoint(req: ApiKeyRequest) -> dict:
    provider = (req.provider or "anthropic").strip()
    key = (req.key or "").strip()
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    if not key:
        raise HTTPException(status_code=400, detail="Empty key.")
    if not key.startswith("sk-"):
        raise HTTPException(
            status_code=400,
            detail=f"That doesn't look like a {provider} API key (expected sk-...).",
        )
    llm_client.set_api_key(provider, key)
    return {"ok": True, "provider": provider, "configured": True}


@app.delete("/api/config/api_key")
def clear_api_key_endpoint(provider: str) -> dict:
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    llm_client.clear_api_key(provider)
    return {
        "ok": True, "provider": provider,
        "configured": llm_client.has_api_key(provider),
    }

class OfficialTestRequest(BaseModel):
    filename: str
    content: str


@app.get("/api/config/official_tests")
def list_official_tests() -> dict:
    from dlc.l3 import official_store
    return {"ok": True, "tests": official_store.list_tests()}


@app.post("/api/config/official_tests")
def save_official_test(req: OfficialTestRequest) -> dict:
    from dlc.l3 import official_store
    try:
        entry = official_store.save_test(req.filename, req.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **entry}


@app.delete("/api/config/official_tests")
def delete_official_test(filename: str) -> dict:
    from dlc.l3 import official_store
    return {"ok": True, "removed": official_store.delete_test(filename),
            "filename": filename}


@app.get("/api/docs/manifest_guide")
def manifest_guide(raw: bool = False):
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from xml.sax.saxutils import escape as _esc
    p = Path(__file__).parent.parent.parent / "docs" / "MANIFEST_GUIDE.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return PlainTextResponse("Guide not found in this build.",
                                 status_code=404)
    if raw:
        return PlainTextResponse(text)
    return HTMLResponse(
        "<title>DLC — configuring new labs</title>"
        "<pre style=\"max-width:860px;margin:24px auto;padding:0 16px;"
        "white-space:pre-wrap;font:13.5px/1.55 ui-monospace,Consolas,"
        "monospace;color:#1f2937\">" + _esc(text) + "</pre>")


@app.get("/api/llm/models")
def list_models() -> dict:
    proxied = bool(llm_client._proxy_config()[0])
    models = []
    for model_id, info in llm_client.MODEL_CATALOG.items():
        models.append({
            "id": model_id,
            "label": info["label"],
            "provider": info["provider"],
            "tier": info["tier"],
            "key_configured": (llm_client.has_api_key(info["provider"])
                               or (proxied
                                   and info["provider"] == "anthropic")),
        })
    return {"models": models, "default": llm_client.DEFAULT_MODEL}

@app.get("/api/library")
def get_library(session_id: str, filename: str) -> dict:
    target = _resolve_target(session_id, filename)
    try:
        circuit = parse_dig_file(target["path"])
        netlist = build_netlist(circuit)
        summary = circuit_summary(circuit, netlist)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not load circuit: {type(exc).__name__}: {exc}",
        )
    inv = dict(summary.get("inventory", {}))
    n_bubbles = sum(len(inverted_input_names(c)) for c in circuit.components)
    if n_bubbles:
        inv["Not"] = inv.get("Not", 0) + n_bubbles
    cards = library_for_inventory(inv)
    return {"cards": cards, "filename": filename}

@app.post("/api/llm/explain")
def llm_explain(req: LlmExplainRequest) -> dict:
    target = _resolve_target(req.session_id, req.filename)
    try:
        circuit = parse_dig_file(target["path"])
        netlist = build_netlist(circuit)
        graph = build_signal_graph(circuit, netlist)
    except Exception as exc:
        return {
            "ok": False, "text": None, "gate_message": None,
            "error": f"Parse failed: {exc}",
            "usage": None, "model": None,
        }
    try:
        facts_obj = extract_facts(circuit, netlist=netlist, graph=graph)
        facts_dict = facts_obj.to_dict() if hasattr(facts_obj, "to_dict") else circuit_summary(circuit, netlist)
    except Exception:
        facts_dict = circuit_summary(circuit, netlist)

    issues_payload = check_all_l1_deep(circuit).to_dict()["issues"]
    student_goal = (req.student_goal or "").strip()[:500]
    if len(student_goal) == 0:
        student_goal = None

    return explain_circuit(
        facts=facts_dict,
        issues=issues_payload,
        test_summary=req.test_summary,
        student_goal=student_goal,
        model=req.model,
    )


@app.post("/api/llm/grade")
def llm_grade(req: LlmGradeRequest) -> dict:
    target = _resolve_target(req.session_id, req.filename)
    try:
        circuit = parse_dig_file(target["path"])
        netlist = build_netlist(circuit)
        graph = build_signal_graph(circuit, netlist)
    except Exception as exc:
        return {"ok": False, "error": f"Parse failed: {exc}", "total": None,
                "sub_scores": [], "grader_model": req.grader_model, "usage": None}
    try:
        facts_obj = extract_facts(circuit, netlist=netlist, graph=graph)
        facts_dict = facts_obj.to_dict() if hasattr(facts_obj, "to_dict") else circuit_summary(circuit, netlist)
    except Exception:
        facts_dict = circuit_summary(circuit, netlist)

    student_goal = (req.student_goal or "").strip()[:500] or None
    return grade_summary(
        facts=facts_dict,
        summary_text=req.summary_text or "",
        student_goal=student_goal,
        test_summary=req.test_summary,
        grader_model=req.grader_model,
    )

def main() -> None:
    import uvicorn
    uvicorn.run(
        "dlc.web.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()